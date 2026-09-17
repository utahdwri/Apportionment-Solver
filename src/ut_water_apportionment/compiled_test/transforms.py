"""Structural compiler passes for the frozen parameterized v2 IR."""

from __future__ import annotations

from .model import (
    CompilerModel,
    GuardPredicate,
    ParamExpr,
    ParametricConstraint,
    ParametricVariable,
    SymbolicExpr,
    constraint_slot,
    variable_slot,
)


ZERO_TOL = 1e-12


def _zero(expr: ParamExpr | None) -> bool:
    return expr is not None and expr.is_constant(0.0)


def _same(a: ParamExpr | None, b: ParamExpr | None) -> bool:
    return a is not None and b is not None and a.equivalent(b)


PATH_LEG_SEPARATOR = "___"
CONTINUITY_PREFIX = "CONT_"
PATH_BOUND_PREFIX = "__V2_PATH_BOUND__"


def _path_transaction_families(model: CompilerModel) -> dict[str, list[str]]:
    """Group production-LP path-leg variables by transaction id."""

    families: dict[str, list[str]] = {}
    for name in model.variables:
        if PATH_LEG_SEPARATOR not in name:
            continue
        transaction_id, _ = name.split(PATH_LEG_SEPARATOR, 1)
        families.setdefault(transaction_id, []).append(name)
    return families


def _continuity_sort_key(name: str, prefix: str) -> tuple[int, str]:
    suffix = name[len(prefix):]
    try:
        return int(suffix), name
    except ValueError:
        return 10**9, name


def _transaction_scales(
    model: CompilerModel,
    transaction_id: str,
    family: list[str],
) -> tuple[str, dict[str, ParamExpr], list[str]] | None:
    """Return ``path_leg = scale(parameter) * logical_transaction``.

    Continuity coefficients may now be runtime parameter expressions. The
    relationship is still exact and linear in LP decision variables; only the
    scalar path multiplier changes by day.
    """

    if len(family) == 1:
        return family[0], {family[0]: ParamExpr.constant_value(1.0)}, []

    family_set = set(family)
    prefix = f"{CONTINUITY_PREFIX}{transaction_id}_"
    continuity = [
        constraint
        for name, constraint in sorted(
            model.constraints.items(),
            key=lambda item: _continuity_sort_key(item[0], prefix),
        )
        if name.startswith(prefix)
        and _zero(constraint.lower)
        and _zero(constraint.upper)
        and set(constraint.coefficients).issubset(family_set)
    ]
    if not continuity:
        return None

    first_terms = [
        (name, coefficient)
        for name, coefficient in continuity[0].coefficients.items()
        if name in family_set and not coefficient.is_constant(0.0)
    ]
    negative = [
        name
        for name, coefficient in first_terms
        if model.coefficient_sign(coefficient) == -1
        and coefficient.evaluate(model.parameter_defaults) < -ZERO_TOL
    ]
    if len(negative) == 1:
        anchor = negative[0]
    else:
        candidates = [
            name for name in family if model.variables[name].upper is not None
        ]
        if len(candidates) != 1:
            return None
        anchor = candidates[0]

    scales: dict[str, ParamExpr] = {anchor: ParamExpr.constant_value(1.0)}
    pending = list(continuity)
    while pending:
        progressed = False
        next_pending = []
        for constraint in pending:
            terms = [
                (name, coefficient)
                for name, coefficient in constraint.coefficients.items()
                if name in family_set and not coefficient.is_constant(0.0)
            ]
            if len(terms) == 1:
                name, _ = terms[0]
                existing = scales.get(name)
                if existing is not None and not existing.is_constant(0.0):
                    return None
                scales[name] = ParamExpr.constant_value(0.0)
                progressed = True
                continue
            if len(terms) != 2:
                return None

            (first, a), (second, b) = terms
            first_known = first in scales
            second_known = second in scales
            if first_known and second_known:
                # This can occur in chained/cyclic continuity definitions.
                # Verify the current defaults and retain the already-derived
                # structural expressions.
                residual = a.multiplied(scales[first]).plus(
                    b.multiplied(scales[second])
                )
                if abs(residual.evaluate(model.parameter_defaults)) > 1e-9:
                    return None
                progressed = True
                continue
            if first_known:
                scales[second] = a.multiplied(scales[first]).scaled(-1.0).divided(b)
                progressed = True
                continue
            if second_known:
                scales[first] = b.multiplied(scales[second]).scaled(-1.0).divided(a)
                progressed = True
                continue
            next_pending.append(constraint)

        if not next_pending:
            break
        if not progressed:
            return None
        pending = next_pending

    if set(scales) != family_set:
        return None
    for value in scales.values():
        low, _ = value.interval(model.parameter_domains, model.parameter_defaults)
        if low < -ZERO_TOL:
            return None

    return anchor, scales, [constraint.name for constraint in continuity]


def _logical_anchor_bound(
    model: CompilerModel,
    expression: ParamExpr | None,
    *,
    source_name: str,
    logical_name: str,
    side: str,
) -> ParamExpr | None:
    """Rename an anchor bound slot for compiler-facing logical output.

    The authoritative runtime value still comes from the production LP anchor
    variable. ``parameter_sources`` records that mapping, allowing all frozen
    equations to refer only to the logical transaction name.
    """

    if expression is None:
        return None
    result = expression.copy()
    source_slot = variable_slot(source_name, side)
    logical_slot = variable_slot(logical_name, side)
    if source_slot in result.slots():
        result.rename_slot(source_slot, logical_slot)
        model.parameter_sources[logical_slot] = source_slot
        if source_slot in model.parameter_defaults:
            model.parameter_defaults[logical_slot] = model.parameter_defaults[source_slot]
        if source_slot in model.parameter_domains:
            model.parameter_domains[logical_slot] = model.parameter_domains[source_slot]
    return result


def _structural_nonnegative_unbounded(variable: ParametricVariable) -> bool:
    return (
        variable.lower is not None
        and variable.lower.is_constant(0.0)
        and variable.upper is None
    )


def collapse_transaction_path_variables(model: CompilerModel) -> dict[str, int]:
    """Replace each LP path-leg family with one logical transaction variable.

    The production LP deliberately uses one nonnegative variable per path leg
    plus homogeneous continuity rows.  That representation is convenient for
    the general LP solver but unnecessarily large for compiled equations.

    V2 derives exact path-leg multipliers from those continuity rows, creates a
    single variable named with the transaction id, substitutes every original
    path-leg occurrence, and reconstructs requested LP values on return.  Any
    nontrivial non-anchor path-leg bounds are preserved as ordinary linear
    constraints on the logical variable.
    """

    transactions = 0
    path_legs = 0
    continuity_rows = 0

    for transaction_id, family in list(_path_transaction_families(model).items()):
        if transaction_id in model.variables:
            # A same-named production LP variable (for example a group) makes
            # the logical name ambiguous.  Transaction ids are normally unique,
            # but skipping is safer than changing the mathematical model.
            continue

        derived = _transaction_scales(model, transaction_id, family)
        if derived is None:
            continue
        anchor, scales, continuity_names = derived
        anchor_variable = model.variables[anchor]
        model.variables[transaction_id] = ParametricVariable(
            transaction_id,
            _logical_anchor_bound(
                model,
                anchor_variable.lower,
                source_name=anchor,
                logical_name=transaction_id,
                side="lower",
            ),
            _logical_anchor_bound(
                model,
                anchor_variable.upper,
                source_name=anchor,
                logical_name=transaction_id,
                side="upper",
            ),
        )
        # Residual kernels can revisit the same logical transaction multiple
        # times during equal-priority water filling.  Preserve an explicit
        # runtime source for its *current* value even when the preparation-day
        # lower bound happened to be structural zero.
        current_slot = f"variable[{transaction_id}].current"
        model.parameter_sources[current_slot] = variable_slot(anchor, "lower")
        model.parameter_defaults[current_slot] = (
            0.0
            if anchor_variable.lower is None
            else anchor_variable.lower.evaluate(model.parameter_defaults)
        )

        # Preserve any path-leg domain that is not the ordinary [0,+inf)
        # domain. The anchor domain is already the logical variable domain.
        for leg_name in family:
            if leg_name == anchor:
                continue
            variable = model.variables[leg_name]
            scale = scales[leg_name]
            scale_low, _ = scale.interval(model.parameter_domains, model.parameter_defaults)
            if _structural_nonnegative_unbounded(variable) and scale_low >= -ZERO_TOL:
                continue
            constraint_name = f"{PATH_BOUND_PREFIX}{leg_name}"
            model.constraints[constraint_name] = ParametricConstraint(
                name=constraint_name,
                lower=None if variable.lower is None else variable.lower.copy(),
                upper=None if variable.upper is None else variable.upper.copy(),
                coefficients={} if scale.is_constant(0.0) else {transaction_id: scale.copy()},
            )

        # Substitute the logical variable through every LP row and through the
        # source-variable reconstruction map used to return original path-leg
        # values to Apportioner.
        for leg_name in family:
            model.substitute_variable(
                leg_name,
                SymbolicExpr.variable(transaction_id, scales[leg_name]),
                reason=f"logical transaction {transaction_id}",
                record_note=False,
            )

        # The continuity equations should now be exact 0 == 0 identities.
        removed_continuity = 0
        for constraint_name in continuity_names:
            constraint = model.constraints.get(constraint_name)
            if constraint is None:
                continue
            if (
                not constraint.coefficients
                and _zero(constraint.lower)
                and _zero(constraint.upper)
            ):
                model.remove_constraint(constraint_name)
                removed_continuity += 1

        if len(family) > 1:
            scale_text = "; ".join(
                f"{leg_name} = {scales[leg_name].text()}*{transaction_id}"
                for leg_name in family
            )
            model.notes.append(
                f"{transaction_id} collapsed {len(family)} LP path-leg variables "
                f"into one logical transaction variable: {scale_text}"
            )
        transactions += 1
        path_legs += len(family)
        continuity_rows += removed_continuity

    return {
        "logical_transactions_collapsed": transactions,
        "path_leg_variables_collapsed": path_legs,
        "continuity_rows_removed": continuity_rows,
    }


def compile_residual_increment_ir(
    model: CompilerModel,
    *,
    transaction_names: set[str],
    committed_names: set[str] | None = None,
) -> dict[str, int]:
    """Rebase logical transactions onto the explicit residual execution state.

    The execution IR subtracts every committed transaction increment from the
    affected constraint RHS values.  A residual LP kernel must therefore *not*
    optimize absolute transaction totals against the original LP RHS or the
    committed contribution would be counted twice.

    For each logical transaction ``x`` this pass changes the kernel decision
    variable to ``dx = x - x.current``:

    * ``dx.lower = 0``;
    * ``dx.upper = x.upper - x.current``;
    * constraint coefficients are unchanged and operate on residual RHS state;
    * reconstruction becomes ``x = x.current + dx``; and
    * transactions proven safe to freeze by the structural scheduler are
      fixed at ``dx = 0`` and disappear from the kernel entirely. Transactions
      involved in directional/counterflow tie breaking remain as residual
      recourse increments because their feasible region can change by priority.

    Constraint sides touched by a rebased transaction are parameterized as
    explicit ``constraint[...].remaining_*`` slots even when their preparation
    value is zero.  Runtime resolves those slots from ``ResidualState`` rather
    than from the mutable production LP.
    """

    committed_names = committed_names or set()
    rebased = 0
    committed_removed = 0
    parameterized_sides = 0

    active_transactions = {
        name for name in transaction_names if name in model.variables
    }
    if not active_transactions:
        return {
            "residual_variables_rebased": 0,
            "committed_variables_removed": 0,
            "residual_constraint_sides_parameterized": 0,
        }

    model.uses_residual_state = True

    # First make every finite row side touched by a transaction an explicit
    # residual-state parameter.  Equality rows that already use a shared
    # ``.remaining`` parameter (measurement rows) are left intact.
    for constraint in model.constraints.values():
        if (
            model.source_constraint_names
            and constraint.name not in model.source_constraint_names
        ):
            continue
        if not any(
            name in active_transactions
            and not coefficient.is_constant(0.0)
            for name, coefficient in constraint.coefficients.items()
        ):
            continue

        is_equality = (
            constraint.lower is not None
            and constraint.upper is not None
            and constraint.lower.equivalent(constraint.upper)
        )
        if is_equality:
            current = constraint.lower.evaluate(model.parameter_defaults)
            slot = f"constraint[{constraint.name}].remaining"
            shared = ParamExpr.slot(slot)
            constraint.lower = shared.copy()
            constraint.upper = shared.copy()
            model.parameter_defaults[slot] = current
            parameterized_sides += 2
            continue

        if constraint.lower is not None:
            current = constraint.lower.evaluate(model.parameter_defaults)
            slot = constraint_slot(constraint.name, "lower")
            constraint.lower = ParamExpr.slot(slot)
            model.parameter_defaults[slot] = current
            parameterized_sides += 1
        if constraint.upper is not None:
            current = constraint.upper.evaluate(model.parameter_defaults)
            slot = constraint_slot(constraint.name, "upper")
            constraint.upper = ParamExpr.slot(slot)
            model.parameter_defaults[slot] = current
            parameterized_sides += 1

    # Then reinterpret logical transaction variables as nonnegative increments.
    for name in sorted(active_transactions):
        variable = model.variables[name]
        # Always make the base a runtime ``current`` parameter.  Sequential
        # kernels normally execute once, but equal-priority members may be
        # revisited after another member caps.  Using the live committed lower
        # bound keeps ``dx`` relative to the current allocation on every
        # water-filling iteration.
        current_slot = f"variable[{name}].current"
        if current_slot not in model.parameter_sources:
            model.parameter_sources[current_slot] = variable_slot(name, "lower")
        if current_slot not in model.parameter_defaults:
            model.parameter_defaults[current_slot] = (
                0.0
                if variable.lower is None
                else variable.lower.evaluate(model.parameter_defaults)
            )
        base = ParamExpr.slot(current_slot)
        model.residual_increment_bases[name] = base.copy()

        # Reconstruction expressions are absolute production values.  Preserve
        # the variable coefficient on dx and add coefficient*x.current to the
        # parameter-only term.
        for expression in model.reconstruction.values():
            coefficient = expression.variables.get(name)
            if coefficient is None:
                continue
            expression.parameters = expression.parameters.plus(
                coefficient.multiplied(base)
            )

        variable.lower = ParamExpr.constant_value(0.0)
        if variable.upper is not None:
            variable.upper = variable.upper.shifted(base, -1.0)
        rebased += 1

    # Structurally safe senior transactions have already been committed into
    # ResidualState and cannot increase in a later objective. Directionally
    # ambiguous seniors are deliberately omitted from committed_names by the
    # scheduler and remain as residual recourse increments.
    for name in sorted(committed_names & active_transactions):
        if name not in model.variables:
            continue
        model.fix_variable(
            name,
            ParamExpr.constant_value(0.0),
            reason="committed senior already represented in residual state",
        )
        committed_removed += 1

    if rebased:
        model.notes.append(
            f"{rebased} logical transaction variable(s) rebased to residual "
            "increments; absolute committed values live in execution ResidualState"
        )
    if committed_removed:
        model.notes.append(
            f"{committed_removed} committed senior transaction variable(s) "
            "removed from this kernel"
        )

    return {
        "residual_variables_rebased": rebased,
        "committed_variables_removed": committed_removed,
        "residual_constraint_sides_parameterized": parameterized_sides,
    }


def structural_equality_elimination(model: CompilerModel, *, protected: set[str]) -> int:
    """Collapse structural homogeneous two-variable equalities.

    Only a literal, non-parameterized ``0 == row == 0`` is used here. This
    captures transaction path continuity without learning anything from a
    particular day's measured RHS values.
    """

    eliminated = 0
    changed = True
    while changed:
        changed = False
        for constraint_name, constraint in list(model.constraints.items()):
            if not (_zero(constraint.lower) and _zero(constraint.upper)):
                continue
            coefficients = [
                (name, coefficient)
                for name, coefficient in constraint.coefficients.items()
                if not coefficient.is_constant(0.0) and name in model.variables
            ]
            if len(coefficients) != 2:
                continue

            (first, a_expr), (second, b_expr) = coefficients
            # General parameterized continuity is handled by the dedicated
            # transaction-collapse pass.  Generic equality elimination stays
            # conservative and only substitutes literal coefficients.
            if not (a_expr.is_constant() and b_expr.is_constant()):
                continue
            a = a_expr.constant_value_number()
            b = b_expr.constant_value_number()
            if first in protected and second in protected:
                continue
            if first in protected:
                keep, keep_coef, remove, remove_coef = first, a, second, b
            elif second in protected:
                keep, keep_coef, remove, remove_coef = second, b, first, a
            elif model.variable_occurrences(first) >= model.variable_occurrences(second):
                keep, keep_coef, remove, remove_coef = first, a, second, b
            else:
                keep, keep_coef, remove, remove_coef = second, b, first, a

            scale = -keep_coef / remove_coef
            if scale <= 0:
                continue

            removed = model.variables[remove]
            # V2 initially substitutes only a path component with the ordinary
            # [0,+inf) domain. More complicated bound translation can be added
            # later without changing this frozen-IR architecture.
            if not (
                removed.lower is not None
                and removed.lower.is_constant(0.0)
                and removed.upper is None
            ):
                continue

            model.remove_constraint(constraint_name)
            model.substitute_variable(
                remove,
                SymbolicExpr.variable(keep, scale),
                reason=f"zero-RHS equality {constraint_name}",
            )
            eliminated += 1
            changed = True
            break
    return eliminated


def fixed_variable_elimination(model: CompilerModel, *, protected: set[str]) -> int:
    eliminated = 0
    for name, variable in list(model.variables.items()):
        if name in protected:
            continue
        if (
            _same(variable.lower, variable.upper)
            and variable.lower is not None
            and not variable.lower.slots()
        ):
            model.fix_variable(name, variable.lower, reason="structural fixed bound")
            eliminated += 1
    return eliminated


def _bound_is_safe(model: CompilerModel, name: str, *, use_lower: bool) -> bool:
    """Whether moving a nonobjective variable to one bound can only relax rows."""

    for constraint in model.constraints.values():
        coefficient = constraint.coefficients.get(name)
        if coefficient is None or coefficient.is_constant(0.0):
            continue
        sign = model.coefficient_sign(coefficient)
        if sign is None:
            return False
        if use_lower:
            if constraint.upper is not None and sign < 0:
                return False
            if constraint.lower is not None and sign > 0:
                return False
        else:
            if constraint.upper is not None and sign > 0:
                return False
            if constraint.lower is not None and sign < 0:
                return False
    return True


def monotone_nonobjective_elimination(model: CompilerModel, *, protected: set[str]) -> int:
    """Move provably harmless nonobjective variables to parameterized bounds.

    Whether a variable is safe at its lower or upper bound depends only on the
    signs of *that variable's* coefficients and on which sides of each row are
    present. Fixing another variable shifts row bounds but does not change
    either fact.  We can therefore classify every monotone variable once and
    substitute all of them in a single model pass instead of repeatedly
    rescanning the whole model after each elimination.
    """

    fixes: dict[str, ParamExpr] = {}
    reasons: dict[str, str] = {}
    for name, variable in list(model.variables.items()):
        if name in protected:
            continue
        if variable.lower is not None and _bound_is_safe(model, name, use_lower=True):
            fixes[name] = variable.lower
            reasons[name] = "monotone lower bound"
        elif variable.upper is not None and _bound_is_safe(model, name, use_lower=False):
            fixes[name] = variable.upper
            reasons[name] = "monotone upper bound"

    model.fix_variables(fixes, reasons=reasons)
    return len(fixes)


def derived_slack_elimination(model: CompilerModel, *, protected: set[str]) -> int:
    """Project deterministic reporting slacks where doing so is exact.

    One-direction reporting slacks can be eliminated analytically from a
    measurement equality, leaving the corresponding one-sided allocation
    constraint. Pure bidirectional reporting pairs can be projected out with
    their entire measurement row because any signed residual is representable.
    Storage-related bidirectional rows retain transient residual-proxy columns
    only for the counterflow ambiguity convention. Final reported slack values
    are always calculated directly from the leftover measurement and never
    taken from an LP solution.
    """

    slack_names = {
        name for name in model.variables
        if name.startswith("SLACK_") and name not in protected
    }
    if not slack_names:
        return 0

    eliminated: set[str] = set()
    for constraint in model.constraints.values():
        row_slacks = [
            name for name in list(constraint.coefficients)
            if name in slack_names
        ]
        if not row_slacks:
            continue

        signed: list[tuple[str, int]] = []
        for name in row_slacks:
            variable = model.variables[name]
            if not _structural_nonnegative_unbounded(variable):
                signed = []
                break
            sign = model.coefficient_sign(constraint.coefficients[name])
            if sign is None or sign == 0:
                signed = []
                break
            signed.append((name, sign))
        if len(signed) != len(row_slacks):
            continue

        has_positive = any(sign > 0 for _, sign in signed)
        has_negative = any(sign < 0 for _, sign in signed)
        if has_positive and has_negative:
            if constraint.name not in model.directional_residual_constraints:
                # Existentially projecting s+ >= 0 and s- >= 0 from
                # row + s+ - s- = measured leaves no restriction on row.
                # This is a pure reporting residual, so remove the entire
                # measurement row and both reporting slack variables.
                for name, _ in signed:
                    eliminated.add(name)
                constraint.coefficients.clear()
                constraint.lower = None
                constraint.upper = None
                model.notes.append(
                    f"{constraint.name} removed: bidirectional reporting "
                    "residual is derived after allocation"
                )
            else:
                model.notes.append(
                    f"{constraint.name} bidirectional reporting slack columns "
                    "retained only as transient directional residual proxies; "
                    "reported values are derived from leftover measurement"
                )
            continue

        for name, _ in signed:
            constraint.coefficients.pop(name, None)
            eliminated.add(name)

        if has_positive:
            # row + s = measured, s >= 0  =>  row <= measured
            constraint.lower = None
            model.notes.append(
                f"{constraint.name} lower side removed: forward reporting "
                "slack is derived from leftover measurement"
            )
        elif has_negative:
            # row - s = measured, s >= 0  =>  row >= measured
            constraint.upper = None
            model.notes.append(
                f"{constraint.name} upper side removed: reverse reporting "
                "slack is derived from leftover measurement"
            )

    for name in [
        name for name, constraint in model.constraints.items()
        if constraint.lower is None and constraint.upper is None
    ]:
        model.constraints.pop(name, None)

    for name in eliminated:
        model.variables.pop(name, None)

    for source_name in list(model.reconstruction):
        transaction_name = source_name.split(PATH_LEG_SEPARATOR, 1)[0]
        if transaction_name in eliminated:
            model.reconstruction.pop(source_name, None)

    if eliminated:
        model.notes.append(
            f"{len(eliminated)} reporting slack variable(s) removed from "
            "optimization and deferred to residual calculation"
        )
    return len(eliminated)


def remove_redundant_constraint_sides(
    model: CompilerModel,
    *,
    protected: set[str],
) -> int:
    """Remove redundant row sides with an explicit proof obligation.

    First try to prove the redundancy over the full declared parameter domain.
    Such a rewrite is structural and needs no runtime guard.  If that proof is
    unavailable but the predicate holds for preparation defaults, retain the
    exact algebraic predicate as a runtime guard; the compilation session is
    responsible for freezing a guard-free alternate program for that region.
    """

    removed = 0
    for constraint in model.constraints.values():
        minimum = ParamExpr.constant_value(0.0)
        maximum = ParamExpr.constant_value(0.0)
        min_finite = True
        max_finite = True
        for name, coefficient in constraint.coefficients.items():
            variable = model.variables[name]
            sign = model.coefficient_sign(coefficient)
            if sign is None:
                min_finite = False
                max_finite = False
                break
            low = variable.lower if sign >= 0 else variable.upper
            high = variable.upper if sign >= 0 else variable.lower
            if low is None:
                min_finite = False
            else:
                minimum = minimum.plus(coefficient.multiplied(low))
            if high is None:
                max_finite = False
            else:
                maximum = maximum.plus(coefficient.multiplied(high))

        if constraint.lower is not None and min_finite:
            predicate = GuardPredicate(
                lhs=minimum.copy(),
                relation=">=",
                rhs=constraint.lower.copy(),
                description=f"{constraint.name} lower side is redundant",
            )
            if predicate.structurally_proven(model.parameter_domains):
                model.add_structural_proof(predicate)
                constraint.lower = None
                removed += 1
            elif predicate.evaluate_margin(model.parameter_defaults) >= -ZERO_TOL:
                model.add_guard(predicate)
                constraint.lower = None
                removed += 1

        if (
            constraint.upper is not None
            and max_finite
            and not any(name in protected for name in constraint.coefficients)
        ):
            predicate = GuardPredicate(
                lhs=maximum.copy(),
                relation="<=",
                rhs=constraint.upper.copy(),
                description=f"{constraint.name} upper side is redundant",
            )
            if predicate.structurally_proven(model.parameter_domains):
                model.add_structural_proof(predicate)
                constraint.upper = None
                removed += 1
            elif predicate.evaluate_margin(model.parameter_defaults) >= -ZERO_TOL:
                model.add_guard(predicate)
                constraint.upper = None
                removed += 1
    return removed


def drop_empty_rows(model: CompilerModel) -> int:
    """Drop only structurally tautological empty rows.

    Parameterized empty rows are retained as runtime feasibility guards by the
    reduced kernel; we do not inspect preparation-day values to remove them.
    """

    removed = 0
    for name, constraint in list(model.constraints.items()):
        if constraint.coefficients:
            continue
        lower_ok = constraint.lower is None or (
            constraint.lower.is_constant()
            and constraint.lower.constant_value_number() <= ZERO_TOL
        )
        upper_ok = constraint.upper is None or (
            constraint.upper.is_constant()
            and constraint.upper.constant_value_number() >= -ZERO_TOL
        )
        if lower_ok and upper_ok:
            model.remove_constraint(name)
            removed += 1
    return removed


def presolve(
    model: CompilerModel,
    *,
    protected: set[str],
    allow_guarded_redundancy: bool = True,
) -> dict[str, int]:
    """Parameter-safe v2 presolve.

    Guarded redundancy is useful for compact direct formulas, but a frozen
    plan also prepares an unguarded variant when a later parameter state leaves
    that region.
    """

    stats = {
        "equality_eliminated": structural_equality_elimination(model, protected=protected),
        "fixed_eliminated": 0,
        "slack_eliminated": 0,
        "redundant_sides_removed": 0,
        "monotone_eliminated": 0,
        "empty_rows_removed": 0,
    }
    stats["fixed_eliminated"] = fixed_variable_elimination(model, protected=protected)
    stats["slack_eliminated"] = derived_slack_elimination(model, protected=protected)
    if allow_guarded_redundancy:
        stats["redundant_sides_removed"] = remove_redundant_constraint_sides(
            model, protected=protected
        )
    stats["monotone_eliminated"] = monotone_nonobjective_elimination(
        model, protected=protected
    )
    if allow_guarded_redundancy:
        stats["redundant_sides_removed"] += remove_redundant_constraint_sides(
            model, protected=protected
        )
    stats["empty_rows_removed"] = drop_empty_rows(model)
    return stats
