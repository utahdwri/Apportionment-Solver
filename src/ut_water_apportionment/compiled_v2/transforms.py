"""Algebraic compiler passes for the v2 experimental backend."""

from __future__ import annotations

from math import inf, isfinite, isclose

from .model import CompilerModel, LinearConstraint, LinearExpr


ZERO_TOL = 1e-12


def structural_equality_elimination(
    model: CompilerModel,
    *,
    protected: set[str],
) -> int:
    """Collapse homogeneous two-variable equalities such as path continuity.

    The production LP commonly represents one logical transaction by one
    variable per path item plus a zero-RHS continuity equation.  This pass is
    intentionally conservative: only exact zero equalities with two variables
    are used, so measured-flow and other parameterized equalities remain rows.
    """

    eliminated = 0
    changed = True
    while changed:
        changed = False
        for constraint_name, constraint in list(model.constraints.items()):
            if not (
                isfinite(constraint.lower)
                and isfinite(constraint.upper)
                and isclose(constraint.lower, 0.0, abs_tol=ZERO_TOL)
                and isclose(constraint.upper, 0.0, abs_tol=ZERO_TOL)
            ):
                continue
            coefficients = [
                (name, coefficient)
                for name, coefficient in constraint.coefficients.items()
                if abs(coefficient) > ZERO_TOL and name in model.variables
            ]
            if len(coefficients) != 2:
                continue

            (first, a), (second, b) = coefficients
            # Preserve requested/objective variables whenever possible.
            if first in protected and second in protected:
                continue
            if first in protected:
                keep, keep_coef = first, a
                remove, remove_coef = second, b
            elif second in protected:
                keep, keep_coef = second, b
                remove, remove_coef = first, a
            else:
                # Prefer keeping the variable with the larger model footprint;
                # substituting the smaller-footprint variable tends to preserve
                # the anchor/path representative used elsewhere in the LP.
                first_occ = model.variable_occurrences(first)
                second_occ = model.variable_occurrences(second)
                if first_occ >= second_occ:
                    keep, keep_coef = first, a
                    remove, remove_coef = second, b
                else:
                    keep, keep_coef = second, b
                    remove, remove_coef = first, a

            scale = -keep_coef / remove_coef
            removed_variable = model.variables[remove]
            kept_variable = model.variables[keep]

            # Translate only simple compatible bounds.  Continuity path
            # variables are normally [0,+inf), for which this is exact and
            # requires no extra row.  More exotic bounds are left untouched by
            # skipping this substitution for now.
            if scale <= 0:
                continue
            translated_lower = (
                removed_variable.lower / scale
                if isfinite(removed_variable.lower)
                else -inf
            )
            translated_upper = (
                removed_variable.upper / scale
                if isfinite(removed_variable.upper)
                else inf
            )
            if translated_lower > kept_variable.lower:
                kept_variable.lower = translated_lower
            if translated_upper < kept_variable.upper:
                kept_variable.upper = translated_upper
            if kept_variable.lower > kept_variable.upper + ZERO_TOL:
                continue

            model.remove_constraint(constraint_name)
            model.substitute_variable(
                remove,
                LinearExpr.variable(keep, scale),
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
        if isfinite(variable.lower) and isfinite(variable.upper) and isclose(
            variable.lower,
            variable.upper,
            abs_tol=ZERO_TOL,
        ):
            model.fix_variable(name, 0.5 * (variable.lower + variable.upper), reason="fixed bound")
            eliminated += 1
    return eliminated


def _bound_is_safe(
    model: CompilerModel,
    name: str,
    *,
    use_lower: bool,
) -> bool:
    """Whether moving a nonobjective variable to one bound can only relax rows."""

    for constraint in model.constraints.values():
        coefficient = constraint.coefficients.get(name, 0.0)
        if abs(coefficient) <= ZERO_TOL:
            continue
        if use_lower:
            # Increasing from the lower bound must never relax a finite side.
            if isfinite(constraint.upper) and coefficient < 0:
                return False
            if isfinite(constraint.lower) and coefficient > 0:
                return False
        else:
            # Decreasing from the upper bound must never relax a finite side.
            if isfinite(constraint.upper) and coefficient > 0:
                return False
            if isfinite(constraint.lower) and coefficient < 0:
                return False
    return True


def monotone_nonobjective_elimination(
    model: CompilerModel,
    *,
    protected: set[str],
) -> int:
    """Move provably harmless nonobjective variables to a bound.

    This is the v2 version of "move solved/junior work to the right-hand side".
    It is objective-independent and therefore deliberately conservative.
    """

    eliminated = 0
    changed = True
    while changed:
        changed = False
        for name, variable in list(model.variables.items()):
            if name in protected:
                continue
            if isfinite(variable.lower) and _bound_is_safe(
                model,
                name,
                use_lower=True,
            ):
                model.fix_variable(name, variable.lower, reason="monotone lower bound")
                eliminated += 1
                changed = True
                break
            if isfinite(variable.upper) and _bound_is_safe(
                model,
                name,
                use_lower=False,
            ):
                model.fix_variable(name, variable.upper, reason="monotone upper bound")
                eliminated += 1
                changed = True
                break
    return eliminated



def one_sided_slack_elimination(
    model: CompilerModel,
    *,
    protected: set[str],
) -> int:
    """Eliminate a nonnegative, unbounded reconciliation variable.

    For an equality ``rest + c*s = b`` with ``s >= 0`` and no upper bound,
    existentially eliminating ``s`` gives ``rest <= b`` when ``c > 0`` and
    ``rest >= b`` when ``c < 0``.  This is the spreadsheet-like residual
    interpretation of many measurement slack variables.
    """

    eliminated = 0
    for variable_name, variable in list(model.variables.items()):
        if variable_name in protected:
            continue
        if not isclose(variable.lower, 0.0, abs_tol=ZERO_TOL):
            continue
        if variable.upper != inf:
            continue

        occurrences = [
            constraint
            for constraint in model.constraints.values()
            if abs(constraint.coefficients.get(variable_name, 0.0)) > ZERO_TOL
        ]
        if len(occurrences) != 1:
            continue
        constraint = occurrences[0]
        if not (
            isfinite(constraint.lower)
            and isfinite(constraint.upper)
            and isclose(constraint.lower, constraint.upper, abs_tol=ZERO_TOL)
        ):
            continue

        coefficient = constraint.coefficients.pop(variable_name)
        equality_value = 0.5 * (constraint.lower + constraint.upper)
        if coefficient > 0:
            constraint.lower = -inf
            constraint.upper = equality_value
        else:
            constraint.lower = equality_value
            constraint.upper = inf

        model.variables.pop(variable_name, None)
        # This variable is existential for this objective and is not requested;
        # there is no need to reconstruct it in the returned component vector.
        model.reconstruction.pop(variable_name, None)
        model.notes.append(
            f"{variable_name} eliminated as one-sided reconciliation slack "
            f"from {constraint.name}"
        )
        eliminated += 1
    return eliminated


def remove_redundant_constraint_sides(
    model: CompilerModel,
    *,
    protected: set[str],
) -> int:
    """Drop row sides already guaranteed by current variable bounds."""

    removed = 0
    for constraint in model.constraints.values():
        minimum = 0.0
        maximum = 0.0
        min_finite = True
        max_finite = True
        for name, coefficient in constraint.coefficients.items():
            variable = model.variables[name]
            low_value = variable.lower if coefficient >= 0 else variable.upper
            high_value = variable.upper if coefficient >= 0 else variable.lower
            if isfinite(low_value):
                minimum += coefficient * low_value
            else:
                min_finite = False
            if isfinite(high_value):
                maximum += coefficient * high_value
            else:
                max_finite = False

        if (
            isfinite(constraint.lower)
            and min_finite
            and minimum >= constraint.lower - ZERO_TOL
        ):
            constraint.lower = -inf
            removed += 1
        # Keep a redundant upper side when it directly contains an objective
        # variable: it may be loose for today's measurements but is exactly the
        # runtime limit a reusable compiled equation should retain.  Upper sides
        # in disconnected/nonobjective components may be removed normally.
        if (
            isfinite(constraint.upper)
            and max_finite
            and maximum <= constraint.upper + ZERO_TOL
            and not any(name in protected for name in constraint.coefficients)
        ):
            constraint.upper = inf
            removed += 1
    return removed

def drop_empty_rows(model: CompilerModel) -> int:
    removed = 0
    for name, constraint in list(model.constraints.items()):
        if constraint.coefficients:
            continue
        if constraint.lower <= 0.0 <= constraint.upper:
            model.remove_constraint(name)
            removed += 1
    return removed


def presolve(
    model: CompilerModel,
    *,
    protected: set[str],
) -> dict[str, int]:
    stats = {
        "equality_eliminated": structural_equality_elimination(
            model,
            protected=protected,
        ),
        "fixed_eliminated": 0,
        "slack_eliminated": 0,
        "redundant_sides_removed": 0,
        "monotone_eliminated": 0,
        "empty_rows_removed": 0,
    }
    stats["fixed_eliminated"] = fixed_variable_elimination(
        model,
        protected=protected,
    )
    stats["slack_eliminated"] = one_sided_slack_elimination(
        model,
        protected=protected,
    )
    stats["redundant_sides_removed"] = remove_redundant_constraint_sides(
        model, protected=protected
    )
    stats["monotone_eliminated"] = monotone_nonobjective_elimination(
        model,
        protected=protected,
    )
    stats["redundant_sides_removed"] += remove_redundant_constraint_sides(
        model, protected=protected
    )
    stats["empty_rows_removed"] = drop_empty_rows(model)
    return stats
