"""LP solver wrapper backed by SciPy's HiGHS interface.

This mirrors the public API used by ``solve_lp_with_GLOP.LPSolver`` closely
so the accounting solver can switch backends with only an import change.

Important implementation difference: ``scipy.optimize.linprog`` is not a
persistent model API. The sparse matrices are rebuilt for each solve. For an
application that performs many small model modifications and re-solves, the
native ``highspy`` package is likely a better long-term HiGHS backend because
it exposes a persistent model and basis operations directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isclose, isnan
from typing import Iterable

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix


class LPSolverError(Exception):
    """The requested LP was infeasible, unbounded, or otherwise unsolved."""


@dataclass
class _Variable:
    name: str
    lower_bound: float
    upper_bound: float

    # Small compatibility surface with OR-Tools' MPVariable.
    def lb(self) -> float:
        return self.lower_bound

    def ub(self) -> float:
        return self.upper_bound

    def SetLb(self, value: float) -> None:
        self.lower_bound = value

    def SetUb(self, value: float) -> None:
        self.upper_bound = value

    def SetBounds(self, lb: float, ub: float) -> None:
        self.lower_bound = lb
        self.upper_bound = ub


@dataclass
class _Constraint:
    name: str
    lower_bound: float
    upper_bound: float
    coefficients: dict[str, float] = field(default_factory=dict)

    def lb(self) -> float:
        return self.lower_bound

    def ub(self) -> float:
        return self.upper_bound

    def SetLb(self, value: float) -> None:
        self.lower_bound = value

    def SetUb(self, value: float) -> None:
        self.upper_bound = value

    def SetBounds(self, lb: float, ub: float) -> None:
        self.lower_bound = lb
        self.upper_bound = ub

    def SetCoefficient(self, variable: _Variable, coefficient: float) -> None:
        if coefficient == 0:
            self.coefficients.pop(variable.name, None)
        else:
            self.coefficients[variable.name] = coefficient

    def GetCoefficient(self, variable: _Variable) -> float:
        return self.coefficients.get(variable.name, 0.0)

    def Clear(self) -> None:
        self.coefficients.clear()


class LPSolver:
    """API-compatible LP wrapper using SciPy/HiGHS.

    ``method='highs-ds'`` is the default because this application repeatedly
    changes bounds and objectives, a workload that usually suits dual simplex.
    SciPy does not currently expose a reusable HiGHS basis through ``linprog``,
    so each call still starts through the SciPy interface from a rebuilt sparse
    matrix.
    """

    SOLVER_OPTIMAL = 0
    PRINT_SOLVER_MESSAGES = False

    def __init__(
        self,
        tolerance: float | None = None,
        method: str = "highs-ds",
        presolve: bool = True,
    ):
        if method not in {"highs", "highs-ds", "highs-ipm"}:
            raise ValueError(
                "method must be 'highs', 'highs-ds', or 'highs-ipm'"
            )

        self.tolerance = tolerance
        self.method = method
        self.presolve = presolve

        self.vars: dict[str, _Variable] = {}
        self.cons: dict[str, _Constraint] = {}
        self._constraint_vars: dict[str, list[str]] = {}

        self._last_solution_values: dict[str, float] = {}
        self._last_variable_reduced_costs: dict[str, float | None] = {}
        self._last_constraint_activities: dict[str, float] = {}
        self._last_constraint_bounds: dict[str, tuple[float, float]] = {}
        self._last_constraint_dual_values: dict[str, float | None] = {}
        self._last_constraint_coefficients: dict[str, dict[str, float]] = {}
        self._last_variable_constraints: dict[str, list[str]] = {}

        # Retained for API compatibility. The accounting solver's corrected
        # feasibility fallback normally leaves this unset.
        self.perminant_minus_var: str | None = None

        # Useful for profiling solver orchestration.
        self.solve_count = 0
        self.total_iterations = 0
        self.last_iterations = 0

    @staticmethod
    def _normalize_lb(value: float | None) -> float:
        return -inf if value is None else value

    @staticmethod
    def _normalize_ub(value: float | None) -> float:
        return inf if value is None else value

    @staticmethod
    def _validate_number(value: float, description: str) -> None:
        if isnan(value):
            raise ValueError(f"{description} is NaN")

    def add_variable(
        self,
        name: str,
        lb: float | None = 0,
        ub: float | None = None,
    ) -> None:
        lb_value = self._normalize_lb(lb)
        ub_value = self._normalize_ub(ub)
        self._validate_number(lb_value, f"Lower bound for variable {name}")
        self._validate_number(ub_value, f"Upper bound for variable {name}")

        if name in self.vars:
            raise ValueError(f"New variable name `{name}` is already being used!")
        if lb_value > ub_value:
            raise ValueError(f"Variable {name} has lb > ub: {lb_value} > {ub_value}")

        self.vars[name] = _Variable(name, lb_value, ub_value)

    def get_variable_bounds(self, name: str) -> tuple[float, float]:
        variable = self.vars[name]
        return variable.lb(), variable.ub()

    def add_constriant(
        self,
        name: str,
        lb: float | None = None,
        ub: float | None = None,
    ) -> None:
        """Add a constraint. The misspelling is retained for compatibility."""
        lb_value = self._normalize_lb(lb)
        ub_value = self._normalize_ub(ub)
        self._validate_number(lb_value, f"Lower bound for constraint {name}")
        self._validate_number(ub_value, f"Upper bound for constraint {name}")

        if name in self.cons:
            raise ValueError(f"New constraint name `{name}` is already being used!")
        if lb_value > ub_value:
            raise ValueError(f"Constraint {name} has lb > ub: {lb_value} > {ub_value}")

        self.cons[name] = _Constraint(name, lb_value, ub_value)
        self._constraint_vars[name] = []

    def set_coeficient(
        self,
        constraint_name: str,
        variable_name: str,
        coef: float | None,
    ) -> None:
        """Set a coefficient. The misspelling is retained for compatibility."""
        if coef is None:
            raise ValueError(f"New coefficient for {constraint_name} is None")
        self._validate_number(coef, f"Coefficient {constraint_name}/{variable_name}")

        variable = self.vars[variable_name]
        constraint = self.cons[constraint_name]
        constraint.SetCoefficient(variable, coef)

        names = self._constraint_vars.setdefault(constraint_name, [])
        if coef == 0:
            if variable_name in names:
                names.remove(variable_name)
        elif variable_name not in names:
            names.append(variable_name)

    def _build_sparse_matrix(
        self,
        rows: list[dict[int, float]],
        column_count: int,
    ) -> csr_matrix | None:
        if not rows:
            return None

        row_indices: list[int] = []
        col_indices: list[int] = []
        data: list[float] = []
        for row_index, row in enumerate(rows):
            for col_index, coefficient in row.items():
                if coefficient != 0:
                    row_indices.append(row_index)
                    col_indices.append(col_index)
                    data.append(coefficient)

        return csr_matrix(
            (data, (row_indices, col_indices)),
            shape=(len(rows), column_count),
            dtype=float,
        )

    def _build_problem(self):
        variable_names = list(self.vars)
        variable_index = {name: i for i, name in enumerate(variable_names)}

        eq_rows: list[dict[int, float]] = []
        eq_rhs: list[float] = []
        ub_rows: list[dict[int, float]] = []
        ub_rhs: list[float] = []

        # Maps an original constraint to one or more generated rows.
        # rhs_factor converts a generated-row RHS perturbation back to the
        # original constraint bound: +1 for upper/equality, -1 for lower.
        row_map: dict[str, list[tuple[str, int, float]]] = {
            name: [] for name in self.cons
        }

        for constraint_name, constraint in self.cons.items():
            row = {
                variable_index[var_name]: coefficient
                for var_name, coefficient in constraint.coefficients.items()
                if coefficient != 0
            }
            lb = constraint.lb()
            ub = constraint.ub()

            if lb != -inf and ub != inf and lb == ub:
                index = len(eq_rows)
                eq_rows.append(row)
                eq_rhs.append(lb)
                row_map[constraint_name].append(("eq", index, 1.0))
                continue

            if ub != inf:
                index = len(ub_rows)
                ub_rows.append(row)
                ub_rhs.append(ub)
                row_map[constraint_name].append(("ub", index, 1.0))

            if lb != -inf:
                index = len(ub_rows)
                ub_rows.append({column: -coef for column, coef in row.items()})
                ub_rhs.append(-lb)
                row_map[constraint_name].append(("ub", index, -1.0))

        bounds = [
            (
                None if self.vars[name].lb() == -inf else self.vars[name].lb(),
                None if self.vars[name].ub() == inf else self.vars[name].ub(),
            )
            for name in variable_names
        ]

        return (
            variable_names,
            variable_index,
            self._build_sparse_matrix(ub_rows, len(variable_names)),
            np.asarray(ub_rhs, dtype=float) if ub_rhs else None,
            self._build_sparse_matrix(eq_rows, len(variable_names)),
            np.asarray(eq_rhs, dtype=float) if eq_rhs else None,
            bounds,
            row_map,
        )

    def solve_objective(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> tuple[float, dict[str, float]]:
        """Solve an LP and return the objective and requested variable values."""
        weights = weights or {}
        (
            all_variable_names,
            variable_index,
            A_ub,
            b_ub,
            A_eq,
            b_eq,
            bounds,
            row_map,
        ) = self._build_problem()

        objective_original = np.zeros(len(all_variable_names), dtype=float)
        for variable_name in variable_names:
            objective_original[variable_index[variable_name]] = weights.get(
                variable_name, 1.0
            )

        if self.perminant_minus_var:
            penalty_coefficient = -1000.0 if maximization else 1000.0
            objective_original[
                variable_index[self.perminant_minus_var]
            ] = penalty_coefficient

        # scipy.optimize.linprog always minimizes.
        objective_multiplier = -1.0 if maximization else 1.0
        objective_min = objective_multiplier * objective_original

        options: dict[str, float | bool] = {
            "disp": self.PRINT_SOLVER_MESSAGES,
            "presolve": self.presolve,
        }
        if self.tolerance is not None:
            options["primal_feasibility_tolerance"] = self.tolerance
            options["dual_feasibility_tolerance"] = self.tolerance

        result = linprog(
            c=objective_min,
            A_ub=A_ub,
            b_ub=b_ub,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method=self.method,
            options=options,
        )

        self.solve_count += 1
        self.last_iterations = int(getattr(result, "nit", 0) or 0)
        self.total_iterations += self.last_iterations

        if not result.success:
            raise LPSolverError(
                "Could not find optimal solution! "
                f"Status={result.status}: {result.message}\n\n{self.lp_string()}"
            )

        x = np.asarray(result.x, dtype=float)
        objective_value = float(objective_original @ x)
        self._last_solution_values = {
            name: float(x[index]) for name, index in variable_index.items()
        }
        requested_values = {
            name: self._last_solution_values[name] for name in variable_names
        }

        # Marginals are for SciPy's minimization objective. Convert them back
        # to the caller's max/min objective convention.
        lower_marginals = np.asarray(result.lower.marginals, dtype=float)
        upper_marginals = np.asarray(result.upper.marginals, dtype=float)
        self._last_variable_reduced_costs = {
            name: float(
                objective_multiplier
                * (lower_marginals[index] + upper_marginals[index])
            )
            for name, index in variable_index.items()
        }

        self._last_constraint_activities = {}
        self._last_constraint_bounds = {}
        self._last_constraint_dual_values = {}
        self._last_constraint_coefficients = {}
        self._last_variable_constraints = {name: [] for name in all_variable_names}

        eq_marginals = np.asarray(result.eqlin.marginals, dtype=float)
        ub_marginals = np.asarray(result.ineqlin.marginals, dtype=float)

        for constraint_name, constraint in self.cons.items():
            coefficients = dict(constraint.coefficients)
            activity = sum(
                coefficient * self._last_solution_values.get(variable_name, 0.0)
                for variable_name, coefficient in coefficients.items()
            )
            dual_value = 0.0
            has_dual = False
            for row_type, row_index, rhs_factor in row_map[constraint_name]:
                marginal = (
                    eq_marginals[row_index]
                    if row_type == "eq"
                    else ub_marginals[row_index]
                )
                dual_value += objective_multiplier * rhs_factor * float(marginal)
                has_dual = True

            self._last_constraint_activities[constraint_name] = activity
            self._last_constraint_bounds[constraint_name] = (
                constraint.lb(),
                constraint.ub(),
            )
            self._last_constraint_dual_values[constraint_name] = (
                dual_value if has_dual else None
            )
            self._last_constraint_coefficients[constraint_name] = coefficients
            for variable_name in coefficients:
                self._last_variable_constraints.setdefault(variable_name, []).append(
                    constraint_name
                )

        return objective_value, requested_values

    def maximize_and_update_variable(self, variable_name: str) -> float:
        _, values = self.solve_objective([variable_name], maximization=True)
        solved_value = values[variable_name]
        self.update_variable_bounds(variable_name, lb=solved_value)
        return solved_value

    def minimize_and_update_variable(self, variable_name: str) -> float:
        _, values = self.solve_objective([variable_name], maximization=False)
        solved_value = values[variable_name]
        self.update_variable_bounds(variable_name, ub=solved_value)
        return solved_value

    def update_variable_bounds(
        self,
        name: str,
        lb: float | None = None,
        ub: float | None = None,
    ) -> None:
        variable = self.vars[name]
        prospective_lb = lb if lb is not None else variable.lb()
        prospective_ub = ub if ub is not None else variable.ub()

        if prospective_lb > prospective_ub:
            small_error = isclose(
                prospective_lb - prospective_ub, 0, abs_tol=0.0001
            )
            if small_error and ub is None:
                lb = variable.ub()
            elif small_error and lb is None:
                ub = variable.lb()
            else:
                raise ValueError(
                    f"Cannot update bounds for {name}: "
                    f"{prospective_lb} > {prospective_ub}"
                )

        if lb is not None:
            self._validate_number(lb, f"New lower bound for variable {name}")
            variable.SetLb(lb)
            if isclose(lb, prospective_ub, abs_tol=0.0001) and prospective_ub > lb:
                variable.SetUb(lb)

        if ub is not None:
            self._validate_number(ub, f"New upper bound for variable {name}")
            variable.SetUb(ub)

    def update_constraint_ub(self, name: str, ub: float | None = None) -> None:
        value = self._normalize_ub(ub)
        self._validate_number(value, f"Upper bound for constraint {name}")
        constraint = self.cons[name]
        if constraint.lb() > value:
            raise ValueError(
                f"Constraint {name} has lb > new ub: {constraint.lb()} > {value}"
            )
        constraint.SetUb(value)

    def update_constraint_lb(self, name: str, lb: float | None = None) -> None:
        value = self._normalize_lb(lb)
        self._validate_number(value, f"Lower bound for constraint {name}")
        constraint = self.cons[name]
        if value > constraint.ub():
            raise ValueError(
                f"Constraint {name} has new lb > ub: {value} > {constraint.ub()}"
            )
        constraint.SetLb(value)

    def get_constraint_names(self) -> list[str]:
        return list(self.cons)

    @staticmethod
    def _format_linear_expression(coefficients: dict[str, float]) -> str:
        if not coefficients:
            return "0"
        parts = []
        for name, coefficient in coefficients.items():
            parts.append(f"{coefficient:+g} {name}")
        return " ".join(parts).lstrip("+")

    def lp_string(self) -> str:
        """Return a readable LP-like model representation for diagnostics."""
        lines = ["Subject To"]
        for name, constraint in self.cons.items():
            expression = self._format_linear_expression(constraint.coefficients)
            lb, ub = constraint.lb(), constraint.ub()
            if lb != -inf and ub != inf and lb == ub:
                lines.append(f" {name}: {expression} = {lb:g}")
            else:
                if lb != -inf:
                    lines.append(f" {name}_lb: {expression} >= {lb:g}")
                if ub != inf:
                    lines.append(f" {name}_ub: {expression} <= {ub:g}")

        lines.append("Bounds")
        for name, variable in self.vars.items():
            lb, ub = variable.lb(), variable.ub()
            lb_text = "-inf" if lb == -inf else f"{lb:g}"
            ub_text = "inf" if ub == inf else f"{ub:g}"
            lines.append(f" {lb_text} <= {name} <= {ub_text}")
        lines.append("End")
        return "\n".join(lines)

    def maximize_group_by_proportions(
        self,
        variable_names: list[str],
        proportion_factors: dict[str, float],
    ) -> dict[str, float]:
        merge_variable_name = "combined"
        if merge_variable_name not in self.vars:
            self.add_variable(merge_variable_name, lb=0)
        merge_variable = self.vars[merge_variable_name]
        merge_variable.SetBounds(0, inf)

        merge_constraint_names: list[str] = []
        for variable_name in variable_names:
            factor = proportion_factors[variable_name]
            self._validate_number(factor, f"Proportion factor for {variable_name}")

            constraint_name = "combined_" + variable_name
            if constraint_name not in self.cons:
                self.add_constriant(constraint_name)
            constraint = self.cons[constraint_name]
            constraint.Clear()
            self._constraint_vars[constraint_name] = []
            constraint.SetBounds(self.vars[variable_name].lb(), inf)
            self.set_coeficient(constraint_name, variable_name, 1.0)
            self.set_coeficient(constraint_name, merge_variable_name, -factor)
            merge_constraint_names.append(constraint_name)

        _, solved_values = self.solve_objective(
            [merge_variable_name], maximization=True
        )
        solved_value = solved_values[merge_variable_name]

        # Capture original lower bounds before disabling the temporary rows.
        initial_values = {
            name: self.vars[name].lb() for name in variable_names
        }

        for constraint_name in merge_constraint_names:
            self.cons[constraint_name].Clear()
            self.cons[constraint_name].SetBounds(-inf, inf)
            self._constraint_vars[constraint_name] = []
        merge_variable.SetBounds(-inf, inf)

        return {
            name: initial_values[name] + proportion_factors[name] * solved_value
            for name in variable_names
        }

    def get_last_variable_reduced_cost(self, variable_name: str) -> float | None:
        return self._last_variable_reduced_costs.get(variable_name)

    def get_last_solve_constraint_evidence(
        self,
        variable_name: str,
        tolerance: float = 1e-6,
    ) -> list[dict]:
        output = []
        for constraint_name in self._last_variable_constraints.get(variable_name, []):
            coefficients = self._last_constraint_coefficients[constraint_name]
            coefficient = coefficients[variable_name]

            activity = self._last_constraint_activities[constraint_name]
            lower_bound, upper_bound = self._last_constraint_bounds[constraint_name]
            lower_slack = None if lower_bound == -inf else activity - lower_bound
            upper_slack = None if upper_bound == inf else upper_bound - activity
            lower_is_tight = lower_slack is not None and isclose(
                lower_slack, 0, abs_tol=tolerance
            )
            upper_is_tight = upper_slack is not None and isclose(
                upper_slack, 0, abs_tol=tolerance
            )
            blocks_direct_increase = (
                (coefficient > 0 and upper_is_tight)
                or (coefficient < 0 and lower_is_tight)
            )

            output.append(
                {
                    "constraint_name": constraint_name,
                    "coefficient": coefficient,
                    "activity": activity,
                    "lower_bound": None if lower_bound == -inf else lower_bound,
                    "upper_bound": None if upper_bound == inf else upper_bound,
                    "lower_slack": lower_slack,
                    "upper_slack": upper_slack,
                    "is_tight": lower_is_tight or upper_is_tight,
                    "blocks_direct_increase": blocks_direct_increase,
                    "dual_value": self._last_constraint_dual_values.get(
                        constraint_name
                    ),
                }
            )
        return output

    def is_constraint_tight(self, constraint_name: str, variable_name: str) -> bool:
        if constraint_name not in self.cons or variable_name not in self.vars:
            return False
        constraint = self.cons[constraint_name]
        coefficient = constraint.GetCoefficient(self.vars[variable_name])
        if coefficient == 0:
            return False

        activity = self._last_constraint_activities.get(constraint_name)
        if activity is None:
            return False
        if coefficient > 0:
            return constraint.ub() != inf and isclose(
                activity, constraint.ub(), abs_tol=1e-4
            )
        return constraint.lb() != -inf and isclose(
            activity, constraint.lb(), abs_tol=1e-4
        )

    def is_variable_maxed(self, variable_name: str) -> bool:
        variable = self.vars.get(variable_name)
        if variable is None:
            return False
        value = self._last_solution_values.get(variable_name, 0.0)
        if variable.ub() != inf and isclose(value, variable.ub(), abs_tol=1e-4):
            return True
        return any(
            self.is_constraint_tight(constraint_name, variable_name)
            for constraint_name in self.cons
            if self.cons[constraint_name].GetCoefficient(variable) != 0
        )

    def set_perminant_minus_var(self, minus_var_name: str | None):
        self.perminant_minus_var = minus_var_name
