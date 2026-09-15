"""Parameterized compiler-owned linear model for the v2 backend.

The production LP remains the authoritative problem definition.  V2 copies its
coefficient structure once, but represents mutable variable/constraint bounds
as named parameter slots.  Compiler transformations therefore produce a frozen
IR whose coefficients/substitutions are reusable across days while the numeric
bounds/RHS values are supplied at execution time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isfinite
from typing import Mapping


ZERO_TOL = 1e-15


@dataclass
class ParamExpr:
    """Affine expression over runtime LP-bound parameters."""

    coefficients: dict[str, float] = field(default_factory=dict)
    constant: float = 0.0

    @classmethod
    def constant_value(cls, value: float) -> "ParamExpr":
        return cls({}, float(value))

    @classmethod
    def slot(cls, name: str, coefficient: float = 1.0) -> "ParamExpr":
        return cls({name: coefficient}, 0.0)

    def copy(self) -> "ParamExpr":
        return ParamExpr(dict(self.coefficients), self.constant)

    def add_scaled(self, other: "ParamExpr", scale: float) -> None:
        self.constant += scale * other.constant
        for name, coefficient in other.coefficients.items():
            value = self.coefficients.get(name, 0.0) + scale * coefficient
            if abs(value) <= ZERO_TOL:
                self.coefficients.pop(name, None)
            else:
                self.coefficients[name] = value

    def shifted(self, other: "ParamExpr", scale: float) -> "ParamExpr":
        result = self.copy()
        result.add_scaled(other, scale)
        return result

    def scaled(self, scale: float) -> "ParamExpr":
        return ParamExpr(
            {
                name: coefficient * scale
                for name, coefficient in self.coefficients.items()
                if abs(coefficient * scale) > ZERO_TOL
            },
            self.constant * scale,
        )

    def evaluate(self, parameters: Mapping[str, float]) -> float:
        return self.constant + sum(
            coefficient * parameters[name]
            for name, coefficient in self.coefficients.items()
        )

    def is_constant(self, value: float | None = None, *, tol: float = 1e-12) -> bool:
        if self.coefficients:
            return False
        return value is None or abs(self.constant - value) <= tol

    def equivalent(self, other: "ParamExpr", *, tol: float = 1e-12) -> bool:
        if abs(self.constant - other.constant) > tol:
            return False
        names = set(self.coefficients) | set(other.coefficients)
        return all(
            abs(self.coefficients.get(name, 0.0) - other.coefficients.get(name, 0.0)) <= tol
            for name in names
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
class SymbolicExpr:
    """Affine expression over active variables and runtime parameters."""

    variables: dict[str, float] = field(default_factory=dict)
    parameters: ParamExpr = field(default_factory=ParamExpr)

    @classmethod
    def variable(cls, name: str, coefficient: float = 1.0) -> "SymbolicExpr":
        return cls({name: coefficient}, ParamExpr())

    @classmethod
    def parameter(cls, value: ParamExpr) -> "SymbolicExpr":
        return cls({}, value.copy())

    def copy(self) -> "SymbolicExpr":
        return SymbolicExpr(dict(self.variables), self.parameters.copy())

    def add_scaled(self, other: "SymbolicExpr", scale: float) -> None:
        self.parameters.add_scaled(other.parameters, scale)
        for name, coefficient in other.variables.items():
            value = self.variables.get(name, 0.0) + scale * coefficient
            if abs(value) <= ZERO_TOL:
                self.variables.pop(name, None)
            else:
                self.variables[name] = value

    def evaluate(self, values: Mapping[str, float], parameters: Mapping[str, float]) -> float:
        return self.parameters.evaluate(parameters) + sum(
            coefficient * values[name]
            for name, coefficient in self.variables.items()
        )

    def text(self) -> str:
        pieces: list[str] = []
        for name, coefficient in sorted(self.variables.items()):
            if coefficient == 1:
                pieces.append(name)
            elif coefficient == -1:
                pieces.append(f"-{name}")
            else:
                pieces.append(f"({coefficient:g})*{name}")
        parameter_text = self.parameters.text()
        if self.parameters.coefficients or abs(self.parameters.constant) > ZERO_TOL or not pieces:
            pieces.append(parameter_text)
        return " + ".join(pieces).replace("+ -", "- ")


@dataclass
class ParametricVariable:
    name: str
    lower: ParamExpr | None = None  # None => -infinity
    upper: ParamExpr | None = None  # None => +infinity

    def copy(self) -> "ParametricVariable":
        return ParametricVariable(
            self.name,
            None if self.lower is None else self.lower.copy(),
            None if self.upper is None else self.upper.copy(),
        )


@dataclass
class ParametricConstraint:
    name: str
    lower: ParamExpr | None = None
    upper: ParamExpr | None = None
    coefficients: dict[str, float] = field(default_factory=dict)

    def copy(self) -> "ParametricConstraint":
        return ParametricConstraint(
            self.name,
            None if self.lower is None else self.lower.copy(),
            None if self.upper is None else self.upper.copy(),
            dict(self.coefficients),
        )


def variable_slot(name: str, side: str) -> str:
    return f"variable[{name}].remaining_{side}"


def constraint_slot(name: str, side: str) -> str:
    return f"constraint[{name}].remaining_{side}"


def _dynamic_side(engine, kind: str, name: str, side: str) -> bool:
    dynamic = getattr(engine, "_v2_dynamic_bound_sides", set())
    return (kind, name, side) in dynamic


def _bound_expr(
    engine,
    *,
    kind: str,
    name: str,
    side: str,
    value: float,
) -> ParamExpr | None:
    """Convert one LP bound to either a constant or a runtime parameter slot."""

    if not isfinite(value):
        return None

    # Bounds changed through the LP protocol are true runtime state even when
    # their current value happens to be zero.
    if _dynamic_side(engine, kind, name, side):
        slot = variable_slot(name, side) if kind == "variable" else constraint_slot(name, side)
        return ParamExpr.slot(slot)

    # Nonzero constructor-time bounds can depend on cross-day account state or
    # other input-derived quantities. Parameterize them conservatively.
    if abs(value) > 1e-15:
        slot = variable_slot(name, side) if kind == "variable" else constraint_slot(name, side)
        return ParamExpr.slot(slot)

    # Unmodified zero bounds are structural (continuity equations, nonnegative
    # variables, etc.) and are safe constants.
    return ParamExpr.constant_value(0.0)


@dataclass
class CompilerModel:
    """Frozen, parameterized copy of one production-LP structural state."""

    variables: dict[str, ParametricVariable]
    constraints: dict[str, ParametricConstraint]
    reconstruction: dict[str, SymbolicExpr]
    source_variable_count: int
    parameter_defaults: dict[str, float]
    notes: list[str] = field(default_factory=list)
    guards: list[tuple[ParamExpr, str]] = field(default_factory=list)

    @classmethod
    def from_engine(cls, engine) -> "CompilerModel":
        defaults: dict[str, float] = {}

        variables: dict[str, ParametricVariable] = {}
        for name, variable in engine.vars.items():
            lb = float(variable.lb())
            ub = float(variable.ub())
            lower = _bound_expr(engine, kind="variable", name=name, side="lower", value=lb)
            upper = _bound_expr(engine, kind="variable", name=name, side="upper", value=ub)
            if lower is not None:
                for slot in lower.coefficients:
                    defaults[slot] = lb
            if upper is not None:
                for slot in upper.coefficients:
                    defaults[slot] = ub
            variables[name] = ParametricVariable(name, lower, upper)

        constraints: dict[str, ParametricConstraint] = {}
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
            lb = float(constraint.lb())
            ub = float(constraint.ub())
            if (
                isfinite(lb) and isfinite(ub) and abs(lb - ub) <= 1e-12
                and (
                    _dynamic_side(engine, "constraint", name, "lower")
                    or _dynamic_side(engine, "constraint", name, "upper")
                    or abs(lb) > 1e-15
                )
            ):
                shared = ParamExpr.slot(f"constraint[{name}].remaining")
                lower = shared.copy()
                upper = shared.copy()
            else:
                lower = _bound_expr(engine, kind="constraint", name=name, side="lower", value=lb)
                upper = _bound_expr(engine, kind="constraint", name=name, side="upper", value=ub)
            if lower is not None:
                for slot in lower.coefficients:
                    defaults[slot] = lb
            if upper is not None:
                for slot in upper.coefficients:
                    defaults[slot] = ub
            constraints[name] = ParametricConstraint(
                name,
                lower,
                upper,
                coefficients,
            )

        return cls(
            variables=variables,
            constraints=constraints,
            reconstruction={name: SymbolicExpr.variable(name) for name in variables},
            source_variable_count=len(variables),
            parameter_defaults=defaults,
        )

    def copy(self) -> "CompilerModel":
        return CompilerModel(
            variables={name: variable.copy() for name, variable in self.variables.items()},
            constraints={name: constraint.copy() for name, constraint in self.constraints.items()},
            reconstruction={name: expr.copy() for name, expr in self.reconstruction.items()},
            source_variable_count=self.source_variable_count,
            parameter_defaults=dict(self.parameter_defaults),
            notes=list(self.notes),
            guards=[(expr.copy(), text) for expr, text in self.guards],
        )

    def variable_occurrences(self, name: str) -> int:
        return sum(name in constraint.coefficients for constraint in self.constraints.values())

    def fix_variable(self, name: str, value: ParamExpr, *, reason: str) -> None:
        if name not in self.variables:
            return
        for constraint in self.constraints.values():
            coefficient = constraint.coefficients.pop(name, 0.0)
            if coefficient == 0.0:
                continue
            if constraint.lower is not None:
                constraint.lower.add_scaled(value, -coefficient)
            if constraint.upper is not None:
                constraint.upper.add_scaled(value, -coefficient)
        replacement = SymbolicExpr.parameter(value)
        self._replace_in_reconstruction(name, replacement)
        self.variables.pop(name, None)
        self.notes.append(f"{name} := {value.text()} ({reason})")

    def substitute_variable(
        self,
        name: str,
        replacement: SymbolicExpr,
        *,
        reason: str,
    ) -> None:
        if name not in self.variables:
            return
        for constraint in self.constraints.values():
            coefficient = constraint.coefficients.pop(name, 0.0)
            if coefficient == 0.0:
                continue
            if constraint.lower is not None:
                constraint.lower.add_scaled(replacement.parameters, -coefficient)
            if constraint.upper is not None:
                constraint.upper.add_scaled(replacement.parameters, -coefficient)
            for other_name, other_coefficient in replacement.variables.items():
                value = constraint.coefficients.get(other_name, 0.0) + coefficient * other_coefficient
                if abs(value) <= ZERO_TOL:
                    constraint.coefficients.pop(other_name, None)
                else:
                    constraint.coefficients[other_name] = value
        self._replace_in_reconstruction(name, replacement)
        self.variables.pop(name, None)
        self.notes.append(f"{name} := {replacement.text()} ({reason})")

    def _replace_in_reconstruction(self, name: str, replacement: SymbolicExpr) -> None:
        for expression in self.reconstruction.values():
            coefficient = expression.variables.pop(name, 0.0)
            if coefficient:
                expression.add_scaled(replacement, coefficient)

    def remove_constraint(self, name: str) -> None:
        self.constraints.pop(name, None)

    def reconstruct(
        self,
        name: str,
        values: Mapping[str, float],
        parameters: Mapping[str, float],
    ) -> float:
        return self.reconstruction[name].evaluate(values, parameters)

    def objective_expression(
        self,
        variable_names: list[str],
        weights: dict[str, float] | None = None,
    ) -> SymbolicExpr:
        weights = weights or {}
        expression = SymbolicExpr()
        for name in variable_names:
            expression.add_scaled(self.reconstruction[name], weights.get(name, 1.0))
        return expression

    def add_guard(self, expression: ParamExpr, description: str) -> None:
        self.guards.append((expression, description))

    def check_guards(self, parameters: Mapping[str, float], *, tolerance: float = 1e-9) -> None:
        for expression, description in self.guards:
            value = expression.evaluate(parameters)
            scale = max(1.0, abs(value))
            if value < -tolerance * scale:
                raise ValueError(
                    f"Frozen v2 parameter guard failed ({description}): {value:g}"
                )

    def runtime_parameters(self, engine) -> dict[str, float]:
        """Read only the slots used by this frozen transformed model."""

        result: dict[str, float] = {}
        slots: set[str] = set(self.parameter_defaults)
        for variable in self.variables.values():
            for bound in (variable.lower, variable.upper):
                if bound is not None:
                    slots.update(bound.coefficients)
        for constraint in self.constraints.values():
            for bound in (constraint.lower, constraint.upper):
                if bound is not None:
                    slots.update(bound.coefficients)
        for expression in self.reconstruction.values():
            slots.update(expression.parameters.coefficients)

        for slot in slots:
            if slot.startswith("variable["):
                prefix, side = slot.rsplit("].", 1)
                name = prefix[len("variable["):]
                lb, ub = engine.get_variable_bounds(name)
                if side == "current":
                    if abs(lb - ub) > 1e-8:
                        raise ValueError(f"Frozen v2 program expected {name} to remain fixed")
                    result[slot] = 0.5 * (lb + ub)
                elif side == "remaining_lower":
                    result[slot] = lb
                elif side == "remaining_upper":
                    result[slot] = ub
                else:
                    raise KeyError(f"Unknown variable parameter side: {side}")
            elif slot.startswith("constraint["):
                prefix, side = slot.rsplit("].", 1)
                name = prefix[len("constraint["):]
                lb, ub = engine.get_constraint_bounds(name)
                if side == "remaining":
                    if abs(lb - ub) > 1e-8:
                        raise ValueError(f"Frozen v2 program expected {name} to remain an equality")
                    result[slot] = 0.5 * (lb + ub)
                elif side == "remaining_lower":
                    result[slot] = lb
                elif side == "remaining_upper":
                    result[slot] = ub
                else:
                    raise KeyError(f"Unknown constraint parameter side: {side}")
            else:
                result[slot] = self.parameter_defaults[slot]
        return result
