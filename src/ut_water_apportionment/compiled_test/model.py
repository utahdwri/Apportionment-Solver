"""Parameterized compiler-owned linear model for the v2 backend.

The production LP remains the authoritative problem definition. V2 copies its
linear structure, but runtime-varying scalar values are represented by
``ParamExpr`` objects.  Parameters can appear in bounds/RHS values *and* in LP
matrix coefficients.  They are parameters, never optimization variables, so a
runtime instantiation is still an ordinary linear program.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isfinite
from typing import Mapping

import numpy as np


ZERO_TOL = 1e-15


@dataclass
class ParamExpr:
    """Scalar expression over runtime parameters.

    The original v2 implementation supported only affine parameter
    expressions.  Coefficient parameterization needs products such as
    ``(1-loss_1) * (1-loss_2)`` and, after exact equality substitution,
    quotients of parameter expressions.  ``ParamExpr`` therefore keeps its
    compact affine representation when possible and falls back to a tiny AST
    for add/multiply/divide operations.

    ``coefficients``/``constant`` are retained for the affine case so older
    compiler code can construct expressions cheaply.  Non-affine expressions
    have ``op != 'affine'`` and store child expressions in ``args``.
    """

    coefficients: dict[str, float] = field(default_factory=dict)
    constant: float = 0.0
    op: str = "affine"
    args: tuple["ParamExpr", ...] = field(default_factory=tuple)

    @classmethod
    def constant_value(cls, value: float) -> "ParamExpr":
        return cls({}, float(value))

    @classmethod
    def slot(cls, name: str, coefficient: float = 1.0) -> "ParamExpr":
        return cls({name: float(coefficient)}, 0.0)

    def copy(self) -> "ParamExpr":
        if self.op == "affine":
            return ParamExpr(dict(self.coefficients), self.constant)
        return ParamExpr({}, 0.0, self.op, tuple(arg.copy() for arg in self.args))

    def _assign(self, other: "ParamExpr") -> None:
        self.coefficients = dict(other.coefficients)
        self.constant = other.constant
        self.op = other.op
        self.args = tuple(arg.copy() for arg in other.args)

    def slots(self) -> set[str]:
        if self.op == "affine":
            return set(self.coefficients)
        result: set[str] = set()
        for arg in self.args:
            result.update(arg.slots())
        return result

    def rename_slot(self, old: str, new: str) -> None:
        if self.op == "affine":
            coefficient = self.coefficients.pop(old, 0.0)
            if coefficient:
                value = self.coefficients.get(new, 0.0) + coefficient
                if abs(value) <= ZERO_TOL:
                    self.coefficients.pop(new, None)
                else:
                    self.coefficients[new] = value
            return
        for arg in self.args:
            arg.rename_slot(old, new)

    def _key(self):
        if self.op == "affine":
            return (
                "affine",
                round(self.constant, 15),
                tuple(
                    sorted(
                        (name, round(value, 15))
                        for name, value in self.coefficients.items()
                        if abs(value) > ZERO_TOL
                    )
                ),
            )
        return (self.op, tuple(arg._key() for arg in self.args))

    def plus(self, other: "ParamExpr") -> "ParamExpr":
        if self.op == "affine" and other.op == "affine":
            result = self.copy()
            result.constant += other.constant
            for name, coefficient in other.coefficients.items():
                value = result.coefficients.get(name, 0.0) + coefficient
                if abs(value) <= ZERO_TOL:
                    result.coefficients.pop(name, None)
                else:
                    result.coefficients[name] = value
            return result
        if self.is_constant(0.0):
            return other.copy()
        if other.is_constant(0.0):
            return self.copy()
        return ParamExpr({}, 0.0, "add", (self.copy(), other.copy()))

    def multiplied(self, other: "ParamExpr") -> "ParamExpr":
        if self.is_constant(0.0) or other.is_constant(0.0):
            return ParamExpr.constant_value(0.0)
        if self.is_constant(1.0):
            return other.copy()
        if other.is_constant(1.0):
            return self.copy()
        if self.is_constant():
            return other.scaled(self.constant_value_number())
        if other.is_constant():
            return self.scaled(other.constant_value_number())
        return ParamExpr({}, 0.0, "mul", (self.copy(), other.copy()))

    def divided(self, other: "ParamExpr") -> "ParamExpr":
        if other.is_constant(1.0):
            return self.copy()
        if self.is_constant(0.0):
            return ParamExpr.constant_value(0.0)
        if other.is_constant():
            denominator = other.constant_value_number()
            if abs(denominator) <= ZERO_TOL:
                raise ZeroDivisionError("parameter expression division by zero")
            return self.scaled(1.0 / denominator)
        return ParamExpr({}, 0.0, "div", (self.copy(), other.copy()))

    def add_scaled(self, other: "ParamExpr", scale: float) -> None:
        self._assign(self.plus(other.scaled(scale)))

    def add_product(self, first: "ParamExpr", second: "ParamExpr", scale: float = 1.0) -> None:
        product = first.multiplied(second).scaled(scale)
        self._assign(self.plus(product))

    def shifted(self, other: "ParamExpr", scale: float) -> "ParamExpr":
        return self.plus(other.scaled(scale))

    def scaled(self, scale: float) -> "ParamExpr":
        scale = float(scale)
        if abs(scale) <= ZERO_TOL:
            return ParamExpr.constant_value(0.0)
        if abs(scale - 1.0) <= ZERO_TOL:
            return self.copy()
        if self.op == "affine":
            return ParamExpr(
                {
                    name: coefficient * scale
                    for name, coefficient in self.coefficients.items()
                    if abs(coefficient * scale) > ZERO_TOL
                },
                self.constant * scale,
            )
        return ParamExpr({}, 0.0, "mul", (ParamExpr.constant_value(scale), self.copy()))

    def evaluate(self, parameters: Mapping[str, float]) -> float:
        if self.op == "affine":
            # This is one of the hottest paths in compiled daily execution.
            # Avoid a generator + sum allocation for the overwhelmingly common
            # affine case; most reduced-kernel expressions have only 0-2 slots.
            value = self.constant
            for name, coefficient in self.coefficients.items():
                value += coefficient * parameters[name]
            return value
        if self.op == "add":
            return self.args[0].evaluate(parameters) + self.args[1].evaluate(parameters)
        if self.op == "mul":
            return self.args[0].evaluate(parameters) * self.args[1].evaluate(parameters)
        if self.op == "div":
            return self.args[0].evaluate(parameters) / self.args[1].evaluate(parameters)
        raise ValueError(f"unknown ParamExpr op {self.op!r}")

    def constant_value_number(self) -> float:
        if self.slots():
            raise ValueError("expression is not constant")
        return self.evaluate({})

    def is_constant(self, value: float | None = None, *, tol: float = 1e-12) -> bool:
        if self.slots():
            return False
        constant = self.evaluate({})
        return value is None or abs(constant - value) <= tol

    def equivalent(self, other: "ParamExpr", *, tol: float = 1e-12) -> bool:
        if self.op == "affine" and other.op == "affine":
            if abs(self.constant - other.constant) > tol:
                return False
            names = set(self.coefficients) | set(other.coefficients)
            return all(
                abs(self.coefficients.get(name, 0.0) - other.coefficients.get(name, 0.0)) <= tol
                for name in names
            )
        return self._key() == other._key()

    def interval(
        self,
        domains: Mapping[str, tuple[float, float]],
        defaults: Mapping[str, float] | None = None,
    ) -> tuple[float, float]:
        """Conservative interval over declared parameter domains."""

        defaults = defaults or {}
        if self.op == "affine":
            low = self.constant
            high = self.constant
            for name, coefficient in self.coefficients.items():
                dlow, dhigh = domains.get(
                    name,
                    (defaults.get(name, -inf), defaults.get(name, inf)),
                )
                if coefficient >= 0:
                    low += coefficient * dlow
                    high += coefficient * dhigh
                else:
                    low += coefficient * dhigh
                    high += coefficient * dlow
            return low, high

        first = self.args[0].interval(domains, defaults)
        second = self.args[1].interval(domains, defaults)
        if self.op == "add":
            return first[0] + second[0], first[1] + second[1]
        if self.op == "mul":
            values = (
                first[0] * second[0],
                first[0] * second[1],
                first[1] * second[0],
                first[1] * second[1],
            )
            return min(values), max(values)
        if self.op == "div":
            if second[0] <= 0 <= second[1]:
                return -inf, inf
            values = (
                first[0] / second[0],
                first[0] / second[1],
                first[1] / second[0],
                first[1] / second[1],
            )
            return min(values), max(values)
        raise ValueError(f"unknown ParamExpr op {self.op!r}")

    def sign(
        self,
        domains: Mapping[str, tuple[float, float]],
        defaults: Mapping[str, float] | None = None,
        *,
        tol: float = 1e-12,
    ) -> int | None:
        """Return +1/-1/0 when sign is structurally known, else ``None``."""

        low, high = self.interval(domains, defaults)
        if low > tol:
            return 1
        if high < -tol:
            return -1
        if low >= -tol and high <= tol:
            return 0
        # Nonnegative/nonpositive expressions that can be zero still have a
        # useful monotonic sign for LP presolve.
        if low >= -tol:
            return 1
        if high <= tol:
            return -1
        return None

    def text(self) -> str:
        if self.op == "affine":
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
        left = self.args[0].text()
        right = self.args[1].text()
        if self.op == "add":
            return f"({left} + {right})"
        if self.op == "mul":
            return f"({left})*({right})"
        if self.op == "div":
            return f"({left})/({right})"
        raise ValueError(f"unknown ParamExpr op {self.op!r}")


@dataclass(frozen=True)
class IndexedParamExpr:
    """Runtime form of :class:`ParamExpr` over a contiguous parameter array.

    The named ``ParamExpr`` remains the compiler/debug representation. Frozen
    programs lower it once to integer parameter slots so daily execution does
    not perform string/dictionary lookup for every algebraic term.
    """

    op: str = "affine"
    constant: float = 0.0
    indices: tuple[int, ...] = ()
    coefficients: tuple[float, ...] = ()
    args: tuple["IndexedParamExpr", ...] = ()

    @classmethod
    def from_param_expr(
        cls,
        expression: "ParamExpr",
        slot_index: Mapping[str, int],
    ) -> "IndexedParamExpr":
        if expression.op == "affine":
            items = tuple(
                (slot_index[name], float(coefficient))
                for name, coefficient in expression.coefficients.items()
                if abs(coefficient) > ZERO_TOL
            )
            return cls(
                op="affine",
                constant=float(expression.constant),
                indices=tuple(index for index, _ in items),
                coefficients=tuple(value for _, value in items),
            )
        return cls(
            op=expression.op,
            args=tuple(
                cls.from_param_expr(arg, slot_index) for arg in expression.args
            ),
        )

    def evaluate(self, parameters: np.ndarray) -> float:
        if self.op == "affine":
            value = self.constant
            # Most frozen expressions contain zero, one, or two parameters.
            # A tiny Python loop is faster than allocating a NumPy gather/dot
            # for those common cases while still using contiguous storage.
            for index, coefficient in zip(self.indices, self.coefficients):
                value += coefficient * parameters[index]
            return float(value)
        if self.op == "add":
            return self.args[0].evaluate(parameters) + self.args[1].evaluate(parameters)
        if self.op == "mul":
            return self.args[0].evaluate(parameters) * self.args[1].evaluate(parameters)
        if self.op == "div":
            return self.args[0].evaluate(parameters) / self.args[1].evaluate(parameters)
        raise ValueError(f"unknown indexed ParamExpr op {self.op!r}")

    def python_text(self, *, array_name: str = "p") -> str:
        """Render executable Python over the indexed parameter array.

        This is intentionally part of the lowered representation so #10 can
        emit ordinary Python without re-parsing compiler slot names.
        """

        if self.op == "affine":
            pieces: list[str] = []
            if abs(self.constant) > ZERO_TOL or not self.indices:
                pieces.append(repr(float(self.constant)))
            for index, coefficient in zip(self.indices, self.coefficients):
                term = f"{array_name}[{index}]"
                if abs(coefficient - 1.0) <= ZERO_TOL:
                    pieces.append(term)
                elif abs(coefficient + 1.0) <= ZERO_TOL:
                    pieces.append(f"-{term}")
                else:
                    pieces.append(f"({coefficient!r} * {term})")
            return " + ".join(pieces).replace("+ -", "- ") or "0.0"
        left = self.args[0].python_text(array_name=array_name)
        right = self.args[1].python_text(array_name=array_name)
        symbol = {"add": "+", "mul": "*", "div": "/"}[self.op]
        return f"({left} {symbol} {right})"


@dataclass(frozen=True)
class IndexedGuardPredicate:
    lhs: IndexedParamExpr
    relation: str
    rhs: IndexedParamExpr
    description: str

    def margin(self, parameters: np.ndarray) -> float:
        lhs = self.lhs.evaluate(parameters)
        rhs = self.rhs.evaluate(parameters)
        return lhs - rhs if self.relation == ">=" else rhs - lhs

    def is_satisfied(
        self, parameters: np.ndarray, *, tolerance: float = 1e-9
    ) -> bool:
        value = self.margin(parameters)
        return value >= -tolerance * max(1.0, abs(value))


@dataclass
class IndexedParameterLayout:
    """Frozen integer slot layout and compiled runtime readers.

    ``readers`` contain slot indices rather than slot names. The ``slot_names``
    tuple is retained only for diagnostics/code generation.
    """

    slot_names: tuple[str, ...]
    readers: tuple[tuple, ...]
    slot_index: dict[str, int]
    _buffer: np.ndarray = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._buffer = np.empty(len(self.slot_names), dtype=float)

    def new_buffer(self) -> np.ndarray:
        return np.empty(len(self.slot_names), dtype=float)

    def read(self, engine, *, out: np.ndarray | None = None) -> np.ndarray:
        values = self._buffer if out is None else out
        if values.shape != (len(self.slot_names),):
            raise ValueError("indexed parameter buffer has the wrong shape")
        residual_state = getattr(engine, "v2_residual_state", None)
        for reader in self.readers:
            kind = reader[0]
            index = reader[1]
            if kind == "variable":
                _, _, name, side = reader
                lb, ub = engine.get_variable_bounds(name)
                if side == "current":
                    if abs(lb - ub) > 1e-8:
                        raise ValueError(
                            f"Frozen v2 program expected {name} to remain fixed"
                        )
                    values[index] = 0.5 * (lb + ub)
                elif side == "remaining_lower":
                    values[index] = lb
                elif side == "remaining_upper":
                    values[index] = ub
                else:
                    raise KeyError(f"Unknown variable parameter side: {side}")
            elif kind in {"source_constraint", "constraint"}:
                _, _, name, side = reader
                if kind == "source_constraint" and residual_state is not None:
                    lb, ub = residual_state.bounds(name)
                else:
                    lb, ub = engine.get_constraint_bounds(name)
                if side == "remaining":
                    if abs(lb - ub) > 1e-8:
                        raise ValueError(
                            f"Frozen v2 program expected {name} to remain an equality"
                        )
                    values[index] = 0.5 * (lb + ub)
                elif side == "remaining_lower":
                    values[index] = lb
                elif side == "remaining_upper":
                    values[index] = ub
                else:
                    raise KeyError(f"Unknown constraint parameter side: {side}")
            elif kind == "coefficient":
                _, _, constraint_name, variable_name = reader
                values[index] = engine.cons[constraint_name].coefficients.get(
                    variable_name, 0.0
                )
            elif kind == "constant":
                values[index] = reader[2]
            else:
                raise KeyError(f"Unknown runtime parameter reader kind: {kind}")
        return values

    def lower(self, expression: ParamExpr) -> IndexedParamExpr:
        return IndexedParamExpr.from_param_expr(expression, self.slot_index)



@dataclass(frozen=True)
class IndexedSymbolicExpr:
    variables: tuple[tuple[str, IndexedParamExpr], ...]
    parameters: IndexedParamExpr

    @classmethod
    def from_symbolic(
        cls, expression: "SymbolicExpr", layout: IndexedParameterLayout
    ) -> "IndexedSymbolicExpr":
        return cls(
            variables=tuple(
                (name, layout.lower(coefficient))
                for name, coefficient in expression.variables.items()
            ),
            parameters=layout.lower(expression.parameters),
        )

    def evaluate(self, values: Mapping[str, float], parameters: np.ndarray) -> float:
        value = self.parameters.evaluate(parameters)
        for name, coefficient in self.variables:
            value += coefficient.evaluate(parameters) * values[name]
        return float(value)

    def python_text(self, *, parameter_array: str = "p") -> str:
        pieces: list[str] = []
        for name, coefficient in self.variables:
            pieces.append(
                f"({coefficient.python_text(array_name=parameter_array)}) * {name}"
            )
        parameter_text = self.parameters.python_text(array_name=parameter_array)
        if parameter_text != "0.0" or not pieces:
            pieces.append(parameter_text)
        return " + ".join(pieces).replace("+ -", "- ")


@dataclass
class SymbolicExpr:
    """Linear expression over active LP variables with parameter scalars."""

    variables: dict[str, ParamExpr] = field(default_factory=dict)
    parameters: ParamExpr = field(default_factory=ParamExpr)

    @classmethod
    def variable(
        cls,
        name: str,
        coefficient: float | ParamExpr = 1.0,
    ) -> "SymbolicExpr":
        expr = (
            coefficient.copy()
            if isinstance(coefficient, ParamExpr)
            else ParamExpr.constant_value(float(coefficient))
        )
        return cls({name: expr}, ParamExpr())

    @classmethod
    def parameter(cls, value: ParamExpr) -> "SymbolicExpr":
        return cls({}, value.copy())

    def copy(self) -> "SymbolicExpr":
        return SymbolicExpr(
            {name: coefficient.copy() for name, coefficient in self.variables.items()},
            self.parameters.copy(),
        )

    def add_scaled(
        self,
        other: "SymbolicExpr",
        scale: float | ParamExpr,
    ) -> None:
        scale_expr = (
            scale.copy()
            if isinstance(scale, ParamExpr)
            else ParamExpr.constant_value(float(scale))
        )
        self.parameters = self.parameters.plus(
            other.parameters.multiplied(scale_expr)
        )
        for name, coefficient in other.variables.items():
            value = self.variables.get(name, ParamExpr.constant_value(0.0)).plus(
                coefficient.multiplied(scale_expr)
            )
            if value.is_constant(0.0):
                self.variables.pop(name, None)
            else:
                self.variables[name] = value

    def evaluate(self, values: Mapping[str, float], parameters: Mapping[str, float]) -> float:
        value = self.parameters.evaluate(parameters)
        for name, coefficient in self.variables.items():
            value += coefficient.evaluate(parameters) * values[name]
        return value

    def text(self) -> str:
        pieces: list[str] = []
        for name, coefficient in sorted(self.variables.items()):
            if coefficient.is_constant(1.0):
                pieces.append(name)
            elif coefficient.is_constant(-1.0):
                pieces.append(f"-{name}")
            else:
                pieces.append(f"({coefficient.text()})*{name}")
        if self.parameters.slots() or not self.parameters.is_constant(0.0) or not pieces:
            pieces.append(self.parameters.text())
        return " + ".join(pieces).replace("+ -", "- ")


@dataclass
class ParametricVariable:
    name: str
    lower: ParamExpr | None = None
    upper: ParamExpr | None = None

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
    coefficients: dict[str, ParamExpr] = field(default_factory=dict)

    def copy(self) -> "ParametricConstraint":
        return ParametricConstraint(
            self.name,
            None if self.lower is None else self.lower.copy(),
            None if self.upper is None else self.upper.copy(),
            {name: value.copy() for name, value in self.coefficients.items()},
        )


@dataclass
class GuardPredicate:
    """Algebraic condition required by a parameter-dependent simplification.

    Guards are stored as the actual comparison that justified the compiler
    rewrite, rather than as an opaque prose label.  ``description`` explains
    what was simplified; ``lhs relation rhs`` is the executable proof
    obligation checked at runtime.
    """

    lhs: ParamExpr
    relation: str
    rhs: ParamExpr
    description: str

    def __post_init__(self) -> None:
        if self.relation not in (">=", "<="):
            raise ValueError(f"unsupported guard relation {self.relation!r}")

    def copy(self) -> "GuardPredicate":
        return GuardPredicate(
            self.lhs.copy(),
            self.relation,
            self.rhs.copy(),
            self.description,
        )

    def margin(self) -> ParamExpr:
        """Return an expression that must remain nonnegative."""

        if self.relation == ">=":
            return self.lhs.shifted(self.rhs, -1.0)
        return self.rhs.shifted(self.lhs, -1.0)

    def evaluate_margin(self, parameters: Mapping[str, float]) -> float:
        # Runtime guards are checked frequently.  Evaluate the original two
        # sides directly instead of allocating a temporary shifted ParamExpr
        # for every check.
        lhs = self.lhs.evaluate(parameters)
        rhs = self.rhs.evaluate(parameters)
        return lhs - rhs if self.relation == ">=" else rhs - lhs

    def structurally_proven(
        self,
        domains: Mapping[str, tuple[float, float]],
        *,
        tolerance: float = ZERO_TOL,
    ) -> bool:
        # Runtime-varying bound/RHS parameters usually have no declared
        # finite domain.  Passing no defaults here intentionally gives those
        # slots [-inf, +inf], so a "structural" proof cannot accidentally be
        # inferred from preparation-day numeric values.
        low, _ = self.margin().interval(domains, {})
        return low >= -tolerance

    def is_satisfied(
        self,
        parameters: Mapping[str, float],
        *,
        tolerance: float = 1e-9,
    ) -> bool:
        value = self.evaluate_margin(parameters)
        scale = max(1.0, abs(value))
        return value >= -tolerance * scale

    def text(self) -> str:
        return f"{self.lhs.text()} {self.relation} {self.rhs.text()}"


def variable_slot(name: str, side: str) -> str:
    return f"variable[{name}].remaining_{side}"


def constraint_slot(name: str, side: str) -> str:
    return f"constraint[{name}].remaining_{side}"


def coefficient_slot(constraint_name: str, variable_name: str) -> str:
    return f"coefficient[{constraint_name},{variable_name}]"


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
    if _dynamic_side(engine, kind, name, side):
        slot = variable_slot(name, side) if kind == "variable" else constraint_slot(name, side)
        return ParamExpr.slot(slot)
    if not isfinite(value):
        return None
    if abs(value) > 1e-15:
        slot = variable_slot(name, side) if kind == "variable" else constraint_slot(name, side)
        return ParamExpr.slot(slot)
    return ParamExpr.constant_value(0.0)


@dataclass
class CompilerModel:
    """Frozen, parameterized copy of one production-LP structural state."""

    variables: dict[str, ParametricVariable]
    constraints: dict[str, ParametricConstraint]
    reconstruction: dict[str, SymbolicExpr]
    source_variable_count: int
    parameter_defaults: dict[str, float]
    parameter_sources: dict[str, str] = field(default_factory=dict)
    parameter_domains: dict[str, tuple[float, float]] = field(default_factory=dict)
    source_constraint_names: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)
    guards: list[GuardPredicate] = field(default_factory=list)
    structural_proofs: list[GuardPredicate] = field(default_factory=list)
    # Residual-kernel programs operate on transaction *increments* while the
    # already-committed absolute transaction values live in the execution
    # IR's ResidualState.  The base expression is retained for reconstructing
    # the absolute production-LP value returned to Apportioner.
    residual_increment_bases: dict[str, ParamExpr] = field(default_factory=dict)
    # When true, constraint ``remaining`` slots are read from the explicit
    # execution ResidualState instead of from the mutable production LP.
    uses_residual_state: bool = False
    # Bidirectional measurement rows that participate in the storage/counterflow
    # ambiguity convention.  Other bidirectional slack rows are pure reporting
    # residuals and can be projected out completely.
    directional_residual_constraints: set[str] = field(default_factory=set)
    _runtime_slots_cache: tuple[str, ...] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _runtime_readers_cache: tuple[tuple, ...] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _indexed_parameter_layout_cache: IndexedParameterLayout | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @classmethod
    def from_engine(cls, engine) -> "CompilerModel":
        defaults: dict[str, float] = {}
        domains: dict[str, tuple[float, float]] = {}

        variables: dict[str, ParametricVariable] = {}
        for name, variable in engine.vars.items():
            lb = float(variable.lb())
            ub = float(variable.ub())
            lower = _bound_expr(engine, kind="variable", name=name, side="lower", value=lb)
            upper = _bound_expr(engine, kind="variable", name=name, side="upper", value=ub)
            if lower is not None:
                for slot in lower.slots():
                    defaults[slot] = lb
            if upper is not None:
                for slot in upper.slots():
                    defaults[slot] = ub
            variables[name] = ParametricVariable(name, lower, upper)

        dynamic_coefficients = getattr(engine, "_v2_dynamic_coefficients", {})
        constraints: dict[str, ParametricConstraint] = {}
        for name, constraint in engine.cons.items():
            coefficient_values = (
                dict(constraint.coefficients)
                if hasattr(constraint, "coefficients")
                else {
                    variable: float(constraint.GetCoefficient(engine.vars[variable]))
                    for variable in variables
                    if constraint.GetCoefficient(engine.vars[variable]) != 0
                }
            )
            for constraint_name, variable_name in dynamic_coefficients:
                if constraint_name == name:
                    coefficient_values.setdefault(variable_name, 0.0)
            coefficients: dict[str, ParamExpr] = {}
            for variable, value in coefficient_values.items():
                value = float(value)
                metadata = dynamic_coefficients.get((name, variable))
                if metadata is None:
                    if value != 0:
                        coefficients[variable] = ParamExpr.constant_value(value)
                    continue
                slot, lower_domain, upper_domain = metadata
                coefficients[variable] = ParamExpr.slot(slot)
                defaults[slot] = value
                domains[slot] = (float(lower_domain), float(upper_domain))

            lb = float(constraint.lb())
            ub = float(constraint.ub())
            if (
                name.startswith("MEAS_")
                and isfinite(lb)
                and isfinite(ub)
                and abs(lb - ub) <= 1e-12
            ):
                shared = ParamExpr.slot(f"constraint[{name}].remaining")
                lower = shared.copy()
                upper = shared.copy()
            else:
                lower = _bound_expr(engine, kind="constraint", name=name, side="lower", value=lb)
                upper = _bound_expr(engine, kind="constraint", name=name, side="upper", value=ub)
            if lower is not None:
                for slot in lower.slots():
                    defaults[slot] = lb
            if upper is not None:
                for slot in upper.slots():
                    defaults[slot] = ub
            constraints[name] = ParametricConstraint(name, lower, upper, coefficients)

        return cls(
            variables=variables,
            constraints=constraints,
            reconstruction={name: SymbolicExpr.variable(name) for name in variables},
            source_variable_count=len(variables),
            parameter_defaults=defaults,
            parameter_sources={},
            parameter_domains=domains,
            source_constraint_names=set(constraints),
            directional_residual_constraints=set(
                getattr(engine, "_v2_directional_residual_constraints", set())
            ),
        )

    def copy(self) -> "CompilerModel":
        return CompilerModel(
            variables={name: variable.copy() for name, variable in self.variables.items()},
            constraints={name: constraint.copy() for name, constraint in self.constraints.items()},
            reconstruction={name: expr.copy() for name, expr in self.reconstruction.items()},
            source_variable_count=self.source_variable_count,
            parameter_defaults=dict(self.parameter_defaults),
            parameter_sources=dict(self.parameter_sources),
            parameter_domains=dict(self.parameter_domains),
            source_constraint_names=set(self.source_constraint_names),
            notes=list(self.notes),
            guards=[guard.copy() for guard in self.guards],
            structural_proofs=[proof.copy() for proof in self.structural_proofs],
            residual_increment_bases={
                name: expression.copy()
                for name, expression in self.residual_increment_bases.items()
            },
            uses_residual_state=self.uses_residual_state,
            directional_residual_constraints=set(self.directional_residual_constraints),
        )

    def coefficient_sign(self, expression: ParamExpr) -> int | None:
        return expression.sign(self.parameter_domains, self.parameter_defaults)

    def variable_occurrences(self, name: str) -> int:
        return sum(name in constraint.coefficients for constraint in self.constraints.values())

    def fix_variable(self, name: str, value: ParamExpr, *, reason: str) -> None:
        if name not in self.variables:
            return
        self._runtime_slots_cache = None
        self._runtime_readers_cache = None
        self._indexed_parameter_layout_cache = None
        for constraint in self.constraints.values():
            coefficient = constraint.coefficients.pop(name, None)
            if coefficient is None or coefficient.is_constant(0.0):
                continue
            shift = coefficient.multiplied(value).scaled(-1.0)
            if constraint.lower is not None:
                constraint.lower = constraint.lower.plus(shift)
            if constraint.upper is not None:
                constraint.upper = constraint.upper.plus(shift)
        replacement = SymbolicExpr.parameter(value)
        self._replace_in_reconstruction(name, replacement)
        self.variables.pop(name, None)
        self.notes.append(f"{name} := {value.text()} ({reason})")

    def fix_variables(
        self,
        values: Mapping[str, ParamExpr],
        *,
        reasons: Mapping[str, str] | None = None,
        reason: str = "fixed bound",
    ) -> None:
        """Fix several variables in one structural pass.

        Repeated ``fix_variable`` calls rescan every constraint and every
        reconstruction expression for each eliminated variable.  Presolve can
        eliminate hundreds of independent non-objective transaction variables
        at once, so doing the algebra row-by-row reduces that work from roughly
        O(variables * model_size) to O(model_size).

        ``values`` has exactly the same substitution semantics as calling
        ``fix_variable`` for every member; the operation is safe to batch
        because all replacements are parameter-only expressions.
        """

        active = {
            name: value
            for name, value in values.items()
            if name in self.variables
        }
        if not active:
            return

        self._runtime_slots_cache = None
        self._runtime_readers_cache = None
        self._indexed_parameter_layout_cache = None

        for constraint in self.constraints.values():
            shift = ParamExpr.constant_value(0.0)
            changed = False
            for name, coefficient in list(constraint.coefficients.items()):
                value = active.get(name)
                if value is None:
                    continue
                constraint.coefficients.pop(name, None)
                if coefficient.is_constant(0.0):
                    continue
                shift = shift.plus(coefficient.multiplied(value).scaled(-1.0))
                changed = True
            if not changed:
                continue
            if constraint.lower is not None:
                constraint.lower = constraint.lower.plus(shift)
            if constraint.upper is not None:
                constraint.upper = constraint.upper.plus(shift)

        for expression in self.reconstruction.values():
            parameter_shift = ParamExpr.constant_value(0.0)
            changed = False
            for name, coefficient in list(expression.variables.items()):
                value = active.get(name)
                if value is None:
                    continue
                expression.variables.pop(name, None)
                parameter_shift = parameter_shift.plus(
                    coefficient.multiplied(value)
                )
                changed = True
            if changed:
                expression.parameters = expression.parameters.plus(parameter_shift)

        for name, value in active.items():
            self.variables.pop(name, None)
            why = reasons.get(name, reason) if reasons is not None else reason
            self.notes.append(f"{name} := {value.text()} ({why})")

    def substitute_variable(
        self,
        name: str,
        replacement: SymbolicExpr,
        *,
        reason: str,
        record_note: bool = True,
    ) -> None:
        if name not in self.variables:
            return
        self._runtime_slots_cache = None
        self._runtime_readers_cache = None
        self._indexed_parameter_layout_cache = None
        for constraint in self.constraints.values():
            coefficient = constraint.coefficients.pop(name, None)
            if coefficient is None or coefficient.is_constant(0.0):
                continue
            shift = coefficient.multiplied(replacement.parameters).scaled(-1.0)
            if constraint.lower is not None:
                constraint.lower = constraint.lower.plus(shift)
            if constraint.upper is not None:
                constraint.upper = constraint.upper.plus(shift)
            for other_name, other_coefficient in replacement.variables.items():
                value = constraint.coefficients.get(
                    other_name, ParamExpr.constant_value(0.0)
                ).plus(coefficient.multiplied(other_coefficient))
                if value.is_constant(0.0):
                    constraint.coefficients.pop(other_name, None)
                else:
                    constraint.coefficients[other_name] = value
        self._replace_in_reconstruction(name, replacement)
        self.variables.pop(name, None)
        if record_note:
            self.notes.append(f"{name} := {replacement.text()} ({reason})")

    def _replace_in_reconstruction(self, name: str, replacement: SymbolicExpr) -> None:
        for expression in self.reconstruction.values():
            coefficient = expression.variables.pop(name, None)
            if coefficient is not None:
                expression.add_scaled(replacement, coefficient)

    def remove_constraint(self, name: str) -> None:
        self._runtime_slots_cache = None
        self._runtime_readers_cache = None
        self._indexed_parameter_layout_cache = None
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

    def add_guard(self, predicate: GuardPredicate) -> None:
        self.guards.append(predicate)

    def add_structural_proof(self, predicate: GuardPredicate) -> None:
        self.structural_proofs.append(predicate)

    def check_guards(self, parameters: Mapping[str, float], *, tolerance: float = 1e-9) -> None:
        for guard in self.guards:
            value = guard.evaluate_margin(parameters)
            scale = max(1.0, abs(value))
            if value < -tolerance * scale:
                raise ValueError(
                    "Frozen v2 parameter guard failed "
                    f"({guard.description}; REQUIRE {guard.text()}): margin={value:g}"
                )

    def _all_slots(self) -> tuple[str, ...]:
        if self._runtime_slots_cache is not None:
            return self._runtime_slots_cache

        # ``parameter_defaults`` originates on the source production LP and can
        # contain hundreds or thousands of slots that were eliminated by the
        # compiler. Runtime execution must refresh only parameters still
        # referenced by the final transformed IR.
        slots: set[str] = set()
        for variable in self.variables.values():
            for bound in (variable.lower, variable.upper):
                if bound is not None:
                    slots.update(bound.slots())
        for constraint in self.constraints.values():
            for bound in (constraint.lower, constraint.upper):
                if bound is not None:
                    slots.update(bound.slots())
            for coefficient in constraint.coefficients.values():
                slots.update(coefficient.slots())
        for expression in self.reconstruction.values():
            slots.update(expression.parameters.slots())
            for coefficient in expression.variables.values():
                slots.update(coefficient.slots())
        for guard in self.guards:
            slots.update(guard.lhs.slots())
            slots.update(guard.rhs.slots())
        for expression in self.residual_increment_bases.values():
            slots.update(expression.slots())
        self._runtime_slots_cache = tuple(sorted(slots))
        return self._runtime_slots_cache

    def _runtime_readers(self) -> tuple[tuple, ...]:
        """Compile parameter-source strings into cheap runtime descriptors."""

        if self._runtime_readers_cache is not None:
            return self._runtime_readers_cache

        readers: list[tuple] = []
        for slot in self._all_slots():
            source_slot = self.parameter_sources.get(slot, slot)
            if source_slot.startswith("variable["):
                prefix, side = source_slot.rsplit("].", 1)
                name = prefix[len("variable["):]
                readers.append(("variable", slot, name, side))
            elif source_slot.startswith("constraint["):
                prefix, side = source_slot.rsplit("].", 1)
                name = prefix[len("constraint["):]
                source = (
                    "source_constraint"
                    if self.uses_residual_state and name in self.source_constraint_names
                    else "constraint"
                )
                readers.append((source, slot, name, side))
            elif source_slot.startswith("coefficient[") and source_slot.endswith("]"):
                body = source_slot[len("coefficient["):-1]
                constraint_name, variable_name = body.split(",", 1)
                readers.append(
                    ("coefficient", slot, constraint_name, variable_name)
                )
            else:
                value = self.parameter_defaults.get(
                    slot, self.parameter_defaults[source_slot]
                )
                readers.append(("constant", slot, float(value)))

        self._runtime_readers_cache = tuple(readers)
        return self._runtime_readers_cache

    def indexed_parameter_layout(self) -> IndexedParameterLayout:
        """Return the one-time lowered integer parameter layout for this IR."""

        if self._indexed_parameter_layout_cache is not None:
            return self._indexed_parameter_layout_cache
        slots = self._all_slots()
        slot_index = {name: index for index, name in enumerate(slots)}
        indexed_readers: list[tuple] = []
        for reader in self._runtime_readers():
            kind = reader[0]
            slot_name = reader[1]
            indexed_readers.append((kind, slot_index[slot_name], *reader[2:]))
        self._indexed_parameter_layout_cache = IndexedParameterLayout(
            slot_names=slots,
            readers=tuple(indexed_readers),
            slot_index=slot_index,
        )
        return self._indexed_parameter_layout_cache

    def runtime_parameter_array(
        self, engine, *, out: np.ndarray | None = None
    ) -> np.ndarray:
        return self.indexed_parameter_layout().read(engine, out=out)

    def lower_expression(self, expression: ParamExpr) -> IndexedParamExpr:
        return self.indexed_parameter_layout().lower(expression)

    def lower_symbolic(self, expression: SymbolicExpr) -> IndexedSymbolicExpr:
        return IndexedSymbolicExpr.from_symbolic(
            expression, self.indexed_parameter_layout()
        )

    def indexed_guards(self) -> tuple[IndexedGuardPredicate, ...]:
        layout = self.indexed_parameter_layout()
        return tuple(
            IndexedGuardPredicate(
                layout.lower(guard.lhs),
                guard.relation,
                layout.lower(guard.rhs),
                guard.description,
            )
            for guard in self.guards
        )

    @staticmethod
    def check_indexed_guards(
        guards: tuple[IndexedGuardPredicate, ...],
        parameters: np.ndarray,
        *,
        tolerance: float = 1e-9,
    ) -> None:
        for guard in guards:
            value = guard.margin(parameters)
            if value < -tolerance * max(1.0, abs(value)):
                raise ValueError(
                    "Frozen v2 parameter guard failed "
                    f"({guard.description}): margin={value:g}"
                )

    def runtime_parameters(self, engine) -> dict[str, float]:
        """Read only slots used by this frozen transformed model."""

        result: dict[str, float] = {}
        residual_state = (
            getattr(engine, "v2_residual_state", None)
            if self.uses_residual_state
            else None
        )
        for reader in self._runtime_readers():
            kind = reader[0]
            slot = reader[1]
            if kind == "variable":
                _, _, name, side = reader
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
            elif kind in {"source_constraint", "constraint"}:
                _, _, name, side = reader
                if kind == "source_constraint" and residual_state is not None:
                    try:
                        lb, ub = residual_state.bounds(name)
                    except KeyError:
                        raise KeyError(
                            "Frozen residual kernel is missing execution residual "
                            f"for source constraint {name}"
                        ) from None
                else:
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
            elif kind == "coefficient":
                _, _, constraint_name, variable_name = reader
                result[slot] = engine.cons[constraint_name].coefficients.get(
                    variable_name, 0.0
                )
            elif kind == "constant":
                result[slot] = reader[2]
            else:
                raise KeyError(f"Unknown runtime parameter reader kind: {kind}")
        return result
