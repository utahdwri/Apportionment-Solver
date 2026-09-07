"""Exact piecewise loss graphs and priority-ordered incremental attribution.

Natural-flow availability uses exact residual-flow balance equations. Path
components consume a measured-flow pool in priority order, so senior components
retain their incremental losses when juniors are allocated. Segment selectors
represent the graph itself, not its convex relaxation.
"""

from __future__ import annotations

from math import isfinite

from .models import (
    FlowComponentsTypes,
    NaturalFlowMode,
    PathTrxn,
    SolverOutputLossAllocation,
    SolverOutputLossEvent,
    ZoneTypes,
)


def piecewise_allocation_sites(gm, tm, dm):
    """Find endogenous curves; fixed exogenous curves still work on every LP backend."""
    boundaries = dm.get_boundary_natural_flow_values(dm.cur_date)
    sites = []
    for flow in gm.graph.interzone_flows:
        if all(
            loss.is_constant_fraction(dm.cur_date)
            for loss in (flow.loss_from_zone, flow.loss_to_zone)
        ):
            continue
        calculated_route = (
            flow.natural_flow_mode == NaturalFlowMode.CALCULATED
            and flow.id not in boundaries
            and gm.get_zone_by_id(flow.from_zone).type == ZoneTypes.STREAM
            and gm.get_zone_by_id(flow.to_zone).type == ZoneTypes.STREAM
        )
        if calculated_route or any(
            not trxn.is_slack for trxn, _ in tm.lookup_flow_trxns[flow.id]
        ):
            sites.append(flow)
    return sites


class PiecewiseLossModel:
    def __init__(self, apportioner, sites):
        self.a = apportioner
        self.engine = apportioner.engine
        self.gm, self.tm, self.dm, self.nfc = (
            apportioner.gm,
            apportioner.tm,
            apportioner.dm,
            apportioner.nfc,
        )
        self.date = self.dm.cur_date
        self.flows = sites
        self._serial = 0
        self._nf_rows = []
        self._nf_aux = []
        self._tracking_nf = False
        self.pre_components = {}
        self.post_components = {}
        self._source_values = {}
        self._sites = []
        self._graphs = []
        self.events = []
        self._previous_natural = {}
        self._previous_pools = {}
        self.local_gains = None
        self._validate_and_build_paths()
        path_ids = {
            trxn.id for _, _, _, transactions, _ in self._sites for trxn in transactions
        }
        self._path_transactions = sorted(
            (t for t in self.tm.all_trxns if t.id in path_ids),
            key=lambda t: t.priority,
        )
        self._path_ids = path_ids

    def _name(self, label):
        self._serial += 1
        return f"PWL_{self._serial}_{label}"

    def variable(self, label, lb=0, ub=None, binary=False):
        name = self._name(label)
        if binary:
            self.engine.add_binary_variable(name)
        else:
            self.engine.add_variable(name, lb=lb, ub=ub)
        if self._tracking_nf:
            self._nf_aux.append(name)
        return name

    def row(self, label, coefficients, lb=0, ub=0):
        name = self._name(label)
        self.engine.add_constraint(name, lb=lb, ub=ub)
        for var, coefficient in coefficients.items():
            self.engine.set_coefficient(name, var, coefficient)
        if self._tracking_nf:
            self._nf_rows.append(name)
        return name

    def remaining_graph(self, loss, driver, upper, label):
        """Encode y = R(x) on [0, upper] without arbitrary big-M constants."""
        if not isfinite(upper) or upper < 0:
            raise ValueError(
                f"{label}: piecewise driver needs a finite non-negative domain"
            )
        definition = loss.definition_for_date(self.date)
        segments = [
            s
            for s in definition.segments
            if s.min_driver_flow < upper or s.min_driver_flow == 0 == upper
        ]
        result = self.variable(label + "_remaining", ub=upper)
        self._graphs.append((loss, driver, result))
        if len(segments) == 1:
            segment = segments[0]
            self.row(
                label,
                {result: 1, driver: -segment.remaining_slope},
                -segment.loss_intercept,
                -segment.loss_intercept,
            )
            return result
        x_equation, y_equation, selectors = {driver: -1}, {result: -1}, {}
        for i, segment in enumerate(segments):
            lo = segment.min_driver_flow
            hi = min(upper, segment.max_driver_flow or upper)
            selected = self.variable(f"{label}_segment_{i}", binary=True)
            distance = self.variable(f"{label}_distance_{i}", ub=hi - lo)
            self.row(
                label + "_domain", {distance: 1, selected: -(hi - lo)}, lb=None, ub=0
            )
            selectors[selected] = 1
            x_equation[selected] = lo
            x_equation[distance] = 1
            y_equation[selected] = segment.remaining_at(lo)
            y_equation[distance] = segment.remaining_slope
        self.row(label + "_select", selectors, 1, 1)
        self.row(label + "_driver", x_equation)
        self.row(label + "_delivery", y_equation)
        return result

    def _validate_and_build_paths(self):
        if any(abs(lag - round(lag)) > 1e-10 for lag in self.dm.flow_lags.values()):
            raise ValueError(
                "Fractional lags with piecewise losses require a coupled time-expanded model"
            )
        for flow in self.flows:
            losses = [
                ("from_zone", flow.loss_from_zone),
                ("to_zone", flow.loss_to_zone),
            ]
            configured = [
                (side, loss)
                for side, loss in losses
                if not loss.is_constant_fraction(self.date)
            ]
            if not configured:
                continue
            if flow.bidirectional:
                raise ValueError(
                    f"Piecewise loss on {flow.id}: bidirectional loss sites are not supported"
                )
            measured = self.dm.cur_flows_by_id[flow.id].measured
            if measured is None or not isfinite(measured) or measured < 0:
                raise ValueError(
                    f"Piecewise loss on {flow.id}: requires a finite non-negative daily flow"
                )
            if flow.flow_type == FlowComponentsTypes.UNCONSTRAINED:
                raise ValueError(
                    f"Piecewise loss on {flow.id}: an unconstrained physical flow needs an explicit loss driver"
                )
            transactions = []
            for trxn, item in self.tm.lookup_flow_trxns[flow.id]:
                if item.factor != 1:
                    raise ValueError(
                        f"Piecewise loss on {flow.id}: reverse or scaled path factors are not supported"
                    )
                if not trxn.is_slack:
                    transactions.append(trxn)
            transactions.sort(key=lambda t: t.priority)
            priorities = [t.priority for t in transactions]
            if len(priorities) != len(set(priorities)):
                raise ValueError(
                    f"Piecewise loss on {flow.id}: equal-priority transactions sharing this loss site require a loss-sharing rule"
                )
            for side, loss in configured:
                # An inverse curve with a flat delivered-flow segment is not a
                # single-valued relation, even if today's gauge is above it.
                if side == "from_zone" and any(
                    abs(s.remaining_slope) < 1e-10
                    for s in loss.definition_for_date(self.date).segments
                ):
                    raise ValueError(
                        f"Piecewise loss before {flow.id}: a 100% marginal-loss segment makes post-loss attribution ambiguous"
                    )
                initial = (
                    measured
                    if side == "to_zone"
                    else loss.inflow_for_remaining(
                        measured, date=self.date, require_unique=True
                    )
                )
                self._sites.append((flow, side, loss, transactions, initial))
                self._previous_pools[(flow.id, side)] = initial
                previous_pool = self.variable(
                    flow.id + "_pool", lb=measured, ub=measured
                )
                previous_output = self.variable(
                    flow.id + "_output",
                    lb=(
                        loss.transform_total_flow(initial, date=self.date)
                        if side == "to_zone"
                        else initial
                    ),
                    ub=(
                        loss.transform_total_flow(initial, date=self.date)
                        if side == "to_zone"
                        else initial
                    ),
                )
                for trxn in transactions:
                    path_var = f"{trxn.id}___{flow.id}"
                    pool = self.variable(flow.id + "_pool", ub=measured)
                    self.row(
                        flow.id + "_pool", {pool: 1, previous_pool: -1, path_var: 1}
                    )
                    if side == "to_zone":
                        transformed = self.remaining_graph(loss, pool, initial, flow.id)
                    else:
                        transformed = self.variable(flow.id + "_above", ub=initial)
                        below = self.remaining_graph(
                            loss, transformed, initial, flow.id
                        )
                        self.row(flow.id + "_inverse", {below: 1, pool: -1})
                    component = self.variable(flow.id + "_component", ub=initial)
                    self.row(
                        flow.id + "_increment",
                        {component: 1, previous_output: -1, transformed: 1},
                    )
                    collection = (
                        self.pre_components
                        if side == "from_zone"
                        else self.post_components
                    )
                    collection[(trxn.id, flow.id)] = {component: 1}
                    previous_pool, previous_output = pool, transformed

        # Replace only continuity rows that touch a piecewise endpoint.
        for trxn in self.tm.all_trxns:
            if not isinstance(trxn, PathTrxn) or trxn.is_slack:
                continue
            path = self.tm.ordered_paths[trxn.id]
            for first, second in zip(path, path[1:], strict=False):
                f1, f2 = (
                    self.gm.get_flow_by_id(first.flow_id),
                    self.gm.get_flow_by_id(second.flow_id),
                )
                exit_loss = f1.loss_to_zone if first.factor > 0 else f1.loss_from_zone
                entry_loss = f2.loss_from_zone if second.factor > 0 else f2.loss_to_zone
                if exit_loss.is_constant_fraction(
                    self.date
                ) and entry_loss.is_constant_fraction(self.date):
                    continue
                outgoing = self.post_components.get((trxn.id, first.flow_id))
                if outgoing is None:
                    outgoing = {
                        f"{trxn.id}___{first.flow_id}": 1
                        - exit_loss.get_fraction(self.date)
                    }
                incoming = self.pre_components.get((trxn.id, second.flow_id))
                factor = (1 - first.loss_after) * (1 - second.loss_before)
                if incoming is None:
                    incoming = {f"{trxn.id}___{second.flow_id}": 1}
                    factor *= 1 - entry_loss.get_fraction(self.date)
                coefficients = dict(incoming)
                for name, coefficient in outgoing.items():
                    coefficients[name] = (
                        coefficients.get(name, 0) - factor * coefficient
                    )
                self.row("continuity_" + trxn.id, coefficients)

    def source_expression(self, trxn):
        path = self.tm.ordered_paths[trxn.id]
        if not path:
            return {}
        return self.pre_components.get(
            (trxn.id, path[0].flow_id), {self.tm.get_anchor_var(trxn): 1}
        )

    def initialize_natural_flow(self):
        nfc = self.nfc
        self.local_gains = dict(nfc.remaining_natural_at_zone)
        self._previous_natural = dict(nfc.remaining_natural_at_zone)
        for source, flow in nfc._calculated_outflow_by_zone.items():
            self.local_gains[flow.to_zone] -= self._route(
                flow, nfc.remaining_natural_at_zone[source]
            )
        self._rebuild_natural_flow()

    def _route(self, flow, value):
        return flow.loss_to_zone.transform_total_flow(
            flow.loss_from_zone.transform_total_flow(max(0, value), date=self.date),
            date=self.date,
        )

    def credit_natural_flow(self, credits):
        for zone, amount in credits.items():
            self.local_gains[zone] += amount
        if any(abs(amount) > 1e-10 for amount in credits.values()):
            self._rebuild_natural_flow()
        self._record_events("Spills", self._previous_pools)

    def _rebuild_natural_flow(self):
        # The upper domain is the exact no-withdrawal supply for this phase.
        # Spill credits rebuild it; no artificial global flow cap is imposed.
        for row in self._nf_rows:
            self.engine.cons[row].Clear()
            self.engine.cons[row].SetBounds(float("-inf"), float("inf"))
        for variable in self._nf_aux:
            self.engine.vars[variable].SetBounds(0, 0)
        self._nf_rows, self._nf_aux = [], []
        self._tracking_nf = True
        routes = self.nfc._calculated_outflow_by_zone
        incoming = {zone: [] for zone in self.local_gains}
        for source, flow in routes.items():
            incoming[flow.to_zone].append((source, flow))
        upper = {}

        def supply(zone):
            if zone not in upper:
                upper[zone] = max(
                    0,
                    self.local_gains[zone]
                    + sum(
                        self._route(flow, supply(source))
                        for source, flow in incoming[zone]
                    ),
                )
            return upper[zone]

        for zone in incoming:
            supply(zone)
        remaining = {
            zone: self.variable("remaining_" + zone, ub=upper[zone])
            for zone in incoming
        }
        deliveries = {}
        for source, flow in routes.items():
            midway = self.remaining_graph(
                flow.loss_from_zone,
                remaining[source],
                upper[source],
                "nf_from_" + flow.id,
            )
            midway_upper = flow.loss_from_zone.transform_total_flow(
                upper[source], date=self.date
            )
            deliveries[source] = self.remaining_graph(
                flow.loss_to_zone, midway, midway_upper, "nf_to_" + flow.id
            )
        for zone in incoming:
            row = self.engine.cons["NF_ZONE_" + zone]
            row.Clear()
            row.SetBounds(self.local_gains[zone], self.local_gains[zone])
            self.engine.set_coefficient("NF_ZONE_" + zone, remaining[zone], 1)
            for source, _ in incoming[zone]:
                self.engine.set_coefficient("NF_ZONE_" + zone, deliveries[source], -1)
            for trxn in self.tm.get_nf_trxn_ids_for_zone(zone):
                for variable, coefficient in self.source_expression(trxn).items():
                    self.engine.set_coefficient(
                        "NF_ZONE_" + zone, variable, coefficient
                    )
        self._tracking_nf = False
        self.remaining_variables = remaining

    def commit_allocation(self, trxn, delta):
        if not isinstance(trxn, PathTrxn):
            return
        if trxn.id in self._path_ids:
            self.reconstruct_committed_paths(trxn.id)
            return
        # A diversion whose own path has constant losses cannot change the
        # priority pools at a piecewise site. Only its exact NF effect changes.
        source = self.tm.get_nf_zone_id(trxn)
        if source is not None:
            self.nfc.apply_committed_allocation(source, delta)
        self._record_events(trxn.id, self._previous_pools)

    def reconstruct_committed_paths(self, objective_id):
        """Recover committed components without taking tentative junior values."""
        pools = {
            (flow.id, side): (
                initial
                if side == "to_zone"
                else loss.transform_total_flow(initial, date=self.date)
            )
            for flow, side, loss, _, initial in self._sites
        }
        sources = {}
        for trxn in self._path_transactions:
            path = self.tm.ordered_paths[trxn.id]
            if not path:
                continue
            amount = self.a.cur_trxn_value.get(self.tm.get_anchor_var(trxn), 0)
            delivered = 0.0
            for i, item in enumerate(path):
                flow = self.gm.get_flow_by_id(item.flow_id)
                entry = flow.loss_from_zone if item.factor > 0 else flow.loss_to_zone
                exit_loss = (
                    flow.loss_to_zone if item.factor > 0 else flow.loss_from_zone
                )
                if i:
                    before = (
                        delivered
                        * (1 - path[i - 1].loss_after)
                        * (1 - item.loss_before)
                    )
                    if not entry.is_constant_fraction(self.date):
                        pool = pools[(flow.id, "from_zone")]
                        above = entry.inflow_for_remaining(pool, date=self.date)
                        amount = pool - entry.transform_total_flow(
                            max(0, above - before), date=self.date
                        )
                    else:
                        amount = before * (1 - entry.get_fraction(self.date))
                self.a.cur_trxn_value[f"{trxn.id}___{item.flow_id}"] = amount
                if not entry.is_constant_fraction(self.date):
                    pool = pools[(flow.id, "from_zone")]
                    pre = entry.inflow_for_remaining(
                        pool, date=self.date
                    ) - entry.inflow_for_remaining(
                        max(0, pool - amount), date=self.date
                    )
                    pools[(flow.id, "from_zone")] = max(0, pool - amount)
                else:
                    pre = amount  # Preserve the existing anchor convention for constant losses.
                if i == 0:
                    sources[trxn.id] = pre
                if not exit_loss.is_constant_fraction(self.date):
                    pool = pools[(flow.id, "to_zone")]
                    delivered = exit_loss.transform_total_flow(
                        pool, date=self.date
                    ) - exit_loss.transform_total_flow(
                        max(0, pool - amount), date=self.date
                    )
                    pools[(flow.id, "to_zone")] = max(0, pool - amount)
                else:
                    delivered = amount * (1 - exit_loss.get_fraction(self.date))
        for trxn in self._path_transactions:
            source = self.tm.get_nf_zone_id(trxn)
            if source is not None:
                delta = sources[trxn.id] - self._source_values.get(trxn.id, 0)
                self.nfc.apply_committed_allocation(source, delta)
        self._source_values = sources
        drivers = {}
        for flow, side, loss, _, _ in self._sites:
            pool = pools[(flow.id, side)]
            drivers[(flow.id, side)] = (
                pool
                if side == "to_zone"
                else loss.inflow_for_remaining(pool, date=self.date)
            )
        self._record_events(objective_id, drivers)

    def _record_events(self, objective_id, drivers):
        if self.a.generate_audit:
            routes = {flow.id for flow in self.nfc._calculated_outflow_by_zone.values()}
            for flow, side, loss, _, _ in self._sites:
                pairs = [
                    (
                        ("allocated_measured_flow" if getattr(self, "method", "depletion") == "buildup"
                         else "unallocated_measured_flow"),
                        self._previous_pools[(flow.id, side)],
                        drivers[(flow.id, side)],
                    )
                ]
                if flow.id in routes:
                    old = max(0, self._previous_natural[flow.from_zone])
                    new = max(0, self.nfc.remaining_natural_at_zone[flow.from_zone])
                    if side == "to_zone":
                        old = flow.loss_from_zone.transform_total_flow(
                            old, date=self.date
                        )
                        new = flow.loss_from_zone.transform_total_flow(
                            new, date=self.date
                        )
                    pairs.append(("remaining_natural_flow", old, new))
                for kind, old, new in pairs:
                    points = [s.min_driver_flow for s in loss.definition_for_date(self.date).segments[1:]]
                    if hasattr(self, "method"):
                        points.insert(0, 0.0)
                    breaks = [
                        point for point in points
                        if (
                            old < point <= new
                            or new <= point < old
                        )
                    ]
                    if breaks:
                        self.events.append(
                            SolverOutputLossEvent(
                                self.date,
                                objective_id,
                                flow.id,
                                side,
                                kind,
                                old,
                                new,
                                sorted(breaks, reverse=new < old),
                            )
                        )
        self._previous_natural = dict(self.nfc.remaining_natural_at_zone)
        self._previous_pools = dict(drivers)

    def validate_solution(self):
        """Check graph equality independently of the integer formulation."""
        for loss, driver, output in self._graphs:
            q = self.engine._last_solution_values[driver]
            remaining = self.engine._last_solution_values[output]
            expected = loss.transform_total_flow(max(0, q), date=self.date)
            if abs(expected - remaining) > 1e-5:
                raise RuntimeError(
                    f"Piecewise loss graph failed validation: {driver}, {q}, {remaining}, expected {expected}"
                )

    def loss_allocations(self):
        records = []
        for flow, side, loss, transactions, initial in self._sites:
            pool = (
                initial
                if side == "to_zone"
                else loss.transform_total_flow(initial, date=self.date)
            )
            for trxn in transactions:
                amount = self.a.cur_trxn_value.get(f"{trxn.id}___{flow.id}", 0)
                remaining_pool = max(0, pool - amount)
                if side == "to_zone":
                    inflow = amount
                    remaining = loss.transform_total_flow(
                        pool, date=self.date
                    ) - loss.transform_total_flow(remaining_pool, date=self.date)
                else:
                    inflow = loss.inflow_for_remaining(
                        pool, date=self.date
                    ) - loss.inflow_for_remaining(remaining_pool, date=self.date)
                    remaining = amount
                records.append(
                    SolverOutputLossAllocation(
                        self.date,
                        trxn.id,
                        flow.id,
                        side,
                        inflow,
                        remaining,
                        inflow - remaining,
                    )
                )
                pool = remaining_pool
            inflow = (
                pool
                if side == "to_zone"
                else loss.inflow_for_remaining(pool, date=self.date)
            )
            remaining = loss.transform_total_flow(inflow, date=self.date)
            records.append(
                SolverOutputLossAllocation(
                    self.date,
                    f"SLACK_{flow.from_zone}_TO_{flow.to_zone}_{flow.id}",
                    flow.id,
                    side,
                    inflow,
                    remaining,
                    inflow - remaining,
                )
            )
        return records
