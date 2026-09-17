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
    natural_flow: dict[str, Slot] = field(default_factory=dict)
    flow_coefficients: dict[tuple[str, str], tuple[Slot, Slot]] = field(default_factory=dict)
    nf_coefficients: dict[tuple[str, str], tuple[Slot, Slot]] = field(default_factory=dict)

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
            values[slot.index] = measured
        for name, slot in self.natural_flow.items():
            value = natural_flow.remaining_natural_at_zone.get(name, 0.0)
            if not isfinite(value):
                raise ValueError(f"Non-finite natural flow for {name!r} on {date}")
            values[slot.index] = max(0.0, value)
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
                    factor *= ((1.0 - upstream.loss_to_zone.get_fraction(date))
                               * (1.0 - previous.loss_after)
                               * (1.0 - downstream.loss_from_zone.get_fraction(date))
                               * (1.0 - item.loss_before))
                positive, negative = self.flow_coefficients[name, item.flow_id]
                values[positive.index] = item.factor * factor
                values[negative.index] = -item.factor * factor
        for source in {key[0] for key in self.nf_coefficients}:
            coefficients = natural_flow.get_nf_constraint_coefficients(source)
            for zone in self.natural_flow:
                positive, negative = self.nf_coefficients[source, zone]
                value = coefficients.get(zone, 0.0)
                if not isfinite(value) or value < 0:
                    raise UnsupportedBlockInput("Non-monotone natural-flow routing")
                values[positive.index], values[negative.index] = value, -value
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
    daily/call/cumulative path limits, lags, and fractional losses. It deliberately
    rejects storage/spill replay, account balances, reverse allocation, and
    unconstrained physical flows instead of silently running different rules.
    """
    problem = deepcopy(input)
    if any(flow.flow_type == FlowComponentsTypes.UNCONSTRAINED
           for flow in problem.accounting_graph.interzone_flows):
        raise UnsupportedBlockInput("UNCONSTRAINED flows need the post-allocation gain convention")
    graph = GraphManager(problem.accounting_graph)
    natural_types = {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}
    for zone in graph.graph.zones:
        if zone.type == ZoneTypes.STORAGE or zone.storage_meas_ids or zone.accounts:
            raise UnsupportedBlockInput("Storage/accounts require explicit daily state and spill replay")
    for flow in graph.graph.interzone_flows:
        source = graph.get_zone_by_id(flow.from_zone).type
        destination = graph.get_zone_by_id(flow.to_zone).type
        if ((source not in natural_types and destination in natural_types)
                or (flow.bidirectional and destination not in natural_types and source in natural_types)):
            raise UnsupportedBlockInput(f"Flow {flow.id!r} can require spill/import credit replay")
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
            if txn.cumulative_limit is not None or txn.cumulative_reset_before_MMDD is not None:
                raise UnsupportedBlockInput("Cumulative group reservations are not implemented")
            if txn.upper_limit is None and txn.call_limit is None:
                raise UnsupportedBlockInput(f"Group {name!r} needs a finite daily limit")
            layout.groups[name] = layout.add(f"remaining_group[{name!r}]")
            for child in txn.children_trxns:
                layout.parents[child.id] = name
        else:
            if txn.from_account is not None or txn.to_account is not None:
                raise UnsupportedBlockInput("Transaction account transfers are not implemented")
            for item in schedule.ordered_paths[name]:
                flow = graph.get_flow_by_id(item.flow_id)
                if item.factor < 0 or flow.bidirectional:
                    raise UnsupportedBlockInput(f"Transaction {name!r} needs reverse/counterflow allocation")
                layout.flow_coefficients[name, item.flow_id] = layout.coefficient_pair(
                    f"flow_coefficient[{(name, item.flow_id)!r}]"
                )
        layout.allocated[name] = layout.add(f"allocated[{name!r}]")
        layout.limits[name] = layout.add(f"remaining_limit[{name!r}]")
        layout.reference_cfs[name] = layout.add(f"reference_cfs[{name!r}]")
    for flow in graph.graph.interzone_flows:
        layout.measurements[flow.id] = layout.add(f"remaining_measured[{flow.id!r}]")
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
