"""Structural compiler passes for the frozen parameterized v2 IR."""

from __future__ import annotations

from .model import CompilerModel, ParamExpr, SymbolicExpr


ZERO_TOL = 1e-12


def _zero(expr: ParamExpr | None) -> bool:
    return expr is not None and expr.is_constant(0.0)


def _same(a: ParamExpr | None, b: ParamExpr | None) -> bool:
    return a is not None and b is not None and a.equivalent(b)


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
                if abs(coefficient) > ZERO_TOL and name in model.variables
            ]
            if len(coefficients) != 2:
                continue

            (first, a), (second, b) = coefficients
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
            and not variable.lower.coefficients
        ):
            model.fix_variable(name, variable.lower, reason="structural fixed bound")
            eliminated += 1
    return eliminated


def _bound_is_safe(model: CompilerModel, name: str, *, use_lower: bool) -> bool:
    """Whether moving a nonobjective variable to one bound can only relax rows."""

    for constraint in model.constraints.values():
        coefficient = constraint.coefficients.get(name, 0.0)
        if abs(coefficient) <= ZERO_TOL:
            continue
        if use_lower:
            if constraint.upper is not None and coefficient < 0:
                return False
            if constraint.lower is not None and coefficient > 0:
                return False
        else:
            if constraint.upper is not None and coefficient > 0:
                return False
            if constraint.lower is not None and coefficient < 0:
                return False
    return True


def monotone_nonobjective_elimination(model: CompilerModel, *, protected: set[str]) -> int:
    """Move provably harmless nonobjective variables to parameterized bounds."""

    eliminated = 0
    changed = True
    while changed:
        changed = False
        for name, variable in list(model.variables.items()):
            if name in protected:
                continue
            if variable.lower is not None and _bound_is_safe(model, name, use_lower=True):
                model.fix_variable(name, variable.lower, reason="monotone lower bound")
                eliminated += 1
                changed = True
                break
            if variable.upper is not None and _bound_is_safe(model, name, use_lower=False):
                model.fix_variable(name, variable.upper, reason="monotone upper bound")
                eliminated += 1
                changed = True
                break
    return eliminated


def one_sided_slack_elimination(model: CompilerModel, *, protected: set[str]) -> int:
    """Eliminate a nonnegative, unbounded reconciliation variable exactly."""

    eliminated = 0
    for variable_name, variable in list(model.variables.items()):
        if variable_name in protected:
            continue
        if variable.lower is None or not variable.lower.is_constant(0.0):
            continue
        if variable.upper is not None:
            continue

        occurrences = [
            constraint
            for constraint in model.constraints.values()
            if abs(constraint.coefficients.get(variable_name, 0.0)) > ZERO_TOL
        ]
        if len(occurrences) != 1:
            continue
        constraint = occurrences[0]
        if not _same(constraint.lower, constraint.upper):
            continue

        coefficient = constraint.coefficients.pop(variable_name)
        equality = constraint.lower
        assert equality is not None
        if coefficient > 0:
            constraint.lower = None
            constraint.upper = equality.copy()
        else:
            constraint.lower = equality.copy()
            constraint.upper = None

        model.variables.pop(variable_name, None)
        model.reconstruction.pop(variable_name, None)
        model.notes.append(
            f"{variable_name} eliminated as one-sided reconciliation slack from {constraint.name}"
        )
        eliminated += 1
    return eliminated


def remove_redundant_constraint_sides(
    model: CompilerModel,
    *,
    protected: set[str],
) -> int:
    """Remove sides redundant for the prepared parameter region, with guards.

    The decision is made against preparation defaults, but the algebraic
    condition proving redundancy is retained as a runtime guard. Therefore no
    numeric bound is baked into the frozen program.
    """

    removed = 0
    for constraint in model.constraints.values():
        minimum = ParamExpr.constant_value(0.0)
        maximum = ParamExpr.constant_value(0.0)
        min_finite = True
        max_finite = True
        for name, coefficient in constraint.coefficients.items():
            variable = model.variables[name]
            low = variable.lower if coefficient >= 0 else variable.upper
            high = variable.upper if coefficient >= 0 else variable.lower
            if low is None:
                min_finite = False
            else:
                minimum.add_scaled(low, coefficient)
            if high is None:
                max_finite = False
            else:
                maximum.add_scaled(high, coefficient)

        if constraint.lower is not None and min_finite:
            guard = minimum.shifted(constraint.lower, -1.0)
            if guard.evaluate(model.parameter_defaults) >= -ZERO_TOL:
                model.add_guard(guard, f"{constraint.name} lower side remains redundant")
                constraint.lower = None
                removed += 1

        if (
            constraint.upper is not None
            and max_finite
            and not any(name in protected for name in constraint.coefficients)
        ):
            guard = constraint.upper.shifted(maximum, -1.0)
            if guard.evaluate(model.parameter_defaults) >= -ZERO_TOL:
                model.add_guard(guard, f"{constraint.name} upper side remains redundant")
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
            constraint.lower.is_constant() and constraint.lower.constant <= ZERO_TOL
        )
        upper_ok = constraint.upper is None or (
            constraint.upper.is_constant() and constraint.upper.constant >= -ZERO_TOL
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
    stats["slack_eliminated"] = one_sided_slack_elimination(model, protected=protected)
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
