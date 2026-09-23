"""Compact scalar formulas for parent-group feasibility blocks.

A parent-priority block may contain temporary child variables whose only job is
to prove that a newly allocated parent could be consumed under the conditions
that exist at the parent's priority.  Generic Fourier-Motzkin elimination can
expand these split variables combinatorially even when every parent is simply
choosing between two shared routes.

This module recognizes that structure and projects the child witnesses directly.
The result is still a ``ScalarFormulaProgram`` consumed by ``ScalarFormulaKernel``;
there is no runtime LP or new allocation semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .formula import (
    FORMULA_TOL,
    FORMULA_ZERO,
    FormulaUnsupported,
    ScalarFormulaProgram,
)
from .lp import BlockLP, Maximize, Proportional, Scalar, Slot


def _constant(value: Scalar) -> float | None:
    if isinstance(value, Slot):
        constant = value.constant_value
        return None if constant is None else float(constant)
    return float(value)


def _scalar_key(value: Scalar):
    """Structural identity used only while recognizing the compact pattern."""
    if isinstance(value, Slot):
        if value.constant_value is not None:
            return ('constant', float(value.constant_value))
        if value.source_index is not None:
            return ('slot', int(value.source_index), float(value.source_factor))
        return ('slot', int(value.index), 1.0)
    return ('constant', float(value))


def _scalar_code(value: Scalar) -> str:
    if isinstance(value, Slot):
        if value.constant_value is not None:
            return repr(float(value.constant_value))
        if value.source_index is not None:
            base = f"float(state[{value.source_index}])"
            factor = float(value.source_factor)
            if factor == 1.0:
                return base
            return f"({factor!r} * {base})"
        return f"float(state[{value.index}])"
    return repr(float(value))


def _is_zero_lower(value: Scalar) -> bool:
    constant = _constant(value)
    return constant is not None and constant == 0.0


def _is_exact(value: Scalar, expected: float) -> bool:
    constant = _constant(value)
    return constant is not None and constant == expected


def _is_structurally_nonnegative(value: Scalar) -> bool:
    constant = _constant(value)
    if constant is not None:
        return constant >= 0.0
    return isinstance(value, Slot) and value.sign > 0


@dataclass(frozen=True)
class _Parent:
    name: str
    children: tuple[str, ...]
    remaining: Scalar
    fixed_coefficients: dict[int, Scalar]
    choice_rows: tuple[int, ...]
    route_children: dict[int, tuple[str, ...]]


@dataclass(frozen=True)
class _RouteComponent:
    rows: tuple[int, int]
    parents: tuple[_Parent, ...]
    route_coefficients: dict[int, Scalar]


def _targets(model: BlockLP) -> tuple[str, ...] | None:
    if isinstance(model.rule, Proportional):
        targets = tuple(model.rule.reference_cfs)
        return targets if set(targets) == set(model.updates) else None
    if isinstance(model.rule, Maximize):
        if len(model.rule.coefficients) != 1:
            return None
        name, coefficient = next(iter(model.rule.coefficients.items()))
        if isinstance(coefficient, Slot):
            return None
        if not isfinite(float(coefficient)) or float(coefficient) <= 0:
            return None
        return (name,) if set(model.updates) == {name} else None
    return None


def _recognize(model: BlockLP):
    """Determine whether the model has the the special structure that allows simplified compiling."""

    targets = _targets(model)
    if not targets:
        raise FormulaUnsupported('not a scalar target block')
    target_set = set(targets)

    # Every target will be replaced by its proportional floor.  This is exact
    # only for zero-lower, monotone target variables outside the special
    # parent-feasibility equalities.
    for name in targets:
        if name not in model.variables or not _is_zero_lower(model.variables[name].lower):
            raise FormulaUnsupported('parent formula requires zero target lower bounds')

    parent_rows = {}
    witness_parent: dict[str, str] = {}
    parent_constraints = set()

    for row_index, constraint in enumerate(model.constraints):
        if not constraint.name.startswith('parent_feasibility['):
            continue
        if constraint.lower is None or constraint.upper is None:
            raise FormulaUnsupported('parent feasibility must be an equality')
        if _scalar_key(constraint.lower) != _scalar_key(constraint.upper):
            raise FormulaUnsupported('parent feasibility sides differ')

        parents = [
            name for name, coefficient in constraint.coefficients.items()
            if name in target_set and _is_exact(coefficient, -1.0)
        ]
        if len(parents) != 1:
            raise FormulaUnsupported('parent feasibility must contain one target parent')
        parent = parents[0]
        children = []
        for name, coefficient in constraint.coefficients.items():
            if name == parent:
                continue
            if name in target_set or not _is_exact(coefficient, 1.0):
                raise FormulaUnsupported('unsupported parent-feasibility coefficient')
            if name in witness_parent:
                raise FormulaUnsupported('child witness belongs to multiple parents')
            witness_parent[name] = parent
            children.append(name)
        if not children:
            raise FormulaUnsupported('parent has no feasibility children')
        parent_rows[parent] = (tuple(children), constraint.lower)
        parent_constraints.add(row_index)

    if not parent_rows:
        raise FormulaUnsupported('no parent-feasibility equalities')

    # This specialized projection owns all non-target variables.  Counterflow
    # and other unrelated witnesses should continue through the generic formula
    # compiler instead.
    non_targets = set(model.variables) - target_set
    if non_targets != set(witness_parent):
        raise FormulaUnsupported('unrelated witness variables are present')

    for name in non_targets:
        variable = model.variables[name]
        if not _is_zero_lower(variable.lower):
            raise FormulaUnsupported('child witness has a nonzero lower bound')

    # All remaining rows must be monotone upper-capacity rows.  This guarantees
    # that fixing each target at its proportional floor is feasibility-optimal.
    for row_index, constraint in enumerate(model.constraints):
        if row_index in parent_constraints:
            continue
        if constraint.lower is not None or constraint.upper is None:
            raise FormulaUnsupported('parent formula supports upper-capacity rows only')
        for name, coefficient in constraint.coefficients.items():
            if not _is_structurally_nonnegative(coefficient):
                raise FormulaUnsupported(
                    f'non-monotone coefficient in {constraint.name!r}'
                )

    parents: dict[str, _Parent] = {}
    for parent_name, (children, remaining) in parent_rows.items():
        fixed_coefficients: dict[int, Scalar] = {}
        choice_rows = []
        route_children: dict[int, tuple[str, ...]] = {}

        for row_index, constraint in enumerate(model.constraints):
            if row_index in parent_constraints:
                continue
            child_coefficients = [constraint.coefficients.get(child, 0.0) for child in children]
            keys = {_scalar_key(value) for value in child_coefficients}
            if len(keys) == 1:
                value = child_coefficients[0]
                if not _is_exact(value, 0.0):
                    fixed_coefficients[row_index] = value
                continue

            # A flexible route row must be zero for some children and one
            # common positive coefficient for the others.
            nonzero = [value for value in child_coefficients if not _is_exact(value, 0.0)]
            if not nonzero:
                continue
            nonzero_keys = {_scalar_key(value) for value in nonzero}
            if len(nonzero_keys) != 1 or not _is_structurally_nonnegative(nonzero[0]):
                raise FormulaUnsupported('child route coefficients are not interchangeable')
            selected = tuple(
                child for child, value in zip(children, child_coefficients)
                if not _is_exact(value, 0.0)
            )
            choice_rows.append(row_index)
            route_children[row_index] = selected

        if len(choice_rows) not in (0, 2):
            raise FormulaUnsupported('parent children do not form a two-route split')
        if len(choice_rows) == 2:
            row_a, row_b = choice_rows
            set_a = set(route_children[row_a])
            set_b = set(route_children[row_b])
            if not set_a or not set_b or set_a & set_b or set_a | set_b != set(children):
                raise FormulaUnsupported('two-route children do not form a partition')

        parents[parent_name] = _Parent(
            parent_name,
            children,
            remaining,
            fixed_coefficients,
            tuple(choice_rows),
            route_children,
        )

    # Flexible parents sharing a route are one feasibility component.  This
    # compiler handles components containing exactly two route rows.
    pair_by_row: dict[int, frozenset[int]] = {}
    parents_by_pair: dict[frozenset[int], list[_Parent]] = {}
    for parent in parents.values():
        if not parent.choice_rows:
            continue
        pair = frozenset(parent.choice_rows)
        for row_index in pair:
            previous = pair_by_row.get(row_index)
            if previous is not None and previous != pair:
                raise FormulaUnsupported('overlapping route pairs require a general flow projection')
            pair_by_row[row_index] = pair
        parents_by_pair.setdefault(pair, []).append(parent)

    components = []
    for pair, members in parents_by_pair.items():
        rows = tuple(sorted(pair))
        coefficients = {}
        for row_index in rows:
            coefficient = None
            for parent in members:
                for child in parent.route_children[row_index]:
                    value = model.constraints[row_index].coefficients[child]
                    if coefficient is None:
                        coefficient = value
                    elif _scalar_key(coefficient) != _scalar_key(value):
                        raise FormulaUnsupported('shared route uses differing child coefficients')
            if coefficient is None:
                raise FormulaUnsupported('empty shared route')
            coefficients[row_index] = coefficient
        components.append(_RouteComponent(rows, tuple(members), coefficients))

    return targets, parents, tuple(components), parent_constraints, pair_by_row


def _sum_code(parts: list[str]) -> str:
    parts = [part for part in parts if part not in ('0.0', '-0.0')]
    if not parts:
        return '0.0'
    if len(parts) == 1:
        return parts[0]
    return '(' + ' + '.join(parts) + ')'


def compile_parent_group_program(model: BlockLP) -> ScalarFormulaProgram:
    """Return a compact scalar formula for a recognized two-route group block.

    ``FormulaUnsupported`` is raised when the block does not match the exact
    structure described in the module docstring.
    """
    targets, parents, components, parent_constraints, pair_by_row = _recognize(model)
    target_set = set(targets)

    # Rows used as flexible route capacities are emitted by component formulas;
    # all other capacity rows become ordinary linear bounds after each target
    # and fixed-route parent is substituted at its proportional floor.
    route_rows = set(pair_by_row)

    lines = [
        'def maximum(state, factors):',
        '    from math import isfinite',
        f'    _ZERO = {FORMULA_ZERO!r}',
        f'    _TOL = {FORMULA_TOL!r}',
        '    _upper = float("inf")',
        '    _scale = 1.0',
        '',
        '    def _checked_nonnegative(value, label):',
        '        value = float(value)',
        '        if value != value or value == float("-inf"):',
        '            raise FormulaEvaluationError("invalid " + label)',
        '        if value < -_TOL * max(1.0, abs(value)):',
        '            raise FormulaEvaluationError("negative " + label)',
        '        return max(0.0, value)',
        '',
        '    def _positive_coefficient(value, label):',
        '        value = float(value)',
        '        if not isfinite(value) or value <= _ZERO:',
        '            raise FormulaGuardFailed("nonpositive route coefficient: " + label)',
        '        return value',
        '',
        '    def _linear_limit(capacity, intercept, slope):',
        '        nonlocal _upper, _scale',
        '        capacity = float(capacity); intercept = float(intercept); slope = float(slope)',
        '        if capacity != capacity or intercept != intercept or slope != slope:',
        '            raise FormulaEvaluationError("invalid group formula value")',
        '        residual = capacity - intercept',
        '        _scale = max(_scale, abs(capacity) if isfinite(capacity) else 1.0, abs(intercept))',
        '        if slope < -_TOL:',
        '            raise FormulaGuardFailed("negative group formula slope")',
        '        if slope > _ZERO:',
        '            candidate = residual / slope',
        '            if candidate < -_TOL * _scale:',
        '                raise FormulaEvaluationError("group formula is infeasible")',
        '            _upper = min(_upper, max(0.0, candidate))',
        '            if isfinite(candidate): _scale = max(_scale, abs(candidate))',
        '        elif residual < -_TOL * _scale:',
        '            raise FormulaEvaluationError("group formula is infeasible")',
        '',
        '    def _positive_part_limit(capacity, fixed_slope, terms):',
        '        """Largest g with fixed_slope*g + sum(max(0,b+a*g)) <= capacity."""',
        '        capacity = float(capacity); fixed_slope = float(fixed_slope)',
        '        if capacity != capacity or fixed_slope != fixed_slope or fixed_slope < -_TOL:',
        '            raise FormulaEvaluationError("invalid positive-part formula")',
        '        value = 0.0; slope = max(0.0, fixed_slope); breaks = []',
        '        for intercept, term_slope in terms:',
        '            intercept = float(intercept); term_slope = float(term_slope)',
        '            if intercept != intercept or term_slope != term_slope or term_slope < -_TOL:',
        '                raise FormulaEvaluationError("invalid positive-part term")',
        '            term_slope = max(0.0, term_slope)',
        '            if intercept >= 0.0:',
        '                value += intercept; slope += term_slope',
        '            elif term_slope > _ZERO:',
        '                breaks.append((-intercept / term_slope, term_slope))',
        '        scale = max(1.0, abs(capacity) if isfinite(capacity) else 1.0, abs(value))',
        '        if value > capacity + _TOL * scale:',
        '            raise FormulaEvaluationError("group route is infeasible at zero")',
        '        current = 0.0',
        '        for point, added_slope in sorted(breaks):',
        '            if point < current: point = current',
        '            if slope > _ZERO:',
        '                projected = value + slope * (point - current)',
        '                if projected > capacity:',
        '                    return current + max(0.0, capacity - value) / slope',
        '                value = projected',
        '            current = point; slope += added_slope',
        '        if slope <= _ZERO:',
        '            return float("inf")',
        '        return current + max(0.0, capacity - value) / slope',
        '',
    ]

    factor = {name: f"float(factors.get({name!r}, 0.0))" for name in targets}
    remaining = {
        name: _scalar_code(parent.remaining)
        for name, parent in parents.items()
    }

    # Target bounds and the total child capacity available to every parent.
    for name in targets:
        variable = model.variables[name]
        if variable.upper is not None:
            lines.append(
                f"    _linear_limit({_scalar_code(variable.upper)}, 0.0, {factor[name]})"
            )

    for name, parent in parents.items():
        child_uppers = [
            'float("inf")' if model.variables[child].upper is None
            else _scalar_code(model.variables[child].upper)
            for child in parent.children
        ]
        lines.append(
            f"    _linear_limit({_sum_code(child_uppers)}, {remaining[name]}, {factor[name]})"
        )

    def fixed_row_terms(row_index: int, excluded_parents: set[str] = set()):
        constraint = model.constraints[row_index]
        intercept_parts: list[str] = []
        slope_parts: list[str] = []

        for name in targets:
            coefficient = constraint.coefficients.get(name)
            if coefficient is None or _is_exact(coefficient, 0.0):
                continue
            slope_parts.append(f"({_scalar_code(coefficient)}) * ({factor[name]})")

        for name, parent in parents.items():
            if name in excluded_parents:
                continue
            coefficient = parent.fixed_coefficients.get(row_index)
            if coefficient is None:
                continue
            code = _scalar_code(coefficient)
            intercept_parts.append(f"({code}) * ({remaining[name]})")
            slope_parts.append(f"({code}) * ({factor[name]})")

        return _sum_code(intercept_parts), _sum_code(slope_parts)

    # Ordinary monotone rows, including natural-flow rows.  Since siblings have
    # identical coefficients in these rows, their split disappears and the
    # parent demand contributes directly.
    for row_index, constraint in enumerate(model.constraints):
        if row_index in parent_constraints or row_index in route_rows:
            continue
        intercept_code, slope_code = fixed_row_terms(row_index)
        lines.append(
            f"    _linear_limit({_scalar_code(constraint.upper)}, {intercept_code}, {slope_code})"
        )

    # Two-route components.  Each parent demand q_i may split between route A
    # and B, with per-route child upper limits.  The four conditions below are
    # the exact projection of those split variables.
    for component_index, component in enumerate(components):
        row_a, row_b = component.rows
        constraint_a = model.constraints[row_a]
        constraint_b = model.constraints[row_b]
        member_names = {parent.name for parent in component.parents}

        fixed_intercept_a, fixed_slope_a = fixed_row_terms(row_a, member_names)
        fixed_intercept_b, fixed_slope_b = fixed_row_terms(row_b, member_names)
        coeff_a = _scalar_code(component.route_coefficients[row_a])
        coeff_b = _scalar_code(component.route_coefficients[row_b])

        ca = f'_route_capacity_{component_index}_a'
        cb = f'_route_capacity_{component_index}_b'
        sa = f'_route_slope_{component_index}_a'
        sb = f'_route_slope_{component_index}_b'
        lines.extend([
            f"    _route_coeff_{component_index}_a = _positive_coefficient({coeff_a}, {constraint_a.name!r})",
            f"    _route_coeff_{component_index}_b = _positive_coefficient({coeff_b}, {constraint_b.name!r})",
            f"    {ca} = ({_scalar_code(constraint_a.upper)} - ({fixed_intercept_a})) / _route_coeff_{component_index}_a",
            f"    {cb} = ({_scalar_code(constraint_b.upper)} - ({fixed_intercept_b})) / _route_coeff_{component_index}_b",
            f"    {sa} = ({fixed_slope_a}) / _route_coeff_{component_index}_a",
            f"    {sb} = ({fixed_slope_b}) / _route_coeff_{component_index}_b",
            f"    if {ca} < -_TOL or {cb} < -_TOL: raise FormulaEvaluationError('negative route capacity')",
            f"    {ca} = max(0.0, {ca}); {cb} = max(0.0, {cb})",
        ])

        demand_intercepts = []
        demand_slopes = []
        forced_a_terms = []
        forced_b_terms = []

        for parent in component.parents:
            route_a_children = parent.route_children[row_a]
            route_b_children = parent.route_children[row_b]
            upper_a = _sum_code([
                'float("inf")' if model.variables[child].upper is None
                else _scalar_code(model.variables[child].upper)
                for child in route_a_children
            ])
            upper_b = _sum_code([
                'float("inf")' if model.variables[child].upper is None
                else _scalar_code(model.variables[child].upper)
                for child in route_b_children
            ])
            demand_intercepts.append(remaining[parent.name])
            demand_slopes.append(factor[parent.name])
            forced_a_terms.append(
                f"(({remaining[parent.name]}) - ({upper_b}), {factor[parent.name]})"
            )
            forced_b_terms.append(
                f"(({remaining[parent.name]}) - ({upper_a}), {factor[parent.name]})"
            )

        # Total demand cannot exceed combined route capacity after fixed users.
        lines.append(
            f"    _linear_limit({ca} + {cb}, {_sum_code(demand_intercepts)}, "
            f"{_sum_code(demand_slopes + [sa, sb])})"
        )
        lines.append(
            f"    _upper = min(_upper, _positive_part_limit({ca}, {sa}, [{', '.join(forced_a_terms)}]))"
        )
        lines.append(
            f"    _upper = min(_upper, _positive_part_limit({cb}, {sb}, [{', '.join(forced_b_terms)}]))"
        )

    lines.extend([
        '    if _upper < -_TOL * _scale:',
        '        raise FormulaEvaluationError("group formula is infeasible")',
        '    if not isfinite(_upper):',
        '        raise FormulaEvaluationError("group formula is unbounded")',
        '    return max(0.0, float(_upper))',
        '',
    ])

    source = '\n'.join(lines)
    formula_rows = (
        len(targets)
        + len(parents)
        + sum(1 for i in range(len(model.constraints)) if i not in parent_constraints and i not in route_rows)
        + 3 * len(components)
    )
    return ScalarFormulaProgram(
        source=source,
        maximum_intermediate_rows=formula_rows,
        final_rows=formula_rows,
        guards=(),
    )
