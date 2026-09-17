"""Explicit day/priority execution IR for compiled v2.

The objective compiler answers "how far can this priority increase?".  This
module owns the state transition around that answer: assign the transaction,
commit its LP-row effects to a residual-state view, update natural flow, run
spill/equal-priority kernels, and repeat the priority routine on pass two.

The authoritative mutable LP is still kept in sync for reduced kernels.  The
residual state is therefore not an explanatory trace: it is an executable IR
state updated by the same nodes that commit each allocation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isfinite
from typing import Iterable

import numpy as np

from ..models import (
    CorePropSchedule,
    CoreScheduleVariable,
    CoreSeqSchedule,
    PathTrxn,
    TrxnGroup,
    ZoneTypes,
)
from .model import (
    CompilerModel,
    IndexedParameterLayout,
    IndexedParamExpr,
    IndexedSymbolicExpr,
    ParamExpr,
    SymbolicExpr,
)


@dataclass(frozen=True)
class ResidualEffect:
    """One logical transaction's coefficient in one compiler LP row."""

    constraint_name: str
    coefficient: ParamExpr
    has_lower: bool
    has_upper: bool
    equality: bool = False
    constraint_index: int | None = None
    indexed_coefficient: IndexedParamExpr | None = None

    def with_constraint_index(
        self,
        index: int,
        parameter_layout: IndexedParameterLayout | None = None,
    ) -> "ResidualEffect":
        return ResidualEffect(
            constraint_name=self.constraint_name,
            coefficient=self.coefficient,
            has_lower=self.has_lower,
            has_upper=self.has_upper,
            equality=self.equality,
            constraint_index=index,
            indexed_coefficient=(
                None
                if parameter_layout is None
                else parameter_layout.lower(self.coefficient)
            ),
        )

    def coefficient_value(
        self, engine, parameters: np.ndarray | None = None
    ) -> float:
        if self.indexed_coefficient is not None and parameters is not None:
            return self.indexed_coefficient.evaluate(parameters)
        parameters_by_name: dict[str, float] = {}
        for slot in self.coefficient.slots():
            if slot.startswith("coefficient[") and slot.endswith("]"):
                body = slot[len("coefficient["):-1]
                constraint_name, variable_name = body.split(",", 1)
                parameters_by_name[slot] = engine.cons[constraint_name].coefficients.get(
                    variable_name, 0.0
                )
            else:
                raise KeyError(
                    "Residual-effect coefficient contains a non-coefficient "
                    f"runtime parameter {slot!r}"
                )
        return self.coefficient.evaluate(parameters_by_name)

    def update_text(self, transaction_id: str) -> list[str]:
        coefficient = self.coefficient.text()
        increment = f"{transaction_id}.increment"
        amount = increment if self.coefficient.is_constant(1.0) else f"({coefficient}) * {increment}"
        if self.equality:
            return [f"constraint[{self.constraint_name}].remaining -= {amount}"]
        lines: list[str] = []
        if self.has_lower:
            lines.append(
                f"constraint[{self.constraint_name}].remaining_lower -= {amount}"
            )
        if self.has_upper:
            lines.append(
                f"constraint[{self.constraint_name}].remaining_upper -= {amount}"
            )
        return lines


@dataclass(frozen=True)
class DerivedSlackMember:
    """One reporting slack reconstructed from a measurement residual."""

    transaction_id: str
    anchor_variable: str
    flow_id: str
    coefficient: float
    spill_to_natural: bool = False
    receiving_zone_id: str | None = None


@dataclass(frozen=True)
class DerivedSlackRule:
    """Residual decomposition rule for one interzone-flow measurement row."""

    constraint_name: str
    flow_id: str
    members: tuple[DerivedSlackMember, ...]

    @property
    def positive_members(self) -> tuple[DerivedSlackMember, ...]:
        return tuple(member for member in self.members if member.coefficient > 0)

    @property
    def negative_members(self) -> tuple[DerivedSlackMember, ...]:
        return tuple(member for member in self.members if member.coefficient < 0)




@dataclass
class ConstraintResidual:
    """Compatibility value object for constructing residual test fixtures."""

    lower: float = -inf
    upper: float = inf
    equality: bool = False


@dataclass(frozen=True)
class ResidualLayout:
    """Frozen integer layout for residual constraint sides."""

    names: tuple[str, ...]
    index: dict[str, int]

    @classmethod
    def from_names(cls, names: Iterable[str]) -> "ResidualLayout":
        ordered = tuple(sorted(set(names)))
        return cls(ordered, {name: i for i, name in enumerate(ordered)})


@dataclass(init=False)
class ResidualState:
    """Executable residual RHS state in contiguous numeric arrays.

    Constraint names are resolved once through :class:`ResidualLayout`; all
    allocation commits then update lower/upper arrays by integer offset.
    """

    layout: ResidualLayout
    lower: np.ndarray
    upper: np.ndarray
    equality: np.ndarray

    def __init__(
        self,
        layout: ResidualLayout | None = None,
        lower: np.ndarray | None = None,
        upper: np.ndarray | None = None,
        equality: np.ndarray | None = None,
        *,
        constraints: dict[str, ConstraintResidual] | None = None,
    ) -> None:
        if constraints is not None:
            layout = ResidualLayout.from_names(constraints)
            lower = np.asarray(
                [constraints[name].lower for name in layout.names], dtype=float
            )
            upper = np.asarray(
                [constraints[name].upper for name in layout.names], dtype=float
            )
            equality = np.asarray(
                [constraints[name].equality for name in layout.names], dtype=bool
            )
        if layout is None or lower is None or upper is None or equality is None:
            raise TypeError(
                "ResidualState requires either indexed arrays or constraints="
            )
        self.layout = layout
        self.lower = lower
        self.upper = upper
        self.equality = equality

    @classmethod
    def from_engine(
        cls, engine, layout: ResidualLayout | None = None
    ) -> "ResidualState":
        if layout is None:
            layout = ResidualLayout.from_names(engine.cons)
        lower = np.empty(len(layout.names), dtype=float)
        upper = np.empty(len(layout.names), dtype=float)
        equality = np.empty(len(layout.names), dtype=bool)
        for index, name in enumerate(layout.names):
            constraint = engine.cons[name]
            lb = float(constraint.lb())
            ub = float(constraint.ub())
            lower[index] = lb
            upper[index] = ub
            equality[index] = (
                isfinite(lb)
                and isfinite(ub)
                and abs(lb - ub) <= 1e-12
            )
        return cls(layout=layout, lower=lower, upper=upper, equality=equality)

    def index_of(self, name: str) -> int:
        return self.layout.index[name]

    def bounds(self, name: str) -> tuple[float, float]:
        index = self.layout.index[name]
        return float(self.lower[index]), float(self.upper[index])

    def bounds_at(self, index: int) -> tuple[float, float]:
        return float(self.lower[index]), float(self.upper[index])

    def add_upper(self, name: str, delta: float) -> None:
        index = self.layout.index.get(name)
        if index is not None and isfinite(self.upper[index]):
            self.upper[index] += delta

    def apply_effects(
        self,
        engine,
        effects: Iterable[ResidualEffect],
        delta: float,
        parameters: np.ndarray | None = None,
    ) -> None:
        if abs(delta) <= 1e-15:
            return
        for effect in effects:
            index = effect.constraint_index
            if index is None:
                index = self.layout.index.get(effect.constraint_name)
            if index is None:
                continue
            amount = effect.coefficient_value(engine, parameters) * delta
            if effect.has_lower and isfinite(self.lower[index]):
                self.lower[index] -= amount
            if effect.has_upper and isfinite(self.upper[index]):
                self.upper[index] -= amount

    def snapshot(self) -> dict[str, tuple[float, float]]:
        return {
            name: (float(self.lower[index]), float(self.upper[index]))
            for index, name in enumerate(self.layout.names)
        }


@dataclass
class V2ExecutionContext:
    apportioner: object
    residual_state: ResidualState
    effects: dict[str, tuple[ResidualEffect, ...]]
    residual_parameters: np.ndarray | None = None

    def target_name(self, var: PathTrxn | TrxnGroup) -> str | None:
        if isinstance(var, PathTrxn):
            return self.apportioner.tm.get_anchor_var(var)
        return var.id

    def current_value(self, var: PathTrxn | TrxnGroup) -> float:
        target = self.target_name(var)
        if not target:
            return 0.0
        return self.apportioner.cur_trxn_value.get(target, 0.0)

    def commit_residual(self, var: PathTrxn | TrxnGroup, delta: float) -> None:
        effects = self.effects.get(var.id, ())
        self.residual_state.apply_effects(
            self.apportioner.engine,
            effects,
            delta,
            self.residual_parameters,
        )
        if abs(delta) > 1e-15:
            self.apportioner.engine.v2_session.stats[
                "execution_residual_commits"
            ] += 1
            self.apportioner.engine.v2_session.stats[
                "execution_residual_row_updates"
            ] += len(effects)


class ExecutionNode:
    def execute(self, context: V2ExecutionContext, **kwargs) -> None:
        raise NotImplementedError

    def text(self, *, indent: int = 0) -> list[str]:
        raise NotImplementedError


@dataclass
class AssignTransaction(ExecutionNode):
    """Execute one sequential priority and commit its state transitions."""

    var: PathTrxn | TrxnGroup
    effects: tuple[ResidualEffect, ...] = ()

    def execute(self, context: V2ExecutionContext, **kwargs) -> None:
        apportioner = context.apportioner
        target = context.target_name(self.var)
        if not target:
            return

        original_ub = apportioner._minimize_minus_vars([self.var])
        value_before = apportioner.cur_trxn_value.get(target, 0.0)
        try:
            new_value = apportioner._with_feasibility_fallback(
                "compiled IR maximize " + self.var.id,
                lambda: apportioner.engine.maximize_and_update_variable(target),
            )
            apportioner.cur_trxn_value[target] = new_value
            delta = new_value - value_before

            # These are explicit execution-IR state transitions.  The engine's
            # lower-bound commit remains synchronized for residual LP kernels.
            context.commit_residual(self.var, delta)
            apportioner._apply_natural_flow_change(self.var, delta)
        finally:
            apportioner._reset_minus_vars(original_ub)

    def text(self, *, indent: int = 0) -> list[str]:
        pad = " " * indent
        lines = [
            f"{pad}{self.var.id}.before = {self.var.id}.current",
            f"{pad}{self.var.id} = COMPILED_MAX({self.var.id})",
            f"{pad}{self.var.id}.increment = {self.var.id} - {self.var.id}.before",
        ]
        for effect in self.effects:
            lines.extend(f"{pad}{line}" for line in effect.update_text(self.var.id))
        if isinstance(self.var, PathTrxn):
            lines.append(
                f"{pad}UPDATE NATURAL FLOW for {self.var.id} by {self.var.id}.increment"
            )
        return lines


@dataclass
class EqualPriorityKernel(ExecutionNode):
    """Execute a compiled logical-transaction proportional cohort."""

    series: CorePropSchedule
    member_ids: tuple[str, ...]
    effects: dict[str, tuple[ResidualEffect, ...]]

    def execute(self, context: V2ExecutionContext, **kwargs) -> None:
        # Apportioner still owns schedule traversal/NF bookkeeping, while the
        # v2 backend now performs both common-increment solves and blocked-member
        # classification against one frozen logical-transaction residual model.
        context.apportioner.maximize_series(self.series)

    def text(self, *, indent: int = 0) -> list[str]:
        pad = " " * indent
        lines = [
            f"{pad}EQUAL-PRIORITY LOGICAL KERNEL ({', '.join(self.member_ids)})",
            f"{pad}REPEAT UNTIL NO ACTIVE MEMBER CAN INCREASE:",
            f"{pad}    common_increment = COMPILED_COHORT_MAX(active logical transactions)",
            f"{pad}    REQUIRE dTRXN[member] >= member.proportion * common_increment",
            f"{pad}    FOR member IN active_members:",
            f"{pad}        member.increment = member.proportion * common_increment",
            f"{pad}        member = member.current + member.increment",
            f"{pad}        UPDATE RESIDUAL STATE using logical transaction coefficients",
            f"{pad}    classify blocked members with SAME LOGICAL KERNEL",
            f"{pad}    remove blocked/capped members and recalculate proportions",
        ]
        return lines


@dataclass
class ExecuteSchedule(ExecutionNode):
    """Interpret the structural priority schedule using explicit IR nodes."""

    effects: dict[str, tuple[ResidualEffect, ...]]

    def execute_schedule(self, context: V2ExecutionContext, schedule: CoreSeqSchedule) -> None:
        from ..apportioner import SLACK_TRXN_PRIORITY

        for entry in schedule.series:
            if entry.priority >= SLACK_TRXN_PRIORITY:
                return
            item = entry.item
            if isinstance(item, CoreScheduleVariable):
                AssignTransaction(
                    item.var,
                    self.effects.get(item.var.id, ()),
                ).execute(context)
            elif isinstance(item, CorePropSchedule):
                member_ids = tuple(
                    prop.item.var.id
                    for prop in item.series
                    if isinstance(prop.item, CoreScheduleVariable)
                )
                EqualPriorityKernel(item, member_ids, self.effects).execute(context)
            elif isinstance(item, CoreSeqSchedule):
                self.execute_schedule(context, item)
            else:
                raise TypeError(f"Unsupported schedule node {type(item)!r}")

    def execute(self, context: V2ExecutionContext, **kwargs) -> None:
        schedule = kwargs["schedule"]
        self.execute_schedule(context, schedule)

    def text(self, *, indent: int = 0) -> list[str]:
        pad = " " * indent
        return [f"{pad}EXECUTE FROZEN PRIORITY SCHEDULE"]


@dataclass
class DeriveReportingSlacks(ExecutionNode):
    """Calculate all reporting slacks directly from leftover measurements."""

    rules: tuple[DerivedSlackRule, ...]
    apply_spill_credit: bool = False

    @staticmethod
    def _signed_residual(
        state: ResidualState,
        rule: DerivedSlackRule,
    ) -> float | None:
        index = state.layout.index.get(rule.constraint_name)
        if index is None:
            return None
        lower = float(state.lower[index])
        upper = float(state.upper[index])
        if isfinite(lower) and isfinite(upper):
            scale = max(1.0, abs(lower), abs(upper))
            if abs(lower - upper) <= 1e-9 * scale:
                return 0.5 * (lower + upper)
        # No fixed measurement means there is no unique "leftover
        # measurement" to report.  Leave the reporting slack at zero.
        return None

    def _assign_rule(
        self,
        context: V2ExecutionContext,
        rule: DerivedSlackRule,
    ) -> dict[str, float]:
        apportioner = context.apportioner
        for member in rule.members:
            apportioner.cur_trxn_value[member.anchor_variable] = 0.0

        residual = self._signed_residual(context.residual_state, rule)
        if residual is None or abs(residual) <= 1e-6:
            return {member.transaction_id: 0.0 for member in rule.members}

        candidates = (
            rule.positive_members if residual > 0 else rule.negative_members
        )
        if not candidates:
            raise ValueError(
                f"Measurement residual {residual:g} for {rule.flow_id!r} "
                "cannot be represented by the available reporting slack direction"
            )

        # There is normally exactly one slack per direction.  If a future LP
        # builder produces duplicates, keep the residual representation
        # deterministic by assigning it to the first structural member.
        member = candidates[0]
        value = residual / member.coefficient
        if value < -1e-9:
            raise ValueError(
                f"Derived reporting slack {member.transaction_id!r} became "
                f"negative ({value:g})"
            )
        if abs(value) <= 1e-6:
            value = 0.0
        apportioner.cur_trxn_value[member.anchor_variable] = value
        return {
            candidate.transaction_id: (value if candidate is member else 0.0)
            for candidate in rule.members
        }

    def _apply_spill_credits(
        self,
        context: V2ExecutionContext,
        values: dict[str, float],
    ) -> None:
        apportioner = context.apportioner
        previous_remaining_nf = apportioner.nfc.remaining_natural_at_zone.copy()

        for rule in self.rules:
            for member in rule.members:
                if not member.spill_to_natural:
                    continue
                value = values.get(member.transaction_id, 0.0)
                if value <= 1e-6 or member.receiving_zone_id is None:
                    continue

                # The spill amount is a derived physical residual, not an LP
                # objective.  Freeze that derived residual for pass two so the
                # reallocation cannot consume through it and change the amount
                # of water that was credited to natural flow.
                apportioner.engine.update_variable_bounds(
                    member.anchor_variable, lb=value, ub=value
                )

                trxn = next(
                    (
                        item for item in apportioner.tm.all_trxns
                        if isinstance(item, PathTrxn) and item.id == member.transaction_id
                    ),
                    None,
                )
                if trxn is None or len(trxn.path) != 1:
                    continue
                path_item = trxn.path[0]
                flow = apportioner.gm.get_flow_by_id(path_item.flow_id)
                endpoint_loss = (
                    flow.loss_to_zone if path_item.factor > 0
                    else flow.loss_from_zone
                )
                credit_at_zone = endpoint_loss.transform_total_flow(
                    value, date=apportioner.dm.cur_date
                )
                apportioner.nfc.apply_committed_allocation(
                    member.receiving_zone_id, -credit_at_zone
                )

        # Synchronize NF upper bounds with the newly derived physical spill.
        from ..apportioner import PREFIX_NF_ZONE

        for zone_id, previous in previous_remaining_nf.items():
            current = apportioner.nfc.remaining_natural_at_zone[zone_id]
            delta = current - previous
            if abs(delta) <= 1e-15:
                continue
            constraint_name = PREFIX_NF_ZONE + zone_id
            lower, upper = apportioner.engine.get_constraint_bounds(constraint_name)
            if isfinite(upper):
                apportioner.engine.update_constraint_ub(
                    constraint_name, ub=upper + delta
                )
                context.residual_state.add_upper(constraint_name, delta)

    def execute(self, context: V2ExecutionContext, **kwargs) -> None:
        all_values: dict[str, float] = {}
        for rule in self.rules:
            all_values.update(self._assign_rule(context, rule))

        context.apportioner.engine.v2_session.stats[
            "derived_slack_reconciliations"
        ] += 1
        context.apportioner.engine.v2_session.stats[
            "derived_slack_values"
        ] += len(all_values)

        if self.apply_spill_credit:
            self._apply_spill_credits(context, all_values)

    def text(self, *, indent: int = 0) -> list[str]:
        pad = " " * indent
        lines = [
            f"{pad}DERIVE REPORTING SLACKS FROM LEFTOVER MEASUREMENTS:",
        ]
        for rule in self.rules:
            positive = [m.transaction_id for m in rule.positive_members]
            negative = [m.transaction_id for m in rule.negative_members]
            if positive and negative:
                lines.append(
                    f"{pad}    residual[{rule.flow_id}] = "
                    f"constraint[{rule.constraint_name}].remaining"
                )
                lines.append(
                    f"{pad}    {positive[0]} = MAX(residual[{rule.flow_id}], 0)"
                )
                lines.append(
                    f"{pad}    {negative[0]} = MAX(-residual[{rule.flow_id}], 0)"
                )
            elif positive:
                lines.append(
                    f"{pad}    {positive[0]} = MAX("
                    f"constraint[{rule.constraint_name}].remaining, 0)"
                )
            elif negative:
                lines.append(
                    f"{pad}    {negative[0]} = MAX(-"
                    f"constraint[{rule.constraint_name}].remaining, 0)"
                )
        if self.apply_spill_credit:
            lines.append(
                f"{pad}    credit derived storage-to-natural residuals to natural flow"
            )
        return lines


@dataclass(frozen=True)
class FrozenPathReconstruction:
    """Compile-time path-output reconstruction for one structural regime.

    Path collapse is a compiler transform, so repeating it after every daily
    solve is pure overhead. This object retains only the final reconstruction
    expressions and the minimal runtime value/parameter sources they require.
    """

    parameter_model: CompilerModel
    expressions: tuple[tuple[str, SymbolicExpr], ...]
    value_sources: tuple[tuple[str, str], ...]
    _parameter_layout: IndexedParameterLayout = field(init=False, repr=False, compare=False)
    _indexed_expressions: tuple[tuple[str, IndexedSymbolicExpr], ...] = field(
        init=False, default=(), repr=False, compare=False
    )

    def __post_init__(self) -> None:
        layout = self.parameter_model.indexed_parameter_layout()
        object.__setattr__(self, "_parameter_layout", layout)
        object.__setattr__(
            self,
            "_indexed_expressions",
            tuple(
                (name, self.parameter_model.lower_symbolic(expression))
                for name, expression in self.expressions
            ),
        )

    def execute(self, apportioner) -> None:
        parameters = self._parameter_layout.read(apportioner.engine)
        values: dict[str, float] = {}
        for logical_name, source_name in self.value_sources:
            if source_name in apportioner.cur_trxn_value:
                values[logical_name] = apportioner.cur_trxn_value[source_name]
            else:
                variable = apportioner.engine.vars.get(source_name)
                if variable is None:
                    raise KeyError(
                        f"Frozen path reconstruction is missing source {source_name!r}"
                    )
                values[logical_name] = float(variable.lb())

        for source_name, expression in self._indexed_expressions:
            apportioner.cur_trxn_value[source_name] = expression.evaluate(
                values, parameters
            )


@dataclass
class FinalDerivedOutputs(ExecutionNode):
    """Reconstruct final reporting values without rebuilding compiler IR."""

    slack_rules: tuple[DerivedSlackRule, ...]
    path_reconstruction: FrozenPathReconstruction | None = None

    def execute(self, context: V2ExecutionContext, **kwargs) -> None:
        # Refresh reporting slacks using the final post-reallocation residuals.
        DeriveReportingSlacks(self.slack_rules).execute(context)
        if self.path_reconstruction is None:
            raise RuntimeError("FinalDerivedOutputs requires frozen path reconstruction")
        self.path_reconstruction.execute(context.apportioner)
        context.apportioner.engine.v2_session.stats[
            "derived_path_reconstructions"
        ] += 1

    def text(self, *, indent: int = 0) -> list[str]:
        pad = " " * indent
        return [
            *DeriveReportingSlacks(self.slack_rules).text(indent=indent),
            f"{pad}RECONSTRUCT transaction path legs from frozen continuity equations",
        ]


@dataclass
class DayExecutionProgram:
    """The executable two-pass compiled-v2 day routine."""

    effects_by_regime: dict[tuple, dict[str, tuple[ResidualEffect, ...]]] = field(
        default_factory=dict
    )
    slack_rules_by_regime: dict[tuple, tuple[DerivedSlackRule, ...]] = field(
        default_factory=dict
    )
    path_reconstruction_by_regime: dict[tuple, FrozenPathReconstruction] = field(
        default_factory=dict
    )
    residual_layout_by_regime: dict[tuple, ResidualLayout] = field(
        default_factory=dict
    )
    residual_parameter_layout_by_regime: dict[tuple, IndexedParameterLayout] = field(
        default_factory=dict
    )

    def add_regime(
        self,
        signature: tuple,
        effects: dict[str, tuple[ResidualEffect, ...]],
        slack_rules: tuple[DerivedSlackRule, ...] = (),
        path_reconstruction: FrozenPathReconstruction | None = None,
        constraint_names: Iterable[str] = (),
        residual_parameter_model: CompilerModel | None = None,
    ) -> None:
        layout = ResidualLayout.from_names(
            constraint_names or (
                effect.constraint_name
                for transaction_effects in effects.values()
                for effect in transaction_effects
            )
        )
        parameter_layout: IndexedParameterLayout | None = None
        if residual_parameter_model is not None:
            coefficient_expressions = {
                f"effect_{transaction_id}_{effect_index}": SymbolicExpr.parameter(
                    effect.coefficient
                )
                for transaction_id, transaction_effects in effects.items()
                for effect_index, effect in enumerate(transaction_effects)
                if effect.coefficient.slots()
            }
            if coefficient_expressions:
                coefficient_model = CompilerModel(
                    variables={},
                    constraints={},
                    reconstruction=coefficient_expressions,
                    source_variable_count=residual_parameter_model.source_variable_count,
                    parameter_defaults=residual_parameter_model.parameter_defaults,
                    parameter_sources=residual_parameter_model.parameter_sources,
                    parameter_domains=residual_parameter_model.parameter_domains,
                )
                parameter_layout = coefficient_model.indexed_parameter_layout()
                self.residual_parameter_layout_by_regime.setdefault(
                    signature, parameter_layout
                )

        indexed_effects = {
            transaction_id: tuple(
                effect.with_constraint_index(
                    layout.index[effect.constraint_name], parameter_layout
                )
                for effect in transaction_effects
                if effect.constraint_name in layout.index
            )
            for transaction_id, transaction_effects in effects.items()
        }
        self.effects_by_regime.setdefault(signature, indexed_effects)
        self.residual_layout_by_regime.setdefault(signature, layout)
        self.slack_rules_by_regime.setdefault(signature, slack_rules)
        if path_reconstruction is not None:
            self.path_reconstruction_by_regime.setdefault(
                signature, path_reconstruction
            )

    @staticmethod
    def _for_regime(mapping: dict, signature: tuple, description: str):
        value = mapping.get(signature)
        if value is not None:
            return value
        # Parameterized coefficients can change the numeric engine fingerprint
        # while leaving the frozen compiler regime unchanged.
        if len(mapping) == 1:
            return next(iter(mapping.values()))
        raise KeyError(f"No frozen {description} regime for runtime LP structure")

    def effects_for(self, signature: tuple) -> dict[str, tuple[ResidualEffect, ...]]:
        return self._for_regime(
            self.effects_by_regime, signature, "residual-effect"
        )

    def slack_rules_for(self, signature: tuple) -> tuple[DerivedSlackRule, ...]:
        return self._for_regime(
            self.slack_rules_by_regime, signature, "derived-slack"
        )

    def residual_parameter_layout_for(
        self, signature: tuple
    ) -> IndexedParameterLayout | None:
        if not self.residual_parameter_layout_by_regime:
            return None
        return self._for_regime(
            self.residual_parameter_layout_by_regime,
            signature,
            "residual-parameter-layout",
        )

    def residual_layout_for(self, signature: tuple) -> ResidualLayout:
        return self._for_regime(
            self.residual_layout_by_regime, signature, "residual-layout"
        )

    def path_reconstruction_for(
        self, signature: tuple
    ) -> FrozenPathReconstruction:
        return self._for_regime(
            self.path_reconstruction_by_regime,
            signature,
            "path-reconstruction",
        )

    def execute(self, apportioner, *, date: str, schedule: CoreSeqSchedule, regime_signature: tuple) -> None:
        effects = self.effects_for(regime_signature)
        slack_rules = self.slack_rules_for(regime_signature)
        path_reconstruction = self.path_reconstruction_for(regime_signature)
        residual_layout = self.residual_layout_for(regime_signature)
        residual_parameter_layout = self.residual_parameter_layout_for(
            regime_signature
        )
        residual_parameters = (
            None
            if residual_parameter_layout is None
            else residual_parameter_layout.read(apportioner.engine)
        )
        context = V2ExecutionContext(
            apportioner=apportioner,
            residual_state=ResidualState.from_engine(
                apportioner.engine, residual_layout
            ),
            effects=effects,
            residual_parameters=residual_parameters,
        )
        # Expose the already-resolved structural regime and residual state to
        # the backend. Frozen kernels can then use direct compiler references
        # instead of re-fingerprinting the production LP during every solve.
        apportioner.engine.v2_regime_signature = regime_signature
        apportioner.engine.v2_residual_state = context.residual_state
        apportioner.engine.v2_residual_effects = effects
        apportioner.engine.v2_residual_parameters = residual_parameters

        schedule_node = ExecuteSchedule(effects)
        schedule_node.execute(context, schedule=schedule)

        # Slacks are deterministic reporting residuals.  Derive them after the
        # first allocation pass and credit any storage-to-natural residual as a
        # physical spill before the conservative reallocation pass.
        DeriveReportingSlacks(
            slack_rules, apply_spill_credit=True
        ).execute(context)

        # A second pass exists to handle storage recourse (which can matter
        # even when the derived spill is numerically zero) and physical slacks
        # that can spill from a non-natural zone back to natural flow.  If the
        # compiled graph contains neither possibility, pass two is provably a
        # duplicate of pass one and can be omitted.
        has_storage = any(
            zone.type == ZoneTypes.STORAGE
            for zone in apportioner.gm.graph.zones
        )
        has_possible_spill_credit = any(
            member.spill_to_natural
            for rule in slack_rules
            for member in rule.members
        )
        if has_storage or has_possible_spill_credit:
            schedule_node.execute(context, schedule=schedule)

        # No final LP solve is needed merely to retrieve slack/path-leg values.
        FinalDerivedOutputs(slack_rules, path_reconstruction).execute(context)

    @staticmethod
    def _flatten_vars(schedule: CoreSeqSchedule) -> list[PathTrxn | TrxnGroup]:
        result: list[PathTrxn | TrxnGroup] = []
        for entry in schedule.series:
            item = entry.item
            if isinstance(item, CoreScheduleVariable):
                result.append(item.var)
            elif isinstance(item, CorePropSchedule):
                for prop in item.series:
                    if isinstance(prop.item, CoreScheduleVariable):
                        result.append(prop.item.var)
            elif isinstance(item, CoreSeqSchedule):
                result.extend(DayExecutionProgram._flatten_vars(item))
        return result

    def text(
        self,
        schedule: CoreSeqSchedule,
        effects: dict[str, tuple[ResidualEffect, ...]],
        slack_rules: tuple[DerivedSlackRule, ...] = (),
        assignment_renderer=None,
    ) -> str:
        lines = [
            "EXECUTABLE DAY ROUTINE",
            "======================",
            "",
            "INITIALIZE residual state from today's parameterized LP bounds/RHS",
            "",
            "PASS 1 — PRIORITY ALLOCATION",
            "----------------------------",
        ]

        def render_schedule(seq: CoreSeqSchedule, indent: int = 0) -> None:
            from ..apportioner import SLACK_TRXN_PRIORITY

            for entry in seq.series:
                if entry.priority >= SLACK_TRXN_PRIORITY:
                    return
                item = entry.item
                if isinstance(item, CoreScheduleVariable):
                    var = item.var
                    pad = " " * indent
                    lines.append(f"{pad}{var.id}.before = {var.id}.current")
                    if assignment_renderer is None:
                        lines.append(f"{pad}{var.id} = COMPILED_MAX({var.id})")
                    else:
                        lines.extend(assignment_renderer(var.id, indent=indent))
                    lines.append(
                        f"{pad}{var.id}.increment = {var.id} - {var.id}.before"
                    )
                    for effect in effects.get(var.id, ()):
                        lines.extend(
                            f"{pad}{line}" for line in effect.update_text(var.id)
                        )
                    if isinstance(var, PathTrxn):
                        lines.append(
                            f"{pad}UPDATE NATURAL FLOW for {var.id} by {var.id}.increment"
                        )
                    lines.append("")
                elif isinstance(item, CorePropSchedule):
                    member_ids = tuple(
                        prop.item.var.id
                        for prop in item.series
                        if isinstance(prop.item, CoreScheduleVariable)
                        and not (isinstance(prop.item.var, PathTrxn) and prop.item.var.is_slack)
                    )
                    if not member_ids:
                        continue
                    lines.extend(
                        EqualPriorityKernel(item, member_ids, effects).text(indent=indent)
                    )
                    lines.append("")
                elif isinstance(item, CoreSeqSchedule):
                    render_schedule(item, indent + 4)

        render_schedule(schedule)
        lines.extend([
            "DERIVED SLACKS / SPILL REALLOCATION",
            "-----------------------------------",
            *DeriveReportingSlacks(slack_rules, apply_spill_credit=True).text(),
            "",
            "PASS 2 — RERUN PRIORITY ALLOCATION",
            "----------------------------------",
            "Repeat PASS 1 using the updated residual/NF state.",
            "",
            "FINAL DERIVED OUTPUTS",
            "---------------------",
            *FinalDerivedOutputs(slack_rules).text(),
        ])
        return "\n".join(lines).rstrip()
