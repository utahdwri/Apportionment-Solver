"""Frozen execution IR for the parameterized v2 compiler."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isfinite

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix

from ..lp_solver import LPSolverError
from .model import (
    CompilerModel,
    IndexedGuardPredicate,
    IndexedParamExpr,
    IndexedParameterLayout,
    IndexedSymbolicExpr,
    ParamExpr,
    ParametricConstraint,
    SymbolicExpr,
)


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
class EqualPriorityProgram(V2Program):
    """Frozen logical-transaction kernel for one equal-priority cohort.

    The production LP represents a transaction with one variable per path leg.
    By the time this program is built those path legs have already been
    collapsed and every allocation transaction is a residual increment.  A
    water-filling iteration therefore needs only one extra scalar ``g`` and
    the inequalities::

        dTRXN_i >= proportion_i * g

    for the currently active cohort members.  No temporary variables or rows
    are added to the production LP, and runtime member classification uses the
    same frozen transformed model.
    """

    model: CompilerModel = field(default=None)  # type: ignore[assignment]
    member_ids: tuple[str, ...] = ()
    priority: float = 0.0
    _names: tuple[str, ...] = field(init=False, default=())
    _index: dict[str, int] = field(init=False, default_factory=dict)
    _ub_rows: list[dict[int, ParamExpr]] = field(init=False, default_factory=list)
    _ub_rhs: list[ParamExpr] = field(init=False, default_factory=list)
    _eq_rows: list[dict[int, ParamExpr]] = field(init=False, default_factory=list)
    _eq_rhs: list[ParamExpr] = field(init=False, default_factory=list)
    _bounds: list[tuple[ParamExpr | None, ParamExpr | None]] = field(
        init=False, default_factory=list
    )
    _member_column_signatures: dict[str, tuple] = field(
        init=False, default_factory=dict
    )
    _use_dense_numeric: bool = field(init=False, default=False)
    _direct_common_increment: bool = field(init=False, default=False)
    _direct_ub_rows: tuple[tuple[tuple[int, float], ...], ...] = field(
        init=False, default=()
    )
    _parameter_layout: IndexedParameterLayout = field(init=False, repr=False)
    _indexed_guards: tuple[IndexedGuardPredicate, ...] = field(
        init=False, default=(), repr=False
    )
    _member_bases: dict[str, IndexedParamExpr] = field(
        init=False, default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        self._names = tuple(self.model.variables)
        self._index = {name: i for i, name in enumerate(self._names)}

        missing = [name for name in self.member_ids if name not in self._index]
        if missing:
            raise ValueError(
                "Equal-priority logical members disappeared from transformed IR: "
                + ", ".join(missing)
            )

        for constraint in self.model.constraints.values():
            row = {
                self._index[name]: coefficient.copy()
                for name, coefficient in constraint.coefficients.items()
                if name in self._index and not coefficient.is_constant(0.0)
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
                    self._ub_rows.append(
                        {column: value.scaled(-1.0) for column, value in row.items()}
                    )
                    self._ub_rhs.append(constraint.lower.scaled(-1.0))

        self._bounds = [
            (variable.lower, variable.upper)
            for variable in self.model.variables.values()
        ]

        # Tiny reduced kernels are faster through SciPy/HiGHS as dense arrays:
        # constructing, stacking, and reindexing CSR matrices can cost more than
        # solving a 10-50 variable LP.  Keep large/conservative variants sparse.
        row_count = len(self._ub_rows) + len(self._eq_rows)
        self._use_dense_numeric = (
            len(self._names) <= 64
            and row_count * max(1, len(self._names)) <= 8192
        )

        # A common-increment LP has a closed form when transformed IR proves
        # that only cohort increments remain, every increment has zero lower
        # bound, and every residual row consumes capacity monotonically. In
        # that case the optimum is simply the smallest row/bound capacity per
        # unit common increment. This avoids launching HiGHS for tiny kernels.
        self._direct_common_increment = (
            not self._eq_rows
            and set(self._names) == set(self.member_ids)
            and all(
                lower is None or lower.is_constant(0.0)
                for lower, _upper in self._bounds
            )
            and all(
                expression.is_constant()
                and expression.constant_value_number() >= -1e-15
                for row in self._ub_rows
                for expression in row.values()
            )
        )
        if self._direct_common_increment:
            self._direct_ub_rows = tuple(
                tuple(
                    (column, expression.constant_value_number())
                    for column, expression in row.items()
                    if abs(expression.constant_value_number()) > 1e-15
                )
                for row in self._ub_rows
            )
            self.stats["equal_priority_direct_common_increment_formulas"] = (
                self.stats.get("equal_priority_direct_common_increment_formulas", 0) + 1
            )

        signature_parts: dict[str, list[tuple[str, str]]] = {
            member: [] for member in self.member_ids
        }
        member_set = set(self.member_ids)
        for constraint in self.model.constraints.values():
            for variable_name, coefficient in constraint.coefficients.items():
                if (
                    variable_name in member_set
                    and not coefficient.is_constant(0.0)
                ):
                    signature_parts[variable_name].append(
                        (constraint.name, coefficient.text())
                    )
        self._member_column_signatures = {
            member: tuple(parts)
            for member, parts in signature_parts.items()
        }

        # Lower the named compiler IR once. Daily cohort execution thereafter
        # evaluates only integer-indexed expressions over one contiguous array.
        self._parameter_layout = self.model.indexed_parameter_layout()
        self._indexed_guards = self.model.indexed_guards()
        self._ub_rows = [
            {column: self._parameter_layout.lower(expr) for column, expr in row.items()}
            for row in self._ub_rows
        ]
        self._ub_rhs = [self._parameter_layout.lower(expr) for expr in self._ub_rhs]
        self._eq_rows = [
            {column: self._parameter_layout.lower(expr) for column, expr in row.items()}
            for row in self._eq_rows
        ]
        self._eq_rhs = [self._parameter_layout.lower(expr) for expr in self._eq_rhs]
        self._bounds = [
            (
                None if lower is None else self._parameter_layout.lower(lower),
                None if upper is None else self._parameter_layout.lower(upper),
            )
            for lower, upper in self._bounds
        ]
        self._member_bases = {
            name: self._parameter_layout.lower(base)
            for name, base in self.model.residual_increment_bases.items()
            if name in self.member_ids
        }
        self.stats["indexed_parameter_slots"] = len(self._parameter_layout.slot_names)
        self.stats["indexed_equal_priority_programs"] = self.stats.get(
            "indexed_equal_priority_programs", 0
        ) + 1

    @staticmethod
    def _evaluate_sparse(
        rows: list[dict[int, ParamExpr]],
        width: int,
        parameters: np.ndarray,
    ) -> csr_matrix | None:
        if not rows:
            return None
        r: list[int] = []
        c: list[int] = []
        data: list[float] = []
        for row_index, row in enumerate(rows):
            for column, expression in row.items():
                value = expression.evaluate(parameters)
                if abs(value) > 1e-15:
                    r.append(row_index)
                    c.append(column)
                    data.append(value)
        return csr_matrix((data, (r, c)), shape=(len(rows), width), dtype=float)

    @staticmethod
    def _evaluate_dense(
        rows: list[dict[int, ParamExpr]],
        width: int,
        parameters: np.ndarray,
    ) -> np.ndarray | None:
        if not rows:
            return None
        matrix = np.zeros((len(rows), width), dtype=float)
        for row_index, row in enumerate(rows):
            for column, expression in row.items():
                matrix[row_index, column] = expression.evaluate(parameters)
        return matrix

    def _numeric_problem(self, parameters: np.ndarray):
        evaluator = self._evaluate_dense if self._use_dense_numeric else self._evaluate_sparse
        A_ub = evaluator(self._ub_rows, len(self._names), parameters)
        b_ub = None
        if self._ub_rhs:
            raw = np.asarray(
                [expression.evaluate(parameters) for expression in self._ub_rhs],
                dtype=float,
            )
            if np.isnan(raw).any() or np.isneginf(raw).any():
                raise LPSolverError(
                    "v2 equal-priority kernel has an invalid -inf/nan upper RHS"
                )
            active = np.isfinite(raw)
            if active.any():
                b_ub = raw[active]
                if A_ub is not None:
                    A_ub = A_ub[active]
            else:
                A_ub = None

        A_eq = evaluator(self._eq_rows, len(self._names), parameters)
        b_eq = None
        if self._eq_rhs:
            b_eq = np.asarray(
                [expression.evaluate(parameters) for expression in self._eq_rhs],
                dtype=float,
            )
            if not np.isfinite(b_eq).all():
                raise LPSolverError(
                    "v2 equal-priority kernel has a non-finite equality RHS"
                )

        bounds = []
        for lower, upper in self._bounds:
            lower_value = None if lower is None else lower.evaluate(parameters)
            upper_value = None if upper is None else upper.evaluate(parameters)
            bounds.append(
                (
                    None if lower_value is None or np.isneginf(lower_value) else lower_value,
                    None if upper_value is None or np.isposinf(upper_value) else upper_value,
                )
            )
        return A_ub, b_ub, A_eq, b_eq, bounds

    def _parameters(self, engine) -> np.ndarray:
        parameters = self._parameter_layout.read(engine)
        self.model.check_indexed_guards(self._indexed_guards, parameters)
        return parameters

    def column_signature(self, transaction_id: str) -> tuple:
        return self._member_column_signatures[transaction_id]

    def _absolute_member_value(
        self,
        transaction_id: str,
        increment: float,
        parameters: np.ndarray,
    ) -> float:
        base = self._member_bases.get(transaction_id)
        current = 0.0 if base is None else base.evaluate(parameters)
        return current + increment

    def _maximize_common_increment_direct(
        self,
        proportion_factors: dict[str, float],
        parameters: np.ndarray,
    ) -> dict[str, float]:
        """Solve a proven monotone cohort by direct residual-capacity ratios."""

        factors_by_column: dict[int, float] = {}
        common_increment = inf
        has_positive_factor = False

        for transaction_id, raw_factor in proportion_factors.items():
            factor = float(raw_factor)
            if factor < 0:
                raise ValueError(
                    f"Negative equal-priority factor for {transaction_id}: {factor}"
                )
            column = self._index[transaction_id]
            factors_by_column[column] = factor
            if factor <= 1e-15:
                continue
            has_positive_factor = True
            _lower, upper = self._bounds[column]
            if upper is not None:
                available = upper.evaluate(parameters)
                if available < -1e-9:
                    raise LPSolverError(
                        "v2 direct equal-priority kernel has a negative residual "
                        f"upper bound for {transaction_id}: {available:g}"
                    )
                common_increment = min(
                    common_increment, max(0.0, available) / factor
                )

        if not has_positive_factor:
            raise LPSolverError(
                "v2 equal-priority common increment has no positive proportion factor"
            )

        for row, rhs_expression in zip(self._direct_ub_rows, self._ub_rhs):
            rhs = rhs_expression.evaluate(parameters)
            denominator = 0.0
            for column, coefficient in row:
                denominator += coefficient * factors_by_column.get(column, 0.0)
            if denominator > 1e-15:
                common_increment = min(common_increment, rhs / denominator)
            elif rhs < -1e-9:
                raise LPSolverError(
                    "v2 direct equal-priority kernel encountered an infeasible "
                    f"zero-consumption row with RHS {rhs:g}"
                )

        if common_increment == inf:
            raise LPSolverError(
                "v2 direct equal-priority common-increment kernel is unbounded"
            )
        if common_increment < -1e-9:
            raise LPSolverError(
                "v2 direct equal-priority common-increment kernel is infeasible: "
                f"maximum common increment {common_increment:g}"
            )
        common_increment = max(0.0, common_increment)

        return {
            transaction_id: self._absolute_member_value(
                transaction_id,
                float(factor) * common_increment,
                parameters,
            )
            for transaction_id, factor in proportion_factors.items()
        }

    def maximize_common_increment(
        self,
        engine,
        proportion_factors: dict[str, float],
        *,
        parameters: np.ndarray | None = None,
    ) -> dict[str, float]:
        """Return absolute member values for one water-filling iteration."""

        if not proportion_factors:
            return {}
        unknown = set(proportion_factors) - set(self.member_ids)
        if unknown:
            raise KeyError(
                "Equal-priority kernel received members outside its cohort: "
                + ", ".join(sorted(unknown))
            )

        if parameters is None:
            parameters = self._parameters(engine)
        stats = getattr(getattr(engine, "v2_session", None), "stats", None)
        if self._direct_common_increment:
            if stats is not None:
                stats["execution_equal_priority_direct_common_increment_formulas"] += 1
            return self._maximize_common_increment_direct(
                proportion_factors, parameters
            )
        if stats is not None:
            stats["execution_equal_priority_lp_common_increment_solves"] += 1
        A_ub, b_ub, A_eq, b_eq, bounds = self._numeric_problem(parameters)
        width = len(self._names)
        common_index = width

        # Extend frozen rows by one common-increment column and append the
        # dTRXN_i >= factor_i * g rows.  Dense reduced kernels avoid SciPy
        # sparse stacking altogether; conservative large variants stay sparse.
        extra_row_count = len(proportion_factors)
        if self._use_dense_numeric:
            base_ub_rows = 0 if A_ub is None else A_ub.shape[0]
            common_ub = np.zeros(
                (base_ub_rows + extra_row_count, width + 1), dtype=float
            )
            if A_ub is not None:
                common_ub[:base_ub_rows, :width] = A_ub
            for row_offset, (transaction_id, factor) in enumerate(
                proportion_factors.items()
            ):
                factor = float(factor)
                if factor < 0:
                    raise ValueError(
                        f"Negative equal-priority factor for {transaction_id}: {factor}"
                    )
                row = base_ub_rows + row_offset
                common_ub[row, self._index[transaction_id]] = -1.0
                common_ub[row, common_index] = factor
            A_ub = common_ub
            base_rhs = (
                np.empty(0, dtype=float)
                if b_ub is None
                else np.asarray(b_ub, dtype=float)
            )
            b_ub = np.concatenate(
                [base_rhs, np.zeros(extra_row_count, dtype=float)]
            )
            if A_eq is not None:
                widened_eq = np.zeros((A_eq.shape[0], width + 1), dtype=float)
                widened_eq[:, :width] = A_eq
                A_eq = widened_eq
        else:
            # Extend frozen rows by one all-zero common-increment column.
            if A_ub is not None:
                A_ub = csr_matrix(
                    (
                        A_ub.data,
                        A_ub.indices,
                        A_ub.indptr,
                    ),
                    shape=(A_ub.shape[0], width + 1),
                )
            if A_eq is not None:
                A_eq = csr_matrix(
                    (
                        A_eq.data,
                        A_eq.indices,
                        A_eq.indptr,
                    ),
                    shape=(A_eq.shape[0], width + 1),
                )

            extra_row_indexes: list[int] = []
            extra_columns: list[int] = []
            extra_data: list[float] = []
            for row_index, (transaction_id, factor) in enumerate(
                proportion_factors.items()
            ):
                factor = float(factor)
                if factor < 0:
                    raise ValueError(
                        f"Negative equal-priority factor for {transaction_id}: {factor}"
                    )
                extra_row_indexes.append(row_index)
                extra_columns.append(self._index[transaction_id])
                extra_data.append(-1.0)
                if factor != 0.0:
                    extra_row_indexes.append(row_index)
                    extra_columns.append(common_index)
                    extra_data.append(factor)

            if extra_row_count:
                extra = csr_matrix(
                    (extra_data, (extra_row_indexes, extra_columns)),
                    shape=(extra_row_count, width + 1),
                    dtype=float,
                )
                if A_ub is None:
                    A_ub = extra
                    b_ub = np.zeros(extra_row_count, dtype=float)
                else:
                    from scipy.sparse import vstack

                    A_ub = vstack([A_ub, extra], format="csr")
                    base_rhs = (
                        np.empty(0, dtype=float)
                        if b_ub is None
                        else np.asarray(b_ub, dtype=float)
                    )
                    b_ub = np.concatenate(
                        [base_rhs, np.zeros(extra_row_count, dtype=float)]
                    )

        c = np.zeros(width + 1, dtype=float)
        c[common_index] = -1.0
        result = linprog(
            c=c,
            A_ub=A_ub,
            b_ub=b_ub,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=[*bounds, (0.0, None)],
            method="highs-ds",
        )
        if not result.success:
            raise LPSolverError(
                "v2 equal-priority common-increment kernel could not solve: "
                f"{result.status}: {result.message}"
            )

        common_increment = float(result.x[common_index])
        return {
            transaction_id: self._absolute_member_value(
                transaction_id,
                float(factor) * common_increment,
                parameters,
            )
            for transaction_id, factor in proportion_factors.items()
        }

    def maximize_member_sum(
        self,
        engine,
        transaction_ids: list[str],
        *,
        parameters: np.ndarray | None = None,
    ) -> float:
        """Maximum absolute sum of selected logical members."""
        value, _ = self.maximize_member_values(
            engine, transaction_ids, parameters=parameters
        )
        return value

    def maximize_member_values(
        self,
        engine,
        transaction_ids: list[str],
        *,
        parameters: np.ndarray | None = None,
    ) -> tuple[float, dict[str, float]]:
        """Return the maximum sum and a feasible absolute-value witness.

        This is used only to decide which water-filling members are blocked.
        The objective is assembled numerically over the frozen logical columns;
        no objective program is compiled at runtime.
        """

        if not transaction_ids:
            return 0.0, {}
        unknown = set(transaction_ids) - set(self.member_ids)
        if unknown:
            raise KeyError(
                "Equal-priority classification received members outside its cohort: "
                + ", ".join(sorted(unknown))
            )

        if parameters is None:
            parameters = self._parameters(engine)
        A_ub, b_ub, A_eq, b_eq, bounds = self._numeric_problem(parameters)
        c = np.zeros(len(self._names), dtype=float)
        for transaction_id in transaction_ids:
            c[self._index[transaction_id]] -= 1.0

        result = linprog(
            c=c,
            A_ub=A_ub,
            b_ub=b_ub,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs-ds",
        )
        if not result.success:
            raise LPSolverError(
                "v2 equal-priority member-classification kernel could not solve: "
                f"{result.status}: {result.message}"
            )

        values = {
            transaction_id: self._absolute_member_value(
                transaction_id,
                float(result.x[self._index[transaction_id]]),
                parameters,
            )
            for transaction_id in transaction_ids
        }
        # Preserve the previous objective summation order near the caller's
        # classification tolerance while also exposing the feasible vector.
        increment_sum = sum(
            float(result.x[self._index[transaction_id]])
            for transaction_id in transaction_ids
        )
        current_sum = sum(
            self._absolute_member_value(transaction_id, 0.0, parameters)
            for transaction_id in transaction_ids
        )
        return current_sum + increment_sum, values

    def maximize_single_member(
        self,
        engine,
        transaction_id: str,
        *,
        parameters: np.ndarray | None = None,
    ) -> float:
        if parameters is None:
            parameters = self._parameters(engine)
        A_ub, b_ub, A_eq, b_eq, bounds = self._numeric_problem(parameters)
        c = np.zeros(len(self._names), dtype=float)
        c[self._index[transaction_id]] = -1.0
        result = linprog(
            c=c,
            A_ub=A_ub,
            b_ub=b_ub,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs-ds",
        )
        if not result.success:
            raise LPSolverError(
                "v2 equal-priority scalar kernel could not solve: "
                f"{result.status}: {result.message}"
            )
        increment = float(result.x[self._index[transaction_id]])
        return self._absolute_member_value(transaction_id, increment, parameters)

    def execute(self, engine) -> ProgramResult:
        raise TypeError(
            "EqualPriorityProgram requires active member factors; use "
            "maximize_common_increment()"
        )

    def text(self) -> str:
        lines = [
            f"{self.name}: FROZEN LOGICAL EQUAL-PRIORITY KERNEL priority={self.priority:g}",
            f"    cohort members: {', '.join(self.member_ids)}",
            f"    source variables: {self.source_variable_count}",
            f"    active variables after v2 presolve: {self.active_variable_count}",
            f"    active constraints: {len(self.model.constraints)}",
            "    runtime active members use one common increment g:",
            "        dTRXN_i >= proportion_i * g",
            "    member classification reuses the same logical residual IR",
            "    no production path-leg rows or temporary production-LP variables are used",
        ]
        if self.model.guards:
            lines.append("    runtime guards:")
            for guard in self.model.guards:
                lines.append(f"        REQUIRE {guard.text()}")
                lines.append(f"            # {guard.description}")
        if self.model.structural_proofs:
            lines.append("    structural simplification proofs:")
            for proof in self.model.structural_proofs:
                lines.append(f"        PROVEN {proof.text()}")
                lines.append(f"            # {proof.description}")
        if self.model.notes:
            lines.append("    frozen compiler substitutions:")
            lines.extend(f"        {note}" for note in self.model.notes)
        return "\n".join(lines)


@dataclass
class DirectScalarProgram(V2Program):
    model: CompilerModel = field(default=None)  # type: ignore[assignment]
    active_name: str = ""
    _parameter_layout: IndexedParameterLayout = field(init=False, repr=False)
    _indexed_guards: tuple[IndexedGuardPredicate, ...] = field(
        init=False, default=(), repr=False
    )
    _indexed_lower: IndexedParamExpr | None = field(init=False, default=None, repr=False)
    _indexed_upper: IndexedParamExpr | None = field(init=False, default=None, repr=False)
    _indexed_constraints: tuple[
        tuple[str, IndexedParamExpr | None, IndexedParamExpr | None, IndexedParamExpr | None], ...
    ] = field(init=False, default=(), repr=False)
    _indexed_objective: IndexedSymbolicExpr = field(init=False, repr=False)
    _indexed_reconstruction: dict[str, IndexedSymbolicExpr] = field(
        init=False, default_factory=dict, repr=False
    )

    def __post_init__(self) -> None:
        self._parameter_layout = self.model.indexed_parameter_layout()
        self._indexed_guards = self.model.indexed_guards()
        variable = self.model.variables[self.active_name]
        self._indexed_lower = (
            None if variable.lower is None else self._parameter_layout.lower(variable.lower)
        )
        self._indexed_upper = (
            None if variable.upper is None else self._parameter_layout.lower(variable.upper)
        )
        rows = []
        for constraint in self.model.constraints.values():
            other = [
                name
                for name, value in constraint.coefficients.items()
                if name != self.active_name and not value.is_constant(0.0)
            ]
            if other:
                raise ValueError("DirectScalarProgram received coupled row")
            coefficient = constraint.coefficients.get(self.active_name)
            rows.append(
                (
                    constraint.name,
                    None if constraint.lower is None else self._parameter_layout.lower(constraint.lower),
                    None if constraint.upper is None else self._parameter_layout.lower(constraint.upper),
                    None if coefficient is None else self._parameter_layout.lower(coefficient),
                )
            )
        self._indexed_constraints = tuple(rows)
        self._indexed_objective = self.model.lower_symbolic(self.objective)
        self._indexed_reconstruction = {
            name: self.model.lower_symbolic(self.model.reconstruction[name])
            for name in self.requested
        }
        self.stats["indexed_parameter_slots"] = len(self._parameter_layout.slot_names)
        self.stats["indexed_direct_programs"] = self.stats.get(
            "indexed_direct_programs", 0
        ) + 1

    @property
    def parameter_slot_names(self) -> tuple[str, ...]:
        return self._parameter_layout.slot_names

    def runtime_parameter_array(self, engine) -> np.ndarray:
        return self._parameter_layout.read(engine)

    def check_indexed_guards(self, parameters: np.ndarray) -> None:
        self.model.check_indexed_guards(self._indexed_guards, parameters)

    def _interval(self, parameters: np.ndarray) -> tuple[float, float]:
        lower = -inf if self._indexed_lower is None else self._indexed_lower.evaluate(parameters)
        upper = inf if self._indexed_upper is None else self._indexed_upper.evaluate(parameters)
        for constraint_name, lower_expr, upper_expr, coefficient_expr in self._indexed_constraints:
            coefficient = 0.0 if coefficient_expr is None else coefficient_expr.evaluate(parameters)
            if abs(coefficient) <= 1e-12:
                if lower_expr is not None and lower_expr.evaluate(parameters) > 1e-8:
                    raise LPSolverError(
                        f"v2 parameter guard failed: {constraint_name} lower > 0"
                    )
                if upper_expr is not None and upper_expr.evaluate(parameters) < -1e-8:
                    raise LPSolverError(
                        f"v2 parameter guard failed: {constraint_name} upper < 0"
                    )
                continue
            if coefficient > 0:
                if lower_expr is not None:
                    lower = max(lower, lower_expr.evaluate(parameters) / coefficient)
                if upper_expr is not None:
                    upper = min(upper, upper_expr.evaluate(parameters) / coefficient)
            else:
                if lower_expr is not None:
                    upper = min(upper, lower_expr.evaluate(parameters) / coefficient)
                if upper_expr is not None:
                    lower = max(lower, upper_expr.evaluate(parameters) / coefficient)
        return float(lower), float(upper)

    def execute_with_parameters(
        self, engine, parameters: np.ndarray
    ) -> ProgramResult:
        lower, upper = self._interval(parameters)
        scale = max(
            1.0,
            abs(lower) if np.isfinite(lower) else 1.0,
            abs(upper) if np.isfinite(upper) else 1.0,
        )
        if upper < lower - 1e-9 * scale:
            raise LPSolverError(
                f"v2 direct scalar interval is infeasible: {lower} > {upper}"
            )
        if upper < lower:
            middle = 0.5 * (lower + upper)
            lower = upper = middle

        values_for_objective = {self.active_name: 1.0}
        objective_coefficient = self._indexed_objective.variables[0][1].evaluate(parameters)
        if abs(objective_coefficient) <= 1e-15:
            raise LPSolverError("v2 direct scalar objective coefficient became zero")
        maximize_active = self.maximization == (objective_coefficient > 0)
        active_value = upper if maximize_active else lower
        active_values = {self.active_name: active_value}
        requested_values = {
            name: expression.evaluate(active_values, parameters)
            for name, expression in self._indexed_reconstruction.items()
        }
        objective_value = self._indexed_objective.evaluate(active_values, parameters)
        return ProgramResult(objective_value, requested_values, active_values)

    def execute(self, engine) -> ProgramResult:
        parameters = self.runtime_parameter_array(engine)
        self.check_indexed_guards(parameters)
        return self.execute_with_parameters(engine, parameters)

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
            coefficient = constraint.coefficients.get(self.active_name)
            if coefficient is None or coefficient.is_constant(0.0):
                continue
            sign = self.model.coefficient_sign(coefficient)
            coefficient_text = coefficient.text()
            if sign is None:
                # Execution can still evaluate this safely. The renderer keeps
                # the ambiguity explicit instead of pretending one side is
                # always the limiting side.
                upper_candidates.append(
                    f"RUNTIME_INTERVAL({constraint.name}, coefficient={coefficient_text})"
                )
                continue
            if sign > 0:
                if constraint.upper is not None:
                    upper_candidates.append(
                        f"({constraint.upper.text()}) / ({coefficient_text})"
                    )
                if constraint.lower is not None:
                    lower_candidates.append(
                        f"({constraint.lower.text()}) / ({coefficient_text})"
                    )
            elif sign < 0:
                if constraint.lower is not None:
                    upper_candidates.append(
                        f"({constraint.lower.text()}) / ({coefficient_text})"
                    )
                if constraint.upper is not None:
                    lower_candidates.append(
                        f"({constraint.upper.text()}) / ({coefficient_text})"
                    )
        return upper_candidates, lower_candidates

    def assignment_lines(self, *, indent: int = 0) -> list[str]:
        """Render only the executable assignment, for the day-program IR."""

        upper_candidates, lower_candidates = self._candidate_text()
        objective_coefficient = self.objective.variables[self.active_name]
        sign = self.model.coefficient_sign(objective_coefficient)
        maximize_active = self.maximization if sign != -1 else not self.maximization
        chosen = upper_candidates if maximize_active else lower_candidates
        aggregate = "MIN" if maximize_active else "MAX"
        pad = " " * indent
        base = self.model.residual_increment_bases.get(self.active_name)
        if base is None:
            prefix = f"{pad}{self.active_name} = {aggregate}("
        else:
            prefix = (
                f"{pad}{self.active_name} = {base.text()} + {aggregate}("
            )
        lines = [prefix]
        lines.extend(f"{pad}    {candidate}," for candidate in chosen)
        lines.append(f"{pad})")
        return lines

    def text(self) -> str:
        direction = "MAXIMIZE" if self.maximization else "MINIMIZE"
        lines = [f"{self.name}: DIRECT {direction} {self.objective.text()}"]
        lines.extend(self.assignment_lines(indent=4))
        if self.model.guards:
            lines.append("    runtime guards:")
            for guard in self.model.guards:
                lines.append(f"        REQUIRE {guard.text()}")
                lines.append(f"            # {guard.description}")
        if self.model.structural_proofs:
            lines.append("    structural simplification proofs:")
            for proof in self.model.structural_proofs:
                lines.append(f"        PROVEN {proof.text()}")
                lines.append(f"            # {proof.description}")
        if self.model.notes:
            lines.append("    frozen compiler substitutions:")
            lines.extend(f"        {note}" for note in self.model.notes)
        return "\n".join(lines)


@dataclass
class ReducedLPProgram(V2Program):
    model: CompilerModel = field(default=None)  # type: ignore[assignment]
    _names: tuple[str, ...] = field(init=False, default=())
    _index: dict[str, int] = field(init=False, default_factory=dict)
    _objective_exprs: list[ParamExpr] = field(init=False, default_factory=list)
    _ub_rows: list[dict[int, ParamExpr]] = field(init=False, default_factory=list)
    _ub_rhs: list[ParamExpr] = field(init=False, default_factory=list)
    _eq_rows: list[dict[int, ParamExpr]] = field(init=False, default_factory=list)
    _eq_rhs: list[ParamExpr] = field(init=False, default_factory=list)
    _bounds: list[tuple[ParamExpr | None, ParamExpr | None]] = field(
        init=False, default_factory=list
    )
    _quick_scalar_index: int | None = field(init=False, default=None)
    _quick_scalar_name: str | None = field(init=False, default=None)
    _quick_scalar_reconstruction_safe: bool = field(init=False, default=False)
    _quick_scalar_rows: list[tuple[ParamExpr, dict[int, ParamExpr], ParamExpr]] = field(
        init=False, default_factory=list
    )
    _projected_scalar: DirectScalarProgram | None = field(init=False, default=None)
    _parameter_layout: IndexedParameterLayout = field(init=False, repr=False)
    _indexed_guards: tuple[IndexedGuardPredicate, ...] = field(
        init=False, default=(), repr=False
    )
    _indexed_reconstruction: dict[str, IndexedSymbolicExpr] = field(
        init=False, default_factory=dict, repr=False
    )
    _indexed_objective: IndexedSymbolicExpr = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._names = tuple(self.model.variables)
        self._index = {name: i for i, name in enumerate(self._names)}
        self._objective_exprs = [
            self.objective.variables.get(name, ParamExpr.constant_value(0.0)).copy()
            for name in self._names
        ]

        for constraint in self.model.constraints.values():
            row = {
                self._index[name]: coefficient.copy()
                for name, coefficient in constraint.coefficients.items()
                if name in self._index and not coefficient.is_constant(0.0)
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
                    self._ub_rows.append(
                        {column: value.scaled(-1.0) for column, value in row.items()}
                    )
                    self._ub_rhs.append(constraint.lower.scaled(-1.0))

        self._bounds = [
            (variable.lower, variable.upper)
            for variable in self.model.variables.values()
        ]

        # Conservative alternates can remain large even when the scalar target
        # has no residual capacity left.  Identify the common scalar-objective
        # case once so execution can prove "no improvement" from row/bound
        # intervals without launching HiGHS.
        if len(self.objective.variables) == 1:
            target_name = next(iter(self.objective.variables))
            if target_name in self._index:
                target_index = self._index[target_name]
                reconstruction_safe = all(
                    set(self.model.reconstruction[name].variables).issubset({target_name})
                    for name in self.requested
                    if name in self.model.reconstruction
                )
                if reconstruction_safe:
                    self._quick_scalar_name = target_name
                    self._quick_scalar_index = target_index
                    self._quick_scalar_reconstruction_safe = True
                    self._quick_scalar_rows = [
                        (row[target_index], row, rhs)
                        for row, rhs in zip(self._ub_rows, self._ub_rhs)
                        if target_index in row
                    ]
                    self._projected_scalar = self._compile_lower_bound_projection(
                        target_name, target_index
                    )

        # Lower the final reduced kernel once to the same indexed runtime frame
        # used by direct programs. Projection compilation above intentionally
        # runs first because it reasons over named ParamExpr structure.
        self._parameter_layout = self.model.indexed_parameter_layout()
        self._indexed_guards = self.model.indexed_guards()
        self._objective_exprs = [
            self._parameter_layout.lower(expr) for expr in self._objective_exprs
        ]
        self._ub_rows = [
            {column: self._parameter_layout.lower(expr) for column, expr in row.items()}
            for row in self._ub_rows
        ]
        self._ub_rhs = [self._parameter_layout.lower(expr) for expr in self._ub_rhs]
        self._eq_rows = [
            {column: self._parameter_layout.lower(expr) for column, expr in row.items()}
            for row in self._eq_rows
        ]
        self._eq_rhs = [self._parameter_layout.lower(expr) for expr in self._eq_rhs]
        self._bounds = [
            (
                None if lower is None else self._parameter_layout.lower(lower),
                None if upper is None else self._parameter_layout.lower(upper),
            )
            for lower, upper in self._bounds
        ]
        self._quick_scalar_rows = [
            (
                self._parameter_layout.lower(target),
                {column: self._parameter_layout.lower(expr) for column, expr in row.items()},
                self._parameter_layout.lower(rhs),
            )
            for target, row, rhs in self._quick_scalar_rows
        ]
        self._indexed_reconstruction = {
            name: self.model.lower_symbolic(self.model.reconstruction[name])
            for name in self.requested
        }
        self._indexed_objective = self.model.lower_symbolic(self.objective)
        self.stats["indexed_parameter_slots"] = len(self._parameter_layout.slot_names)
        self.stats["indexed_reduced_lp_programs"] = self.stats.get(
            "indexed_reduced_lp_programs", 0
        ) + 1


    def _compile_lower_bound_projection(
        self,
        target_name: str,
        target_index: int,
    ) -> DirectScalarProgram | None:
        """Freeze a compact scalar witness for a conservative kernel.

        Sequential conservative variants can retain every junior transaction
        even though their exact optimum is attained with all juniors at their
        residual lower bounds (normally zero).  The old runtime shortcut proved
        this by scanning the full reduced model on every priority.  Here the
        proof is structural and the lower-bound substitution is compiled once.

        The projection is exact whenever it is feasible if:

        * every non-target variable has a finite constant lower bound, and
        * in every normalized <= row that can upper-bound the target, all
          non-target coefficients are non-negative.

        Under those conditions moving a junior above its lower bound can never
        loosen a target upper bound.  Rows that only lower-bound the target may
        still make the canonical lower-bound witness infeasible; execution then
        falls back to the original exact reduced kernel.

        Build the one-variable model directly rather than copying/fixing the
        large source model.  This keeps the optimization compile-time linear in
        the sparse rows instead of reintroducing O(N^2) substitution work.
        """

        lower_values: dict[str, float] = {}
        for name, variable in self.model.variables.items():
            if name == target_name:
                continue
            lower = variable.lower
            if lower is None or not lower.is_constant():
                return None
            value = lower.constant_value_number()
            if not isfinite(value):
                return None
            lower_values[name] = value

        # For each normalized <= row with positive target coefficient, the
        # lower-bound witness must minimize every non-target term.
        for row in self._ub_rows:
            target_expr = row.get(target_index)
            if target_expr is None or target_expr.is_constant(0.0):
                continue
            target_sign = self.model.coefficient_sign(target_expr)
            if target_sign is None:
                return None
            if target_sign <= 0:
                continue
            for column, expression in row.items():
                if column == target_index or expression.is_constant(0.0):
                    continue
                sign = self.model.coefficient_sign(expression)
                if sign is None or sign < 0:
                    return None

        # A target-containing equality can allow bidirectional recourse.  Only
        # a genuinely scalar equality is safe for this projection.
        for row in self._eq_rows:
            target_expr = row.get(target_index)
            if target_expr is None or target_expr.is_constant(0.0):
                continue
            if any(
                column != target_index and not expression.is_constant(0.0)
                for column, expression in row.items()
            ):
                return None

        compact_constraints: dict[str, ParametricConstraint] = {}
        for name, constraint in self.model.constraints.items():
            shift = ParamExpr.constant_value(0.0)
            target_coefficient = constraint.coefficients.get(target_name)
            for variable_name, coefficient in constraint.coefficients.items():
                if variable_name == target_name or coefficient.is_constant(0.0):
                    continue
                lower_value = lower_values[variable_name]
                if abs(lower_value) > 1e-15:
                    shift = shift.plus(coefficient.scaled(-lower_value))

            lower = (
                None
                if constraint.lower is None
                else constraint.lower.plus(shift)
            )
            upper = (
                None
                if constraint.upper is None
                else constraint.upper.plus(shift)
            )
            coefficients = {}
            if target_coefficient is not None and not target_coefficient.is_constant(0.0):
                coefficients[target_name] = target_coefficient.copy()
            compact_constraints[name] = ParametricConstraint(
                name=name,
                lower=lower,
                upper=upper,
                coefficients=coefficients,
            )

        compact = CompilerModel(
            variables={target_name: self.model.variables[target_name].copy()},
            constraints=compact_constraints,
            reconstruction={
                name: expression.copy()
                for name, expression in self.model.reconstruction.items()
                if name in self.requested
            },
            source_variable_count=self.model.source_variable_count,
            # These source dictionaries are immutable during frozen execution.
            # Sharing them avoids copying O(total-system) metadata into every
            # scalar acceleration kernel; _all_slots() still binds only slots
            # referenced by this compact transformed IR.
            parameter_defaults=self.model.parameter_defaults,
            parameter_sources=self.model.parameter_sources,
            parameter_domains=self.model.parameter_domains,
            source_constraint_names=set(compact_constraints),
            guards=[guard.copy() for guard in self.model.guards],
            structural_proofs=[],
            residual_increment_bases={
                target_name: self.model.residual_increment_bases[target_name].copy()
            }
            if target_name in self.model.residual_increment_bases
            else {},
            uses_residual_state=self.model.uses_residual_state,
            directional_residual_constraints=set(
                self.model.directional_residual_constraints
            ),
        )

        return DirectScalarProgram(
            name=self.name + "/LOWER_BOUND_PROJECTION",
            requested=self.requested,
            objective=self.objective.copy(),
            maximization=self.maximization,
            source_variable_count=self.source_variable_count,
            active_variable_count=1,
            stats={},
            model=compact,
            active_name=target_name,
        )

    def _evaluated_bounds(
        self, parameters: np.ndarray
    ) -> list[tuple[float, float]]:
        result: list[tuple[float, float]] = []
        for lower, upper in self._bounds:
            lower_value = -inf if lower is None else lower.evaluate(parameters)
            upper_value = inf if upper is None else upper.evaluate(parameters)
            result.append((float(lower_value), float(upper_value)))
        return result

    def _quick_scalar_solution(
        self,
        parameters: np.ndarray,
    ) -> tuple[ProgramResult | None, list[tuple[float, float]] | None, bool]:
        """Try to solve a conservative scalar kernel without HiGHS.

        The first stage retains the very cheap exhausted-capacity proof: if a
        target-containing row proves that the scalar cannot rise above its
        lower bound, return immediately without materializing the other bounds.

        Otherwise interval bounds give a rigorous global upper bound on the
        scalar.  If assigning every non-target variable to its lower bound is a
        feasible witness at that global bound, the scalar optimum is proven and
        can also be returned directly.  This is common in sequential-priority
        systems: future transactions are non-negative residual increments and
        do not need to move in order to maximize the current right.

        Returns ``(result, numeric_bounds, no_improvement)``.  ``numeric_bounds``
        is retained for the LP fallback so failed analytic attempts do not pay
        to evaluate every bound twice.
        """

        if not self._quick_scalar_reconstruction_safe:
            return None, None, False
        target_index = self._quick_scalar_index
        target_name = self._quick_scalar_name
        if target_index is None or target_name is None:
            return None, None, False

        objective_coefficient = self._objective_exprs[target_index].evaluate(parameters)
        if abs(objective_coefficient) <= 1e-15:
            return None, None, False
        maximize_target = self.maximization == (objective_coefficient > 0)
        if not maximize_target:
            return None, None, False

        target_lower_expr, target_upper_expr = self._bounds[target_index]
        lower = -inf if target_lower_expr is None else target_lower_expr.evaluate(parameters)
        upper = inf if target_upper_expr is None else target_upper_expr.evaluate(parameters)
        lower = float(lower)
        upper = float(upper)
        if not isfinite(lower):
            return None, None, False

        def scalar_solution(value: float) -> ProgramResult:
            active_values = {target_name: value}
            requested_values = {
                name: expression.evaluate(active_values, parameters)
                for name, expression in self._indexed_reconstruction.items()
            }
            objective_value = self._indexed_objective.evaluate(
                active_values, parameters
            )
            return ProgramResult(objective_value, requested_values, active_values)

        scale = max(1.0, abs(lower), abs(upper) if isfinite(upper) else 1.0)
        if upper <= lower + 1e-9 * scale:
            return scalar_solution(lower), None, True

        # First pass: derive the rigorous target upper bound using only the
        # bounds referenced by target-containing rows.  This preserves the
        # very cheap zero-capacity exit for large conservative alternates.
        best_upper = upper
        for target_expression, row, rhs_expression in self._quick_scalar_rows:
            target_coefficient = target_expression.evaluate(parameters)
            if target_coefficient <= 1e-15:
                continue
            rhs = rhs_expression.evaluate(parameters)
            if not isfinite(rhs):
                continue

            min_other = 0.0
            bounded = True
            for column, expression in row.items():
                if column == target_index:
                    continue
                coefficient = expression.evaluate(parameters)
                if abs(coefficient) <= 1e-15:
                    continue
                other_lower_expr, other_upper_expr = self._bounds[column]
                bound_expr = other_lower_expr if coefficient > 0 else other_upper_expr
                if bound_expr is None:
                    bounded = False
                    break
                bound = bound_expr.evaluate(parameters)
                if not isfinite(bound):
                    bounded = False
                    break
                min_other += coefficient * bound
            if not bounded:
                continue

            best_upper = min(best_upper, (rhs - min_other) / target_coefficient)
            scale = max(
                1.0,
                abs(lower),
                abs(best_upper) if isfinite(best_upper) else 1.0,
            )
            if best_upper <= lower + 1e-9 * scale:
                return scalar_solution(lower), None, True

        if not isfinite(best_upper):
            return None, None, False

        # Prove attainability of the global upper bound with the canonical
        # sequential witness: every future/residual variable at its lower
        # bound.  If this witness is not feasible we simply retain the exact LP
        # fallback; no approximation is made.
        numeric_bounds = self._evaluated_bounds(parameters)
        witness = [bounds[0] for bounds in numeric_bounds]
        if any(not isfinite(value) for value in witness):
            return None, numeric_bounds, False
        candidate = max(lower, min(upper, best_upper))
        witness[target_index] = candidate

        for row, rhs_expression in zip(self._ub_rows, self._ub_rhs):
            rhs = rhs_expression.evaluate(parameters)
            if not isfinite(rhs):
                continue
            lhs = sum(
                expression.evaluate(parameters) * witness[column]
                for column, expression in row.items()
            )
            tolerance = 1e-8 * max(1.0, abs(lhs), abs(rhs))
            if lhs > rhs + tolerance:
                return None, numeric_bounds, False

        for row, rhs_expression in zip(self._eq_rows, self._eq_rhs):
            rhs = rhs_expression.evaluate(parameters)
            if not isfinite(rhs):
                return None, numeric_bounds, False
            lhs = sum(
                expression.evaluate(parameters) * witness[column]
                for column, expression in row.items()
            )
            tolerance = 1e-8 * max(1.0, abs(lhs), abs(rhs))
            if abs(lhs - rhs) > tolerance:
                return None, numeric_bounds, False

        return scalar_solution(candidate), numeric_bounds, candidate <= lower + 1e-9 * max(1.0, abs(lower))

    @staticmethod
    def _evaluate_sparse(
        rows: list[dict[int, ParamExpr]],
        width: int,
        parameters: np.ndarray,
    ) -> csr_matrix | None:
        if not rows:
            return None
        r: list[int] = []
        c: list[int] = []
        data: list[float] = []
        for row_index, row in enumerate(rows):
            for column, expression in row.items():
                value = expression.evaluate(parameters)
                if abs(value) > 1e-15:
                    r.append(row_index)
                    c.append(column)
                    data.append(value)
        return csr_matrix((data, (r, c)), shape=(len(rows), width), dtype=float)

    def execute(self, engine) -> ProgramResult:
        if self._projected_scalar is not None:
            projected_model = self._projected_scalar.model
            try:
                projected_parameters = self._projected_scalar.runtime_parameter_array(
                    engine
                )
                self._projected_scalar.check_indexed_guards(projected_parameters)
                result = self._projected_scalar.execute_with_parameters(
                    engine, projected_parameters
                )
            except (LPSolverError, ValueError, KeyError):
                session = getattr(engine, "v2_session", None)
                if session is not None:
                    session.stats[
                        "execution_reduced_lp_projected_scalar_fallbacks"
                    ] += 1
            else:
                session = getattr(engine, "v2_session", None)
                if session is not None:
                    session.stats[
                        "execution_reduced_lp_projected_scalar_shortcuts"
                    ] += 1
                return result

        parameters = self._parameter_layout.read(engine)
        self.model.check_indexed_guards(self._indexed_guards, parameters)
        quick, numeric_bounds, no_improvement = self._quick_scalar_solution(parameters)
        if quick is not None:
            session = getattr(engine, "v2_session", None)
            if session is not None:
                session.stats["execution_reduced_lp_scalar_shortcuts"] += 1
                if no_improvement:
                    session.stats["execution_reduced_lp_no_improvement_shortcuts"] += 1
            return quick

        if numeric_bounds is None:
            numeric_bounds = self._evaluated_bounds(parameters)
        A_ub = self._evaluate_sparse(
            self._ub_rows, len(self._names), parameters
        )
        b_ub = None
        if self._ub_rhs:
            raw_b_ub = np.asarray(
                [expr.evaluate(parameters) for expr in self._ub_rhs], dtype=float
            )
            if np.isnan(raw_b_ub).any() or np.isneginf(raw_b_ub).any():
                raise LPSolverError(
                    "v2 frozen reduced LP kernel has an invalid -inf/nan upper RHS"
                )
            active_rows = np.isfinite(raw_b_ub)
            if active_rows.any():
                b_ub = raw_b_ub[active_rows]
                if A_ub is not None:
                    A_ub = A_ub[active_rows]
            else:
                A_ub = None

        A_eq = self._evaluate_sparse(
            self._eq_rows, len(self._names), parameters
        )
        b_eq = None
        if self._eq_rhs:
            b_eq = np.asarray(
                [expr.evaluate(parameters) for expr in self._eq_rhs], dtype=float
            )
            if not np.isfinite(b_eq).all():
                raise LPSolverError(
                    "v2 frozen reduced LP kernel has a non-finite equality RHS"
                )

        bounds = [
            (
                None if np.isneginf(lower) else lower,
                None if np.isposinf(upper) else upper,
            )
            for lower, upper in numeric_bounds
        ]
        objective_vector = np.asarray(
            [expr.evaluate(parameters) for expr in self._objective_exprs], dtype=float
        )
        c = -objective_vector if self.maximization else objective_vector
        result = linprog(
            c=c,
            A_ub=A_ub,
            b_ub=b_ub,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs-ds",
        )
        if not result.success:
            raise LPSolverError(
                "v2 frozen reduced LP kernel could not solve objective: "
                f"{result.status}: {result.message}"
            )
        active_values = {
            name: float(result.x[i]) for i, name in enumerate(self._names)
        }
        requested_values = {
            name: expression.evaluate(active_values, parameters)
            for name, expression in self._indexed_reconstruction.items()
        }
        objective_value = self._indexed_objective.evaluate(active_values, parameters)
        return ProgramResult(objective_value, requested_values, active_values)

    def text(self) -> str:
        direction = "MAXIMIZE" if self.maximization else "MINIMIZE"
        parameterized_coefficients = sum(
            bool(coefficient.slots())
            for constraint in self.model.constraints.values()
            for coefficient in constraint.coefficients.values()
        )
        lines = [
            f"{self.name}: FROZEN REDUCED LP {direction} {self.objective.text()}",
            f"    source variables: {self.source_variable_count}",
            f"    active variables after v2 presolve: {self.active_variable_count}",
            f"    active constraints: {len(self.model.constraints)}",
            f"    parameterized matrix coefficients: {parameterized_coefficients}",
            (
                "    final transformed IR: residual transaction increments + "
                "frozen sparse rows"
                if self.model.uses_residual_state
                else "    final transformed IR: frozen sparse rows"
            ),
            (
                "    residual RHS values come from execution ResidualState; "
                "no production-LP rows are solved"
                if self.model.uses_residual_state
                else "    matrix sparsity is frozen; parameterized coefficients/bounds/RHS are refreshed at runtime"
            ),
        ]
        if self.model.guards:
            lines.append("    runtime guards:")
            for guard in self.model.guards:
                lines.append(f"        REQUIRE {guard.text()}")
                lines.append(f"            # {guard.description}")
        if self.model.structural_proofs:
            lines.append("    structural simplification proofs:")
            for proof in self.model.structural_proofs:
                lines.append(f"        PROVEN {proof.text()}")
                lines.append(f"            # {proof.description}")
        if self.model.notes:
            lines.append("    frozen compiler substitutions:")
            lines.extend(f"        {note}" for note in self.model.notes)
        return "\n".join(lines)
