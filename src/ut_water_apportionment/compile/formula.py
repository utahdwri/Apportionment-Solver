"""Compile scalar block-LP objectives to static symbolic Python formulas.

A proportional block repeatedly asks one scalar question::

    maximize g
    subject to x_i >= factor_i * g

A one-target maximization is the same question with one factor equal to 1.
This module projects every LP decision variable out *during compile()* while
keeping runtime state slots and proportional factors symbolic.  The projected
DAG is then lowered to straight-line Python source.  Solve-time execution is
therefore arithmetic/min/max only; no formula cache or runtime projection is
used.

Fourier-Motzkin elimination is performed without dividing by runtime
coefficients.  For rows::

    a*x + P <= U       a >= 0
   -b*x + Q <= V       b >= 0

the compiler emits::

    b*P + a*Q <= b*U + a*V

so daily loss/routing coefficients can remain symbolic as long as their sign is
structurally known.  Runtime guards verify those sign assumptions; a failed
guard lets the surrounding kernel use its exact LP fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .lp import BlockLP, Slot

FORMULA_ZERO = 1e-12
FORMULA_TOL = 1e-7

NEG = -1
ZERO = 0
POS = 1
ALL_SIGNS = frozenset((NEG, ZERO, POS))
NONNEGATIVE = frozenset((ZERO, POS))
NONPOSITIVE = frozenset((NEG, ZERO))


class FormulaTooLarge(RuntimeError):
    pass


class FormulaUnsupported(RuntimeError):
    pass


class FormulaGuardFailed(RuntimeError):
    pass


class FormulaEvaluationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Hashable symbolic expression DAG.
# ---------------------------------------------------------------------------


class Expr:
    pass


@dataclass(frozen=True)
class Constant(Expr):
    value: float


@dataclass(frozen=True)
class SlotExpr(Expr):
    index: int
    name: str = field(compare=False)  # Display labels do not change slot identity.
    sign: int = 0  # +1 => nonnegative, -1 => nonpositive, 0 => unknown


@dataclass(frozen=True)
class FactorExpr(Expr):
    name: str  # proportional factor; runtime factors are always nonnegative


@dataclass(frozen=True)
class AddExpr(Expr):
    terms: tuple[Expr, ...]


@dataclass(frozen=True)
class MulExpr(Expr):
    factors: tuple[Expr, ...]


@dataclass(frozen=True)
class DivExpr(Expr):
    numerator: Expr
    denominator: Expr


@dataclass(frozen=True)
class MinExpr(Expr):
    terms: tuple[Expr, ...]


@dataclass(frozen=True)
class MaxExpr(Expr):
    terms: tuple[Expr, ...]


ZERO_EXPR = Constant(0.0)
ONE_EXPR = Constant(1.0)
NEG_ONE_EXPR = Constant(-1.0)


def _constant(expr: Expr) -> float | None:
    return expr.value if isinstance(expr, Constant) else None


def _numeric_factor(expr: Expr) -> tuple[float, Expr | None]:
    """Return ``(numeric coefficient, nonnumeric core)`` for addition merging."""
    if isinstance(expr, Constant):
        return expr.value, None
    if isinstance(expr, MulExpr) and expr.factors and isinstance(expr.factors[0], Constant):
        coefficient = expr.factors[0].value
        rest = expr.factors[1:]
        if not rest:
            return coefficient, None
        if len(rest) == 1:
            return coefficient, rest[0]
        return coefficient, MulExpr(rest)
    return 1.0, expr


def add_expr(*expressions: Expr) -> Expr:
    flat: list[Expr] = []
    for expr in expressions:
        if isinstance(expr, AddExpr):
            flat.extend(expr.terms)
        else:
            flat.append(expr)

    constant = 0.0
    grouped: dict[Expr, float] = {}
    for expr in flat:
        coefficient, core = _numeric_factor(expr)
        if core is None:
            constant += coefficient
        else:
            grouped[core] = grouped.get(core, 0.0) + coefficient

    terms: list[Expr] = []
    if constant != 0.0:
        terms.append(Constant(float(constant)))
    for core, coefficient in grouped.items():
        if coefficient == 0.0:
            continue
        if coefficient == 1.0:
            terms.append(core)
        else:
            terms.append(mul_expr(Constant(float(coefficient)), core))

    if not terms:
        return ZERO_EXPR
    if len(terms) == 1:
        return terms[0]
    return AddExpr(tuple(sorted(terms, key=repr)))


def mul_expr(*expressions: Expr) -> Expr:
    numeric = 1.0
    factors: list[Expr] = []
    for expr in expressions:
        if isinstance(expr, Constant):
            numeric *= expr.value
        elif isinstance(expr, MulExpr):
            for factor in expr.factors:
                if isinstance(factor, Constant):
                    numeric *= factor.value
                else:
                    factors.append(factor)
        else:
            factors.append(expr)
    if numeric == 0.0:
        return ZERO_EXPR
    factors = sorted(factors, key=repr)
    if not factors:
        return Constant(float(numeric))
    if numeric != 1.0:
        factors.insert(0, Constant(float(numeric)))
    if len(factors) == 1:
        return factors[0]
    return MulExpr(tuple(factors))


def neg_expr(expression: Expr) -> Expr:
    return mul_expr(NEG_ONE_EXPR, expression)


def div_expr(numerator: Expr, denominator: Expr) -> Expr:
    if is_symbolic_zero(numerator):
        return ZERO_EXPR
    if numerator == denominator:
        return ONE_EXPR
    if isinstance(denominator, Constant):
        if denominator.value == 0.0:
            raise ZeroDivisionError("symbolic division by zero")
        return mul_expr(Constant(1.0 / denominator.value), numerator)
    # Cancel an exact multiplicative factor when possible.  A runtime positive
    # guard on the denominator is recorded by row normalization.
    if isinstance(numerator, MulExpr):
        factors = list(numerator.factors)
        if denominator in factors:
            factors.remove(denominator)
            return mul_expr(*factors) if factors else ONE_EXPR
    return DivExpr(numerator, denominator)


def min_expr(*expressions: Expr) -> Expr:
    terms: list[Expr] = []
    for expr in expressions:
        if isinstance(expr, MinExpr):
            terms.extend(expr.terms)
        else:
            terms.append(expr)
    unique = tuple(sorted(set(terms), key=repr))
    if not unique:
        raise ValueError("min_expr requires at least one term")
    if len(unique) == 1:
        return unique[0]
    return MinExpr(unique)


def max_expr(*expressions: Expr) -> Expr:
    terms: list[Expr] = []
    for expr in expressions:
        if isinstance(expr, MaxExpr):
            terms.extend(expr.terms)
        else:
            terms.append(expr)
    unique = tuple(sorted(set(terms), key=repr))
    if not unique:
        raise ValueError("max_expr requires at least one term")
    if len(unique) == 1:
        return unique[0]
    return MaxExpr(unique)


def scalar_expr(scalar, factor: float = 1.0) -> Expr:
    if isinstance(scalar, Slot):
        constant_value = getattr(scalar, 'constant_value', None)
        if constant_value is not None:
            return Constant(float(factor) * float(constant_value))
        source_index = getattr(scalar, 'source_index', None)
        source_factor = float(getattr(scalar, 'source_factor', 1.0))
        if source_index is None:
            base = SlotExpr(scalar.index, scalar.name, getattr(scalar, 'sign', 0))
        else:
            # A coefficient-pair slot is an exact symbolic alias of its source,
            # not an independent runtime parameter.  Preserving that identity
            # lets projection simplify a + (-a) exactly.
            source_sign = getattr(scalar, 'sign', 0)
            if source_factor < 0:
                source_sign = -source_sign
            base = SlotExpr(source_index, scalar.name, source_sign)
            factor *= source_factor
    else:
        base = Constant(float(scalar))
    return mul_expr(Constant(float(factor)), base)


def possible_signs(expr: Expr) -> frozenset[int]:
    if isinstance(expr, Constant):
        if expr.value > 0:
            return frozenset((POS,))
        if expr.value < 0:
            return frozenset((NEG,))
        return frozenset((ZERO,))
    if isinstance(expr, SlotExpr):
        if expr.sign > 0:
            return NONNEGATIVE
        if expr.sign < 0:
            return NONPOSITIVE
        return ALL_SIGNS
    if isinstance(expr, FactorExpr):
        return NONNEGATIVE
    if isinstance(expr, MulExpr):
        signs = frozenset((POS,))
        for factor in expr.factors:
            next_signs = possible_signs(factor)
            products = set()
            for a in signs:
                for b in next_signs:
                    products.add(a * b)
            signs = frozenset(products)
        return signs
    if isinstance(expr, AddExpr):
        signs = [possible_signs(term) for term in expr.terms]
        if all(sign <= NONNEGATIVE for sign in signs):
            if all(sign == frozenset((ZERO,)) for sign in signs):
                return frozenset((ZERO,))
            return NONNEGATIVE
        if all(sign <= NONPOSITIVE for sign in signs):
            if all(sign == frozenset((ZERO,)) for sign in signs):
                return frozenset((ZERO,))
            return NONPOSITIVE
        return ALL_SIGNS
    if isinstance(expr, DivExpr):
        denominator_signs = possible_signs(expr.denominator)
        if not denominator_signs <= NONNEGATIVE:
            return ALL_SIGNS
        return possible_signs(expr.numerator)
    # Min/Max appear on projected RHS, not variable coefficients.  Be
    # conservative if a future transform ever puts one on the LHS.
    return ALL_SIGNS


def is_symbolic_zero(expr: Expr) -> bool:
    return isinstance(expr, Constant) and expr.value == 0.0


def _sign_class(expr: Expr) -> str:
    signs = possible_signs(expr)
    if signs == frozenset((ZERO,)):
        return 'zero'
    if signs <= NONNEGATIVE:
        return 'positive'
    if signs <= NONPOSITIVE:
        return 'negative'
    return 'unknown'


# ---------------------------------------------------------------------------
# Symbolic row projection.
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    coefficients: dict[str, Expr]
    factor_coefficients: dict[str, Expr]
    rhs: Expr


def _add_scaled_dicts(
    a: dict[str, Expr], fa: Expr,
    b: dict[str, Expr], fb: Expr,
    *, eliminated: str | None = None,
) -> dict[str, Expr]:
    names = set(a) | set(b)
    result = {}
    for name in names:
        if name == eliminated:
            continue
        expression = add_expr(
            mul_expr(fa, a.get(name, ZERO_EXPR)),
            mul_expr(fb, b.get(name, ZERO_EXPR)),
        )
        if not is_symbolic_zero(expression):
            result[name] = expression
    return result


def _combine_rows(a: _Row, fa: Expr, b: _Row, fb: Expr, eliminated: str) -> _Row:
    return _Row(
        coefficients=_add_scaled_dicts(
            a.coefficients, fa, b.coefficients, fb, eliminated=eliminated,
        ),
        factor_coefficients=_add_scaled_dicts(
            a.factor_coefficients, fa, b.factor_coefficients, fb,
        ),
        rhs=add_expr(mul_expr(fa, a.rhs), mul_expr(fb, b.rhs)),
    )


def _scale_row(row: _Row, factor: float) -> _Row:
    scale = Constant(float(factor))
    return _Row(
        {name: mul_expr(scale, coefficient) for name, coefficient in row.coefficients.items()},
        {name: mul_expr(scale, coefficient) for name, coefficient in row.factor_coefficients.items()},
        mul_expr(scale, row.rhs),
    )


@dataclass
class _ProjectionContext:
    positive_guards: set[Expr]


def _divide_row(row: _Row, denominator: Expr) -> _Row:
    return _Row(
        {name: div_expr(coefficient, denominator) for name, coefficient in row.coefficients.items()},
        {name: div_expr(coefficient, denominator) for name, coefficient in row.factor_coefficients.items()},
        div_expr(row.rhs, denominator),
    )


def _normalize_row(row: _Row, context: _ProjectionContext) -> _Row:
    """Normalize identical LHS shapes by a positive symbolic pivot.

    Constant pivots are preferred.  If none exists, a one-sided runtime
    coefficient may be used; its absolute value is guarded strictly positive
    by the generated formula.  That lets rows differing only by a daily loss
    multiplier merge into one nested-MIN row without recompilation.
    """
    candidates = []
    for mapping in (row.coefficients, row.factor_coefficients):
        for name in sorted(mapping):
            coefficient = mapping[name]
            if isinstance(coefficient, Constant) and coefficient.value != 0.0:
                pivot = abs(coefficient.value)
                if pivot != 1.0:
                    return _scale_row(row, 1.0 / pivot)
                return row
            classification = _sign_class(coefficient)
            if classification in ('positive', 'negative'):
                candidates.append(coefficient)
    if not candidates:
        return row

    pivot = min(candidates, key=repr)
    denominator = pivot if _sign_class(pivot) == 'positive' else neg_expr(pivot)
    context.positive_guards.add(denominator)
    return _divide_row(row, denominator)


def _row_key(row: _Row):
    return (
        tuple(sorted(row.coefficients.items())),
        tuple(sorted(row.factor_coefficients.items())),
    )


def _merge_rows(rows: list[_Row], context: _ProjectionContext) -> list[_Row]:
    merged: dict[tuple, _Row] = {}
    for raw in rows:
        row = _normalize_row(raw, context)
        key = _row_key(row)
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
        else:
            existing.rhs = min_expr(existing.rhs, row.rhs)
    return list(merged.values())


def _literal_zero_scalar(scalar) -> bool:
    return not isinstance(scalar, Slot) and float(scalar) == 0.0


def _coefficient_expr(scalar) -> Expr:
    expression = scalar_expr(scalar)
    if isinstance(scalar, Slot) and getattr(scalar, 'sign', 0) == 0:
        raise FormulaUnsupported(
            f"Runtime coefficient slot {scalar.name!r} has no structural sign"
        )
    if _sign_class(expression) == 'unknown':
        raise FormulaUnsupported("Coefficient sign cannot be proven symbolically")
    return expression


def _easy_floor_targets(model: BlockLP, targets: tuple[str, ...]) -> set[str]:
    easy = set()
    for name in targets:
        variable = model.variables[name]
        if not _literal_zero_scalar(variable.lower):
            continue
        safe = True
        for constraint in model.constraints:
            scalar = constraint.coefficients.get(name)
            if scalar is None:
                continue
            expression = _coefficient_expr(scalar)
            signs = possible_signs(expression)
            if constraint.upper is not None and not signs <= NONNEGATIVE:
                safe = False
                break
            if constraint.lower is not None and not signs <= NONPOSITIVE:
                safe = False
                break
        if safe:
            easy.add(name)
    return easy


def _split_coefficients(coefficients, easy_targets):
    decision: dict[str, Expr] = {}
    factors: dict[str, Expr] = {}
    for name, scalar in coefficients.items():
        expression = _coefficient_expr(scalar)
        if is_symbolic_zero(expression):
            continue
        if name in easy_targets:
            factors[name] = add_expr(factors.get(name, ZERO_EXPR), expression)
        else:
            decision[name] = add_expr(decision.get(name, ZERO_EXPR), expression)
    return decision, factors


def _collect_coefficient_guards(model: BlockLP) -> tuple[tuple[int, str, int], ...]:
    guards = {}
    for constraint in model.constraints:
        for scalar in constraint.coefficients.values():
            if not isinstance(scalar, Slot):
                continue
            if getattr(scalar, 'constant_value', None) is not None:
                continue
            sign = getattr(scalar, 'sign', 0)
            if sign == 0:
                raise FormulaUnsupported(
                    f"Runtime coefficient slot {scalar.name!r} has no structural sign"
                )
            source_index = getattr(scalar, 'source_index', None)
            source_factor = float(getattr(scalar, 'source_factor', 1.0))
            guard_index = scalar.index if source_index is None else source_index
            guard_sign = sign if source_factor > 0 else -sign
            existing = guards.get(guard_index)
            if existing is not None and existing[2] != guard_sign:
                raise FormulaUnsupported(
                    f"Conflicting sign metadata for runtime slot {scalar.name!r}"
                )
            guards[guard_index] = (guard_index, scalar.name, guard_sign)
    return tuple(sorted(guards.values()))


def _initial_system(
    model: BlockLP, targets: tuple[str, ...], context: _ProjectionContext
):
    rows: list[_Row] = []
    equalities: list[_Row] = []
    easy_targets = _easy_floor_targets(model, targets)

    for name, variable in model.variables.items():
        if name in easy_targets:
            if variable.upper is not None:
                rows.append(_Row({}, {name: ONE_EXPR}, scalar_expr(variable.upper)))
            continue
        if variable.upper is not None:
            rows.append(_Row({name: ONE_EXPR}, {}, scalar_expr(variable.upper)))
        rows.append(_Row({name: NEG_ONE_EXPR}, {}, scalar_expr(variable.lower, -1.0)))

    for constraint in model.constraints:
        decision, factors = _split_coefficients(
            constraint.coefficients, easy_targets
        )
        is_equality = (
            constraint.lower is not None
            and constraint.upper is not None
            and constraint.lower == constraint.upper
        )
        if is_equality:
            equalities.append(_Row(
                dict(decision), dict(factors), scalar_expr(constraint.upper)
            ))
            continue
        if constraint.upper is not None:
            rows.append(_Row(
                dict(decision), dict(factors), scalar_expr(constraint.upper)
            ))
        if constraint.lower is not None:
            rows.append(_Row(
                {name: neg_expr(coefficient) for name, coefficient in decision.items()},
                {name: neg_expr(coefficient) for name, coefficient in factors.items()},
                scalar_expr(constraint.lower, -1.0),
            ))

    for name in targets:
        if name not in easy_targets:
            if name not in model.variables:
                raise FormulaUnsupported(f"Unknown formula target: {name}")
            rows.append(_Row({name: NEG_ONE_EXPR}, {name: ONE_EXPR}, ZERO_EXPR))

    remaining = set(model.variables) - easy_targets
    return _merge_rows(rows, context), equalities, remaining


def _strict_sign(expr: Expr) -> int | None:
    signs = possible_signs(expr)
    if signs == frozenset((POS,)):
        return POS
    if signs == frozenset((NEG,)):
        return NEG
    return None


def _substitute_equality(rows, equalities, remaining, name, context):
    candidates = []
    for index, equality in enumerate(equalities):
        coefficient = equality.coefficients.get(name, ZERO_EXPR)
        sign = _strict_sign(coefficient)
        if sign is None:
            continue
        sparsity = len(equality.coefficients)
        candidates.append((sparsity, index, coefficient, sign))
    if not candidates:
        return None

    _sparsity, pivot_index, pivot_coefficient, pivot_sign = min(
        candidates, key=lambda item: (item[0], item[1])
    )
    pivot = equalities[pivot_index]
    alpha = pivot_coefficient if pivot_sign > 0 else neg_expr(pivot_coefficient)

    def eliminate(row):
        coefficient = row.coefficients.get(name, ZERO_EXPR)
        if is_symbolic_zero(coefficient):
            if name not in row.coefficients:
                return row
            return _Row(
                {key: value for key, value in row.coefficients.items() if key != name},
                dict(row.factor_coefficients), row.rhs,
            )
        beta = neg_expr(coefficient) if pivot_sign > 0 else coefficient
        return _combine_rows(row, alpha, pivot, beta, name)

    projected_rows = [eliminate(row) for row in rows]
    projected_equalities = [
        eliminate(equality)
        for index, equality in enumerate(equalities)
        if index != pivot_index
    ]
    remaining.remove(name)
    return _merge_rows(projected_rows, context), projected_equalities


def _classify_rows_for_variable(rows: list[_Row], name: str):
    positive = []
    negative = []
    zero = []
    for row in rows:
        coefficient = row.coefficients.get(name, ZERO_EXPR)
        classification = _sign_class(coefficient)
        if classification == 'positive':
            positive.append((row, coefficient))
        elif classification == 'negative':
            negative.append((row, coefficient))
        elif classification == 'zero':
            if name in row.coefficients:
                row = _Row(
                    {key: value for key, value in row.coefficients.items() if key != name},
                    dict(row.factor_coefficients), row.rhs,
                )
            zero.append(row)
        else:
            raise FormulaUnsupported(
                f"Projected coefficient of {name!r} can change sign at runtime: {coefficient!r}"
            )
    return positive, negative, zero


def compile_scalar_program(
    model: BlockLP,
    targets: tuple[str, ...],
    *,
    max_rows: int = 5000,
):
    """Compile a symbolic target-only scalar formula during ``compile()``."""
    guards = _collect_coefficient_guards(model)
    context = _ProjectionContext(set())
    rows, equalities, remaining = _initial_system(model, targets, context)
    maximum_rows = len(rows) + len(equalities)

    while remaining:
        # Reservation/group equalities normally have constant +/-1 pivots.
        equality_choices = []
        for name in remaining:
            for equality in equalities:
                coefficient = equality.coefficients.get(name, ZERO_EXPR)
                if _strict_sign(coefficient) is not None:
                    equality_choices.append((len(equality.coefficients), name))
                    break
        if equality_choices:
            _sparsity, name = min(equality_choices)
            got = _substitute_equality(
                rows, equalities, remaining, name, context
            )
            if got is not None:
                rows, equalities = got
                maximum_rows = max(maximum_rows, len(rows) + len(equalities))
                if len(rows) > max_rows:
                    raise FormulaTooLarge(
                        f"Formula projection has {len(rows)} rows (budget {max_rows})"
                    )
                continue

        choices = []
        classified = {}
        for name in remaining:
            positive, negative, zero = _classify_rows_for_variable(rows, name)
            # If a one-sided coefficient can become zero and there is no
            # opposite-sign row, zero-regime projection would need a branch.
            # Current block variables all have finite lower+upper bounds, so
            # this is mainly a safety rule for hand-built models.
            if not negative:
                for _row, coefficient in positive:
                    if ZERO in possible_signs(coefficient):
                        raise FormulaUnsupported(
                            f"Zero-sensitive positive coefficient for unbounded {name!r}"
                        )
            if not positive:
                for _row, coefficient in negative:
                    if ZERO in possible_signs(coefficient):
                        raise FormulaUnsupported(
                            f"Zero-sensitive negative coefficient for unbounded {name!r}"
                        )
            score = len(positive) * len(negative) + len(zero)
            choices.append((score, len(positive) * len(negative), name))
            classified[name] = (positive, negative, zero)

        _score, _pairs, name = min(choices)
        positive_rows, negative_rows, zero_rows = classified[name]

        projected = list(zero_rows)
        if positive_rows and negative_rows:
            for positive, a_positive in positive_rows:
                for negative, a_negative in negative_rows:
                    # (-a_negative) and a_positive are nonnegative symbolic
                    # multipliers, so inequality direction is preserved even
                    # when a daily loss coefficient evaluates to zero.
                    projected.append(_combine_rows(
                        positive, neg_expr(a_negative),
                        negative, a_positive,
                        name,
                    ))
                    if len(projected) > max_rows * 4:
                        raise FormulaTooLarge(
                            f"Formula projection exceeded row budget while eliminating {name!r}"
                        )
        # With no opposite-sign rows the one-sided rows impose no condition on
        # the remaining variables (the eliminated variable can move freely in
        # the needed direction).  The zero rows are retained above.

        rows = _merge_rows(projected, context)
        remaining.remove(name)
        maximum_rows = max(maximum_rows, len(rows))
        if len(rows) > max_rows:
            raise FormulaTooLarge(
                f"Formula projection has {len(rows)} rows (budget {max_rows})"
            )

    for equality in equalities:
        if equality.coefficients:
            raise FormulaUnsupported("Could not symbolically eliminate equality variables")
        rows.append(_Row({}, dict(equality.factor_coefficients), equality.rhs))
        rows.append(_Row(
            {},
            {name: neg_expr(coefficient)
             for name, coefficient in equality.factor_coefficients.items()},
            neg_expr(equality.rhs),
        ))
    rows = _merge_rows(rows, context)

    source = _compile_python_source(
        rows, guards, tuple(sorted(context.positive_guards, key=repr))
    )
    return ScalarFormulaProgram(
        source=source,
        maximum_intermediate_rows=maximum_rows,
        final_rows=len(rows),
        guards=guards,
    )


# ---------------------------------------------------------------------------
# DAG -> straight-line Python.
# ---------------------------------------------------------------------------


# The same helper source is executed by standalone scalar programs and emitted
# once in a complete plan, rather than repeating these checks for every row.
PROJECTED_ROW_SOURCE = f"""
def _intersect_projected_row(upper, scale, coefficient, rhs):
    from math import isfinite, isnan
    if not isfinite(coefficient) or isnan(rhs):
        raise FormulaEvaluationError('invalid projected row')
    if coefficient < -{FORMULA_ZERO!r}:
        raise FormulaGuardFailed('negative projected coefficient')
    if rhs == float('inf'):
        return upper, scale
    if rhs == float('-inf'):
        raise FormulaEvaluationError('scalar formula is infeasible')
    if coefficient > {FORMULA_ZERO!r}:
        candidate = rhs / coefficient
        upper = min(upper, candidate)
        if isfinite(candidate):
            scale = max(scale, abs(candidate))
    elif rhs < -{FORMULA_TOL!r}:
        raise FormulaEvaluationError('scalar formula is infeasible')
    return upper, scale
"""


@dataclass(frozen=True)
class ScalarFormulaProgram:
    source: str
    maximum_intermediate_rows: int
    final_rows: int
    guards: tuple[tuple[int, str, int], ...]

    def compile(self) -> Callable:
        namespace = {
            'FormulaGuardFailed': FormulaGuardFailed,
            'FormulaEvaluationError': FormulaEvaluationError,
        }
        exec(compile(PROJECTED_ROW_SOURCE + self.source, '<scalar-formula>', 'exec'), namespace)
        return namespace['maximum']


def _walk_expr(expr: Expr, seen: set[Expr], slots: dict[int, SlotExpr], factors: set[str]):
    if expr in seen:
        return
    seen.add(expr)
    if isinstance(expr, SlotExpr):
        slots[expr.index] = expr
    elif isinstance(expr, FactorExpr):
        factors.add(expr.name)
    elif isinstance(expr, (AddExpr, MinExpr, MaxExpr)):
        for term in expr.terms:
            _walk_expr(term, seen, slots, factors)
    elif isinstance(expr, MulExpr):
        for factor in expr.factors:
            _walk_expr(factor, seen, slots, factors)
    elif isinstance(expr, DivExpr):
        _walk_expr(expr.numerator, seen, slots, factors)
        _walk_expr(expr.denominator, seen, slots, factors)


def _compile_python_source(rows: list[_Row], guards, positive_guards=()) -> str:
    row_expressions = []
    for row in rows:
        coefficient_terms = [
            mul_expr(coefficient, FactorExpr(name))
            for name, coefficient in row.factor_coefficients.items()
        ]
        coefficient = add_expr(*coefficient_terms) if coefficient_terms else ZERO_EXPR
        # g only appears in x_i >= factor_i * g, with nonnegative factors.
        # Eliminating x preserves this sign: every projected row caps g (or
        # only checks feasibility). A negative/unknown sign is not supported.
        if not possible_signs(coefficient) <= NONNEGATIVE:
            raise FormulaUnsupported("Projected scalar coefficient is not nonnegative")
        row_expressions.append((coefficient, row.rhs))

    seen: set[Expr] = set()
    slots: dict[int, SlotExpr] = {}
    factors: set[str] = set()
    for coefficient, rhs in row_expressions:
        _walk_expr(coefficient, seen, slots, factors)
        _walk_expr(rhs, seen, slots, factors)
    for guard in positive_guards:
        _walk_expr(guard, seen, slots, factors)

    factor_names = sorted(factors)
    factor_vars = {name: f'_f{index}' for index, name in enumerate(factor_names)}
    slot_vars = {index: f'_s{index}' for index in sorted(slots)}

    lines = [
        'def maximum(state, factors):',
        '    from math import isfinite',
    ]
    for index, expr in sorted(slots.items()):
        var = slot_vars[index]
        lines.append(f'    {var} = float(state[{index}])  # {expr.name!r}')
    for index, name, sign in guards:
        var = slot_vars.get(index, f'float(state[{index}])')
        if sign > 0:
            lines.append(
                f"    if not isfinite({var}) or {var} < -{FORMULA_TOL!r}: raise FormulaGuardFailed({('expected nonnegative coefficient: ' + name)!r})"
            )
        else:
            lines.append(
                f"    if not isfinite({var}) or {var} > {FORMULA_TOL!r}: raise FormulaGuardFailed({('expected nonpositive coefficient: ' + name)!r})"
            )
    for name in factor_names:
        var = factor_vars[name]
        lines.append(f'    {var} = float(factors.get({name!r}, 0.0))')
        lines.append(
            f"    if not isfinite({var}) or {var} < -{FORMULA_TOL!r}: raise FormulaEvaluationError({('invalid factor: ' + name)!r})"
        )

    temp_by_expr: dict[Expr, str] = {}
    temp_index = 0

    def ref(expr: Expr) -> str:
        nonlocal temp_index
        if isinstance(expr, Constant):
            return repr(float(expr.value))
        if isinstance(expr, SlotExpr):
            return slot_vars[expr.index]
        if isinstance(expr, FactorExpr):
            return factor_vars[expr.name]
        cached = temp_by_expr.get(expr)
        if cached is not None:
            return cached
        if isinstance(expr, AddExpr):
            code = ' + '.join(ref(term) for term in expr.terms)
        elif isinstance(expr, MulExpr):
            code = ' * '.join(ref(factor) for factor in expr.factors)
        elif isinstance(expr, DivExpr):
            code = f'({ref(expr.numerator)}) / ({ref(expr.denominator)})'
        elif isinstance(expr, MinExpr):
            code = 'min(' + ', '.join(ref(term) for term in expr.terms) + ')'
        elif isinstance(expr, MaxExpr):
            code = 'max(' + ', '.join(ref(term) for term in expr.terms) + ')'
        else:
            raise TypeError(f'Unknown formula expression: {expr!r}')
        name = f'_e{temp_index}'
        temp_index += 1
        lines.append(f'    {name} = {code}')
        temp_by_expr[expr] = name
        return name

    for guard in positive_guards:
        guard_ref = ref(guard)
        lines.append(
            f"    if not isfinite({guard_ref}) or {guard_ref} <= {FORMULA_ZERO!r}: raise FormulaGuardFailed({('normalization coefficient is zero: ' + repr(guard)[:240])!r})"
        )

    lines.extend([
        "    _upper = float('inf')",
        '    _scale = 1.0',
    ])
    for coefficient_expr, rhs_expr in row_expressions:
        coefficient = ref(coefficient_expr)
        rhs = ref(rhs_expr)
        lines.append(f'    _upper, _scale = _intersect_projected_row(_upper, _scale, {coefficient}, {rhs})')
    lines.extend([
        f'    if _upper < -{FORMULA_TOL!r} * _scale:',
        "        raise FormulaEvaluationError('scalar formula is infeasible')",
        "    if not isfinite(_upper): raise FormulaEvaluationError('scalar formula is unbounded')",
        '    return max(0.0, float(_upper))',
        '',
    ])
    return '\n'.join(lines)
