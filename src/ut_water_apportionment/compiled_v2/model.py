"""Compiler-owned linear model used by the v2 experimental backend.

The production :class:`Apportioner` remains the source of the LP.  V2 copies
that LP into this representation before each objective so compiler passes are
free to substitute variables, shift bounds, and drop rows without mutating the
problem definition used by the accounting code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isfinite


@dataclass
class LinearExpr:
    """Small affine expression over active compiler variables."""

    coefficients: dict[str, float] = field(default_factory=dict)
    constant: float = 0.0

    @classmethod
    def variable(cls, name: str, coefficient: float = 1.0) -> "LinearExpr":
        return cls({name: coefficient}, 0.0)

    @classmethod
    def constant_value(cls, value: float) -> "LinearExpr":
        return cls({}, float(value))

    def copy(self) -> "LinearExpr":
        return LinearExpr(dict(self.coefficients), self.constant)

    def add_scaled(self, other: "LinearExpr", scale: float) -> None:
        self.constant += scale * other.constant
        for name, coefficient in other.coefficients.items():
            value = self.coefficients.get(name, 0.0) + scale * coefficient
            if abs(value) <= 1e-15:
                self.coefficients.pop(name, None)
            else:
                self.coefficients[name] = value

    def scaled(self, scale: float) -> "LinearExpr":
        return LinearExpr(
            {
                name: coefficient * scale
                for name, coefficient in self.coefficients.items()
                if coefficient * scale != 0.0
            },
            self.constant * scale,
        )

    def evaluate(self, values: dict[str, float]) -> float:
        return self.constant + sum(
            coefficient * values[name]
            for name, coefficient in self.coefficients.items()
        )

    def text(self) -> str:
        pieces: list[str] = []
        for name, coefficient in sorted(self.coefficients.items()):
            if coefficient == 1:
                pieces.append(name)
            elif coefficient == -1:
                pieces.append(f"-{name}")
            else:
                pieces.append(f"({coefficient:g})*{name}")
        if self.constant or not pieces:
            pieces.append(f"{self.constant:g}")
        return " + ".join(pieces).replace("+ -", "- ")


@dataclass
class LinearVariable:
    name: str
    lower: float = 0.0
    upper: float = inf

    def copy(self) -> "LinearVariable":
        return LinearVariable(self.name, self.lower, self.upper)


@dataclass
class LinearConstraint:
    name: str
    lower: float = -inf
    upper: float = inf
    coefficients: dict[str, float] = field(default_factory=dict)

    def copy(self) -> "LinearConstraint":
        return LinearConstraint(
            self.name,
            self.lower,
            self.upper,
            dict(self.coefficients),
        )


@dataclass
class CompilerModel:
    """Independent copy of one current production LP state."""

    variables: dict[str, LinearVariable]
    constraints: dict[str, LinearConstraint]
    reconstruction: dict[str, LinearExpr]
    source_variable_count: int
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_engine(cls, engine) -> "CompilerModel":
        variables = {
            name: LinearVariable(name, float(variable.lb()), float(variable.ub()))
            for name, variable in engine.vars.items()
        }
        constraints: dict[str, LinearConstraint] = {}
        for name, constraint in engine.cons.items():
            if hasattr(constraint, "coefficients"):
                coefficients = {
                    variable: float(value)
                    for variable, value in constraint.coefficients.items()
                    if value != 0
                }
            else:
                coefficients = {
                    variable: float(constraint.GetCoefficient(engine.vars[variable]))
                    for variable in variables
                    if constraint.GetCoefficient(engine.vars[variable]) != 0
                }
            constraints[name] = LinearConstraint(
                name,
                float(constraint.lb()),
                float(constraint.ub()),
                coefficients,
            )
        return cls(
            variables=variables,
            constraints=constraints,
            reconstruction={
                name: LinearExpr.variable(name) for name in variables
            },
            source_variable_count=len(variables),
        )

    def copy(self) -> "CompilerModel":
        return CompilerModel(
            variables={name: variable.copy() for name, variable in self.variables.items()},
            constraints={name: constraint.copy() for name, constraint in self.constraints.items()},
            reconstruction={name: expr.copy() for name, expr in self.reconstruction.items()},
            source_variable_count=self.source_variable_count,
            notes=list(self.notes),
        )

    def variable_occurrences(self, name: str) -> int:
        return sum(name in constraint.coefficients for constraint in self.constraints.values())

    def fix_variable(self, name: str, value: float, *, reason: str) -> None:
        """Substitute a numeric value and remove the variable."""

        if name not in self.variables:
            return
        for constraint in self.constraints.values():
            coefficient = constraint.coefficients.pop(name, 0.0)
            if coefficient == 0.0:
                continue
            shift = coefficient * value
            if isfinite(constraint.lower):
                constraint.lower -= shift
            if isfinite(constraint.upper):
                constraint.upper -= shift
        replacement = LinearExpr.constant_value(value)
        self._replace_in_reconstruction(name, replacement)
        self.variables.pop(name, None)
        self.notes.append(f"{name} := {value:g} ({reason})")

    def substitute_variable(
        self,
        name: str,
        replacement: LinearExpr,
        *,
        reason: str,
    ) -> None:
        """Replace ``name`` by an affine expression everywhere."""

        if name not in self.variables:
            return
        for constraint in self.constraints.values():
            coefficient = constraint.coefficients.pop(name, 0.0)
            if coefficient == 0.0:
                continue
            if replacement.constant:
                shift = coefficient * replacement.constant
                if isfinite(constraint.lower):
                    constraint.lower -= shift
                if isfinite(constraint.upper):
                    constraint.upper -= shift
            for other_name, other_coefficient in replacement.coefficients.items():
                value = (
                    constraint.coefficients.get(other_name, 0.0)
                    + coefficient * other_coefficient
                )
                if abs(value) <= 1e-15:
                    constraint.coefficients.pop(other_name, None)
                else:
                    constraint.coefficients[other_name] = value
        self._replace_in_reconstruction(name, replacement)
        self.variables.pop(name, None)
        self.notes.append(f"{name} := {replacement.text()} ({reason})")

    def _replace_in_reconstruction(self, name: str, replacement: LinearExpr) -> None:
        for original_name, expression in self.reconstruction.items():
            coefficient = expression.coefficients.pop(name, 0.0)
            if coefficient:
                expression.add_scaled(replacement, coefficient)

    def remove_constraint(self, name: str) -> None:
        self.constraints.pop(name, None)

    def reconstruct(self, name: str, values: dict[str, float]) -> float:
        return self.reconstruction[name].evaluate(values)

    def objective_expression(
        self,
        variable_names: list[str],
        weights: dict[str, float] | None = None,
    ) -> LinearExpr:
        weights = weights or {}
        expression = LinearExpr()
        for name in variable_names:
            expression.add_scaled(
                self.reconstruction[name],
                weights.get(name, 1.0),
            )
        return expression
