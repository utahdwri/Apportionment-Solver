"""Capture the production LP and reuse bounded symbolic objective programs.

No graph accounting rules are duplicated here. Parent equations, temporary
counterflow caps, proportional rows, and spill locks come from Apportioner and
the selected backend's existing methods.
"""

from collections import Counter
from dataclasses import dataclass, field, replace
from fractions import Fraction
from math import inf, isfinite
from time import perf_counter

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix

from .projection import (
    Bounds,
    CannotCompile,
    CompilationOptions,
    Projector,
    Row,
    rational,
)


@dataclass
class Snapshot:
    names: tuple
    signature: tuple
    rows: tuple
    parameters: np.ndarray
    labels: tuple
    source_variable_count: int = 0
    equality_eliminated: int = 0
    bound_eliminated: int = 0
    target_bound_eliminated: int = 0
    resolved_targets: tuple = ()


@dataclass(frozen=True)
class PreparedProgramCall:
    """One objective call encountered while tracing a prepared date."""

    targets: tuple
    costs: tuple
    maximization: bool
    return_values: bool
    program: object


@dataclass(frozen=True)
class EqualPriorityRoutine:
    """Human-readable description of one equal-priority allocation loop.

    This is presentation metadata only.  The actual numeric work is still
    performed by the cached ObjectiveProgram objects.  We deliberately record
    the *algorithm* rather than the number of iterations observed during plan
    preparation, because runtime inputs can change which members become blocked
    on each pass.
    """

    members: tuple[str, ...]
    initial_factors: tuple[tuple[str, float], ...]
    residual_updates: tuple[str, ...]

    def text(self) -> str:
        lines = [
            "EQUAL-PRIORITY ALLOCATION",
            "Members:",
            *(f"    {_display_transaction_name(name)}" for name in self.members),
            "",
            "Initial proportional factors:",
            *(
                f"    {_display_transaction_name(name)}: {_formula_number(factor)}"
                for name, factor in self.initial_factors
            ),
            "",
            "REPEAT UNTIL NO ACTIVE TRANSACTION CAN INCREASE:",
            "    common_increment = CALCULATE_COMMON_INCREMENT(",
            "        active_transactions,",
            "        remaining_constraint_capacity,",
            "    )",
            "",
            "    FOR transaction IN active_transactions:",
            "        transaction.increment = (",
            "            transaction.proportion * common_increment",
            "        )",
            "        transaction = transaction.current + transaction.increment",
            "",
            "    UPDATE RESIDUAL STATE:",
        ]
        if self.residual_updates:
            lines.extend(f"        {line}" for line in self.residual_updates)
        else:
            lines.append("        (no displayed residual constraints)")
        lines.extend(
            [
                "",
                "    active_transactions = (",
                "        transactions_that_can_still_increase()",
                "    )",
                "    RECALCULATE proportional factors among active_transactions",
                "",
                "The common-increment MIN/MAX expressions used by this loop",
                "are the PROGRAM definitions printed below. Different active",
                "member sets may select different cached PROGRAMs.",
            ]
        )
        return "\n".join(lines)


@dataclass
class _SparseRow:
    """A sparse symbolic row: coefficients @ variables <= parameters @ values."""

    coefficients: dict[str, float]
    parameters: dict[str, float]
    # Only direct variable-bound rows get this marker. Once another variable is
    # substituted into such a row, it becomes an ordinary feasibility row.
    bound_var: str | None = None
    # Equality-derived rows can establish objective/component uniqueness and
    # must never be discarded merely because today's bounds imply them.
    preserve: bool = False

    def negative(self) -> "_SparseRow":
        return _SparseRow(
            {name: -value for name, value in self.coefficients.items()},
            {name: -value for name, value in self.parameters.items()},
            self.bound_var,
            self.preserve,
        )


@dataclass
class _VirtualVariable:
    lower: float
    upper: float

    def lb(self) -> float:
        return self.lower

    def ub(self) -> float:
        return self.upper


@dataclass
class _VirtualConstraint:
    lower: float
    upper: float
    coefficients: dict[str, float]

    def lb(self) -> float:
        return self.lower

    def ub(self) -> float:
        return self.upper


class _VirtualEngine:
    """Minimal LP view used only for symbolic proportional compilation."""

    def __init__(self):
        self.vars: dict[str, _VirtualVariable] = {}
        self.cons: dict[str, _VirtualConstraint] = {}
        self.perminant_minus_var = None
        self.source_variable_count_override: int | None = None

    def get_variable_bounds(self, name: str) -> tuple[float, float]:
        variable = self.vars[name]
        return variable.lb(), variable.ub()


_COMMON_INCREMENT = "__compiled_common_increment__"
_PROP_RESIDUAL_PREFIX = "__compiled_prop_residual__"


def _formula_number(value: float) -> str:
    """Format a numeric display value as a small exact rational when possible."""

    value = rational(value)
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def _display_transaction_name(name: str) -> str:
    """Hide the internal path suffix in human-readable transaction output."""

    return name.split("___", 1)[0]


def _equal_priority_residual_updates(engine, members) -> tuple[str, ...]:
    """Describe residual-state updates for an equal-priority group.

    The display intentionally uses the active-set summation rather than the
    particular members that happened to be active on a warm-up iteration.
    This is the reusable runtime calculation: signed coefficients naturally
    increase remaining capacity when their contribution is negative.
    """

    member_set = set(members)
    all_names = tuple(engine.vars)
    updates: list[str] = []

    for constraint_name, constraint in engine.cons.items():
        coefficients = _constraint_coefficients(engine, constraint, all_names)
        member_coefficients = {
            name: coefficient
            for name, coefficient in coefficients.items()
            if name in member_set and coefficient != 0.0
        }
        if not member_coefficients:
            continue

        values = tuple(member_coefficients.values())
        if all(value == 1.0 for value in values):
            activity = (
                "SUM(transaction.increment FOR transaction IN active_transactions)"
            )
        elif len(set(values)) == 1:
            activity = (
                f"({_formula_number(values[0])}) * "
                "SUM(transaction.increment FOR transaction IN active_transactions)"
            )
        else:
            pieces = ", ".join(
                f"{_display_transaction_name(name)}={_formula_number(value)}"
                for name, value in member_coefficients.items()
            )
            activity = (
                "SUM(coefficient[transaction] * transaction.increment "
                "FOR transaction IN active_transactions)"
                f"  # coefficients: {pieces}"
            )

        lower, upper = constraint.lb(), constraint.ub()
        if isfinite(lower) and isfinite(upper) and lower == upper:
            updates.append(
                f"constraint[{constraint_name}].remaining -= {activity}"
            )
            continue
        if isfinite(lower):
            updates.append(
                f"constraint[{constraint_name}].remaining_lower -= {activity}"
            )
        if isfinite(upper):
            updates.append(
                f"constraint[{constraint_name}].remaining_upper -= {activity}"
            )

    return tuple(updates)


def _constraint_coefficients(engine, constraint, all_names) -> dict[str, float]:
    if hasattr(constraint, "coefficients"):
        return {
            variable: float(value)
            for variable, value in constraint.coefficients.items()
            if value != 0
        }
    return {
        variable: float(constraint.GetCoefficient(engine.vars[variable]))
        for variable in all_names
        if constraint.GetCoefficient(engine.vars[variable]) != 0
    }


def _prepared_parameters(engine, labels, targets) -> np.ndarray:
    """Rebuild only the live RHS parameters for an already-prepared program.

    ``snapshot`` is intentionally expensive because it proves which variables
    can be removed and derives the residual structure. Once that structure is
    frozen, runtime only needs the current values of the named bounds. This
    helper reproduces snapshot's parameter definitions without repeating any
    equality substitution, sign analysis, or projection presolve.
    """

    protected = set(targets)
    all_names = tuple(engine.vars)
    baseline_values = {
        name: float(variable.lb())
        for name, variable in engine.vars.items()
        if name not in protected and isfinite(variable.lb())
    }
    residual_cache: dict[tuple[str, str], float] = {}

    def variable_parameter(body: str) -> float:
        name, separator, side = body.rpartition("].")
        if not separator:
            raise CannotCompile(f"unsupported prepared variable parameter: {body}")
        variable_name = name
        lower, upper = engine.get_variable_bounds(variable_name)
        if side == "lower" or side == "value":
            value = lower
        elif side == "upper":
            value = upper
        elif side == "remaining_upper":
            value = upper - lower
        else:
            raise CannotCompile(f"unsupported prepared variable parameter: {side}")
        if not isfinite(value):
            raise CannotCompile(f"nonfinite prepared variable parameter: {body}")
        return float(value)

    def constraint_parameter(body: str) -> float:
        name, separator, side = body.rpartition("].")
        if not separator:
            raise CannotCompile(f"unsupported prepared constraint parameter: {body}")
        constraint_name = name
        cache_key = (constraint_name, side)
        if cache_key in residual_cache:
            return residual_cache[cache_key]
        constraint = engine.cons[constraint_name]
        coefficients = _constraint_coefficients(engine, constraint, all_names)
        committed_activity = sum(
            coefficients.get(variable, 0.0) * value
            for variable, value in baseline_values.items()
        )
        if side == "remaining" or side == "remaining_upper":
            bound = constraint.ub()
        elif side == "remaining_lower":
            bound = constraint.lb()
        else:
            raise CannotCompile(f"unsupported prepared constraint parameter: {side}")
        value = bound - committed_activity
        if not isfinite(value):
            raise CannotCompile(f"nonfinite prepared constraint parameter: {body}")
        residual_cache[cache_key] = float(value)
        return float(value)

    values: list[float] = []
    for label in labels:
        if label == "objective_value":
            values.append(0.0)
        elif label.startswith("variable["):
            values.append(variable_parameter(label[len("variable[") :]))
        elif label.startswith("constraint["):
            values.append(constraint_parameter(label[len("constraint[") :]))
        else:
            raise CannotCompile(f"unsupported prepared parameter label: {label}")
    return np.asarray(values, dtype=float)


def _proportional_virtual_engine(engine, variable_names, proportion_factors):
    """Substitute an equal-priority group with one increment plus residuals.

    Members that have identical LP columns share one aggregate residual. This is
    exact because only the sum of their residuals can affect the rest of the LP;
    individual upper bounds become floor caps on the common increment, while a
    shared residual-capacity row preserves the total distributable headroom.
    Large groups on the same flow therefore compile with one residual instead
    of hundreds of interchangeable residual variables.
    """

    members = tuple(dict.fromkeys(variable_names))
    member_set = set(members)
    if not members or any(name not in engine.vars for name in members):
        raise CannotCompile("empty or unknown proportional objective targets")
    if _COMMON_INCREMENT in engine.vars or any(
        name.startswith(_PROP_RESIDUAL_PREFIX) for name in engine.vars
    ):
        raise CannotCompile("reserved compiled proportional variable name collision")

    initial_values: dict[str, float] = {}
    factors: dict[str, float] = {}
    virtual = _VirtualEngine()
    virtual.source_variable_count_override = len(engine.vars) + 1

    for name, variable in engine.vars.items():
        lower, upper = variable.lb(), variable.ub()
        if name in member_set:
            factor = float(proportion_factors[name])
            if not isfinite(factor) or factor < 0:
                raise CannotCompile(
                    "compiled proportional groups require finite nonnegative factors"
                )
            if not isfinite(lower):
                raise CannotCompile(
                    "compiled proportional groups require finite member lower bounds"
                )
            initial_values[name] = float(lower)
            factors[name] = factor
        else:
            virtual.vars[name] = _VirtualVariable(float(lower), float(upper))

    all_names = tuple(engine.vars)
    constraint_data = []
    member_columns: dict[str, list[tuple[str, float]]] = {
        name: [] for name in members
    }
    for constraint_name, constraint in engine.cons.items():
        coefficients = _constraint_coefficients(engine, constraint, all_names)
        constraint_data.append(
            (constraint_name, constraint.lb(), constraint.ub(), coefficients)
        )
        for name in members:
            coefficient = coefficients.get(name, 0.0)
            if coefficient != 0.0:
                member_columns[name].append((constraint_name, coefficient))

    groups: dict[tuple[tuple[str, float], ...], list[str]] = {}
    for name in members:
        signature = tuple(member_columns[name])
        groups.setdefault(signature, []).append(name)

    residual_names: dict[str, str] = {}
    residual_groups: dict[str, list[str]] = {}
    for index, grouped_members in enumerate(groups.values()):
        residual_name = f"{_PROP_RESIDUAL_PREFIX}{index}"
        residual_groups[residual_name] = grouped_members
        virtual.vars[residual_name] = _VirtualVariable(0.0, inf)
        for name in grouped_members:
            residual_names[name] = residual_name

    virtual.vars[_COMMON_INCREMENT] = _VirtualVariable(0.0, inf)

    for constraint_name, lower, upper, coefficients in constraint_data:
        transformed: dict[str, float] = {}
        shift = 0.0
        increment_coefficient = 0.0
        residual_coefficients: dict[str, float] = {}
        for variable, coefficient in coefficients.items():
            if variable not in member_set:
                transformed[variable] = coefficient
                continue
            shift += coefficient * initial_values[variable]
            increment_coefficient += coefficient * factors[variable]
            residual_name = residual_names[variable]
            previous = residual_coefficients.get(residual_name)
            if previous is not None and previous != coefficient:
                raise CannotCompile(
                    "proportional residual grouping encountered inconsistent columns"
                )
            residual_coefficients[residual_name] = coefficient

        transformed.update(residual_coefficients)
        if increment_coefficient != 0.0:
            transformed[_COMMON_INCREMENT] = increment_coefficient

        virtual.cons[constraint_name] = _VirtualConstraint(
            float(lower - shift) if isfinite(lower) else -inf,
            float(upper - shift) if isfinite(upper) else inf,
            transformed,
        )

    # Every finite member upper bound caps its proportional floor. For members
    # sharing a residual column, one additional row preserves the aggregate
    # residual capacity that can be distributed among those members.
    floor_cap_index = 0
    for residual_name, grouped_members in residual_groups.items():
        all_finite = True
        total_gap = 0.0
        total_factor = 0.0
        for name in grouped_members:
            _, upper = engine.get_variable_bounds(name)
            factor = factors[name]
            if isfinite(upper):
                gap = float(upper - initial_values[name])
                if factor != 0.0:
                    virtual.cons[f"__compiled_prop_floor_upper__{floor_cap_index}"] = (
                        _VirtualConstraint(
                            -inf,
                            gap,
                            {_COMMON_INCREMENT: factor},
                        )
                    )
                    floor_cap_index += 1
                total_gap += gap
                total_factor += factor
            else:
                all_finite = False

        if all_finite:
            capacity_coefficients = {residual_name: 1.0}
            if total_factor != 0.0:
                capacity_coefficients[_COMMON_INCREMENT] = total_factor
            virtual.cons[f"__compiled_prop_residual_capacity__{residual_name}"] = (
                _VirtualConstraint(
                    -inf,
                    total_gap,
                    capacity_coefficients,
                )
            )

    return virtual, initial_values, factors


def _add_scaled(target: dict[str, float], source: dict[str, float], scale: float) -> None:
    for name, value in source.items():
        updated = target.get(name, 0.0) + scale * value
        if updated == 0.0:
            target.pop(name, None)
        else:
            target[name] = updated


def _substitute_equation(row: _SparseRow, equation: _SparseRow, variable: str) -> None:
    """Eliminate ``variable`` from ``row`` using an equality row exactly."""

    coefficient = row.coefficients.get(variable)
    if coefficient is None:
        return
    pivot = equation.coefficients[variable]
    factor = coefficient / pivot
    _add_scaled(row.coefficients, equation.coefficients, -factor)
    _add_scaled(row.parameters, equation.parameters, -factor)
    # A bound on the eliminated variable is now a general inequality involving
    # the variables that replaced it, so it must participate in sign analysis.
    if row.bound_var == variable:
        row.bound_var = None


def _substitute_bound(
    rows: list[_SparseRow],
    variable: str,
    parameter_label: str,
) -> None:
    """Move a variable fixed at one of its live bounds to the symbolic RHS."""

    for row in rows:
        if row.bound_var == variable:
            # The engine guarantees lb <= ub. Once this objective has safely
            # selected one live bound for the variable, its original bound rows
            # cannot further restrict the projected problem.
            row.coefficients.clear()
            row.parameters.clear()
            row.bound_var = None
            continue
        coefficient = row.coefficients.pop(variable, None)
        if coefficient is None:
            continue
        updated = row.parameters.get(parameter_label, 0.0) - coefficient
        if updated == 0.0:
            row.parameters.pop(parameter_label, None)
        else:
            row.parameters[parameter_label] = updated


def _substitute_constant_bound(
    rows: list[_SparseRow],
    variable: str,
    value: float,
) -> None:
    """Substitute a compile-time constant bound without creating a parameter."""

    for row in rows:
        if row.bound_var == variable:
            row.coefficients.clear()
            row.parameters.clear()
            row.bound_var = None
            continue
        coefficient = row.coefficients.pop(variable, None)
        if coefficient is None or value == 0.0:
            continue
        # row is coefficients @ x <= parameters @ values. Moving c*value to
        # the RHS requires a constant parameter. This helper is currently used
        # for zero-based increments, so retain a defensive assertion rather
        # than silently baking a nonzero constant into the symbolic program.
        raise CannotCompile("nonzero constant bound substitution is unsupported")


def snapshot(engine, options, targets=(), target_preferences=None):
    """Capture an objective-specific, symbolically reduced LP snapshot.

    The production model can be much larger than an individual priority solve.
    Exact equalities are substituted first. Remaining non-objective variables
    that can only tighten every active inequality are fixed at the bound that
    weakens those inequalities (normally the committed lower bound). Variables
    with mixed/opposing signs remain live. This preserves feasibility while
    moving solved senior allocations and harmless junior allocations to the RHS.
    """

    if getattr(engine, "perminant_minus_var", None) is not None:
        raise CannotCompile("backend permanent-minus objective requires LP")

    all_names = tuple(engine.vars)
    protected = set(targets)
    target_preferences = target_preferences or {}
    resolved_targets: dict[str, str] = {}
    if any(name not in engine.vars for name in protected):
        raise CannotCompile("empty or unknown objective targets")

    parameter_values: dict[str, float] = {}
    variable_bounds: dict[str, tuple[float, float]] = {}
    bound_labels: dict[tuple[str, str], str | None] = {}
    # Every non-target variable with a finite lower bound is represented as an
    # increment above that bound: x = baseline + delta, delta >= 0. The baseline
    # contribution is absorbed into each constraint's residual RHS. This makes
    # committed senior allocations disappear from junior formulas even when a
    # senior has not reached its own upper limit.
    baseline_values: dict[str, float] = {}
    fixed_increment_variables: set[str] = set()
    inequalities: list[_SparseRow] = []
    equations: list[_SparseRow] = []

    def parameter(label: str, value: float) -> str:
        if not isfinite(value):
            raise CannotCompile(f"nonfinite parameter: {label}")
        parameter_values[label] = float(value)
        return label

    # Targets remain in absolute-value coordinates because the caller requests
    # their value. Every finite-lower-bound non-target is shifted to a zero-based
    # increment. Its current lower-bound allocation becomes part of the residual
    # state, not a formula parameter.
    for name, var in engine.vars.items():
        lb, ub = var.lb(), var.ub()
        finite_lb, finite_ub = isfinite(lb), isfinite(ub)

        if name not in protected and finite_lb:
            baseline_values[name] = float(lb)
            remaining_upper = float(ub - lb) if finite_ub else inf
            variable_bounds[name] = (0.0, remaining_upper)
            bound_labels[(name, "lower")] = None
            if remaining_upper == 0.0:
                fixed_increment_variables.add(name)
                bound_labels[(name, "upper")] = None
                continue

            inequalities.append(
                _SparseRow({name: -1.0}, {}, bound_var=name)
            )
            if finite_ub:
                label = parameter(
                    f"variable[{name}].remaining_upper",
                    remaining_upper,
                )
                bound_labels[(name, "upper")] = label
                inequalities.append(
                    _SparseRow({name: 1.0}, {label: 1.0}, bound_var=name)
                )
            else:
                bound_labels[(name, "upper")] = None
            continue

        variable_bounds[name] = (lb, ub)
        if finite_lb and finite_ub and lb == ub:
            label = parameter(f"variable[{name}].value", lb)
            bound_labels[(name, "lower")] = label
            bound_labels[(name, "upper")] = label
            equations.append(
                _SparseRow({name: 1.0}, {label: 1.0}, preserve=True)
            )
            continue
        if finite_lb:
            label = parameter(f"variable[{name}].lower", lb)
            bound_labels[(name, "lower")] = label
            inequalities.append(
                _SparseRow({name: -1.0}, {label: -1.0}, bound_var=name)
            )
        else:
            bound_labels[(name, "lower")] = None
        if finite_ub:
            label = parameter(f"variable[{name}].upper", ub)
            bound_labels[(name, "upper")] = label
            inequalities.append(
                _SparseRow({name: 1.0}, {label: 1.0}, bound_var=name)
            )
        else:
            bound_labels[(name, "upper")] = None

    # Constraint equalities are represented once during the cheap substitution
    # phase instead of as two dense inequalities. Contributions from committed
    # variables are subtracted before the row is built. The resulting parameter
    # is the live residual capacity available to the unsolved variables.
    for name, con in engine.cons.items():
        lb, ub = con.lb(), con.ub()
        if not isfinite(lb) and not isfinite(ub):
            continue
        coefficients = _constraint_coefficients(engine, con, all_names)
        if not all(isfinite(value) for value in coefficients.values()):
            raise CannotCompile("nonfinite matrix coefficient")

        committed_activity = sum(
            coefficients.get(variable, 0.0) * value
            for variable, value in baseline_values.items()
        )
        if fixed_increment_variables:
            coefficients = {
                variable: value
                for variable, value in coefficients.items()
                if variable not in fixed_increment_variables
            }

        finite_lb, finite_ub = isfinite(lb), isfinite(ub)
        residual_lb = lb - committed_activity if finite_lb else -inf
        residual_ub = ub - committed_activity if finite_ub else inf
        if finite_lb and finite_ub and lb == ub:
            label = parameter(
                f"constraint[{name}].remaining",
                residual_ub,
            )
            equations.append(_SparseRow(coefficients, {label: 1.0}))
        else:
            if finite_lb:
                label = parameter(
                    f"constraint[{name}].remaining_lower",
                    residual_lb,
                )
                inequalities.append(
                    _SparseRow(
                        {variable: -value for variable, value in coefficients.items()},
                        {label: -1.0},
                    )
                )
            if finite_ub:
                label = parameter(
                    f"constraint[{name}].remaining_upper",
                    residual_ub,
                )
                inequalities.append(_SparseRow(coefficients.copy(), {label: 1.0}))

    # Eliminate every non-objective variable that has an exact defining
    # equality. Choosing the least-connected pivot limits fill-in.
    equality_eliminated = 0
    while True:
        occurrences = Counter(
            variable
            for row in (*equations, *inequalities)
            for variable in row.coefficients
        )
        choice = None
        for index, equation in enumerate(equations):
            for variable in equation.coefficients:
                if variable in protected:
                    continue
                score = (occurrences[variable], len(equation.coefficients), variable)
                if choice is None or score < choice[0]:
                    choice = (score, index, variable)
        if choice is None:
            break

        _, index, variable = choice
        pivot = equations.pop(index)
        for row in equations:
            _substitute_equation(row, pivot, variable)
        for row in inequalities:
            _substitute_equation(row, pivot, variable)
        equality_eliminated += 1

    # Any equality left now contains objective variables only. Convert it to
    # the pair of inequalities expected by the projector.
    for equation in equations:
        inequalities.append(equation)
        inequalities.append(equation.negative())
    equations.clear()

    # Remove rows that are currently implied by variable bounds, but retain a
    # parameter-only proof condition so reuse is safe when daily bounds change.
    # This is particularly valuable for rows such as ``sum(x) >= 0`` when all
    # x already have non-negative lower bounds: without this presolve row, the
    # redundant lower side looks like an opposing sign and prevents reduction.
    for row in inequalities:
        if (
            row.bound_var is not None
            or row.preserve
            or not row.coefficients
        ):
            continue
        maximum_terms: list[tuple[str, float, float]] = []
        maximum_value = 0.0
        bounded = True
        for variable, coefficient in row.coefficients.items():
            lb, ub = variable_bounds[variable]
            if coefficient > 0:
                if not isfinite(ub):
                    bounded = False
                    break
                label = bound_labels.get((variable, "upper"))
                bound = ub
            else:
                if not isfinite(lb):
                    bounded = False
                    break
                label = bound_labels.get((variable, "lower"))
                bound = lb
            maximum_terms.append((label, coefficient, bound))
            maximum_value += coefficient * bound
        if not bounded:
            continue
        rhs_value = sum(
            coefficient * parameter_values[label]
            for label, coefficient in row.parameters.items()
        )
        redundancy_scale = max(1.0, abs(maximum_value), abs(rhs_value))
        if maximum_value > rhs_value + 1e-9 * redundancy_scale:
            continue
        for label, coefficient, bound in maximum_terms:
            if label is None:
                if bound != 0.0:
                    raise CannotCompile(
                        "nonzero constant redundancy proof is unsupported"
                    )
                continue
            parameter(label, bound)
            updated = row.parameters.get(label, 0.0) - coefficient
            if updated == 0.0:
                row.parameters.pop(label, None)
            else:
                row.parameters[label] = updated
        row.coefficients.clear()

    # Safely collapse remaining zero-cost variables when their non-bound
    # coefficients all have one sign. If all coefficients are positive,
    # lowering the variable can only relax the LP, so its live lower bound is
    # exact for this objective. The mirror case uses the upper bound. Mixed
    # signs are deliberately retained because they can represent counterflow.
    bound_eliminated = 0
    while True:
        eliminated = False
        variables_in_rows = {
            variable for row in inequalities for variable in row.coefficients
        }
        for variable in all_names:
            if variable not in variables_in_rows:
                continue
            is_target = variable in protected
            preference = target_preferences.get(variable) if is_target else None
            relevant = [
                row.coefficients[variable]
                for row in inequalities
                if variable in row.coefficients and row.bound_var != variable
            ]
            lb, ub = variable_bounds[variable]
            label = None
            value = None
            side = None
            if not relevant:
                if preference == "upper" and isfinite(ub):
                    side, value = "upper", ub
                elif preference == "lower" and isfinite(lb):
                    side, value = "lower", lb
                elif not is_target and isfinite(lb):
                    side, value = "lower", lb
                elif not is_target and isfinite(ub):
                    side, value = "upper", ub
            elif all(coefficient > 0 for coefficient in relevant) and isfinite(lb):
                if not is_target or preference == "lower":
                    side, value = "lower", lb
            elif all(coefficient < 0 for coefficient in relevant) and isfinite(ub):
                if not is_target or preference == "upper":
                    side, value = "upper", ub

            if side is not None and value is not None:
                label = bound_labels.get((variable, side))
                if label is None:
                    _substitute_constant_bound(inequalities, variable, value)
                else:
                    parameter(label, value)
                    _substitute_bound(inequalities, variable, label)
                bound_eliminated += 1
                if is_target:
                    if label is None:
                        raise CannotCompile(
                            "target resolved to an unnamed constant bound"
                        )
                    protected.remove(variable)
                    resolved_targets[variable] = label
                eliminated = True
                break

            # A one-sided non-objective variable that is unbounded in the helpful direction
            # can satisfy all rows containing it and therefore projects out
            # without leaving a condition. This is exact Fourier-Motzkin
            # elimination with one empty sign set, and is important for slack
            # and counterflow variables.
            if not is_target and relevant and (
                (all(coefficient > 0 for coefficient in relevant) and not isfinite(lb))
                or (
                    all(coefficient < 0 for coefficient in relevant)
                    and not isfinite(ub)
                )
            ):
                inequalities = [
                    row
                    for row in inequalities
                    if variable not in row.coefficients
                ]
                bound_eliminated += 1
                eliminated = True
                break
        if not eliminated:
            break

    inequalities = [
        row for row in inequalities if row.coefficients or row.parameters
    ]

    active_variables = {
        variable for row in inequalities for variable in row.coefficients
    } | protected
    names = tuple(name for name in all_names if name in active_variables)
    referenced_labels = {
        label for row in inequalities for label in row.parameters
    } | set(resolved_targets.values())
    labels = tuple(
        label for label in parameter_values if label in referenced_labels
    )
    label_indices = {label: index for index, label in enumerate(labels)}

    rows = []
    for row in inequalities:
        coefficients = tuple(row.coefficients.get(name, 0.0) for name in names)
        p = [0.0] * len(labels)
        for label, value in row.parameters.items():
            if label in label_indices:
                p[label_indices[label]] = value
        if not any(coefficients) and not any(p):
            continue
        rows.append((coefficients, tuple(p)))

    labels_with_objective = labels + ("objective_value",)
    parameters = np.asarray(
        [parameter_values[label] for label in labels] + [0.0],
        dtype=float,
    )

    # The key intentionally excludes live RHS values but includes every reduced
    # matrix coefficient and parameter role. Changed measurements reuse a
    # program; changed coefficient/sign structure selects a different one.
    resolved_target_items = tuple(
        (name, resolved_targets[name]) for name in targets if name in resolved_targets
    )
    signature = (names, labels, tuple(rows), resolved_target_items)
    return Snapshot(
        names=names,
        signature=signature,
        rows=tuple(rows),
        parameters=parameters,
        labels=labels_with_objective,
        source_variable_count=(
            getattr(engine, "source_variable_count_override", None)
            or len(all_names)
        ),
        equality_eliminated=equality_eliminated,
        bound_eliminated=bound_eliminated,
        target_bound_eliminated=len(resolved_targets),
        resolved_targets=resolved_target_items,
    )

@dataclass
class ObjectiveProgram:
    name: str
    labels: tuple
    targets: tuple
    costs: tuple
    maximization: bool
    optimum: object
    ranges: tuple
    peak_rows: int
    source_variable_count: int = 0
    active_variable_count: int = 0
    equality_eliminated: int = 0
    bound_eliminated: int = 0
    target_bound_eliminated: int = 0
    return_values: bool = True
    coefficient_count: int = field(init=False)

    def __post_init__(self):
        # Count actual symbolic terms once. Re-walking all Fraction rows every
        # time a new program is cached made total-budget bookkeeping quadratic
        # in the number of compiled programs.
        self.coefficient_count = sum(
            sum(bool(coefficient) for coefficient in row)
            for bounds in (self.optimum, *self.ranges)
            for rows in (bounds.upper, bounds.lower, bounds.conditions)
            for row in rows
        )

    def evaluate(self, parameters):
        params = parameters.copy()
        lo, hi = self.optimum.interval(params)
        value = hi if self.maximization else lo
        if not isfinite(value):
            raise CannotCompile("compiled objective is unbounded")
        if not self.return_values:
            return value, {}
        params[-1] = value
        if len(self.targets) == 1 and self.costs[0] != 0:
            return value, {self.targets[0]: value / self.costs[0]}
        values = {}
        for name, bounds in zip(self.targets, self.ranges, strict=True):
            lo, hi = bounds.interval(params)
            if (
                not isfinite(lo)
                or not isfinite(hi)
                or hi - lo > 1e-9 * max(1.0, abs(lo), abs(hi))
            ):
                # A unique objective sum need not identify its components. The
                # production backend's choice can change later allocations, so
                # restart the day rather than inventing a tie-breaking rule.
                raise CannotCompile(f"nonunique minimum/maximum components: {name}")
            values[name] = (lo + hi) / 2
        return value, values

    def text(self):
        lines = [
            f"PROGRAM {self.name}: "
            + ("MAXIMIZE " if self.maximization else "MINIMIZE ")
            + " + ".join(
                f"({c}) * {s}" for s, c in zip(self.targets, self.costs, strict=True)
            ),
            self.optimum.text(self.labels),
            "objective_value = " + ("upper" if self.maximization else "lower"),
        ]
        if not self.return_values:
            lines.append("requested component values are not required")
            return "\n".join(lines)
        if len(self.targets) == 1 and self.costs[0] != 0:
            lines.append(f"{self.targets[0]} = objective_value / ({self.costs[0]})")
        else:
            for name, bounds in zip(self.targets, self.ranges, strict=True):
                lines += [
                    f"VALUE RANGE FOR {name}",
                    bounds.text(self.labels),
                    f"REQUIRE upper == lower; {name} = lower; otherwise RESTART_DAY_WITH_LP",
                ]
        return "\n".join(lines)
    def code(self):
        """Executable Python for this scalar objective and requested values."""

        def encoded(bounds):
            return repr(
                tuple(
                    tuple(tuple(float(c) for c in row) for row in rows)
                    for rows in (bounds.upper, bounds.lower, bounds.conditions)
                )
            )

        lines = [
            f"def {self.name}(parameters):",
            '    """Input: dict of the named live LP bounds; output: objective, values."""',
            f"    labels = {self.labels!r}",
            '    p = [float(parameters[s]) if s != "objective_value" else 0.0 for s in labels]',
            f"    lo, hi = _compiled_interval(p, *{encoded(self.optimum)})",
            "    value = " + ("hi" if self.maximization else "lo"),
            '    if not isfinite(value): raise CompiledFallback("unbounded objective")',
        ]
        if not self.return_values:
            lines.append("    return value, {}")
            return "\n".join(lines)
        lines.append("    p[-1] = value")
        if len(self.targets) == 1 and self.costs[0] != 0:
            lines.append(
                f"    return value, {{{self.targets[0]!r}: value / {self.costs[0]!r}}}"
            )
        else:
            lines.append("    values = {}")
            for name, bounds in zip(self.targets, self.ranges, strict=True):
                lines += [
                    f"    lo, hi = _compiled_interval(p, *{encoded(bounds)})",
                    "    if not isfinite(lo) or not isfinite(hi) or hi-lo > 1e-9*max(1., abs(lo), abs(hi)):",
                    f"        raise CompiledFallback({('nonunique component: ' + name)!r})",
                    f"    values[{name!r}] = (lo+hi)/2",
                ]
            lines.append("    return value, values")
        return "\n".join(lines)



@dataclass
class ReducedLPProgram:
    """A presolved residual LP retained when symbolic projection is expensive.

    This is deliberately *not* the original day-wide LP. ``snapshot`` has
    already substituted equalities, absorbed committed senior allocations into
    residual RHS parameters, and removed variables that can safely sit at a
    bound for this objective. Only the remaining coupled variables/rows are
    solved numerically at runtime.
    """

    name: str
    labels: tuple
    targets: tuple
    costs: tuple
    maximization: bool
    variable_names: tuple
    matrix: object
    parameter_matrix: object
    resolved_targets: tuple
    trigger_reason: str
    source_variable_count: int = 0
    active_variable_count: int = 0
    equality_eliminated: int = 0
    bound_eliminated: int = 0
    target_bound_eliminated: int = 0
    return_values: bool = True
    coefficient_count: int = field(init=False)

    def __post_init__(self):
        self.coefficient_count = (
            int(self.matrix.nnz)
            + int(self.parameter_matrix.nnz)
            + sum(bool(cost) for cost in self.costs)
        )

    def _objective_vector(self) -> np.ndarray:
        indices = {name: index for index, name in enumerate(self.variable_names)}
        objective = np.zeros(len(self.variable_names), dtype=float)
        for target, cost in zip(self.targets, self.costs, strict=True):
            index = indices.get(target)
            if index is not None:
                objective[index] = float(cost)
        return objective

    def evaluate(self, parameters):
        params = np.asarray(parameters[:-1], dtype=float)
        rhs = np.asarray(self.parameter_matrix @ params, dtype=float).reshape(-1)
        if not np.isfinite(rhs).all():
            raise CannotCompile("nonfinite reduced LP kernel parameter")

        objective = self._objective_vector()
        if not len(self.variable_names):
            scale = np.maximum(1.0, np.abs(rhs))
            if rhs.size and np.any(rhs < -1e-10 * scale):
                raise CannotCompile("reduced LP kernel is infeasible")
            x = np.asarray([], dtype=float)
        else:
            minimized = -objective if self.maximization else objective
            result = linprog(
                c=minimized,
                A_ub=self.matrix if self.matrix.shape[0] else None,
                b_ub=rhs if rhs.size else None,
                bounds=[(None, None)] * len(self.variable_names),
                method="highs-ds",
                options={"presolve": True},
            )
            if not result.success:
                raise CannotCompile(
                    "reduced LP kernel failed: "
                    f"status={result.status}: {result.message}"
                )
            x = np.asarray(result.x, dtype=float)
        objective_value = float(objective @ x)
        label_indices = {label: index for index, label in enumerate(self.labels)}
        for target, label in self.resolved_targets:
            cost = self.costs[self.targets.index(target)]
            objective_value += float(cost) * float(parameters[label_indices[label]])

        if not self.return_values:
            return objective_value, {}

        variable_indices = {
            name: index for index, name in enumerate(self.variable_names)
        }
        resolved = dict(self.resolved_targets)
        values = {}
        for target in self.targets:
            if target in variable_indices:
                values[target] = float(x[variable_indices[target]])
            elif target in resolved:
                values[target] = float(parameters[label_indices[resolved[target]]])
            else:
                raise CannotCompile(
                    f"reduced LP kernel cannot recover target value: {target}"
                )
        return objective_value, values

    def text(self):
        objective = " + ".join(
            f"({cost}) * {target}"
            for target, cost in zip(self.targets, self.costs, strict=True)
        )
        lines = [
            f"REDUCED LP {self.name}: "
            + ("MAXIMIZE " if self.maximization else "MINIMIZE ")
            + objective,
            f"SYMBOLIC PROJECTION STOPPED: {self.trigger_reason}",
            (
                "Solve only this presolved residual kernel at runtime; "
                "continue the compiled day afterward."
            ),
            f"active variables = {len(self.variable_names)}",
            f"reduced rows = {self.matrix.shape[0]}",
        ]
        if len(self.variable_names) <= 40:
            lines.append("variables:")
            lines.extend(f"    {name}" for name in self.variable_names)
        if not self.return_values:
            lines.append("requested component values are not required")
        return "\n".join(lines)

    def code(self):
        """Executable Python for this reduced residual LP kernel."""

        matrix = self.matrix.toarray().tolist()
        parameter_matrix = self.parameter_matrix.toarray().tolist()
        return "\n".join(
            [
                f"def {self.name}(parameters):",
                "    import numpy as np",
                "    from scipy.optimize import linprog",
                f"    labels = {self.labels!r}",
                f"    variable_names = {self.variable_names!r}",
                f"    targets = {self.targets!r}",
                f"    costs = {self.costs!r}",
                f"    A = np.asarray({matrix!r}, dtype=float)",
                f"    P = np.asarray({parameter_matrix!r}, dtype=float)",
                '    p = np.asarray([float(parameters[s]) for s in labels[:-1]], dtype=float)',
                "    b = P @ p",
                "    index = {name: i for i, name in enumerate(variable_names)}",
                "    c = np.zeros(len(variable_names), dtype=float)",
                "    for target, cost in zip(targets, costs, strict=True):",
                "        if target in index: c[index[target]] = float(cost)",
                f"    minimized = {'-c' if self.maximization else 'c'}",
                "    result = linprog(c=minimized, A_ub=A if len(A) else None,",
                "                     b_ub=b if len(b) else None,",
                "                     bounds=[(None, None)] * len(variable_names),",
                '                     method="highs-ds", options={"presolve": True})',
                '    if not result.success: raise CompiledFallback("reduced LP kernel failed")',
                "    x = np.asarray(result.x, dtype=float)",
                "    value = float(c @ x)",
                f"    resolved = {dict(self.resolved_targets)!r}",
                "    label_index = {name: i for i, name in enumerate(labels)}",
                "    for target, label in resolved.items():",
                "        value += float(costs[targets.index(target)]) * float(parameters[label])",
                "    values = {}",
                "    if " + repr(self.return_values) + ":",
                "        for target in targets:",
                "            if target in index: values[target] = float(x[index[target]])",
                "            elif target in resolved: values[target] = float(parameters[resolved[target]])",
                "            else: raise CompiledFallback('missing reduced LP target')",
                "    return value, values",
            ]
        )


def compile_reduced_lp(
    state,
    targets,
    costs,
    maximization,
    options,
    name,
    trigger_reason,
    return_values=True,
):
    """Retain an objective-specific presolved LP as a runtime numeric kernel."""

    if len(state.names) > options.max_kernel_variables:
        raise CannotCompile(
            f"reduced LP kernel has {len(state.names)} variables "
            f"(budget {options.max_kernel_variables})"
        )
    if len(state.rows) > options.max_kernel_rows:
        raise CannotCompile(
            f"reduced LP kernel has {len(state.rows)} rows "
            f"(budget {options.max_kernel_rows})"
        )

    if state.rows:
        matrix = csr_matrix(
            np.asarray([coefficients for coefficients, _ in state.rows], dtype=float)
        )
        parameter_matrix = csr_matrix(
            np.asarray(
                [parameter_coefficients for _, parameter_coefficients in state.rows],
                dtype=float,
            )
        )
    else:
        matrix = csr_matrix((0, len(state.names)), dtype=float)
        parameter_matrix = csr_matrix((0, len(state.labels) - 1), dtype=float)

    return ReducedLPProgram(
        name=name,
        labels=state.labels,
        targets=targets,
        costs=costs,
        maximization=maximization,
        variable_names=state.names,
        matrix=matrix,
        parameter_matrix=parameter_matrix,
        resolved_targets=state.resolved_targets,
        trigger_reason=trigger_reason,
        source_variable_count=state.source_variable_count,
        active_variable_count=len(state.names),
        equality_eliminated=state.equality_eliminated,
        bound_eliminated=state.bound_eliminated,
        target_bound_eliminated=state.target_bound_eliminated,
        return_values=return_values,
    )


def compile_objective(
    state,
    targets,
    costs,
    maximization,
    options,
    name,
    seconds,
    return_values=True,
):
    if len(state.names) > options.max_variables:
        raise CannotCompile(
            f"reduced objective has {len(state.names)} variables "
            f"(budget {options.max_variables}; "
            f"source model {state.source_variable_count})"
        )
    if len(state.rows) > options.max_rows:
        raise CannotCompile(
            f"reduced objective has {len(state.rows)} rows "
            f"(budget {options.max_rows})"
        )

    projector = Projector(options, perf_counter() + seconds)
    n, m = len(state.names), len(state.labels)
    zero = rational(0)
    rows = []
    for coefficients, parameter_coefficients in state.rows:
        p = tuple(rational(value) for value in parameter_coefficients) + (zero,)
        rows.append(Row(tuple(rational(c) for c in coefficients) + (zero,), p))
    c = dict(zip(targets, costs, strict=True))
    coefficients = tuple(rational(c.get(s, 0.0)) for s in state.names)
    resolved_targets = dict(state.resolved_targets)
    label_indices = {label: index for index, label in enumerate(state.labels)}
    objective_rhs = [zero] * m
    for target, label in resolved_targets.items():
        objective_rhs[label_indices[label]] -= rational(c[target])
    equation = Row(
        coefficients + (-rational(1),),
        tuple(objective_rhs),
    )

    def scaled_expression(expression, multiplier, add=()):
        values = tuple(value * multiplier for value in expression)
        if add:
            values = tuple(
                value + extra for value, extra in zip(values, add, strict=True)
            )
        return values

    # Most sequential/objective-classification solves presolve to zero or one
    # active variable. Projecting an artificial objective variable through that
    # scalar system is unnecessary: isolate it directly from each row. This
    # avoids hundreds of nearly identical Fourier-Motzkin runs for large
    # equal-priority groups while retaining exact Fraction formulas.
    direct_scalar = n == 0 or (n == 1 and coefficients[0] != 0)
    ranges = []
    if direct_scalar:
        fixed_expression = tuple(-value for value in objective_rhs)
        if n == 0:
            optimum = Bounds(
                upper=(fixed_expression,),
                lower=(fixed_expression,),
                conditions=(),
            )
        else:
            cost = coefficients[0]
            upper = {}
            lower = {}
            conditions = {}
            for row in rows:
                a = row.a[0]
                if not a:
                    conditions[row.p] = None
                    continue
                # objective = cost * x + fixed_expression. Substituting x into
                # a*x <= p gives an objective bound with multiplier cost/a.
                ratio = cost * Fraction(a.denominator, a.numerator)
                expression = scaled_expression(
                    row.p, ratio, fixed_expression
                )
                if (a > 0) == (cost > 0):
                    upper[expression] = None
                else:
                    lower[expression] = None
            optimum = Bounds(
                upper=tuple(upper),
                lower=tuple(lower),
                conditions=tuple(conditions),
            )

        if return_values:
            objective_index = state.labels.index("objective_value")
            for target in targets:
                if target in resolved_targets:
                    index = label_indices[resolved_targets[target]]
                    parameter_value = tuple(
                        rational(1) if i == index else zero
                        for i in range(m)
                    )
                    ranges.append(
                        Bounds(
                            upper=(parameter_value,),
                            lower=(parameter_value,),
                            conditions=(),
                        )
                    )
                elif n == 1 and target == state.names[0]:
                    cost = coefficients[0]
                    inverse = Fraction(cost.denominator, cost.numerator)
                    value_expression = [
                        -value * inverse for value in fixed_expression
                    ]
                    value_expression[objective_index] += inverse
                    value_expression = tuple(value_expression)
                    ranges.append(
                        Bounds(
                            upper=(value_expression,),
                            lower=(value_expression,),
                            conditions=(),
                        )
                    )
                else:
                    direct_scalar = False
                    ranges.clear()
                    break

    if not direct_scalar:
        optimum = projector.bounds(rows + [equation, equation.negative()], n)
        ranges = []

    def check_size():
        count = sum(
            sum(bool(coefficient) for coefficient in row)
            for bounds in (optimum, *ranges)
            for rs in (bounds.upper, bounds.lower, bounds.conditions)
            for row in rs
        )
        if count > options.max_program_coefficients:
            raise CannotCompile("objective program coefficient budget reached")

    check_size()
    if (
        not direct_scalar
        and return_values
        and (len(targets) != 1 or costs[0] == 0)
    ):
        # Restrict the original system to the optimal objective value, then
        # project each requested component. This detects ambiguous endpoint
        # caps, spill components, and final residual values.
        face_rhs = list(objective_rhs)
        face_rhs[-1] += rational(1)
        face = Row(coefficients + (zero,), tuple(face_rhs))
        for target in targets:
            if target in resolved_targets:
                index = label_indices[resolved_targets[target]]
                parameter_value = tuple(
                    rational(1) if i == index else zero for i in range(m)
                )
                ranges.append(
                    Bounds(
                        upper=(parameter_value,),
                        lower=(parameter_value,),
                        conditions=(),
                    )
                )
            else:
                ranges.append(
                    projector.bounds(
                        rows + [face, face.negative()], state.names.index(target)
                    )
                )
            check_size()
    return ObjectiveProgram(
        name,
        state.labels,
        targets,
        costs,
        maximization,
        optimum,
        tuple(ranges),
        projector.peak_rows,
        state.source_variable_count,
        len(state.names),
        state.equality_eliminated,
        state.bound_eliminated,
        state.target_bound_eliminated,
        return_values,
    )


def _direct_bound_objective(engine, names, maximization, weights):
    """Return an exact bound optimum when the preferred-bound point is feasible.

    This is especially useful for finalization after the priority loop has
    committed transaction lower bounds. If every objective variable can sit at
    its individually optimal bound simultaneously, no projection is needed.
    Non-objective variables must already be fixed so the feasibility check is
    complete rather than existential.
    """

    requested = tuple(dict.fromkeys(names))
    requested_set = set(requested)
    objective_weights = weights or {}
    candidate: dict[str, float] = {}

    for name, variable in engine.vars.items():
        lower, upper = variable.lb(), variable.ub()
        if name not in requested_set:
            if not (isfinite(lower) and lower == upper):
                return None
            candidate[name] = lower
            continue

        cost = objective_weights.get(name, 1.0)
        if cost == 0:
            return None
        prefer_upper = (cost > 0) == maximization
        value = upper if prefer_upper else lower
        if not isfinite(value):
            return None
        candidate[name] = value

    for constraint in engine.cons.values():
        if hasattr(constraint, "coefficients"):
            activity = sum(
                coefficient * candidate[name]
                for name, coefficient in constraint.coefficients.items()
            )
        else:
            activity = sum(
                constraint.GetCoefficient(variable) * candidate[name]
                for name, variable in engine.vars.items()
            )
        lower, upper = constraint.lb(), constraint.ub()
        scale = max(
            1.0,
            abs(activity),
            abs(lower) if isfinite(lower) else 0.0,
            abs(upper) if isfinite(upper) else 0.0,
        )
        tolerance = 1e-9 * scale
        if isfinite(lower) and activity < lower - tolerance:
            return None
        if isfinite(upper) and activity > upper + tolerance:
            return None

    values = {name: candidate[name] for name in requested}
    objective = sum(
        objective_weights.get(name, 1.0) * value
        for name, value in values.items()
    )
    return objective, values


class CompilationSession:
    """Bounded program cache owned by one reusable SolverInput plan."""

    def __init__(self, options=None):
        self.options = options or CompilationOptions()
        self.programs = {}
        self.failures = {}
        self.compile_seconds = 0.0
        self.cached_coefficients = 0
        self.date = None
        self.stats = Counter()
        self.events = []
        self.last_events = []
        self.preparation_events = []
        self.warmup_reasons = Counter()
        self.equal_priority_routines: list[EqualPriorityRoutine] = []
        self.prepared_calls: dict[str, tuple[PreparedProgramCall, ...]] = {}
        self._current_prepared_calls: list[PreparedProgramCall] = []
        self._runtime_calls: tuple[PreparedProgramCall, ...] | None = None
        self._runtime_call_index = 0
        self.frozen = False

    def program(
        self,
        engine,
        names,
        maximization=True,
        weights=None,
        return_values=True,
    ):
        targets = tuple(dict.fromkeys(names))
        if not targets or any(s not in engine.vars for s in targets):
            raise CannotCompile("empty or unknown objective targets")
        costs = tuple((weights or {}).get(s, 1.0) for s in targets)

        # Fast frozen execution follows the exact objective-call sequence that
        # was traced for this date. The expensive structural snapshot has
        # already been proved; only current RHS/bound parameters are rebuilt.
        if self.frozen and self._runtime_calls is not None:
            if self._runtime_call_index < len(self._runtime_calls):
                prepared = self._runtime_calls[self._runtime_call_index]
                if (
                    prepared.targets == targets
                    and prepared.costs == costs
                    and prepared.maximization == maximization
                    and prepared.return_values == return_values
                ):
                    try:
                        parameters = _prepared_parameters(
                            engine,
                            prepared.program.labels,
                            targets,
                        )
                    except CannotCompile:
                        self._runtime_calls = None
                        self.stats["prepared_call_parameter_misses"] += 1
                    else:
                        self._runtime_call_index += 1
                        self.stats["prepared_call_hits"] += 1
                        return prepared.program, parameters
                else:
                    # Different measurements can change an active-set branch.
                    # Stop trusting the tape for this date and use the slower
                    # structural cache lookup, which may still find a prepared
                    # program or safely trigger the whole-day fallback.
                    self._runtime_calls = None
                    self.stats["prepared_call_sequence_misses"] += 1
            else:
                self._runtime_calls = None
                self.stats["prepared_call_sequence_misses"] += 1

        preferences = {}
        for target, cost in zip(targets, costs, strict=True):
            if cost == 0:
                continue
            prefer_upper = (cost > 0) == maximization
            preferences[target] = "upper" if prefer_upper else "lower"
        state = snapshot(
            engine,
            self.options,
            targets,
            target_preferences=preferences,
        )
        key = (state.signature, targets, costs, maximization, return_values)
        if key in self.failures:
            raise CannotCompile(self.failures[key])
        if key not in self.programs:
            if self.frozen:
                raise CannotCompile(
                    "objective pattern was not compiled during plan preparation"
                )
            if len(self.programs) + len(self.failures) >= self.options.max_plans:
                raise CannotCompile("program cache budget reached")
            remaining = self.options.max_total_compile_seconds - self.compile_seconds
            start = perf_counter()
            try:
                sequence = len(self.programs) + 1
                symbolic_error = None
                try:
                    if remaining <= 0:
                        raise CannotCompile("total compilation time budget reached")
                    symbolic_options = replace(
                        self.options,
                        max_variables=min(
                            self.options.max_variables,
                            self.options.max_symbolic_variables_before_kernel,
                        ),
                        max_rows=min(
                            self.options.max_rows,
                            self.options.max_symbolic_rows_before_kernel,
                        ),
                        max_pairs=min(
                            self.options.max_pairs,
                            self.options.max_symbolic_pairs_before_kernel,
                        ),
                    )
                    program = compile_objective(
                        state,
                        targets,
                        costs,
                        maximization,
                        symbolic_options,
                        f"P{sequence}",
                        min(
                            remaining,
                            self.options.max_seconds_per_plan,
                            self.options.max_symbolic_seconds_before_kernel,
                        ),
                        return_values=return_values,
                    )
                except CannotCompile as error:
                    symbolic_error = str(error)
                    program = compile_reduced_lp(
                        state,
                        targets,
                        costs,
                        maximization,
                        self.options,
                        f"R{sequence}",
                        symbolic_error,
                        return_values=return_values,
                    )
                if (
                    self.cached_coefficients + program.coefficient_count
                    > self.options.max_total_coefficients
                ):
                    raise CannotCompile("total cached coefficient budget reached")
                self.programs[key] = program
                self.cached_coefficients += program.coefficient_count
                if symbolic_error is not None:
                    self.stats["reduced_lp_programs_created"] += 1
            except CannotCompile as error:
                self.failures[key] = str(error)
                raise
            finally:
                self.compile_seconds += perf_counter() - start
        else:
            self.stats["cache_hits"] += 1
        program = self.programs[key]
        if not self.frozen:
            self._current_prepared_calls.append(
                PreparedProgramCall(
                    targets=targets,
                    costs=costs,
                    maximization=maximization,
                    return_values=return_values,
                    program=program,
                )
            )
        return program, state.parameters

    def _record_program_evaluation(self, program) -> None:
        if isinstance(program, ReducedLPProgram):
            self.stats["reduced_lp_evaluations"] += 1
        else:
            self.stats["formula_evaluations"] += 1
        self.events.append(program.name)

    def evaluate_value(self, engine, names, maximization=True, weights=None):
        direct = _direct_bound_objective(engine, names, maximization, weights)
        if direct is not None:
            self.stats["bound_objective_resolutions"] += 1
            return direct[0]

        requested = tuple(dict.fromkeys(names))
        objective_weights = weights or {}
        fixed_objective = 0.0
        live_names: list[str] = []
        for name in requested:
            if name not in engine.vars:
                raise CannotCompile("empty or unknown objective targets")
            lower, upper = engine.get_variable_bounds(name)
            if isfinite(lower) and lower == upper:
                fixed_objective += objective_weights.get(name, 1.0) * lower
            else:
                live_names.append(name)

        if not live_names:
            return fixed_objective

        live_weights = {
            name: objective_weights.get(name, 1.0) for name in live_names
        }
        program, params = self.program(
            engine,
            live_names,
            maximization,
            live_weights,
            return_values=False,
        )
        value, _ = program.evaluate(params)
        self._record_program_evaluation(program)
        self.stats["objective_value_only_evaluations"] += 1
        return value + fixed_objective

    def _record_equal_priority_routine(
        self,
        engine,
        names,
        proportion_factors,
    ) -> None:
        """Record one reusable equal-priority loop for formulas().

        Warm-up may call evaluate_common_increment repeatedly while members drop
        out.  A later active set that is a subset of an already-recorded group
        is therefore the same loop, not another displayed iteration.
        """

        members = tuple(dict.fromkeys(names))
        member_set = set(members)
        if len(members) <= 1:
            return

        # The first call for a group should normally contain its full member
        # set.  If a larger superset is encountered later, replace any smaller
        # warm-up subset so the display still describes the full routine.
        for routine in self.equal_priority_routines:
            if member_set.issubset(set(routine.members)):
                return

        self.equal_priority_routines = [
            routine
            for routine in self.equal_priority_routines
            if not set(routine.members).issubset(member_set)
        ]
        factors = tuple(
            (name, float(proportion_factors[name]))
            for name in members
        )
        self.equal_priority_routines.append(
            EqualPriorityRoutine(
                members=members,
                initial_factors=factors,
                residual_updates=_equal_priority_residual_updates(
                    engine, members
                ),
            )
        )

    def evaluate_common_increment(self, engine, names, proportion_factors):
        self._record_equal_priority_routine(
            engine,
            names,
            proportion_factors,
        )
        virtual, initial_values, factors = _proportional_virtual_engine(
            engine,
            names,
            proportion_factors,
        )
        increment = self.evaluate_value(
            virtual,
            [_COMMON_INCREMENT],
            maximization=True,
        )
        self.stats["proportional_common_increment_evaluations"] += 1
        return {
            name: initial_values[name] + factors[name] * increment
            for name in names
        }

    def evaluate(self, engine, names, maximization=True, weights=None):
        direct = _direct_bound_objective(engine, names, maximization, weights)
        if direct is not None:
            self.stats["bound_objective_resolutions"] += 1
            return direct

        # Priority solves commonly leave earlier transactions fixed exactly.
        # They need to be returned to the caller, but carrying them as objective
        # variables through projection adds no information. Peel them off as a
        # symbolic constant contribution and compile only the still-live targets.
        requested = tuple(dict.fromkeys(names))
        objective_weights = weights or {}
        fixed_values: dict[str, float] = {}
        live_names: list[str] = []
        for name in requested:
            if name not in engine.vars:
                raise CannotCompile("empty or unknown objective targets")
            lower, upper = engine.get_variable_bounds(name)
            if isfinite(lower) and lower == upper:
                fixed_values[name] = lower
            else:
                live_names.append(name)

        fixed_objective = sum(
            objective_weights.get(name, 1.0) * value
            for name, value in fixed_values.items()
        )
        if not live_names:
            self.stats["fixed_target_eliminations"] += len(fixed_values)
            return fixed_objective, dict(fixed_values)

        live_weights = {
            name: objective_weights.get(name, 1.0) for name in live_names
        }
        program, params = self.program(
            engine, live_names, maximization, live_weights
        )
        value, live_values = program.evaluate(params)
        self._record_program_evaluation(program)
        self.stats["fixed_target_eliminations"] += len(fixed_values)
        values = {
            name: fixed_values[name] if name in fixed_values else live_values[name]
            for name in requested
        }
        return value + fixed_objective, values

    def freeze(self):
        """Prevent runtime execution from compiling previously unseen programs."""
        self.frozen = True

    def finish_preparation(self):
        """Keep compile coverage but reset execution statistics for plan.solve()."""
        self.preparation_events = list(self.last_events)
        self.stats.clear()
        self.events.clear()
        self.last_events.clear()
        self.date = None
        self.freeze()

    def begin_day(self, date):
        self.date, self.events = date, []
        if self.frozen:
            self._runtime_calls = self.prepared_calls.get(date)
            self._runtime_call_index = 0
        else:
            self._current_prepared_calls = []

    def finish_day(self, reason=None):
        used_kernel = any(event.startswith("R") for event in self.events)
        if reason:
            method = "lp"
            self.stats["lp_days"] += 1
        elif used_kernel:
            method = "compiled+reduced_lp"
            self.stats["hybrid_days"] += 1
        else:
            method = "compiled"
            self.stats["compiled_days"] += 1
        self.last_events.append(
            {
                "date": self.date,
                "method": method,
                "formula_calls": list(self.events),
                "fallback_reason": reason,
            }
        )
        if not self.frozen:
            if reason is None:
                self.prepared_calls[self.date] = tuple(self._current_prepared_calls)
            else:
                self.prepared_calls.pop(self.date, None)
        if reason:
            self.stats["discarded_formula_evaluations"] += len(self.events)

    def report(self):
        reduced_programs = [
            program
            for program in self.programs.values()
            if isinstance(program, ReducedLPProgram)
        ]
        counters = {
            "compiled_days": 0,
            "hybrid_days": 0,
            "lp_days": 0,
            "formula_evaluations": 0,
            "reduced_lp_evaluations": 0,
            **dict(self.stats),
        }
        return {
            **counters,
            "program_count": len(self.programs),
            "symbolic_program_count": len(self.programs) - len(reduced_programs),
            "reduced_lp_program_count": len(reduced_programs),
            "rejected_program_count": len(self.failures),
            "compile_seconds": self.compile_seconds,
            "cached_coefficients": self.cached_coefficients,
            "program_reductions": {
                p.name: {
                    "kind": (
                        "reduced_lp"
                        if isinstance(p, ReducedLPProgram)
                        else "symbolic"
                    ),
                    "source_variables": p.source_variable_count,
                    "active_variables": p.active_variable_count,
                    "equality_eliminated": p.equality_eliminated,
                    "bound_eliminated": p.bound_eliminated,
                    "target_bound_eliminated": p.target_bound_eliminated,
                    **(
                        {"symbolic_fallback_reason": p.trigger_reason}
                        if isinstance(p, ReducedLPProgram)
                        else {}
                    ),
                }
                for p in self.programs.values()
            },
            "days": list(self.last_events),
            "preparation_days": list(self.preparation_events),
            "preparation_fallbacks": dict(self.warmup_reasons),
            "frozen": self.frozen,
        }

    def formulas(self):
        lines = [
            "COMPILED CALCULATION ROUTINE",
            "============================",
            "",
            "Residual-state rule:",
            "    Once a transaction increment is committed, its contribution",
            "    is absorbed into constraint[...].remaining. Later formulas",
            "    therefore do not carry solved senior transactions as variables.",
            "",
            "Priority processing:",
            "    Process priority groups from senior to junior.",
            "    A single active transaction is assigned by its cached MAXIMUM",
            "    program, then all affected residual constraints are updated.",
            "    If symbolic projection is too expensive, a cached REDUCED LP",
            "    solves only that presolved residual objective; the compiled",
            "    day continues from the resulting residual state.",
            "    Equal-priority groups use the reusable loop(s) below.",
            "",
        ]

        if self.equal_priority_routines:
            lines.extend(
                [
                    "EQUAL-PRIORITY ROUTINES",
                    "=======================",
                    "",
                ]
            )
            for index, routine in enumerate(
                self.equal_priority_routines, start=1
            ):
                if len(self.equal_priority_routines) > 1:
                    lines.append(f"ROUTINE {index}")
                lines.append(routine.text())
                lines.append("")

        if self.preparation_events:
            lines.extend(
                [
                    "PREPARED PROGRAM COVERAGE",
                    "=========================",
                    "These are the cached symbolic programs encountered while",
                    "tracing the supplied date range. They are implementation",
                    "details used by the calculation routine above; the number",
                    "of calls during preparation is not a runtime iteration count.",
                    "",
                ]
            )
            for event in self.preparation_events:
                calls = " -> ".join(event["formula_calls"]) or "(no formulas)"
                suffix = (
                    f"  [LP fallback: {event['fallback_reason']}]"
                    if event.get("fallback_reason")
                    else ""
                )
                lines.append(f"{event['date']}: {calls}{suffix}")
            lines.append("")

        lines.extend(
            [
                "PROGRAM DEFINITIONS",
                "===================",
                "",
            ]
        )
        for program in self.programs.values():
            text = program.text().replace(
                _COMMON_INCREMENT,
                "common_increment",
            )
            lines.append(text)
            lines.append("")

        reasons = list(
            dict.fromkeys(
                [*self.failures.values(), *self.warmup_reasons]
            )
        )
        if reasons:
            lines.extend(
                [
                    "FALLBACK CONDITIONS",
                    "===================",
                    *(f"RESTART_DAY_WITH_LP: {reason}" for reason in reasons),
                ]
            )

        return "\n".join(lines).rstrip() + "\n"

    def execution_outline(self):
        # This is a faithful execution outline, not a portable standalone export
        # of graph preprocessing or state objects. formulas() gives actual bounds.
        return """# compile_solver_input() has already traced this schedule and frozen the formula cache.
# plan.solve() performs numeric execution only; it never compiles a new objective.
for day in dates:
    initialize_daily_measurements_accounts_and_natural_flow()
    initialize_residual_constraint_state()
    try:
        for allocation_pass in (1, 2):
            for priority_group in production_schedule:
                while active_members_remain(priority_group):
                    apply_counterflow_caps_using_compiled_objectives()
                    increment = evaluate_formula_or_reduced_lp_kernel()
                    commit_allocations_and_update_residual_constraints(increment)
                    update_remaining_natural_flow(increment)
                    remove_blocked_members_using_compiled_objectives()
                    release_temporary_caps()
            if allocation_pass == 1:
                minimize_and_lock_spills_then_credit_natural_flow()
        finalize_using_formula_or_reduced_lp_kernel()
    except CannotCompile:
        discard_tentative_day_and_run_original_lp_day()
    commit_final_account_and_cumulative_balances_once()
"""

    def code(self):
        helpers = """# Generated scalar objective programs. Run the full accounting workflow with plan.solve().
# Each function accepts named LP bounds. CompiledFallback means restart the day with LP.
from math import inf, isfinite

class CompiledFallback(RuntimeError):
    pass

def _compiled_interval(p, upper, lower, conditions):
    from sys import float_info

    def dot_and_tolerance(row):
        products = tuple(c*x for c, x in zip(row, p, strict=True))
        value = sum(products)
        scale = sum(abs(x) for x in products)
        if not isfinite(value) or not isfinite(scale):
            raise CompiledFallback("nonfinite formula")
        tolerance = (
            64.0
            * float_info.epsilon
            * max(1, len(row))
            * max(1.0, scale)
        )
        return value, tolerance

    upper_values = tuple(dot_and_tolerance(row) for row in upper)
    lower_values = tuple(dot_and_tolerance(row) for row in lower)
    condition_values = tuple(dot_and_tolerance(row) for row in conditions)

    if upper_values:
        hi, hi_tolerance = min(upper_values, key=lambda item: item[0])
    else:
        hi, hi_tolerance = inf, 0.0
    if lower_values:
        lo, lo_tolerance = max(lower_values, key=lambda item: item[0])
    else:
        lo, lo_tolerance = -inf, 0.0

    if hi < lo:
        if lo-hi > hi_tolerance+lo_tolerance:
            raise CompiledFallback("domain/interval check failed")
        lo = hi = (lo+hi)/2.0

    if any(value < -tolerance for value, tolerance in condition_values):
        raise CompiledFallback("domain/interval check failed")
    return lo, hi
"""
        return (
            helpers
            + "\n\n"
            + "\n\n".join(p.code() for p in self.programs.values())
            + "\n"
        )


def compiled_factory(base, session):
    class CompiledEngine(base):
        # In compiled execution, keep explicit priority solves even when the
        # natural-flow helper already knows a source is exhausted. The residual
        # NF constraint then evaluates the transaction formula to its committed
        # value (normally zero headroom), which keeps the generated routine
        # complete and inspectable without changing the allocation.
        force_explicit_priority_solves = True

        def solve_objective(self, variable_names, maximization=True, weights=None):
            return session.evaluate(self, variable_names, maximization, weights)

        def solve_objective_value(
            self,
            variable_names,
            maximization=True,
            weights=None,
        ):
            return session.evaluate_value(
                self,
                variable_names,
                maximization,
                weights,
            )

        def maximize_group_by_proportions(
            self,
            variable_names,
            proportion_factors,
        ):
            return session.evaluate_common_increment(
                self,
                variable_names,
                proportion_factors,
            )

        def get_last_variable_reduced_cost(self, variable_name):
            return None

        def get_last_solve_constraint_evidence(self, variable_name, tolerance=1e-6):
            # No fictitious dual evidence from a previous native solve. Requests
            # for the detailed production audit use the ordinary LP day instead.
            return []

    return CompiledEngine
