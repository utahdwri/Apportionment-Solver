"""Frozen execution IR for the parameterized v2 compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix

from ..lp_solver import LPSolverError
from .model import CompilerModel, ParamExpr, SymbolicExpr


@dataclass
class ProgramResult:
    objective_value: float
    requested_values: dict[str, float]
    active_values: dict[str, float]


@dataclass
class V2Program:
    name: str
    requested: tuple[str, ...]
    objective: SymbolicExpr
    maximization: bool
    source_variable_count: int
    active_variable_count: int
    stats: dict[str, int] = field(default_factory=dict)

    def execute(self, engine) -> ProgramResult:
        raise NotImplementedError

    def text(self) -> str:
        raise NotImplementedError


@dataclass
class DirectScalarProgram(V2Program):
    model: CompilerModel = field(default=None)  # type: ignore[assignment]
    active_name: str = ""

    def _interval(self, parameters: dict[str, float]) -> tuple[float, float]:
        variable = self.model.variables[self.active_name]
        lower = -inf if variable.lower is None else variable.lower.evaluate(parameters)
        upper = inf if variable.upper is None else variable.upper.evaluate(parameters)
        for constraint in self.model.constraints.values():
            coefficient = constraint.coefficients.get(self.active_name, 0.0)
            other = {
                name: value
                for name, value in constraint.coefficients.items()
                if name != self.active_name and abs(value) > 1e-12
            }
            if other:
                raise ValueError("DirectScalarProgram received coupled row")
            if abs(coefficient) <= 1e-12:
                # Empty parameterized rows are feasibility guards.
                if constraint.lower is not None and constraint.lower.evaluate(parameters) > 1e-8:
                    raise LPSolverError(f"v2 parameter guard failed: {constraint.name} lower > 0")
                if constraint.upper is not None and constraint.upper.evaluate(parameters) < -1e-8:
                    raise LPSolverError(f"v2 parameter guard failed: {constraint.name} upper < 0")
                continue
            if coefficient > 0:
                if constraint.lower is not None:
                    lower = max(lower, constraint.lower.evaluate(parameters) / coefficient)
                if constraint.upper is not None:
                    upper = min(upper, constraint.upper.evaluate(parameters) / coefficient)
            else:
                if constraint.lower is not None:
                    upper = min(upper, constraint.lower.evaluate(parameters) / coefficient)
                if constraint.upper is not None:
                    lower = max(lower, constraint.upper.evaluate(parameters) / coefficient)
        return lower, upper

    def execute(self, engine) -> ProgramResult:
        parameters = self.model.runtime_parameters(engine)
        self.model.check_guards(parameters)
        lower, upper = self._interval(parameters)
        scale = max(1.0, abs(lower) if np.isfinite(lower) else 1.0, abs(upper) if np.isfinite(upper) else 1.0)
        if upper < lower - 1e-9 * scale:
            raise LPSolverError(f"v2 direct scalar interval is infeasible: {lower} > {upper}")
        if upper < lower:
            middle = 0.5 * (lower + upper)
            lower = upper = middle

        objective_coefficient = self.objective.variables[self.active_name]
        maximize_active = self.maximization == (objective_coefficient > 0)
        active_value = upper if maximize_active else lower
        active_values = {self.active_name: active_value}
        requested_values = {
            name: self.model.reconstruct(name, active_values, parameters)
            for name in self.requested
        }
        objective_value = self.objective.evaluate(active_values, parameters)
        return ProgramResult(objective_value, requested_values, active_values)

    @staticmethod
    def _bound_text(expr: ParamExpr | None) -> str | None:
        return None if expr is None else expr.text()

    def _candidate_text(self) -> tuple[list[str], list[str]]:
        variable = self.model.variables[self.active_name]
        upper_candidates: list[str] = []
        lower_candidates: list[str] = []
        upper = self._bound_text(variable.upper)
        lower = self._bound_text(variable.lower)
        if upper is not None:
            upper_candidates.append(upper)
        if lower is not None:
            lower_candidates.append(lower)

        for constraint in self.model.constraints.values():
            coefficient = constraint.coefficients.get(self.active_name, 0.0)
            if abs(coefficient) <= 1e-12:
                continue
            if coefficient > 0:
                if constraint.upper is not None:
                    upper_candidates.append(f"({constraint.upper.text()}) / ({coefficient:g})")
                if constraint.lower is not None:
                    lower_candidates.append(f"({constraint.lower.text()}) / ({coefficient:g})")
            else:
                if constraint.lower is not None:
                    upper_candidates.append(f"({constraint.lower.text()}) / ({coefficient:g})")
                if constraint.upper is not None:
                    lower_candidates.append(f"({constraint.upper.text()}) / ({coefficient:g})")
        return upper_candidates, lower_candidates

    def text(self) -> str:
        direction = "MAXIMIZE" if self.maximization else "MINIMIZE"
        upper_candidates, lower_candidates = self._candidate_text()
        objective_coefficient = self.objective.variables[self.active_name]
        maximize_active = self.maximization == (objective_coefficient > 0)
        chosen = upper_candidates if maximize_active else lower_candidates
        aggregate = "MIN" if maximize_active else "MAX"
        lines = [
            f"{self.name}: DIRECT {direction} {self.objective.text()}",
            f"    {self.active_name} = {aggregate}(",
        ]
        lines.extend(f"        {candidate}," for candidate in chosen)
        lines.append("    )")
        if self.model.guards:
            lines.append("    runtime guards:")
            lines.extend(f"        REQUIRE {description}" for _, description in self.model.guards)
        if self.model.notes:
            lines.append("    frozen compiler substitutions:")
            lines.extend(f"        {note}" for note in self.model.notes)
        return "\n".join(lines)


@dataclass
class ReducedLPProgram(V2Program):
    model: CompilerModel = field(default=None)  # type: ignore[assignment]
    _names: tuple[str, ...] = field(init=False, default=())
    _index: dict[str, int] = field(init=False, default_factory=dict)
    _objective_vector: np.ndarray = field(init=False, repr=False, default_factory=lambda: np.zeros(0))
    _ub_rows: list[dict[int, float]] = field(init=False, default_factory=list)
    _ub_rhs: list[ParamExpr] = field(init=False, default_factory=list)
    _eq_rows: list[dict[int, float]] = field(init=False, default_factory=list)
    _eq_rhs: list[ParamExpr] = field(init=False, default_factory=list)
    _bounds: list[tuple[ParamExpr | None, ParamExpr | None]] = field(init=False, default_factory=list)
    _A_ub: csr_matrix | None = field(init=False, default=None, repr=False)
    _A_eq: csr_matrix | None = field(init=False, default=None, repr=False)

    def __post_init__(self) -> None:
        self._names = tuple(self.model.variables)
        self._index = {name: i for i, name in enumerate(self._names)}
        self._objective_vector = np.zeros(len(self._names), dtype=float)
        for name, coefficient in self.objective.variables.items():
            self._objective_vector[self._index[name]] += coefficient

        for constraint in self.model.constraints.values():
            row = {
                self._index[name]: coefficient
                for name, coefficient in constraint.coefficients.items()
                if name in self._index and coefficient != 0
            }
            if (
                constraint.lower is not None
                and constraint.upper is not None
                and constraint.lower.equivalent(constraint.upper)
            ):
                self._eq_rows.append(row)
                self._eq_rhs.append(constraint.lower.copy())
            else:
                if constraint.upper is not None:
                    self._ub_rows.append(row)
                    self._ub_rhs.append(constraint.upper.copy())
                if constraint.lower is not None:
                    self._ub_rows.append({column: -value for column, value in row.items()})
                    self._ub_rhs.append(constraint.lower.scaled(-1.0))

        self._bounds = [
            (variable.lower, variable.upper)
            for variable in self.model.variables.values()
        ]
        self._A_ub = self._sparse(self._ub_rows, len(self._names))
        self._A_eq = self._sparse(self._eq_rows, len(self._names))

    @staticmethod
    def _sparse(rows: list[dict[int, float]], width: int) -> csr_matrix | None:
        if not rows:
            return None
        r: list[int] = []
        c: list[int] = []
        data: list[float] = []
        for row_index, row in enumerate(rows):
            for column, value in row.items():
                if value != 0:
                    r.append(row_index)
                    c.append(column)
                    data.append(value)
        return csr_matrix((data, (r, c)), shape=(len(rows), width), dtype=float)

    def execute(self, engine) -> ProgramResult:
        parameters = self.model.runtime_parameters(engine)
        self.model.check_guards(parameters)
        b_ub = np.asarray([expr.evaluate(parameters) for expr in self._ub_rhs], dtype=float) if self._ub_rhs else None
        b_eq = np.asarray([expr.evaluate(parameters) for expr in self._eq_rhs], dtype=float) if self._eq_rhs else None
        bounds = [
            (
                None if lower is None else lower.evaluate(parameters),
                None if upper is None else upper.evaluate(parameters),
            )
            for lower, upper in self._bounds
        ]
        c = -self._objective_vector if self.maximization else self._objective_vector
        result = linprog(
            c=c,
            A_ub=self._A_ub,
            b_ub=b_ub,
            A_eq=self._A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs-ds",
        )
        if not result.success:
            raise LPSolverError(
                "v2 frozen reduced LP kernel could not solve objective: "
                f"{result.status}: {result.message}"
            )
        active_values = {name: float(result.x[i]) for i, name in enumerate(self._names)}
        requested_values = {
            name: self.model.reconstruct(name, active_values, parameters)
            for name in self.requested
        }
        objective_value = self.objective.evaluate(active_values, parameters)
        return ProgramResult(objective_value, requested_values, active_values)

    def text(self) -> str:
        direction = "MAXIMIZE" if self.maximization else "MINIMIZE"
        lines = [
            f"{self.name}: FROZEN REDUCED LP {direction} {self.objective.text()}",
            f"    source variables: {self.source_variable_count}",
            f"    active variables after v2 presolve: {self.active_variable_count}",
            f"    active constraints: {len(self.model.constraints)}",
            "    coefficient matrix is frozen; only parameterized bounds/RHS are refreshed at runtime",
        ]
        if self.model.guards:
            lines.append("    runtime guards:")
            lines.extend(f"        REQUIRE {description}" for _, description in self.model.guards)
        if self.model.notes:
            lines.append("    frozen compiler substitutions:")
            lines.extend(f"        {note}" for note in self.model.notes)
        return "\n".join(lines)
