"""Execution IR for the v2 experimental compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isfinite

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix

from ..lp_solver import LPSolverError
from .model import CompilerModel, LinearExpr


@dataclass
class ProgramResult:
    objective_value: float
    requested_values: dict[str, float]
    active_values: dict[str, float]


@dataclass
class V2Program:
    name: str
    requested: tuple[str, ...]
    objective: LinearExpr
    maximization: bool
    source_variable_count: int
    active_variable_count: int
    stats: dict[str, int] = field(default_factory=dict)

    def execute(self) -> ProgramResult:
        raise NotImplementedError

    def text(self) -> str:
        raise NotImplementedError


@dataclass
class DirectScalarProgram(V2Program):
    model: CompilerModel = field(default=None)  # type: ignore[assignment]
    active_name: str = ""

    def _interval(self) -> tuple[float, float]:
        variable = self.model.variables[self.active_name]
        lower = variable.lower
        upper = variable.upper
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
                continue
            if coefficient > 0:
                if isfinite(constraint.lower):
                    lower = max(lower, constraint.lower / coefficient)
                if isfinite(constraint.upper):
                    upper = min(upper, constraint.upper / coefficient)
            else:
                if isfinite(constraint.lower):
                    upper = min(upper, constraint.lower / coefficient)
                if isfinite(constraint.upper):
                    lower = max(lower, constraint.upper / coefficient)
        return lower, upper

    def execute(self) -> ProgramResult:
        lower, upper = self._interval()
        if upper < lower - 1e-8:
            raise LPSolverError(
                f"v2 direct scalar interval is infeasible: {lower} > {upper}"
            )
        objective_coefficient = self.objective.coefficients[self.active_name]
        maximize_active = self.maximization == (objective_coefficient > 0)
        active_value = upper if maximize_active else lower
        active_values = {self.active_name: active_value}
        requested_values = {
            name: self.model.reconstruct(name, active_values)
            for name in self.requested
        }
        objective_value = self.objective.evaluate(active_values)
        return ProgramResult(objective_value, requested_values, active_values)

    def _candidate_text(self) -> tuple[list[str], list[str]]:
        variable = self.model.variables[self.active_name]
        upper_candidates: list[str] = []
        lower_candidates: list[str] = []
        if isfinite(variable.upper):
            upper_candidates.append(
                f"variable[{self.active_name}].remaining_upper (= {variable.upper:g})"
            )
        if isfinite(variable.lower):
            lower_candidates.append(
                f"variable[{self.active_name}].lower (= {variable.lower:g})"
            )

        for constraint in self.model.constraints.values():
            coefficient = constraint.coefficients.get(self.active_name, 0.0)
            if abs(coefficient) <= 1e-12:
                continue
            if coefficient > 0:
                if isfinite(constraint.upper):
                    upper_candidates.append(
                        f"constraint[{constraint.name}].remaining_upper / "
                        f"({coefficient:g}) (= {constraint.upper / coefficient:g})"
                    )
                if isfinite(constraint.lower):
                    lower_candidates.append(
                        f"constraint[{constraint.name}].remaining_lower / "
                        f"({coefficient:g}) (= {constraint.lower / coefficient:g})"
                    )
            else:
                if isfinite(constraint.lower):
                    upper_candidates.append(
                        f"constraint[{constraint.name}].remaining_lower / "
                        f"({coefficient:g}) (= {constraint.lower / coefficient:g})"
                    )
                if isfinite(constraint.upper):
                    lower_candidates.append(
                        f"constraint[{constraint.name}].remaining_upper / "
                        f"({coefficient:g}) (= {constraint.upper / coefficient:g})"
                    )
        return upper_candidates, lower_candidates

    def text(self) -> str:
        direction = "MAXIMIZE" if self.maximization else "MINIMIZE"
        upper_candidates, lower_candidates = self._candidate_text()
        objective_coefficient = self.objective.coefficients[self.active_name]
        maximize_active = self.maximization == (objective_coefficient > 0)
        chosen = upper_candidates if maximize_active else lower_candidates
        aggregate = "MIN" if maximize_active else "MAX"
        lines = [
            f"{self.name}: DIRECT {direction} {self.objective.text()}",
            f"    {self.active_name} = {aggregate}("
        ]
        lines.extend(f"        {candidate}," for candidate in chosen)
        lines.append("    )")
        if self.model.notes:
            lines.append("    compiler substitutions:")
            lines.extend(f"        {note}" for note in self.model.notes)
        return "\n".join(lines)


@dataclass
class ReducedLPProgram(V2Program):
    model: CompilerModel = field(default=None)  # type: ignore[assignment]

    @staticmethod
    def _sparse(rows: list[dict[int, float]], width: int):
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

    def execute(self) -> ProgramResult:
        names = list(self.model.variables)
        index = {name: i for i, name in enumerate(names)}
        objective = np.zeros(len(names), dtype=float)
        for name, coefficient in self.objective.coefficients.items():
            objective[index[name]] += coefficient

        ub_rows: list[dict[int, float]] = []
        ub_rhs: list[float] = []
        eq_rows: list[dict[int, float]] = []
        eq_rhs: list[float] = []
        for constraint in self.model.constraints.values():
            row = {
                index[name]: coefficient
                for name, coefficient in constraint.coefficients.items()
                if name in index and coefficient != 0
            }
            if (
                isfinite(constraint.lower)
                and isfinite(constraint.upper)
                and abs(constraint.lower - constraint.upper) <= 1e-12
            ):
                eq_rows.append(row)
                eq_rhs.append(constraint.lower)
            else:
                if isfinite(constraint.upper):
                    ub_rows.append(row)
                    ub_rhs.append(constraint.upper)
                if isfinite(constraint.lower):
                    ub_rows.append({column: -value for column, value in row.items()})
                    ub_rhs.append(-constraint.lower)

        bounds = [
            (
                None if variable.lower == -inf else variable.lower,
                None if variable.upper == inf else variable.upper,
            )
            for variable in self.model.variables.values()
        ]
        c = -objective if self.maximization else objective
        result = linprog(
            c=c,
            A_ub=self._sparse(ub_rows, len(names)),
            b_ub=np.asarray(ub_rhs, dtype=float) if ub_rhs else None,
            A_eq=self._sparse(eq_rows, len(names)),
            b_eq=np.asarray(eq_rhs, dtype=float) if eq_rhs else None,
            bounds=bounds,
            method="highs-ds",
        )
        if not result.success:
            raise LPSolverError(
                "v2 reduced LP kernel could not solve objective: "
                f"{result.status}: {result.message}"
            )
        active_values = {
            name: float(result.x[i]) for i, name in enumerate(names)
        }
        requested_values = {
            name: self.model.reconstruct(name, active_values)
            for name in self.requested
        }
        objective_value = self.objective.constant + float(objective @ result.x)
        return ProgramResult(objective_value, requested_values, active_values)

    def text(self) -> str:
        direction = "MAXIMIZE" if self.maximization else "MINIMIZE"
        lines = [
            f"{self.name}: REDUCED LP {direction} {self.objective.text()}",
            f"    source variables: {self.source_variable_count}",
            f"    active variables after v2 presolve: {self.active_variable_count}",
            f"    active constraints: {len(self.model.constraints)}",
        ]
        if self.model.notes:
            lines.append("    compiler substitutions:")
            lines.extend(f"        {note}" for note in self.model.notes)
        return "\n".join(lines)
