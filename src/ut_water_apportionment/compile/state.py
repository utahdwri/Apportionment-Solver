"""Runtime slots and input preparation for the first block-LP compiler."""
from copy import deepcopy
from dataclasses import dataclass, field
from math import inf, isfinite

import numpy as np

from ..graph_manager import GraphManager
from ..models import FlowComponentsTypes, PathTrxn, SolverInput, TrxnGroup, ZoneTypes
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
    credit_factor: Slot
    nf_coefficients: dict[str, Slot]


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
    natural_flow: dict[str, Slot] = field(default_factory=dict)
    flow_coefficients: dict[tuple[str, str], tuple[Slot, Slot]] = field(default_factory=dict)
    nf_coefficients: dict[tuple[str, str], tuple[Slot, Slot]] = field(default_factory=dict)
    account_out_remaining: dict[tuple[str, str], Slot] = field(default_factory=dict)
    account_in_remaining: dict[tuple[str, str], Slot] = field(default_factory=dict)
    to_account_coefficients: dict[str, tuple[Slot, Slot]] = field(default_factory=dict)
    spill_credits: list[SpillCreditSpec] = field(default_factory=list)
    replay_counterflow_slack_limits: dict[tuple[str, int], Slot] = field(default_factory=dict)

    def add(self, name: str) -> Slot:
        if name in self.slots:
            raise ValueError(f"Duplicate runtime slot: {name}")
        slot = Slot(len(self.slots), name)
        self.slots[name] = slot
        return slot

    def coefficient_pair(self, name: str) -> tuple[Slot, Slot]:
        return self.add(name), self.add("negative_" + name)

    def descendants(self, name: str) -> set[str]:
        result = {name}
        transaction = self.transactions[name]
        if isinstance(transaction, TrxnGroup):
            for child in transaction.children_trxns:
                result.update(self.descendants(child.id))
        return result

    def new_day(self, date, data, schedule, natural_flow) -> np.ndarray:
        """Initialize numeric state."""
        data.set_day(date)
        schedule.begin_day(date)
        natural_flow.calculate(
            date=date, daily_flows=data.cur_flows_by_id,
            specified_values=data.get_specified_natural_flow_values(date),
            boundary_values=data.get_boundary_natural_flow_values(date),
        )
        natural_flow.apply_external_boundary_commitments(
            daily_flows=data.cur_flows_by_id,
            boundary_values=data.get_boundary_natural_flow_values(date),
        )
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

        for name, slot in self.natural_flow.items():
            value = natural_flow.remaining_natural_at_zone.get(name, 0.0)
            if not isfinite(value):
                raise ValueError(f"Non-finite natural flow for {name!r} on {date}")
            values[slot.index] = max(0.0, value)
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
                    upstream_exit_loss = (
                        upstream.loss_to_zone
                        if previous.factor > 0
                        else upstream.loss_from_zone
                    )
                    downstream_entry_loss = (
                        downstream.loss_from_zone
                        if item.factor > 0
                        else downstream.loss_to_zone
                    )
                    factor *= ((1.0 - upstream_exit_loss.get_fraction(date))
                               * (1.0 - previous.loss_after)
                               * (1.0 - downstream_entry_loss.get_fraction(date))
                               * (1.0 - item.loss_before))
                positive, negative = self.flow_coefficients[name, item.flow_id]
                values[positive.index] = item.factor * factor
                values[negative.index] = -item.factor * factor
            if name in self.to_account_coefficients:
                # Legacy to_account uses the variable on the final path leg.
                # Relative to this compiler's anchor-allocation variable, that
                # is the cumulative remaining fraction after path losses.
                positive, negative = self.to_account_coefficients[name]
                values[positive.index], values[negative.index] = factor, -factor
        for source in {key[0] for key in self.nf_coefficients}:
            coefficients = natural_flow.get_nf_constraint_coefficients(source)
            for zone in self.natural_flow:
                positive, negative = self.nf_coefficients[source, zone]
                value = coefficients.get(zone, 0.0)
                if not isfinite(value) or value < 0:
                    raise UnsupportedBlockInput("Non-monotone natural-flow routing")
                values[positive.index], values[negative.index] = value, -value

        # Spill credits enter the natural system at a physical stream zone and
        # then route downstream using the same coefficients as an allocation
        # originating at that zone.
        for spill in self.spill_credits:
            flow = self.graph.get_flow_by_id(spill.flow_id)
            endpoint_loss = (
                flow.loss_to_zone if spill.factor > 0 else flow.loss_from_zone
            )
            factor = 1.0 - endpoint_loss.get_fraction(date)
            if not isfinite(factor) or factor < 0:
                raise UnsupportedBlockInput(
                    f"Invalid spill endpoint delivery factor for {spill.flow_id!r}"
                )
            values[spill.credit_factor.index] = factor
            coefficients = (
                natural_flow.get_nf_constraint_coefficients(spill.receiving_zone)
                if spill.receiving_zone in self.natural_flow else {}
            )
            for zone, coefficient_slot in spill.nf_coefficients.items():
                value = coefficients.get(zone, 0.0)
                if not isfinite(value) or value < 0:
                    raise UnsupportedBlockInput("Non-monotone spill NF routing")
                values[coefficient_slot.index] = value
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


def build_runtime_state_layout(input: SolverInput) -> RuntimeStateLayout:
    """Freeze structure and allocate slots without reading a representative day.

    This first implementation supports forward allocation, nested reservations,
    daily/call/cumulative path limits, lags, fractional losses, and zone
    account balances, and signed/reverse transaction paths on bidirectional
    flows, plus a Pass-1/spill/replay reservoir sequence. It deliberately
    rejects unconstrained physical flows instead of silently running different rules.
    """
    problem = deepcopy(input)
    if any(flow.flow_type == FlowComponentsTypes.UNCONSTRAINED
           for flow in problem.accounting_graph.interzone_flows):
        raise UnsupportedBlockInput("UNCONSTRAINED flows need the post-allocation gain convention")
    graph = GraphManager(problem.accounting_graph)
    natural_types = {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}
    # Storage change is already folded into residual interzone-flow measurements
    # by DailyDataManager. Non-natural -> natural residuals are handled after
    # Pass 1 as locked spill/import credit before the replay pass.
    for flow in graph.graph.interzone_flows:
        _check_fractional_loss(flow.loss_from_zone)
        _check_fractional_loss(flow.loss_to_zone)
    schedule = TrxnSchedule(graph, problem.txns)
    data = DailyDataManager(graph, problem.measurements, problem.external_natural_flows)
    layout = RuntimeStateLayout(problem, graph, schedule, data)
    layout.transactions = {
        txn.id: txn for txn in schedule.all_trxns
        if not (isinstance(txn, PathTrxn) and txn.is_slack)
    }
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
            for item in schedule.ordered_paths[name]:
                flow = graph.get_flow_by_id(item.flow_id)
                # A negative transaction component may traverse a physically
                # non-bidirectional reach as long as the *net* measured flow is
                # still reconciled by the ordinary forward residual slack. This
                # is used by exchange paths between reservoirs.
                layout.flow_coefficients[name, item.flow_id] = layout.coefficient_pair(
                    f"flow_coefficient[{(name, item.flow_id)!r}]"
                )
            if txn.to_account is not None:
                layout.to_account_coefficients[name] = layout.coefficient_pair(
                    f"to_account_coefficient[{name!r}]"
                )
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
            credit_factor=layout.add(f"spill_credit_factor[{flow.id!r}]"),
            nf_coefficients={
                zone.id: layout.add(f"spill_nf_coefficient[{(flow.id, zone.id)!r}]")
                for zone in graph.graph.zones if zone.type == ZoneTypes.STREAM
            },
        ))

    for zone in graph.graph.zones:
        if zone.type == ZoneTypes.STREAM:
            layout.natural_flow[zone.id] = layout.add(f"remaining_nf[{zone.id!r}]")
    sources = {schedule.get_nf_zone_id(t) for t in layout.transactions.values()}
    for source in sorted(sources - {None}):
        for zone in layout.natural_flow:
            layout.nf_coefficients[source, zone] = layout.coefficient_pair(
                f"nf_coefficient[{(source, zone)!r}]"
            )
    return layout
