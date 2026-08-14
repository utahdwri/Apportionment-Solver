"""Persistent LP solver wrapper backed by native HiGHS Python bindings.

The public backend is the free ``highspy`` package.  The wrapper mirrors the
common LP interface and is loaded through the central backend registry.

Unlike ``scipy.optimize.linprog``, this wrapper keeps one native HiGHS model
alive.  Objective coefficients, variable bounds, row bounds, and matrix
coefficients are changed in place.  HiGHS can therefore retain and repair the
simplex basis between the many lexicographic re-solves performed by the water
accounting solver.

Install the public ``highspy`` package to use this backend::

    pip install highspy
"""

from __future__ import annotations

from .lp_solver import LPSolverError

from dataclasses import dataclass, field
from math import inf, isclose, isnan
from typing import Any

import numpy as np

import highspy as _highs_module

_HighsClass = _highs_module.Highs
_ObjSense = _highs_module.ObjSense
_HighsModelStatus = _highs_module.HighsModelStatus
_HighsStatus = _highs_module.HighsStatus
_kHighsInf = _highs_module.kHighsInf
HIGHS_BINDING_SOURCE = "highspy"


@dataclass
class _Variable:
    owner: "LPSolver"
    _name: str
    index: int
    lower_bound: float
    upper_bound: float

    def name(self) -> str:
        return self._name

    def lb(self) -> float:
        return self.lower_bound

    def ub(self) -> float:
        return self.upper_bound

    def SetLb(self, value: float) -> None:
        self.owner._set_variable_bounds(self, value, self.upper_bound)

    def SetUb(self, value: float) -> None:
        self.owner._set_variable_bounds(self, self.lower_bound, value)

    def SetBounds(self, lb: float, ub: float) -> None:
        self.owner._set_variable_bounds(self, lb, ub)


@dataclass
class _Constraint:
    owner: "LPSolver"
    _name: str
    index: int
    lower_bound: float
    upper_bound: float
    coefficients: dict[str, float] = field(default_factory=dict)

    def name(self) -> str:
        return self._name

    def lb(self) -> float:
        return self.lower_bound

    def ub(self) -> float:
        return self.upper_bound

    def SetLb(self, value: float) -> None:
        self.owner._set_constraint_bounds(self, value, self.upper_bound)

    def SetUb(self, value: float) -> None:
        self.owner._set_constraint_bounds(self, self.lower_bound, value)

    def SetBounds(self, lb: float, ub: float) -> None:
        self.owner._set_constraint_bounds(self, lb, ub)

    def SetCoefficient(self, variable: _Variable, coefficient: float) -> None:
        self.owner._set_coefficient(self, variable, coefficient)

    def GetCoefficient(self, variable: _Variable) -> float:
        return self.coefficients.get(variable._name, 0.0)

    def Clear(self) -> None:
        for variable_name in list(self.coefficients):
            self.owner._set_coefficient(
                self,
                self.owner.vars[variable_name],
                0.0,
            )


class LPSolver:
    """API-compatible persistent LP wrapper using native HiGHS.

    The default is serial dual simplex, which is well suited to repeated bound
    and objective changes.  HiGHS retains its current basis in the persistent
    model and normally warm-starts each subsequent solve automatically.
    """

    SOLVER_OPTIMAL = 0
    PRINT_SOLVER_MESSAGES = False

    def __init__(
        self,
        tolerance: float | None = None,
        presolve: str = "choose",
        simplex_strategy: int = 1,
        threads: int = 1,
    ):
        if presolve not in {"off", "choose", "on"}:
            raise ValueError("presolve must be 'off', 'choose', or 'on'")
        if simplex_strategy not in {0, 1, 2, 3, 4}:
            raise ValueError("simplex_strategy must be between 0 and 4")
        if threads < 0:
            raise ValueError("threads cannot be negative")

        self.tolerance = tolerance
        self.solver = _HighsClass()
        self._set_option("output_flag", self.PRINT_SOLVER_MESSAGES)
        self._set_option("log_to_console", self.PRINT_SOLVER_MESSAGES)
        self._set_option("solver", "simplex")
        self._set_option("simplex_strategy", simplex_strategy)
        self._set_option("presolve", presolve)
        self._set_option("parallel", "off")
        self._set_option("threads", threads)
        if tolerance is not None:
            self._validate_number(tolerance, "Solver tolerance")
            self._set_option("primal_feasibility_tolerance", tolerance)
            self._set_option("dual_feasibility_tolerance", tolerance)

        self.vars: dict[str, _Variable] = {}
        self.cons: dict[str, _Constraint] = {}
        self._constraint_vars: dict[str, list[str]] = {}
        self._variable_constraints: dict[str, list[str]] = {}

        self._last_solution_values: dict[str, float] = {}
        self._last_variable_reduced_costs: dict[str, float | None] = {}
        self._last_constraint_activities: dict[str, float] = {}
        self._last_constraint_bounds: dict[str, tuple[float, float]] = {}
        self._last_constraint_dual_values: dict[str, float | None] = {}
        self._last_constraint_coefficients: dict[str, dict[str, float]] = {}
        self._last_variable_constraints: dict[str, list[str]] = {}

        self._objective_costs: dict[str, float] = {}
        self._objective_variable_names: list[str] = []
        self._objective_maximization = True

        # Retained for API compatibility. Corrected feasibility fallback code
        # normally leaves this unset.
        self.perminant_minus_var: str | None = None

        self.solve_count = 0
        self.total_iterations = 0
        self.last_iterations = 0
        self.last_run_time = 0.0

        try:
            self.backend_version = str(self.solver.version())
        except Exception:  # pragma: no cover
            self.backend_version = "unknown"
        self.backend_name = HIGHS_BINDING_SOURCE

    @staticmethod
    def _normalize_lb(value: float | None) -> float:
        return -inf if value is None else float(value)

    @staticmethod
    def _normalize_ub(value: float | None) -> float:
        return inf if value is None else float(value)

    @staticmethod
    def _validate_number(value: float, description: str) -> None:
        if isnan(value):
            raise ValueError(f"{description} is NaN")

    @staticmethod
    def _native_bound(value: float) -> float:
        if value == inf:
            return float(_kHighsInf)
        if value == -inf:
            return -float(_kHighsInf)
        return float(value)

    def _check_status(self, status: Any, operation: str) -> None:
        if status == _HighsStatus.kError:
            raise RuntimeError(f"HiGHS failed while attempting to {operation}")

    def _set_option(self, name: str, value: Any) -> None:
        status = self.solver.setOptionValue(name, value)
        self._check_status(status, f"set option {name}={value!r}")

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

        index = int(self.solver.getNumCol())
        status = self.solver.addVar(
            self._native_bound(lb_value),
            self._native_bound(ub_value),
        )
        self._check_status(status, f"add variable {name}")
        self._check_status(self.solver.passColName(index, name), f"name variable {name}")

        self.vars[name] = _Variable(self, name, index, lb_value, ub_value)
        self._variable_constraints[name] = []

    def get_variable_bounds(self, name: str) -> tuple[float, float]:
        variable = self.vars[name]
        return variable.lb(), variable.ub()

    def get_constraint_bounds(
        self,
        name: str,
    ) -> tuple[float, float]:
        """Return the current lower and upper bounds of a constraint."""
        constraint = self.cons[name]
        return constraint.lb(), constraint.ub()

    def add_constraint(
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

        index = int(self.solver.getNumRow())
        empty_indices = np.empty(0, dtype=np.int32)
        empty_values = np.empty(0, dtype=np.float64)
        status = self.solver.addRow(
            self._native_bound(lb_value),
            self._native_bound(ub_value),
            0,
            empty_indices,
            empty_values,
        )
        self._check_status(status, f"add constraint {name}")
        self._check_status(self.solver.passRowName(index, name), f"name constraint {name}")

        self.cons[name] = _Constraint(self, name, index, lb_value, ub_value)
        self._constraint_vars[name] = []

    def _set_coefficient(
        self,
        constraint: _Constraint,
        variable: _Variable,
        coefficient: float,
    ) -> None:
        coefficient = float(coefficient)
        self._validate_number(
            coefficient,
            f"Coefficient {constraint._name}/{variable._name}",
        )
        status = self.solver.changeCoeff(
            constraint.index,
            variable.index,
            coefficient,
        )
        self._check_status(
            status,
            f"change coefficient {constraint._name}/{variable._name}",
        )

        constraint_names = self._variable_constraints.setdefault(variable._name, [])
        variable_names = self._constraint_vars.setdefault(constraint._name, [])
        if coefficient == 0.0:
            constraint.coefficients.pop(variable._name, None)
            if variable._name in variable_names:
                variable_names.remove(variable._name)
            if constraint._name in constraint_names:
                constraint_names.remove(constraint._name)
        else:
            constraint.coefficients[variable._name] = coefficient
            if variable._name not in variable_names:
                variable_names.append(variable._name)
            if constraint._name not in constraint_names:
                constraint_names.append(constraint._name)

    def set_coefficient(
        self,
        constraint_name: str,
        variable_name: str,
        coef: float | None,
    ) -> None:
        """Set a coefficient. The misspelling is retained for compatibility."""
        if coef is None:
            raise ValueError(f"New coefficient for {constraint_name} is None")
        self._set_coefficient(
            self.cons[constraint_name],
            self.vars[variable_name],
            float(coef),
        )

    def _set_variable_bounds(
        self,
        variable: _Variable,
        lb: float,
        ub: float,
    ) -> None:
        lb = float(lb)
        ub = float(ub)
        self._validate_number(lb, f"Lower bound for variable {variable._name}")
        self._validate_number(ub, f"Upper bound for variable {variable._name}")
        if lb > ub:
            raise ValueError(
                f"Variable {variable._name} has lb > ub: {lb} > {ub}"
            )
        status = self.solver.changeColBounds(
            variable.index,
            self._native_bound(lb),
            self._native_bound(ub),
        )
        self._check_status(status, f"change bounds for variable {variable._name}")
        variable.lower_bound = lb
        variable.upper_bound = ub

    def _set_constraint_bounds(
        self,
        constraint: _Constraint,
        lb: float,
        ub: float,
    ) -> None:
        lb = float(lb)
        ub = float(ub)
        self._validate_number(lb, f"Lower bound for constraint {constraint._name}")
        self._validate_number(ub, f"Upper bound for constraint {constraint._name}")
        if lb > ub:
            raise ValueError(
                f"Constraint {constraint._name} has lb > ub: {lb} > {ub}"
            )
        status = self.solver.changeRowBounds(
            constraint.index,
            self._native_bound(lb),
            self._native_bound(ub),
        )
        self._check_status(status, f"change bounds for constraint {constraint._name}")
        constraint.lower_bound = lb
        constraint.upper_bound = ub

    def _set_objective(
        self,
        variable_names: list[str],
        maximization: bool,
        weights: dict[str, float],
    ) -> None:
        new_costs: dict[str, float] = {}
        for variable_name in variable_names:
            if variable_name not in self.vars:
                raise KeyError(f"Unknown objective variable: {variable_name}")
            coefficient = float(weights.get(variable_name, 1.0))
            self._validate_number(coefficient, f"Objective coefficient {variable_name}")
            if coefficient != 0.0:
                new_costs[variable_name] = coefficient

        if self.perminant_minus_var:
            if self.perminant_minus_var not in self.vars:
                raise KeyError(
                    f"Unknown permanent penalty variable: {self.perminant_minus_var}"
                )
            new_costs[self.perminant_minus_var] = -1000.0 if maximization else 1000.0

        changed_names = sorted(set(self._objective_costs) | set(new_costs))
        if changed_names:
            indices = np.asarray(
                [self.vars[name].index for name in changed_names],
                dtype=np.int32,
            )
            costs = np.asarray(
                [new_costs.get(name, 0.0) for name in changed_names],
                dtype=np.float64,
            )
            status = self.solver.changeColsCost(len(changed_names), indices, costs)
            self._check_status(status, "replace objective coefficients")

        sense = _ObjSense.kMaximize if maximization else _ObjSense.kMinimize
        self._check_status(
            self.solver.changeObjectiveSense(sense),
            "change objective sense",
        )
        self._objective_costs = new_costs
        self._objective_variable_names = list(variable_names)
        self._objective_maximization = maximization

    def _snapshot_solution(self) -> None:
        solution = self.solver.getSolution()
        info = self.solver.getInfo()

        # HiGHS documentation recommends converting returned arrays to lists
        # before entry-by-entry access; direct pybind indexing is much slower.
        col_value = list(solution.col_value)
        col_dual = list(solution.col_dual)
        row_value = list(solution.row_value)
        row_dual = list(solution.row_dual)

        self._last_solution_values = {
            name: float(col_value[variable.index])
            for name, variable in self.vars.items()
        }
        self._last_variable_reduced_costs = {
            name: float(col_dual[variable.index])
            for name, variable in self.vars.items()
        }
        self._last_constraint_activities = {
            name: float(row_value[constraint.index])
            for name, constraint in self.cons.items()
        }
        self._last_constraint_dual_values = {
            name: float(row_dual[constraint.index])
            for name, constraint in self.cons.items()
        }
        self._last_constraint_bounds = {
            name: (constraint.lb(), constraint.ub())
            for name, constraint in self.cons.items()
        }
        self._last_constraint_coefficients = {
            name: dict(constraint.coefficients)
            for name, constraint in self.cons.items()
        }
        self._last_variable_constraints = {
            name: list(constraint_names)
            for name, constraint_names in self._variable_constraints.items()
        }

        self.last_iterations = int(getattr(info, "simplex_iteration_count", 0) or 0)
        self.total_iterations += self.last_iterations
        try:
            self.last_run_time = float(self.solver.getRunTime())
        except Exception:  # pragma: no cover
            self.last_run_time = 0.0

    def solve_objective(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> tuple[float, dict[str, float]]:
        """Solve the current persistent model for the requested objective."""
        self._set_objective(variable_names, maximization, weights or {})
        if self.PRINT_SOLVER_MESSAGES:
            self._set_option("output_flag", True)
            self._set_option("log_to_console", True)

        run_status = self.solver.run()
        self.solve_count += 1
        self._check_status(run_status, "solve model")

        model_status = self.solver.getModelStatus()
        if model_status != _HighsModelStatus.kOptimal:
            status_text = self.solver.modelStatusToString(model_status)
            raise LPSolverError(
                "Could not find optimal solution! "
                f"Status={model_status}: {status_text}\n\n{self.lp_string()}"
            )

        self._snapshot_solution()
        objective_value = float(self.solver.getObjectiveValue())
        requested_values = {
            name: self._last_solution_values[name]
            for name in variable_names
        }
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
        prospective_lb = float(lb) if lb is not None else variable.lb()
        prospective_ub = float(ub) if ub is not None else variable.ub()

        if prospective_lb > prospective_ub:
            small_error = isclose(
                prospective_lb - prospective_ub,
                0,
                abs_tol=0.0001,
            )
            if small_error and ub is None:
                prospective_lb = variable.ub()
            elif small_error and lb is None:
                prospective_ub = variable.lb()
            else:
                raise ValueError(
                    f"Cannot update bounds for {name}: "
                    f"{prospective_lb} > {prospective_ub}"
                )

        # Preserve the GLOP wrapper's numerical-noise behavior: when a new
        # lower bound is effectively the existing upper bound, fix the variable.
        if (
            lb is not None
            and variable.ub() != inf
            and isclose(prospective_lb, variable.ub(), abs_tol=0.0001)
            and variable.ub() > prospective_lb
        ):
            prospective_ub = prospective_lb

        self._set_variable_bounds(variable, prospective_lb, prospective_ub)

    def update_constraint_ub(self, name: str, ub: float | None = None) -> None:
        value = self._normalize_ub(ub)
        constraint = self.cons[name]
        if constraint.lb() > value:
            raise ValueError(
                f"Constraint {name} has lb > new ub: {constraint.lb()} > {value}"
            )
        self._set_constraint_bounds(constraint, constraint.lb(), value)

    def update_constraint_lb(self, name: str, lb: float | None = None) -> None:
        value = self._normalize_lb(lb)
        constraint = self.cons[name]
        if value > constraint.ub():
            raise ValueError(
                f"Constraint {name} has new lb > ub: {value} > {constraint.ub()}"
            )
        self._set_constraint_bounds(constraint, value, constraint.ub())

    def get_constraint_names(self) -> list[str]:
        return list(self.cons)

    @staticmethod
    def _format_linear_expression(coefficients: dict[str, float]) -> str:
        if not coefficients:
            return "0"
        return " ".join(
            f"{coefficient:+g} {name}"
            for name, coefficient in coefficients.items()
        ).lstrip("+")

    def lp_string(self) -> str:
        """Return a readable LP-like representation of the persistent model."""
        sense = "Maximize" if self._objective_maximization else "Minimize"
        objective = self._format_linear_expression(self._objective_costs)
        lines = [sense, f" Obj: {objective}", "Subject To"]
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
        merge_variable.SetBounds(0.0, inf)

        merge_constraints: list[_Constraint] = []
        for variable_name in variable_names:
            factor = float(proportion_factors[variable_name])
            self._validate_number(factor, f"Proportion factor for {variable_name}")

            constraint_name = "combined_" + variable_name
            if constraint_name not in self.cons:
                self.add_constraint(constraint_name)
            constraint = self.cons[constraint_name]
            constraint.Clear()
            constraint.SetBounds(self.vars[variable_name].lb(), inf)
            constraint.SetCoefficient(self.vars[variable_name], 1.0)
            constraint.SetCoefficient(merge_variable, -factor)
            merge_constraints.append(constraint)

        initial_values = {
            name: self.vars[name].lb()
            for name in variable_names
        }
        _, solved_values = self.solve_objective(
            [merge_variable_name],
            maximization=True,
        )
        solved_value = solved_values[merge_variable_name]

        # Preserve the last-solve evidence snapshots, then deactivate the
        # temporary rows and variable in the persistent model.
        for constraint in merge_constraints:
            constraint.Clear()
            constraint.SetBounds(-inf, inf)
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
                lower_slack,
                0,
                abs_tol=tolerance,
            )
            upper_is_tight = upper_slack is not None and isclose(
                upper_slack,
                0,
                abs_tol=tolerance,
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
        if coefficient == 0.0:
            return False
        activity = self._last_constraint_activities.get(constraint_name)
        if activity is None:
            return False
        if coefficient > 0:
            return constraint.ub() != inf and isclose(
                activity,
                constraint.ub(),
                abs_tol=1e-4,
            )
        return constraint.lb() != -inf and isclose(
            activity,
            constraint.lb(),
            abs_tol=1e-4,
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
            for constraint_name in self._variable_constraints.get(variable_name, [])
        )

    def set_perminant_minus_var(self, minus_var_name: str | None):
        self.perminant_minus_var = minus_var_name

    def has_variable(self, name: str) -> bool:
        """Return whether a variable has been added to the model."""
        return name in self.vars
