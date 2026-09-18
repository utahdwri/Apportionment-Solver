"""Numerical execution only. Formula compilation is deliberately deferred."""
from dataclasses import dataclass
from math import isfinite

import numpy as np
from scipy.optimize import linprog

from .lp import BlockLP, Maximize, Proportional, Slot

TOL = 1e-7


class BlockLPError(RuntimeError):
    pass


def value(scalar, state):
    return float(state[scalar.index]) if isinstance(scalar, Slot) else float(scalar)


def _commit_updates(model: BlockLP, state, increments):
    """Apply target increments using one snapshot of runtime coefficients."""
    changes = {}
    for name, increment in increments.items():
        if increment < -TOL or not isfinite(increment):
            raise BlockLPError(f"Invalid increment for {name}: {increment}")
        increment = max(0.0, float(increment))
        for slot, coefficient in model.updates.get(name, {}).items():
            changes[slot.index] = (
                changes.get(slot.index, 0.0)
                + value(coefficient, state) * increment
            )
    for index, change in changes.items():
        state[index] += change


@dataclass
class DirectCalculationKernel:
    """Exact analytical executor for a one-variable maximization block.

    A one-variable LP is just an interval.  At runtime we intersect the
    variable's own bounds with every constraint side, then choose the interval
    endpoint favored by the objective.  Coefficients and bounds may still be
    runtime ``Slot`` values, so losses, measurements, NF, account balances,
    and daily limits do not have to be frozen at compile time.
    """

    model: BlockLP

    def __post_init__(self):
        if len(self.model.variables) != 1:
            raise ValueError("Direct calculation requires exactly one variable")
        if not isinstance(self.model.rule, Maximize):
            raise ValueError("Direct calculation requires a Maximize rule")

        self.name = next(iter(self.model.variables))
        if set(self.model.rule.coefficients) != {self.name}:
            raise ValueError("Direct calculation objective must contain only its variable")
        for constraint in self.model.constraints:
            if set(constraint.coefficients) - {self.name}:
                raise ValueError(
                    f"Direct calculation received a coupled row: {constraint.name}"
                )
        if set(self.model.updates) - {self.name}:
            raise ValueError("Only the direct target may commit state updates")

    def _interval(self, state):
        variable = self.model.variables[self.name]
        lower = value(variable.lower, state)
        upper = float("inf") if variable.upper is None else value(variable.upper, state)

        # Match LPKernel's treatment of a tiny negative residual capacity.
        if upper != float("inf") and -TOL <= upper < 0 and lower == 0:
            upper = 0.0
        if not isfinite(lower) or np.isnan(upper):
            raise BlockLPError("Non-finite variable bound")

        for constraint in self.model.constraints:
            coefficient = value(constraint.coefficients.get(self.name, 0.0), state)
            lo = None if constraint.lower is None else value(constraint.lower, state)
            hi = None if constraint.upper is None else value(constraint.upper, state)

            if not isfinite(coefficient):
                raise BlockLPError(f"Non-finite coefficient: {constraint.name}")
            if any(bound is not None and not isfinite(bound) for bound in (lo, hi)):
                raise BlockLPError(f"Non-finite constraint bound: {constraint.name}")

            if abs(coefficient) <= 1e-15:
                # The row is 0.  It is feasible only if zero lies inside the
                # requested interval.  A small tolerance matches the numerical
                # nature of the LP fallback without weakening real constraints.
                if lo is not None and lo > TOL:
                    raise BlockLPError(
                        f"Block {[self.name]} failed: infeasible {constraint.name}"
                    )
                if hi is not None and hi < -TOL:
                    raise BlockLPError(
                        f"Block {[self.name]} failed: infeasible {constraint.name}"
                    )
                continue

            if coefficient > 0:
                if lo is not None:
                    lower = max(lower, lo / coefficient)
                if hi is not None:
                    upper = min(upper, hi / coefficient)
            else:
                if lo is not None:
                    upper = min(upper, lo / coefficient)
                if hi is not None:
                    lower = max(lower, hi / coefficient)

        scale = max(
            1.0,
            abs(lower) if isfinite(lower) else 1.0,
            abs(upper) if isfinite(upper) else 1.0,
        )
        if upper < lower - TOL * scale:
            raise BlockLPError(
                f"Block {[self.name]} failed: direct interval is infeasible "
                f"({lower} > {upper})"
            )
        if upper < lower:
            # Collapse a numerical hairline inversion rather than producing a
            # negative or order-dependent increment.
            lower = upper = 0.5 * (lower + upper)
        return lower, upper

    def execute(self, state):
        lower, upper = self._interval(state)
        objective = value(self.model.rule.coefficients[self.name], state)
        if not isfinite(objective):
            raise BlockLPError("Non-finite direct objective coefficient")
        if abs(objective) <= 1e-15:
            raise BlockLPError("Direct objective coefficient became zero")

        increment = upper if objective > 0 else lower
        if not isfinite(increment):
            raise BlockLPError(f"Block {[self.name]} failed: unbounded direct objective")
        _commit_updates(self.model, state, {self.name: increment})

        # Direct calculations perform no numerical LP solve.
        return 0


@dataclass
class ProportionalCalculationKernel:
    """Analytical water-filling executor for a monotone proportional block.

    This kernel handles the common equal-priority case where the LP contains
    only the target transactions and every coupling row is an upper-capacity
    constraint with nonnegative coefficients.  In that form, maximizing the
    common proportional increment is just a scalar ``min(...)`` calculation.

    Runtime ``Slot`` coefficients are allowed.  If one becomes materially
    negative, the monotonicity proof no longer applies and execution falls back
    to the generic LP kernel for that day rather than changing solver semantics.
    """

    model: BlockLP

    def __post_init__(self):
        if not isinstance(self.model.rule, Proportional):
            raise ValueError("Proportional calculation requires a Proportional rule")

        self.targets = tuple(self.model.rule.reference_cfs)
        if set(self.model.variables) != set(self.targets):
            raise ValueError(
                "Analytical proportional calculation cannot contain witness variables"
            )
        if set(self.model.updates) - set(self.targets):
            raise ValueError("Only proportional targets may commit state updates")

        for name in self.targets:
            variable = self.model.variables[name]
            if isinstance(variable.lower, Slot) or float(variable.lower) != 0.0:
                raise ValueError(
                    "Analytical proportional calculation requires zero variable lower bounds"
                )

        for constraint in self.model.constraints:
            if constraint.lower is not None:
                raise ValueError(
                    "Analytical proportional calculation requires upper-only constraints"
                )
            if set(constraint.coefficients) - set(self.targets):
                raise ValueError(
                    f"Unknown variable in proportional constraint {constraint.name}"
                )

        self.fallback = LPKernel(self.model)

    def _variable_upper(self, name, state):
        variable = self.model.variables[name]
        if variable.upper is None:
            return float("inf")
        upper = value(variable.upper, state)
        if np.isnan(upper):
            raise BlockLPError("Non-finite variable bound")
        if upper < -TOL:
            raise BlockLPError(
                f"Block {list(self.model.updates)} failed: negative upper bound for {name}: {upper}"
            )
        return max(0.0, upper)

    def _bound_rows(self, state):
        """Bind monotone upper rows, returning ``None`` if a coefficient is negative.

        A negative coefficient means another target can offset resource use, so
        the scalar water-filling proof no longer holds.  That case is uncommon
        in ordinary diversion/NF/account rows and is handled by the LP fallback.
        """
        rows = []
        for constraint in self.model.constraints:
            if constraint.upper is None:
                continue
            upper = value(constraint.upper, state)
            if not isfinite(upper):
                raise BlockLPError(
                    f"Non-finite constraint bound: {constraint.name}"
                )
            if upper < -TOL:
                raise BlockLPError(
                    f"Block {list(self.model.updates)} failed: "
                    f"negative remaining capacity in {constraint.name}: {upper}"
                )
            upper = max(0.0, upper)

            coefficients = {}
            for name, scalar in constraint.coefficients.items():
                coefficient = value(scalar, state)
                if not isfinite(coefficient):
                    raise BlockLPError(
                        f"Non-finite coefficient: {constraint.name}"
                    )
                if coefficient < -TOL:
                    return None
                coefficients[name] = max(0.0, coefficient)
            rows.append((constraint.name, coefficients, upper))
        return rows

    def _common_increment(self, state, factors, rows):
        """Largest common scalar ``t`` such that ``x_i = factor_i * t`` fits."""
        upper = float("inf")

        for name, factor in factors.items():
            if factor <= 0 or not isfinite(factor):
                raise BlockLPError(f"Invalid proportional factor for {name}: {factor}")
            variable_upper = self._variable_upper(name, state)
            upper = min(upper, variable_upper / factor)

        for _row_name, coefficients, capacity in rows:
            consumption = sum(
                coefficients.get(name, 0.0) * factor
                for name, factor in factors.items()
            )
            if consumption > 1e-15:
                upper = min(upper, capacity / consumption)

        if not isfinite(upper):
            raise BlockLPError(
                f"Block {list(self.model.updates)} failed: unbounded proportional increment"
            )
        if upper < -TOL:
            raise BlockLPError(
                f"Block {list(self.model.updates)} failed: infeasible proportional increment"
            )
        return max(0.0, upper)

    def _member_capacity(self, name, state, rows):
        """Exact scalar maximum for one member in a monotone residual model."""
        upper = self._variable_upper(name, state)
        for _row_name, coefficients, capacity in rows:
            coefficient = coefficients.get(name, 0.0)
            if coefficient > 1e-15:
                upper = min(upper, capacity / coefficient)
        if upper < -TOL:
            raise BlockLPError(
                f"Block {list(self.model.updates)} failed: infeasible residual capacity"
            )
        return max(0.0, upper)

    def _execute_analytical(self, state, rows):
        references = {
            name: value(scalar, state)
            for name, scalar in self.model.rule.reference_cfs.items()
        }
        if any(np.isnan(cfs) or cfs < 0 for cfs in references.values()):
            raise BlockLPError("Invalid proportional reference cfs")

        # Preserve LPKernel's existing two-phase convention exactly:
        # unlimited members share equally before finite-reference members.
        phases = [
            {name: 1.0 for name, cfs in references.items() if np.isposinf(cfs)},
            {name: cfs for name, cfs in references.items()
             if isfinite(cfs) and cfs > 0},
        ]
        deferred = []

        for active in phases:
            while active:
                # Normalize in the same overflow-safe way as LPKernel.
                scale = max(active.values())
                total = sum(factor / scale for factor in active.values())
                factors = {
                    name: (factor / scale) / total
                    for name, factor in active.items()
                }

                # Very small shares use the same deferred scalar-allocation
                # convention as LPKernel to avoid numerically meaningless common
                # increment constraints.
                tiny = [name for name, factor in factors.items() if factor < 1e-6]
                if tiny:
                    deferred.extend(tiny)
                    active = {
                        name: cfs for name, cfs in active.items()
                        if name not in tiny
                    }
                    continue

                increment = self._common_increment(state, factors, rows)
                _commit_updates(
                    self.model, state,
                    {name: factor * increment for name, factor in factors.items()},
                )

                # Rebind row capacities after the commit.  Coefficients can be
                # runtime slots, so use the same post-commit state that the LP
                # kernel would see for its classification solves.
                rows = self._bound_rows(state)
                if rows is None:
                    # Update coefficients should not normally change sign, but
                    # if they do we can no longer classify analytically.  The
                    # model has already been partially committed, so fail loudly
                    # rather than replaying the whole block against altered state.
                    raise BlockLPError(
                        "Proportional coefficient changed sign during analytical execution"
                    )

                blocked = [
                    name for name in active
                    if self._member_capacity(name, state, rows) <= TOL
                ]
                if not blocked:
                    raise BlockLPError(
                        "Proportional allocation made no blocking progress"
                    )
                active = {
                    name: cfs for name, cfs in active.items()
                    if name not in blocked
                }

        for name in deferred:
            rows = self._bound_rows(state)
            if rows is None:
                raise BlockLPError(
                    "Proportional coefficient changed sign during analytical execution"
                )
            increment = self._member_capacity(name, state, rows)
            if not isfinite(increment):
                raise BlockLPError(
                    f"Block {list(self.model.updates)} failed: unbounded deferred member {name}"
                )
            _commit_updates(self.model, state, {name: increment})

        # No numerical LP solve was needed.
        return 0

    def execute(self, state):
        if not self.targets:
            return 0
        rows = self._bound_rows(state)
        if rows is None:
            return self.fallback.execute(state)
        return self._execute_analytical(state, rows)


@dataclass
class LPKernel:
    model: BlockLP

    def __post_init__(self):
        self.names = tuple(self.model.variables)
        self.index = {name: i for i, name in enumerate(self.names)}
        targets = (self.model.rule.coefficients if isinstance(self.model.rule, Maximize)
                   else self.model.rule.reference_cfs)
        for name in targets:
            if name not in self.index:
                raise ValueError(f"Unknown objective variable: {name}")
        for constraint in self.model.constraints:
            if set(constraint.coefficients) - self.index.keys():
                raise ValueError(f"Unknown variable in constraint {constraint.name}")
        if set(self.model.updates) - targets.keys():
            raise ValueError("Only target allocations may commit state updates")

    def _solve(self, state, weights=None, proportions=None):
        """Bind slots and solve this block, including reservation witnesses."""
        width = len(self.names) + (proportions is not None)
        bounds = []
        for variable in self.model.variables.values():
            lower = value(variable.lower, state)
            upper = None if variable.upper is None else value(variable.upper, state)
            if upper is not None and -TOL <= upper < 0 and lower == 0:
                upper = 0.0
            if not isfinite(lower) or (upper is not None and np.isnan(upper)):
                raise BlockLPError("Non-finite variable bound")
            bounds.append((lower, upper))
        eq, eq_rhs, ub, ub_rhs = [], [], [], []
        for constraint in self.model.constraints:
            row = np.zeros(width)
            for name, coefficient in constraint.coefficients.items():
                row[self.index[name]] = value(coefficient, state)
            if not np.isfinite(row).all():
                raise BlockLPError(f"Non-finite coefficient: {constraint.name}")
            lo = None if constraint.lower is None else value(constraint.lower, state)
            hi = None if constraint.upper is None else value(constraint.upper, state)
            if any(v is not None and not isfinite(v) for v in (lo, hi)):
                raise BlockLPError(f"Non-finite constraint bound: {constraint.name}")
            if lo is not None and hi is not None and lo == hi:
                eq.append(row); eq_rhs.append(lo)
            else:
                if hi is not None:
                    ub.append(row); ub_rhs.append(hi)
                if lo is not None:
                    ub.append(-row); ub_rhs.append(-lo)
        objective = np.zeros(width)
        if proportions is not None:
            bounds.append((0, None))
            objective[-1] = -1.0
            for name, factor in proportions.items():
                row = np.zeros(width)
                row[self.index[name]], row[-1] = -1.0, factor
                ub.append(row); ub_rhs.append(0.0)
        else:
            for name, coefficient in weights.items():
                objective[self.index[name]] = -coefficient
        result = linprog(
            objective, A_ub=np.asarray(ub) if ub else None,
            b_ub=np.asarray(ub_rhs) if ub else None,
            A_eq=np.asarray(eq) if eq else None,
            b_eq=np.asarray(eq_rhs) if eq else None,
            bounds=bounds, method="highs-ds",
        )
        if not result.success:
            raise BlockLPError(
                f"Block {list(self.model.updates)} failed: {result.message}"
            )
        return result.x

    def _commit(self, state, increments):
        # Evaluate every coefficient against the same pre-commit state. This
        # makes updates independent of dict order even when they share slots.
        _commit_updates(self.model, state, increments)

    def execute(self, state):
        """Commit target allocations only; auxiliary solutions remain witnesses."""
        calls = 0
        if not self.names:
            return calls
        if isinstance(self.model.rule, Maximize):
            weights = {name: value(c, state) for name, c in self.model.rule.coefficients.items()}
            result = self._solve(state, weights=weights)
            self._commit(state, {name: result[self.index[name]] for name in weights})
            return 1
        if not isinstance(self.model.rule, Proportional):
            raise TypeError(f"Unknown allocation rule: {self.model.rule!r}")
        references = {name: value(c, state) for name, c in self.model.rule.reference_cfs.items()}
        if any(np.isnan(c) or c < 0 for c in references.values()):
            raise BlockLPError("Invalid proportional reference cfs")
        # Match the existing schedule: unlimited members share equally first;
        # finite members then share by their effective daily caps.
        phases = [
            {name: 1.0 for name, cfs in references.items() if np.isposinf(cfs)},
            {name: cfs for name, cfs in references.items() if isfinite(cfs) and cfs > 0},
        ]
        deferred = []
        for active in phases:
            while active:
                # Scale before summing to avoid overflow for very large caps.
                scale = max(active.values())
                total = sum(factor / scale for factor in active.values())
                factors = {name: (factor / scale) / total for name, factor in active.items()}
                tiny = [name for name, factor in factors.items() if factor < 1e-6]
                if tiny:
                    deferred.extend(tiny)
                    active = {name: c for name, c in active.items() if name not in tiny}
                    continue
                result = self._solve(state, proportions=factors)
                calls += 1
                self._commit(state, {name: factor * max(0.0, result[-1]) for name, factor in factors.items()})
                # First classify blockers that are structurally obvious from the
                # residual model. This avoids one scalar LP per active member in
                # the common case where a variable limit or a monotone residual
                # row has just been exhausted.
                blocked = []
                for name in active:
                    variable = self.model.variables[name]
                    upper = (
                        None if variable.upper is None
                        else value(variable.upper, state)
                    )
                    if upper is not None and upper <= TOL:
                        blocked.append(name)

                blocked_set = set(blocked)
                for constraint in self.model.constraints:
                    coefficients = {
                        variable_name: value(coefficient, state)
                        for variable_name, coefficient
                        in constraint.coefficients.items()
                    }

                    # A variable already frozen at zero cannot offset another
                    # member's use of this row, so exclude it from the sign test.
                    live_coefficients = {}
                    for variable_name, coefficient in coefficients.items():
                        variable = self.model.variables[variable_name]
                        variable_upper = (
                            None if variable.upper is None
                            else value(variable.upper, state)
                        )
                        if variable_upper is not None and variable_upper <= TOL:
                            continue
                        live_coefficients[variable_name] = coefficient

                    # sum(+a*x) <= 0 with no live negative coefficient means
                    # every positive-coefficient target on this row is blocked.
                    if (
                        constraint.upper is not None
                        and value(constraint.upper, state) <= TOL
                        and not any(
                            coefficient < -TOL
                            for coefficient in live_coefficients.values()
                        )
                    ):
                        for name in active:
                            if (
                                name not in blocked_set
                                and coefficients.get(name, 0.0) > TOL
                            ):
                                blocked.append(name)
                                blocked_set.add(name)

                    # The symmetric lower-side case: 0 <= sum(-a*x).
                    if (
                        constraint.lower is not None
                        and -value(constraint.lower, state) <= TOL
                        and not any(
                            coefficient > TOL
                            for coefficient in live_coefficients.values()
                        )
                    ):
                        for name in active:
                            if (
                                name not in blocked_set
                                and coefficients.get(name, 0.0) < -TOL
                            ):
                                blocked.append(name)
                                blocked_set.add(name)

                # If no blocker is structurally provable, classify exactly with
                # divide and conquer. For a convex residual LP, if every member
                # of a subset can individually increase, averaging those feasible
                # directions gives a feasible solution in which the whole subset
                # increases. Therefore a zero common increment proves the subset
                # contains at least one blocked member.
                if not blocked:
                    def classify(names):
                        nonlocal calls
                        names = list(names)
                        if not names:
                            return []
                        witness = self._solve(
                            state,
                            proportions={name: 1.0 for name in names},
                        )
                        calls += 1
                        if witness[-1] > TOL:
                            return []
                        if len(names) == 1:
                            return names
                        middle = len(names) // 2
                        return (
                            classify(names[:middle])
                            + classify(names[middle:])
                        )

                    blocked = classify(active)
                if not blocked:
                    raise BlockLPError("Proportional allocation made no blocking progress")
                active = {name: c for name, c in active.items() if name not in blocked}
        for name in deferred:
            result = self._solve(state, weights={name: 1.0})
            calls += 1
            self._commit(state, {name: result[self.index[name]]})
        return calls



@dataclass
class ScalarFormulaKernel:
    """Execute a scalar block objective from a formula compiled before solve().

    ``formula_source`` is produced by symbolic projection during ``compile()``.
    It contains no runtime projection/cache: every scalar query is answered by
    straight-line Python arithmetic over state slots and proportional factors.
    The numerical LP remains an exact safety fallback if a structural sign
    guard or formula feasibility guard is violated at runtime.
    """

    model: BlockLP
    formula_source: str
    maximum_intermediate_rows: int = 0
    final_formula_rows: int = 0

    def __post_init__(self):
        from .formula import ScalarFormulaProgram

        self.fallback = LPKernel(self.model)
        self.names = self.fallback.names
        self.index = self.fallback.index
        if isinstance(self.model.rule, Maximize):
            if len(self.model.rule.coefficients) != 1:
                raise ValueError("Scalar formula Maximize requires one objective target")
            self.targets = tuple(self.model.rule.coefficients)
        elif isinstance(self.model.rule, Proportional):
            self.targets = tuple(self.model.rule.reference_cfs)
        else:
            raise ValueError(f"Unknown scalar formula rule: {self.model.rule!r}")
        if set(self.model.updates) - set(self.targets):
            raise ValueError("Only scalar-formula targets may commit state updates")
        self._maximum = ScalarFormulaProgram(
            self.formula_source,
            self.maximum_intermediate_rows,
            self.final_formula_rows,
            (),
        ).compile()

    def _scalar_maximum(self, state, factors):
        from .formula import FormulaEvaluationError, FormulaGuardFailed
        try:
            return self._maximum(state, factors), 0
        except (FormulaGuardFailed, FormulaEvaluationError, ArithmeticError, OverflowError):
            pass

        if len(factors) == 1 and next(iter(factors.values())) == 1.0:
            name = next(iter(factors))
            result = self.fallback._solve(state, weights={name: 1.0})
            return max(0.0, float(result[self.index[name]])), 1
        result = self.fallback._solve(state, proportions=factors)
        return max(0.0, float(result[-1])), 1

    def _structural_blockers(self, state, active):
        blocked = []
        for name in active:
            variable = self.model.variables[name]
            upper = None if variable.upper is None else value(variable.upper, state)
            if upper is not None and upper <= TOL:
                blocked.append(name)

        blocked_set = set(blocked)
        for constraint in self.model.constraints:
            coefficients = {
                variable_name: value(coefficient, state)
                for variable_name, coefficient in constraint.coefficients.items()
            }
            live_coefficients = {}
            for variable_name, coefficient in coefficients.items():
                variable = self.model.variables[variable_name]
                variable_upper = (
                    None if variable.upper is None
                    else value(variable.upper, state)
                )
                if variable_upper is not None and variable_upper <= TOL:
                    continue
                live_coefficients[variable_name] = coefficient

            if (
                constraint.upper is not None
                and value(constraint.upper, state) <= TOL
                and not any(c < -TOL for c in live_coefficients.values())
            ):
                for name in active:
                    if name not in blocked_set and coefficients.get(name, 0.0) > TOL:
                        blocked.append(name)
                        blocked_set.add(name)

            if (
                constraint.lower is not None
                and -value(constraint.lower, state) <= TOL
                and not any(c > TOL for c in live_coefficients.values())
            ):
                for name in active:
                    if name not in blocked_set and coefficients.get(name, 0.0) < -TOL:
                        blocked.append(name)
                        blocked_set.add(name)
        return blocked

    def execute(self, state):
        if not self.names:
            return 0

        if isinstance(self.model.rule, Maximize):
            name, coefficient_scalar = next(iter(self.model.rule.coefficients.items()))
            coefficient = value(coefficient_scalar, state)
            if not isfinite(coefficient) or coefficient <= 0:
                return self.fallback.execute(state)
            increment, calls = self._scalar_maximum(state, {name: 1.0})
            _commit_updates(self.model, state, {name: increment})
            return calls

        references = {
            name: value(c, state)
            for name, c in self.model.rule.reference_cfs.items()
        }
        if any(np.isnan(c) or c < 0 for c in references.values()):
            raise BlockLPError("Invalid proportional reference cfs")

        phases = [
            {name: 1.0 for name, cfs in references.items() if np.isposinf(cfs)},
            {name: cfs for name, cfs in references.items()
             if isfinite(cfs) and cfs > 0},
        ]
        deferred = []
        calls = 0

        for active in phases:
            while active:
                scale = max(active.values())
                total = sum(factor / scale for factor in active.values())
                factors = {
                    name: (factor / scale) / total
                    for name, factor in active.items()
                }
                tiny = [name for name, factor in factors.items() if factor < 1e-6]
                if tiny:
                    deferred.extend(tiny)
                    active = {
                        name: cfs for name, cfs in active.items()
                        if name not in tiny
                    }
                    continue

                increment, scalar_calls = self._scalar_maximum(state, factors)
                calls += scalar_calls
                _commit_updates(
                    self.model, state,
                    {name: factor * increment for name, factor in factors.items()},
                )

                blocked = self._structural_blockers(state, active)
                if not blocked:
                    def classify(names):
                        nonlocal calls
                        names = list(names)
                        if not names:
                            return []
                        witness, scalar_calls = self._scalar_maximum(
                            state, {name: 1.0 for name in names}
                        )
                        calls += scalar_calls
                        if witness > TOL:
                            return []
                        if len(names) == 1:
                            return names
                        middle = len(names) // 2
                        return classify(names[:middle]) + classify(names[middle:])
                    blocked = classify(active)

                if not blocked:
                    raise BlockLPError("Proportional allocation made no blocking progress")
                active = {
                    name: cfs for name, cfs in active.items()
                    if name not in blocked
                }

        for name in deferred:
            increment, scalar_calls = self._scalar_maximum(state, {name: 1.0})
            calls += scalar_calls
            _commit_updates(self.model, state, {name: increment})
        return calls

def compile_direct_kernel(lp_model: BlockLP) -> DirectCalculationKernel:
    return DirectCalculationKernel(lp_model)


def compile_proportional_kernel(lp_model: BlockLP) -> ProportionalCalculationKernel:
    return ProportionalCalculationKernel(lp_model)


def compile_scalar_formula_kernel(
    lp_model: BlockLP,
    formula_source: str,
    *,
    maximum_intermediate_rows: int = 0,
    final_formula_rows: int = 0,
) -> ScalarFormulaKernel:
    return ScalarFormulaKernel(
        lp_model, formula_source, maximum_intermediate_rows, final_formula_rows
    )


def compile_lp_kernel(lp_model: BlockLP) -> LPKernel:
    return LPKernel(lp_model)
