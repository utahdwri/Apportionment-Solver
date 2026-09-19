"""Runtime slots and input preparation for the first block-LP compiler."""
from copy import deepcopy
from dataclasses import dataclass, field
from math import inf, isfinite

import numpy as np

from ..graph_manager import GraphManager
from ..models import (
    NaturalFlowMode, PathTrxn, SolverInput, TrxnGroup, ZoneTypes,
)
from ..timeseries_manager import DailyDataManager
from ..trxn_schedule import TrxnSchedule
from .lp import Slot


class UnsupportedBlockInput(NotImplementedError):
    """An accounting feature has not yet been implemented by this compiler."""


@dataclass(frozen=True)
class SpillCreditSpec:
    """A residual non-natural -> natural direction that can create NF credit."""
    flow_id: str
    factor: int
    receiving_zone: str
    available: Slot
    directional_capacity: Slot


@dataclass
class RuntimeStateLayout:
    input: SolverInput
    graph: GraphManager
    schedule: TrxnSchedule
    data: DailyDataManager
    slots: dict[str, Slot] = field(default_factory=dict)
    transactions: dict[str, PathTrxn | TrxnGroup] = field(default_factory=dict)
    parents: dict[str, str] = field(default_factory=dict)
    allocated: dict[str, Slot] = field(default_factory=dict)
    limits: dict[str, Slot] = field(default_factory=dict)
    reference_cfs: dict[str, Slot] = field(default_factory=dict)
    groups: dict[str, Slot] = field(default_factory=dict)
    measurements: dict[str, Slot] = field(default_factory=dict)
    measurement_available: dict[str, Slot] = field(default_factory=dict)
    measurement_forward_remaining: dict[str, Slot] = field(default_factory=dict)
    measurement_reverse_remaining: dict[str, Slot] = field(default_factory=dict)
    # Raw daily loss/NF inputs are bound by new_day(); all routing and NF
    # accounting from those inputs is performed by the generated program.
    loss_from_delivery: dict[str, Slot] = field(default_factory=dict)
    loss_to_delivery: dict[str, Slot] = field(default_factory=dict)
    specified_natural_flow: dict[str, Slot] = field(default_factory=dict)
    boundary_natural_flow: dict[str, Slot] = field(default_factory=dict)
    boundary_natural_active: dict[str, Slot] = field(default_factory=dict)
    flow_natural: dict[str, Slot] = field(default_factory=dict)
    natural_at_zone: dict[str, Slot] = field(default_factory=dict)
    natural_flow: dict[str, Slot] = field(default_factory=dict)
    flow_coefficients: dict[tuple[str, str], tuple[Slot, Slot]] = field(default_factory=dict)

    # how much NF reaches `zone` per unit of NF withdrawn at `source`
    nf_coefficients: dict[tuple[str, str], tuple[Slot, Slot]] = field(default_factory=dict)

    # Amount of each stream-zone NF residual consumed by one unit of a
    # transaction's anchor allocation.  This includes the
    # conversion from the first path-leg gauge amount back to source-zone
    # withdrawal across the first endpoint/path loss. (See test_source_endpoint_loss_is_charged_to_natural_flow)
    transaction_nf_coefficients: dict[tuple[str, str], tuple[Slot, Slot]] = field(default_factory=dict)

    account_out_remaining: dict[tuple[str, str], Slot] = field(default_factory=dict)
    account_in_remaining: dict[tuple[str, str], Slot] = field(default_factory=dict)
    to_account_coefficients: dict[str, tuple[Slot, Slot]] = field(default_factory=dict)
    spill_credits: list[SpillCreditSpec] = field(default_factory=list)
    replay_counterflow_slack_limits: dict[tuple[str, int], Slot] = field(default_factory=dict)

    def add(
        self, name: str, *, sign: int = 0,
        source_index: int | None = None, source_factor: float = 1.0,
        constant_value: float | None = None,
    ) -> Slot:
        if name in self.slots:
            raise ValueError(f"Duplicate runtime slot: {name}")
        if sign not in (-1, 0, 1):
            raise ValueError(f"Invalid slot sign metadata: {sign}")
        slot = Slot(
            len(self.slots), name, sign,
            source_index=source_index, source_factor=source_factor,
            constant_value=constant_value,
        )
        self.slots[name] = slot
        return slot

    def coefficient_pair(
        self, name: str, *, first_sign: int = 1,
        constant_value: float | None = None,
    ) -> tuple[Slot, Slot]:
        """Create a coefficient and its exact negation with structural signs."""
        if first_sign not in (-1, 1):
            raise ValueError("Coefficient-pair sign must be +1 or -1")
        first = self.add(
            name, sign=first_sign, constant_value=constant_value
        )
        second = self.add(
            "negative_" + name, sign=-first_sign,
            source_index=first.index, source_factor=-1.0,
            constant_value=(None if constant_value is None else -constant_value),
        )
        return first, second

    def descendants(self, name: str) -> set[str]:
        result = {name}
        transaction = self.transactions[name]
        if isinstance(transaction, TrxnGroup):
            for child in transaction.children_trxns:
                result.update(self.descendants(child.id))
        return result

    def new_day(self, date, data, schedule) -> np.ndarray:
        """Bind raw daily inputs into numeric state.

        Natural-flow routing/calculation is deliberately *not* performed here.
        The generated execute_day() program consumes these raw inputs and
        initializes natural-flow state itself, so plan.code() contains the
        complete numerical NF calculation.  ``natural_flow`` is retained as
        an ignored compatibility argument for older tests/callers.
        """
        data.set_day(date)
        schedule.begin_day(date)
        values = np.zeros(len(self.slots), dtype=float)
        for name, slot in self.measurements.items():
            measured = data.cur_flows_by_id[name].measured
            if measured is None or not isfinite(measured):
                raise ValueError(f"Non-finite measurement for {name!r} on {date}")
            measured = float(measured)

            # Preserve the signed physical residual for reporting/slack output,
            # but expose nonnegative directional capacities to the block LPs.
            # This is what lets a reverse transaction use an ordinary <= row:
            # a measured -5 cfs becomes 5 cfs of reverse capacity, not a -5
            # upper bound that would algebraically turn into a lower bound.
            values[slot.index] = measured
            values[self.measurement_available[name].index] = measured
            values[self.measurement_forward_remaining[name].index] = max(0.0, measured)
            if name in self.measurement_reverse_remaining:
                values[self.measurement_reverse_remaining[name].index] = max(0.0, -measured)

        # Replay can sometimes require opposite-direction reporting slack for a
        # transaction that originates in storage.  Do not make that slack
        # available when the measured net flow already contains physical flow
        # in the target direction: in that case Pass 1 establishes the minimum
        # reservoir exchange and replay may use only real counterflow created by
        # earlier replay allocations.  If there is no measured flow in the
        # target direction, allow the reporting slack so a senior storage
        # delivery can still be claimed (for example, a zero-net reservoir with
        # a measured downstream release).
        for (flow_id, direction), slack_slot in self.replay_counterflow_slack_limits.items():
            measured = values[self.measurements[flow_id].index]
            target_direction_capacity = measured * direction
            values[slack_slot.index] = (
                0.0 if target_direction_capacity > 1e-10 else inf
            )

        # Bind endpoint delivery factors.  These are raw inputs to generated
        # loss-transform functions.  Today each endpoint is a single fractional
        # loss; future piecewise support can replace the generated transform
        # bodies without changing the surrounding NF calculation.
        for flow in self.graph.graph.interzone_flows:
            from_factor = 1.0 - flow.loss_from_zone.get_fraction(date)
            to_factor = 1.0 - flow.loss_to_zone.get_fraction(date)
            if (not isfinite(from_factor) or not isfinite(to_factor)
                    or from_factor < 0 or to_factor < 0):
                raise UnsupportedBlockInput(
                    f"Invalid endpoint delivery factor for {flow.id!r}"
                )
            values[self.loss_from_delivery[flow.id].index] = from_factor
            values[self.loss_to_delivery[flow.id].index] = to_factor

        # Bind raw specified/boundary natural-flow inputs only.  Generated
        # execute_day() applies them to the NF network.
        specified_values = data.get_specified_natural_flow_values(date)
        for flow_id, slot in self.specified_natural_flow.items():
            value = float(specified_values.get(flow_id, 0.0))
            if not isfinite(value):
                raise ValueError(
                    f"Specified natural flow for {flow_id!r} is non-finite on {date}"
                )
            values[slot.index] = value

        boundary_values = data.get_boundary_natural_flow_values(date)
        for flow_id, slot in self.boundary_natural_flow.items():
            active = flow_id in boundary_values
            value = float(boundary_values.get(flow_id, 0.0))
            values[slot.index] = value
            values[self.boundary_natural_active[flow_id].index] = 1.0 if active else 0.0

        # Account limits use the balance at the beginning of the day, matching
        # the legacy LP.  Incoming allocations during this day do not create
        # additional same-day withdrawal capacity (and vice versa).
        for zone in self.graph.graph.zones:
            for account in zone.accounts:
                key = (zone.id, account.id)
                balance = schedule.get_account_balance(zone.id, account.id)
                if key in self.account_out_remaining:
                    values[self.account_out_remaining[key].index] = max(
                        0.0, balance - account.balance_floor
                    )
                if key in self.account_in_remaining:
                    values[self.account_in_remaining[key].index] = max(
                        0.0, account.balance_ceiling - balance
                    )

        for name, transaction in self.transactions.items():
            cap = schedule.get_transaction_upper_limit(transaction, date)
            cap = inf if cap is None else float(cap)
            if ((transaction.beg_date and date < transaction.beg_date)
                    or (transaction.end_date and date > transaction.end_date)):
                cap = 0.0
            if isinstance(transaction, PathTrxn) and not transaction.path:
                cap = 0.0
            if cap < 0 or np.isnan(cap):
                raise ValueError(f"Invalid limit for {name!r} on {date}: {cap}")
            if isinstance(transaction, TrxnGroup) and not isfinite(cap):
                raise UnsupportedBlockInput(f"Group {name!r} needs a finite daily limit")
            values[self.limits[name].index] = cap
            values[self.reference_cfs[name].index] = cap
            if not isinstance(transaction, PathTrxn):
                continue
            path = schedule.ordered_paths[name]
            factor = 1.0
            for index, item in enumerate(path):
                if index:
                    previous = path[index - 1]
                    upstream = self.graph.get_flow_by_id(previous.flow_id)
                    downstream = self.graph.get_flow_by_id(item.flow_id)
                    upstream_factor = (
                        values[self.loss_to_delivery[upstream.id].index]
                        if previous.factor > 0
                        else values[self.loss_from_delivery[upstream.id].index]
                    )
                    downstream_factor = (
                        values[self.loss_from_delivery[downstream.id].index]
                        if item.factor > 0
                        else values[self.loss_to_delivery[downstream.id].index]
                    )
                    factor *= (
                        upstream_factor
                        * (1.0 - previous.loss_after)
                        * downstream_factor
                        * (1.0 - item.loss_before)
                    )
                positive, negative = self.flow_coefficients[name, item.flow_id]
                values[positive.index] = item.factor * factor
                values[negative.index] = -item.factor * factor
            if name in self.to_account_coefficients:
                # Legacy to_account uses the variable on the final path leg.
                # Relative to this compiler's anchor-allocation variable, that
                # is the cumulative remaining fraction after path losses.
                positive, negative = self.to_account_coefficients[name]
                values[positive.index], values[negative.index] = factor, -factor

        return values


def _check_fractional_loss(loss):
    definitions = [interval.loss for interval in loss.intervals]
    if loss.default is not None:
        definitions.append(loss.default)
    if not loss.intervals:
        definitions.append(loss)
    for definition in definitions:
        if (len(definition.segments) != 1
                or definition.segments[0].min_driver_flow != 0
                or definition.segments[0].max_driver_flow is not None
                or definition.segments[0].loss_intercept != 0):
            raise UnsupportedBlockInput("Piecewise/absolute losses need a segment kernel")


def build_runtime_state_layout(
    input: SolverInput,
    *,
    max_daily_apportionment: float | None = None,
) -> RuntimeStateLayout:
    """Freeze structure and allocate slots without reading a representative day.

    This first implementation supports forward allocation, nested reservations,
    daily/call/cumulative path limits, whole-day lags, fractional losses, and zone
    account balances, and signed/reverse transaction paths on bidirectional
    flows, plus a Pass-1/spill/replay reservoir sequence. It deliberately
    rejects fractional-day lags; independent daily allocation followed by
    fractional unlagging does not preserve allocation feasibility.
    """
    problem = deepcopy(input)
    for flow in problem.accounting_graph.interzone_flows:
        for field_name in ("lag_from_zone", "lag_to_zone"):
            lag = getattr(flow, field_name)
            if not float(lag).is_integer():
                raise UnsupportedBlockInput(
                    "Fractional-day lags are not supported by the compiled solver: "
                    f"flow {flow.id!r} has {field_name}={lag!r}. "
                    "Use nonnegative whole-day lags."
                )
    graph = GraphManager(problem.accounting_graph)
    natural_types = {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}
    # Storage change is already folded into residual interzone-flow measurements
    # by DailyDataManager. Non-natural -> natural residuals are handled after
    # Pass 1 as locked spill/import credit before the replay pass.
    for flow in graph.graph.interzone_flows:
        _check_fractional_loss(flow.loss_from_zone)
        _check_fractional_loss(flow.loss_to_zone)
    schedule = TrxnSchedule(
        graph, problem.txns, max_daily_apportionment=max_daily_apportionment
    )
    data = DailyDataManager(graph, problem.measurements, problem.external_natural_flows)
    layout = RuntimeStateLayout(problem, graph, schedule, data)
    layout.transactions = {
        txn.id: txn for txn in schedule.all_trxns
        if not (isinstance(txn, PathTrxn) and txn.is_slack)
    }

    # Coefficients with the same physical path-prefix formula are exact aliases,
    # even though older layouts allocated a separate runtime slot per
    # transaction.  Sharing them preserves that algebraic identity for the
    # symbolic formula compiler (for example a + (-a) cancels exactly).
    flow_coefficient_cache: dict[tuple, tuple[Slot, Slot]] = {}
    account_coefficient_cache: dict[tuple, tuple[Slot, Slot]] = {}

    def path_factor_signature(path, index):
        transitions = []
        for j in range(1, index + 1):
            previous = path[j - 1]
            item = path[j]
            transitions.append((
                previous.flow_id, previous.factor > 0, float(previous.loss_after),
                item.flow_id, item.factor > 0, float(item.loss_before),
            ))
        return tuple(transitions)

    def constant_fraction(loss):
        definitions = [interval.loss for interval in loss.intervals]
        if loss.default is not None:
            definitions.append(loss.default)
        if not loss.intervals:
            definitions.append(loss)
        values = {float(definition.segments[0].loss_slope) for definition in definitions}
        return next(iter(values)) if len(values) == 1 else None

    def path_factor_constant(path, index):
        factor = 1.0
        for j in range(1, index + 1):
            previous = path[j - 1]
            item = path[j]
            upstream = graph.get_flow_by_id(previous.flow_id)
            downstream = graph.get_flow_by_id(item.flow_id)
            upstream_exit_loss = (
                upstream.loss_to_zone if previous.factor > 0 else upstream.loss_from_zone
            )
            downstream_entry_loss = (
                downstream.loss_from_zone if item.factor > 0 else downstream.loss_to_zone
            )
            upstream_fraction = constant_fraction(upstream_exit_loss)
            downstream_fraction = constant_fraction(downstream_entry_loss)
            if upstream_fraction is None or downstream_fraction is None:
                return None
            factor *= (
                (1.0 - upstream_fraction)
                * (1.0 - previous.loss_after)
                * (1.0 - downstream_fraction)
                * (1.0 - item.loss_before)
            )
        return factor

    for name, txn in layout.transactions.items():
        if isinstance(txn, TrxnGroup):
            # A cumulative cap is just another source for today's effective
            # variable upper bound.  TrxnSchedule carries the used amount
            # across days and get_transaction_upper_limit() returns the
            # remaining cumulative amount when it is the governing limit.
            if (
                txn.upper_limit is None
                and txn.call_limit is None
                and txn.cumulative_limit is None
            ):
                raise UnsupportedBlockInput(f"Group {name!r} needs a finite daily limit")
            layout.groups[name] = layout.add(f"remaining_group[{name!r}]")
            for child in txn.children_trxns:
                layout.parents[child.id] = name
        else:
            path = schedule.ordered_paths[name]
            for index, item in enumerate(path):
                flow = graph.get_flow_by_id(item.flow_id)
                # The runtime coefficient is item.factor times a cumulative
                # product determined entirely by this path prefix.  Transactions
                # with the same prefix therefore share one coefficient slot.
                signature = (
                    'flow', float(item.factor), path_factor_signature(path, index)
                )
                pair = flow_coefficient_cache.get(signature)
                if pair is None:
                    path_constant = path_factor_constant(path, index)
                    coefficient_constant = (
                        None if path_constant is None
                        else float(item.factor) * path_constant
                    )
                    pair = layout.coefficient_pair(
                        f"flow_coefficient[{(name, item.flow_id)!r}]",
                        first_sign=1 if item.factor > 0 else -1,
                        constant_value=coefficient_constant,
                    )
                    flow_coefficient_cache[signature] = pair
                layout.flow_coefficients[name, item.flow_id] = pair
            if txn.to_account is not None:
                signature = ('to_account', path_factor_signature(path, len(path) - 1))
                pair = account_coefficient_cache.get(signature)
                if pair is None:
                    account_constant = path_factor_constant(path, len(path) - 1)
                    pair = layout.coefficient_pair(
                        f"to_account_coefficient[{name!r}]",
                        constant_value=account_constant,
                    )
                    account_coefficient_cache[signature] = pair
                layout.to_account_coefficients[name] = pair
        layout.allocated[name] = layout.add(f"allocated[{name!r}]")
        layout.limits[name] = layout.add(f"remaining_limit[{name!r}]")
        layout.reference_cfs[name] = layout.add(f"reference_cfs[{name!r}]")
    for zone in graph.graph.zones:
        for account in zone.accounts:
            key = (zone.id, account.id)
            if account.balance_floor is not None:
                layout.account_out_remaining[key] = layout.add(
                    f"remaining_account_out[{key!r}]"
                )
            if account.balance_ceiling is not None:
                layout.account_in_remaining[key] = layout.add(
                    f"remaining_account_in[{key!r}]"
                )

    for flow in graph.graph.interzone_flows:
        layout.measurements[flow.id] = layout.add(f"remaining_measured[{flow.id!r}]")
        layout.measurement_available[flow.id] = layout.add(
            f"remaining_measured_available[{flow.id!r}]"
        )
        layout.measurement_forward_remaining[flow.id] = layout.add(
            f"remaining_measured_forward[{flow.id!r}]"
        )
        if flow.bidirectional:
            layout.measurement_reverse_remaining[flow.id] = layout.add(
                f"remaining_measured_reverse[{flow.id!r}]"
            )

        # Raw endpoint delivery factors and flow-level NF values.  The generated
        # program turns these factors into explicit deliver()/required_inflow()
        # formulas.
        layout.loss_from_delivery[flow.id] = layout.add(
            f"loss_from_delivery[{flow.id!r}]", sign=1
        )
        layout.loss_to_delivery[flow.id] = layout.add(
            f"loss_to_delivery[{flow.id!r}]", sign=1
        )
        layout.flow_natural[flow.id] = layout.add(
            f"natural_flow_on_flow[{flow.id!r}]"
        )
        if flow.natural_flow_mode == NaturalFlowMode.SPECIFIED:
            layout.specified_natural_flow[flow.id] = layout.add(
                f"specified_natural_flow[{flow.id!r}]"
            )
        if flow.id in problem.external_natural_flows:
            layout.boundary_natural_flow[flow.id] = layout.add(
                f"boundary_natural_flow[{flow.id!r}]"
            )
            layout.boundary_natural_active[flow.id] = layout.add(
                f"boundary_natural_active[{flow.id!r}]", sign=1
            )

    # A replayed transaction that originates in a non-natural zone can require
    # opposite-direction reporting slack on its first bidirectional flow.  The
    # slot is a *runtime upper bound* for that slack witness.  Keeping it out of
    # Pass 1 replaces the legacy minimize/lock/release machinery structurally.
    for transaction in layout.transactions.values():
        if not isinstance(transaction, PathTrxn):
            continue
        from_zone = schedule.get_from_zone(transaction)
        path = schedule.ordered_paths[transaction.id]
        if from_zone is None or not path or from_zone.type in natural_types:
            continue
        item = path[0]
        flow = graph.get_flow_by_id(item.flow_id)
        if not flow.bidirectional:
            continue
        direction = 1 if item.factor > 0 else -1
        key = (flow.id, direction)
        if key not in layout.replay_counterflow_slack_limits:
            layout.replay_counterflow_slack_limits[key] = layout.add(
                f"replay_counterflow_slack_limit[{key!r}]"
            )

    # A spill/import candidate is the residual direction from a non-natural
    # zone into the natural system. Slack variables are not compiled; after
    # Pass 1 this residual is therefore the amount to lock and credit.
    for flow in graph.graph.interzone_flows:
        source_type = graph.get_zone_by_id(flow.from_zone).type
        destination_type = graph.get_zone_by_id(flow.to_zone).type
        if source_type not in natural_types and destination_type in natural_types:
            factor = 1
            receiving_zone = flow.to_zone
            directional = layout.measurement_forward_remaining[flow.id]
        elif (flow.bidirectional
              and destination_type not in natural_types
              and source_type in natural_types):
            factor = -1
            receiving_zone = flow.from_zone
            directional = layout.measurement_reverse_remaining[flow.id]
        else:
            continue
        layout.spill_credits.append(SpillCreditSpec(
            flow_id=flow.id, factor=factor, receiving_zone=receiving_zone,
            available=layout.measurement_available[flow.id],
            directional_capacity=directional,
        ))

    for zone in graph.graph.zones:
        if zone.type == ZoneTypes.STREAM:
            layout.natural_at_zone[zone.id] = layout.add(
                f"natural_at_zone[{zone.id!r}]"
            )
            layout.natural_flow[zone.id] = layout.add(f"remaining_nf[{zone.id!r}]")
    # Natural-flow routing topology is structural even though loss fractions may
    # vary by date.  Mark unreachable source/zone pairs as exact zero and mark
    # constant-loss routes with their exact constant coefficient.  Dynamic
    # reachable routes remain nonnegative symbolic slots.
    boundary_flow_ids = set(problem.external_natural_flows)
    calculated_outflows_by_zone: dict[str, list] = {}
    for flow in graph.graph.interzone_flows:
        if flow.natural_flow_mode != NaturalFlowMode.CALCULATED:
            continue
        if not (
            graph.get_zone_by_id(flow.from_zone).type == ZoneTypes.STREAM
            and graph.get_zone_by_id(flow.to_zone).type == ZoneTypes.STREAM
        ):
            continue
        calculated_outflows_by_zone.setdefault(flow.from_zone, []).append(flow)

    def nf_coefficient_constant(source, destination):
        if source == destination:
            return 1.0
        zone_id = source
        factor = 1.0
        visited = set()
        while zone_id in calculated_outflows_by_zone:
            if zone_id in visited:
                return None
            visited.add(zone_id)
            candidates = calculated_outflows_by_zone[zone_id]
            # Multiple possible calculated routes can be disambiguated by an
            # active external boundary at runtime.  Their coefficient is thus
            # structurally nonnegative but not a compile-time constant.
            if len(candidates) != 1:
                return None
            flow = candidates[0]
            # An externally supplied boundary cuts this route only on days when
            # a boundary value is present, so any downstream coefficient is a
            # runtime value even if the loss fractions themselves are constant.
            if flow.id in boundary_flow_ids:
                factor = None
            from_fraction = constant_fraction(flow.loss_from_zone)
            to_fraction = constant_fraction(flow.loss_to_zone)
            if from_fraction is None or to_fraction is None:
                factor = None
            elif factor is not None:
                factor *= (1.0 - from_fraction) * (1.0 - to_fraction)
            zone_id = flow.to_zone
            if zone_id == destination:
                return factor
        return 0.0

    sources = {schedule.get_nf_zone_id(t) for t in layout.transactions.values()}
    valid_sources = {source for source in sources if source is not None}
    for source in sorted(valid_sources):
        for zone in layout.natural_flow:
            constant = nf_coefficient_constant(source, zone)
            layout.nf_coefficients[source, zone] = layout.coefficient_pair(
                f"nf_coefficient[{(source, zone)!r}]",
                constant_value=constant,
            )

    def source_withdrawal_constant(transaction: PathTrxn) -> float | None:
        """Return source-zone withdrawal per anchor unit when it is static."""
        path = schedule.ordered_paths[transaction.id]
        if not path:
            return 0.0
        item = path[0]
        if item.loss_before >= 1.0:
            return None
        flow = graph.get_flow_by_id(item.flow_id)
        endpoint_loss = (
            flow.loss_from_zone if item.factor > 0 else flow.loss_to_zone
        )
        fraction = constant_fraction(endpoint_loss)
        if fraction is None or fraction >= 1.0:
            return None
        delivered = (1.0 - fraction) * (1.0 - item.loss_before)
        return abs(float(item.factor)) / delivered

    # Natural-flow constraints are written in source-zone units.  The anchor
    # allocation, however, is the amount reported on the first path leg.  Build
    # one derived coefficient for each transaction/stream-zone pair so the first
    # endpoint loss is charged before the ordinary downstream NF routing loss.
    for name, transaction in layout.transactions.items():
        if not isinstance(transaction, PathTrxn):
            continue
        source = schedule.get_nf_zone_id(transaction)
        if source is None:
            continue
        source_constant = source_withdrawal_constant(transaction)
        for zone in layout.natural_flow:
            route_constant = nf_coefficient_constant(source, zone)
            constant = (
                None
                if source_constant is None or route_constant is None
                else source_constant * route_constant
            )
            layout.transaction_nf_coefficients[name, zone] = layout.coefficient_pair(
                f"transaction_nf_coefficient[{(name, zone)!r}]",
                constant_value=constant,
            )

    return layout
