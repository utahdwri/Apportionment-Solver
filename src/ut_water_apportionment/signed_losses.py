"""Signed endpoint accounting and delivery-weighted priority cohorts.

Physical flow stays nonnegative. Intermediate accounting states may have either
sign; their loss is L(max(0, q)) and their signed delivery is q - L(max(0, q)).
For a cohort, loss_i * sum(delivery) == total_loss * delivery_i is enforced
inside the optimization model. Delivery means the nonnegative magnitude leaving
this endpoint in the member's transaction direction, in physical flow units.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from math import inf, isfinite

from .lp_solver import LPSolverError
from .models import FlowComponentsTypes, PathTrxn, SolverOutputLossAllocation
from .piecewise_losses import PiecewiseLossModel


def needs_cohort_backend(gm, tm, beg, end):
    for flow in gm.graph.interzone_flows:
        priorities = [
            t.priority for t, _ in tm.lookup_flow_trxns[flow.id] if not t.is_slack
        ]
        if len(priorities) == len(set(priorities)):
            continue
        for loss in (flow.loss_from_zone, flow.loss_to_zone):
            dates = {beg}
            for interval in loss.intervals:
                after = date.fromisoformat(interval.end_date)
                if after < date.max:
                    dates.add((after + timedelta(days=1)).isoformat())
                dates.add(interval.beg_date)
            if any(not loss.is_constant_fraction(d) for d in dates if beg <= d <= end):
                return True
    return False


def needs_signed_loss_model(tm, flows, method):
    if method == "buildup":
        return True
    for flow in flows:
        real = [(t, p) for t, p in tm.lookup_flow_trxns[flow.id] if not t.is_slack]
        priorities = [t.priority for t, _ in real]
        if (
            flow.bidirectional
            or any(p.factor != 1 for _, p in real)
            or len(priorities) != len(set(priorities))
        ):
            return True
    return False


@dataclass
class Member:
    trxn: object
    item: object
    before: str
    after: str
    loss: str

    @property
    def sign(self):
        return 1 if self.item.factor > 0 else -1

    @property
    def delivery(self):
        return self.after if self.sign > 0 else self.before


@dataclass
class Site:
    flow: object
    side: str
    loss: object
    measured: float
    initial: float
    cohorts: list
    pools: list


class SignedLossModel(PiecewiseLossModel):
    joint_commit = True

    def __init__(self, apportioner, sites):
        self.method = apportioner.loss_attribution_method
        self.sites = []
        self._path_binaries = set()
        self._cohort_checks = []
        self._evaluation_omit_rows = set()
        self._evaluated_values = {}
        super().__init__(apportioner, sites)
        self._evaluation_engine = self._make_evaluation_engine()

    @staticmethod
    def remaining(loss, q, day):
        return q - loss.get_loss(max(0.0, q), date=day)

    def inverse(self, loss, q):
        return q if q < 0 else loss.inflow_for_remaining(q, date=self.date)

    def variable(self, label, lb=0, ub=None, binary=False):
        name = super().variable(label, lb, ub, binary)
        if binary and not self._tracking_nf:
            self._path_binaries.add(name)
        return name

    def remaining_graph(self, loss, driver, upper, label, lower=0):
        """Exact signed graph with an additional zero-loss branch below zero."""
        if not all(isfinite(x) for x in (lower, upper)) or lower > upper:
            raise ValueError(f"{label}: finite accounting-flow bounds are required")
        pieces = []
        if lower < 0:
            pieces.append((lower, min(0, upper), 1.0, 0.0))
        if upper >= 0:
            for segment in loss.definition_for_date(self.date).segments:
                lo = max(lower, segment.min_driver_flow)
                hi = min(
                    upper,
                    segment.max_driver_flow
                    if segment.max_driver_flow is not None
                    else upper,
                )
                if hi > lo or (lower == upper == lo):
                    pieces.append(
                        (lo, hi, segment.remaining_slope, -segment.loss_intercept)
                    )
        result = self.variable(
            label + "_remaining",
            lb=self.remaining(loss, lower, self.date),
            ub=self.remaining(loss, upper, self.date),
        )
        self._graphs.append((loss, driver, result))
        if len(pieces) == 1:
            _, _, slope, intercept = pieces[0]
            self.row(label, {result: 1, driver: -slope}, intercept, intercept)
            return result
        xs, ys, selectors = {driver: -1}, {result: -1}, {}
        for lo, hi, slope, intercept in pieces:
            z = self.variable(label + "_segment", binary=True)
            w = self.variable(label + "_distance", ub=hi - lo)
            self.row(label + "_range", {w: 1, z: -(hi - lo)}, lb=None, ub=0)
            selectors[z] = 1
            xs[z], xs[w] = lo, 1
            ys[z], ys[w] = slope * lo + intercept, slope
        self.row(label + "_select", selectors, 1, 1)
        self.row(label + "_input", xs)
        self.row(label + "_output", ys)
        return result

    def _validate_and_build_paths(self):
        if any(abs(lag - round(lag)) > 1e-10 for lag in self.dm.flow_lags.values()):
            raise ValueError(
                "Fractional lags with piecewise losses require a coupled time-expanded model"
            )
        old_vars = set(self.engine.vars)
        old_rows = set(self.engine.cons)
        for flow in self.flows:
            measured = self.dm.cur_flows_by_id[flow.id].measured
            if measured is None or not isfinite(measured) or measured < 0:
                raise ValueError(
                    f"Piecewise loss on {flow.id} requires non-negative, finite physical flow"
                )
            if flow.flow_type == FlowComponentsTypes.UNCONSTRAINED:
                raise ValueError(
                    f"Piecewise loss on {flow.id} requires an explicit physical loss driver"
                )
            entries = self.tm.lookup_flow_trxns[flow.id]
            if any(not isfinite(p.factor) or p.factor == 0 for _, p in entries):
                raise ValueError(
                    f"Piecewise loss on {flow.id} needs nonzero, finite path factors"
                )
            grouped = defaultdict(list)
            for trxn, item in entries:
                grouped[inf if trxn.is_slack else trxn.priority].append((trxn, item))
            cohorts = [
                sorted(grouped[k], key=lambda x: x[0].id) for k in sorted(grouped)
            ]
            if any(len(c) > 1 and not c[0][0].is_slack for c in cohorts):
                if not callable(getattr(self.engine, "add_quadratic_constraint", None)):
                    raise ValueError(
                        "Delivery-weighted equal-priority losses require solver_backend='scip' (install ut-water-apportionment[scip])"
                    )
            real = [t for cohort in cohorts for t, _ in cohort if not t.is_slack]
            for side, loss in (
                ("from_zone", flow.loss_from_zone),
                ("to_zone", flow.loss_to_zone),
            ):
                if side == "from_zone" and any(
                    abs(s.remaining_slope) < 1e-10
                    for s in loss.definition_for_date(self.date).segments
                ):
                    raise ValueError(
                        f"Piecewise loss before {flow.id}: 100% marginal-loss makes the post-loss gauge ambiguous"
                    )
                initial = (
                    measured if side == "to_zone" else self.inverse(loss, measured)
                )
                self._sites.append((flow, side, loss, real, initial))
                self._previous_pools[(flow.id, side)] = (
                    initial if self.method == "depletion" else 0.0
                )
                site = Site(flow, side, loss, measured, initial, [], [])
                self.sites.append(site)
                definition = loss.definition_for_date(self.date)
                loss_cap = max(
                    s.loss_at(s.min_driver_flow) for s in definition.segments
                )
                if definition.segments[-1].loss_slope > 0:
                    loss_cap = inf
                for cohort in cohorts:
                    members = []
                    for trxn, item in cohort:
                        measured_var = f"{trxn.id}___{flow.id}"
                        mag = self.variable(flow.id + "_measured_component")
                        self.row("magnitude", {mag: 1, measured_var: -abs(item.factor)})
                        other = self.variable(flow.id + "_endpoint_component")
                        before, after = (
                            (mag, other) if side == "to_zone" else (other, mag)
                        )
                        assigned = self.variable(
                            flow.id + "_assigned_loss", lb=-loss_cap, ub=loss_cap
                        )
                        sign = 1 if item.factor > 0 else -1
                        self.row(
                            "component_loss", {before: sign, after: -sign, assigned: -1}
                        )
                        members.append(Member(trxn, item, before, after, assigned))
                        collection = (
                            self.pre_components
                            if side == "from_zone"
                            else self.post_components
                        )
                        collection[(trxn.id, flow.id)] = {other: 1}
                    if not isfinite(loss_cap):
                        # A global Lipschitz bound contains every exact cohort
                        # assignment and allows the linear relaxation to infer bounds.
                        slope = max(abs(s.loss_slope) for s in definition.segments)
                        for member in members:
                            for sign in (-1, 1):
                                coefficients = {m.before: -slope for m in members}
                                coefficients[member.loss] = sign
                                self.row("loss_bound", coefficients, lb=None, ub=0)
                    site.cohorts.append(members)

        self._affected_ids = {t.id for _, _, _, txns, _ in self._sites for t in txns}
        self._replace_continuity()
        # Bound inference uses a linear relaxation before any segment selection
        # or nonlinear sharing rows have been added. No arbitrary flow cap.
        self._flow_bounds = self._derive_flow_bounds()
        for site in self.sites:
            self._build_site_graph(site)
        self._path_aux = set(self.engine.vars) - old_vars
        self._path_rows = set(self.engine.cons) - old_rows
        self._path_var_names = self._path_aux | {
            f"{t.id}___{p.flow_id}"
            for t in self.tm.all_trxns
            if t.id in self._affected_ids
            for p in self.tm.ordered_paths[t.id]
        }

    def _endpoint(self, trxn, item, entering):
        """Nonnegative physical magnitude at the endpoint in path direction."""
        forward = item.factor > 0
        from_side = forward if entering else not forward
        collection = self.pre_components if from_side else self.post_components
        expression = collection.get((trxn.id, item.flow_id))
        if expression is not None:
            return dict(expression), 1.0
        flow = self.gm.get_flow_by_id(item.flow_id)
        loss = flow.loss_from_zone if from_side else flow.loss_to_zone
        fraction = loss.get_fraction(self.date)
        measured = {f"{trxn.id}___{item.flow_id}": abs(item.factor)}
        if entering:
            return measured, 1 - fraction
        return {v: c * (1 - fraction) for v, c in measured.items()}, 1.0

    def _replace_continuity(self):
        for trxn in self.tm.all_trxns:
            if trxn.id not in self._affected_ids:
                continue
            path = self.tm.ordered_paths[trxn.id]
            for i, (first, second) in enumerate(zip(path, path[1:], strict=False)):
                old = self.engine.cons[f"CONT_{trxn.id}_{i}"]
                old.Clear()
                outgoing, _ = self._endpoint(trxn, first, entering=False)
                incoming, entry_factor = self._endpoint(trxn, second, entering=True)
                factor = (
                    entry_factor * (1 - first.loss_after) * (1 - second.loss_before)
                )
                coefficients = dict(incoming)
                for v, c in outgoing.items():
                    coefficients[v] = coefficients.get(v, 0) - factor * c
                self.row("continuity_" + trxn.id, coefficients)

    def _derive_flow_bounds(self):
        bounds = {}
        for flow in self.flows:
            real = [
                (t, p) for t, p in self.tm.lookup_flow_trxns[flow.id] if not t.is_slack
            ]
            measured = self.dm.cur_flows_by_id[flow.id].measured
            if not flow.bidirectional and all(p.factor > 0 for _, p in real):
                bounds[flow.id] = (measured, 0.0)
                continue
            positive = negative = 0.0
            for trxn, item in real:
                name = f"{trxn.id}___{flow.id}"
                try:
                    _, values = self.engine.solve_objective([name])
                except (LPSolverError, RuntimeError) as exc:
                    raise ValueError(
                        f"Cannot infer a finite counterflow bound for {name}; add finite transaction or shared limits and check accounting feasibility"
                    ) from exc
                cap = abs(item.factor) * values[name]
                if not isfinite(cap):
                    raise ValueError(f"No finite counterflow bound for {name}")
                # Outward rounding protects valid solutions from solver noise.
                cap = max(0.0, cap) + 1e-6 * max(1.0, abs(cap))
                if item.factor > 0:
                    positive += cap
                else:
                    negative += cap
            bounds[flow.id] = (positive, negative)
        return bounds

    def _build_site_graph(self, site):
        flow, loss = site.flow, site.loss
        positive, negative = self._flow_bounds[flow.id]
        if self.method == "depletion":
            lower, upper, initial, sign = (
                min(0, site.measured - positive),
                site.measured + negative,
                site.measured,
                -1,
            )
        else:
            lower, upper, initial, sign = (
                -negative,
                max(site.measured, positive),
                0.0,
                1,
            )
        previous_pool = self.variable(flow.id + "_pool", initial, initial)
        first = initial if site.side == "to_zone" else self.inverse(loss, initial)
        output_value = (
            self.remaining(loss, first, self.date) if site.side == "to_zone" else first
        )
        previous_output = self.variable(flow.id + "_output", output_value, output_value)
        site.pools.append(first)
        other_lo = (
            self.remaining(loss, lower, self.date)
            if site.side == "to_zone"
            else self.inverse(loss, lower)
        )
        other_hi = (
            self.remaining(loss, upper, self.date)
            if site.side == "to_zone"
            else self.inverse(loss, upper)
        )
        driver_lo, driver_hi = (
            (lower, upper) if site.side == "to_zone" else (other_lo, other_hi)
        )
        extrema = [max(0.0, driver_lo), max(0.0, driver_hi)]
        extrema.extend(
            s.min_driver_flow
            for s in loss.definition_for_date(self.date).segments
            if driver_lo <= s.min_driver_flow <= driver_hi
        )
        loss_range = max(loss.get_loss(q, date=self.date) for q in extrema)
        # Individual gross deliveries can exceed the cohort's net delivery
        # range when opposite directions share a loss. Bound them separately.
        component_cap = site.measured + positive + negative + loss_range
        for members in site.cohorts:
            slack = members[0].trxn.is_slack
            rows_before_cohort = set(self.engine.cons)
            final = 0.0 if self.method == "depletion" else site.measured
            pool = self.variable(
                flow.id + "_pool", final if slack else lower, final if slack else upper
            )
            coefficients = {pool: 1, previous_pool: -1}
            for m in members:
                coefficients[f"{m.trxn.id}___{flow.id}"] = -sign * m.item.factor
            self.row("pool_balance", coefficients)
            if site.side == "to_zone":
                transformed = self.remaining_graph(
                    loss, pool, upper, flow.id, lower=lower
                )
                driver = pool
            else:
                transformed = self.variable(flow.id + "_above", other_lo, other_hi)
                below = self.remaining_graph(
                    loss, transformed, other_hi, flow.id, lower=other_lo
                )
                self.row("inverse", {below: 1, pool: -1})
                driver = transformed
            site.pools.append(driver)
            aggregate = {transformed: -sign, previous_output: sign}
            for m in members:
                variable = m.after if site.side == "to_zone" else m.before
                aggregate[variable] = m.sign
                # This bound concerns magnitude in either transaction direction.
                self.engine.vars[variable].SetBounds(0, component_cap)
            self.row("cohort_increment", aggregate)
            if len(members) > 1 and not slack:
                total = self.variable("cohort_loss", lb=None)
                delivery = self.variable("cohort_delivery")
                self.row("cohort_loss", {total: -1, **{m.loss: 1 for m in members}})
                self.row(
                    "cohort_delivery",
                    {delivery: -1, **{m.delivery: 1 for m in members}},
                )
                for m in members[:-1]:
                    self.engine.add_quadratic_constraint(
                        self._name("delivery_share"),
                        [(m.loss, delivery, 1), (total, m.delivery, -1)],
                    )
                self._cohort_checks.append(members)
            if slack and len(members) == 2:
                # Residuals are one signed component, not two unbounded
                # opposing slack components that can manufacture attribution.
                z = self.variable("residual_direction", binary=True)
                for m in members:
                    for v, cap in (
                        (m.before, max(upper - lower, other_hi - other_lo)),
                        (m.after, max(upper - lower, other_hi - other_lo)),
                    ):
                        if m.sign > 0:
                            self.row("residual_sign", {v: 1, z: -cap}, lb=None, ub=0)
                        else:
                            self.row("residual_sign", {v: 1, z: cap}, lb=None, ub=cap)
            previous_pool, previous_output = pool, transformed
            if slack:
                # A partial priority state may require not-yet-committed juniors
                # to reconcile the physical gauge. Audit that signed state
                # without pretending terminal physical residuals are committed.
                self._evaluation_omit_rows.update(
                    set(self.engine.cons) - rows_before_cohort
                )

    def source_expression(self, trxn):
        if trxn.id not in self._affected_ids:
            return super().source_expression(trxn)
        first = self.tm.ordered_paths[trxn.id][0]
        expression, factor = self._endpoint(trxn, first, entering=True)
        if factor != 1:
            # Retain the legacy gauge-anchor convention outside modeled sites.
            return {self.tm.get_anchor_var(trxn): abs(first.factor)}
        return expression

    def filter_counterflow_minimization(self, objectives, candidates):
        # Retain the established reservoir ambiguity convention elsewhere.
        # At a modeled loss site, a real junior must stay free to support a
        # senior's signed accounting component.
        sites = {f.id for f in self.flows}
        endpoints = set()
        for trxn in objectives:
            path = self.tm.ordered_paths.get(trxn.id, [])
            if path:
                endpoints.update(
                    p.flow_id for p in (path[0], path[-1]) if p.flow_id in sites
                )
        return [
            t
            for t in candidates
            if t.is_slack or not any(p.flow_id in endpoints for p in t.path)
        ]

    def get_spill_credit(self, trxn):
        item = self.tm.ordered_paths[trxn.id][0]
        collection = self.post_components if item.factor > 0 else self.pre_components
        expression = collection.get((trxn.id, item.flow_id))
        if expression is None:
            return None
        return sum(
            c * self.engine._last_solution_values[v] for v, c in expression.items()
        )

    def _make_evaluation_engine(self):
        """Small, reusable path-only model for committed (not tentative) anchors."""
        engine = type(self.engine)(tolerance=1e-8)
        # Slack endpoint variables participate in terminal loss reconciliation;
        # their measured variables are free to be the signed residual.
        names = self._path_var_names | {
            f"{m.trxn.id}___{site.flow.id}"
            for site in self.sites
            for cohort in site.cohorts
            for m in cohort
        }
        for name in sorted(names):
            variable = self.engine.vars[name]
            if name in self._path_binaries:
                engine.add_binary_variable(name)
            else:
                engine.add_variable(name, variable.lb(), variable.ub())
        for name in sorted(self._path_rows - self._evaluation_omit_rows):
            original = self.engine.cons[name]
            engine.add_constraint(name, original.lb(), original.ub())
            for v, coefficient in original.coefficients.items():
                engine.set_coefficient(name, v, coefficient)
        for name, (terms, linear, rhs) in getattr(
            self.engine, "quadratic_rows", {}
        ).items():
            engine.add_quadratic_constraint(name, terms, linear, rhs)
        return engine

    def commit_allocation(self, trxn, delta):
        self.commit_cohort([(trxn, delta)])

    def commit_cohort(self, changes):
        affected = any(t.id in self._path_ids for t, _ in changes)
        if affected:
            evaluation = self._evaluation_engine
            for trxn in self._path_transactions:
                anchor = self.tm.get_anchor_var(trxn)
                amount = self.a.cur_trxn_value.get(anchor, 0.0)
                evaluation.vars[anchor].SetBounds(amount, amount)
            variables = [
                f"{t.id}___{p.flow_id}"
                for t in self._path_transactions
                for p in self.tm.ordered_paths[t.id]
            ]
            try:
                evaluation.solve_objective(variables, maximization=False)
            except LPSolverError as exc:
                raise RuntimeError(
                    "Committed anchors do not admit consistent signed loss attribution"
                ) from exc
            self._evaluated_values = dict(evaluation._last_solution_values)
            for name in variables:
                self.a.cur_trxn_value[name] = self._evaluated_values[name]
            for trxn in self._path_transactions:
                source = self.tm.get_nf_zone_id(trxn)
                if source is not None:
                    amount = sum(
                        c * self._evaluated_values[v]
                        for v, c in self.source_expression(trxn).items()
                    )
                    self.nfc.apply_committed_allocation(
                        source, amount - self._source_values.get(trxn.id, 0.0)
                    )
                    self._source_values[trxn.id] = amount
        for trxn, delta in changes:
            if isinstance(trxn, PathTrxn) and trxn.id not in self._path_ids:
                source = self.tm.get_nf_zone_id(trxn)
                if source is not None:
                    self.nfc.apply_committed_allocation(source, delta)
        drivers = dict(self._previous_pools)
        if affected:
            for site in self.sites:
                # Last real cohort, immediately before terminal residuals.
                q = site.pools[-2]
                drivers[(site.flow.id, site.side)] = (
                    self._evaluated_values[q] if isinstance(q, str) else q
                )
        self._record_events(",".join(t.id for t, _ in changes), drivers)

    def validate_solution(self):
        values = self.engine._last_solution_values
        for loss, driver, result in self._graphs:
            expected = self.remaining(loss, values[driver], self.date)
            if abs(values[result] - expected) > 1e-5:
                raise RuntimeError(f"Signed loss graph failed validation for {driver}")
        for members in self._cohort_checks:
            total_loss = sum(values[m.loss] for m in members)
            delivery = sum(max(0, values[m.delivery]) for m in members)
            if delivery > 1e-7:
                for m in members:
                    expected = total_loss * max(0, values[m.delivery]) / delivery
                    if abs(values[m.loss] - expected) > 1e-5:
                        raise RuntimeError(
                            f"Delivery-weighted loss failed validation for {m.trxn.id}"
                        )

    def loss_allocations(self):
        values = self.engine._last_solution_values
        return [
            SolverOutputLossAllocation(
                self.date,
                m.trxn.id,
                site.flow.id,
                site.side,
                m.sign * values[m.before],
                m.sign * values[m.after],
                values[m.loss],
            )
            for site in self.sites
            for cohort in site.cohorts
            for m in cohort
            if not site.loss.is_constant_fraction(self.date)
        ]
