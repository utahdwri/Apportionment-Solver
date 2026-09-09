"""Commit loss using the predetermined weights of each allocation increment.

Current increments use exact piecewise graphs and constant allocation weights,
including signed/scaled paths. Pending components form a feasibility relaxation;
only exact active increments enter the immutable ledger. Parent reservations
are checked against an isolated replay of their children's allocation schedule.
No continuous-variable products are used.
"""

from contextlib import contextmanager
from copy import deepcopy
from math import inf

from .lp_solver import LPSolverError
from .models import PathTrxn, SolverOutputLossIncrement, TrxnGroup
from .signed_losses import SignedLossModel


class IncrementalLossModel(SignedLossModel):
    incremental = True

    def __init__(self, apportioner, sites):
        self.committed = {}
        self.active_ids = set()
        self.allocation_weights = {}
        self._tail_rows = set()
        self._tail_vars = set()
        self._tail_graphs = []
        self.increments = []
        self._increment_sequence = 0
        self._proportion_rows = []
        self.step_paths = {}
        self.step_components = {}
        self._step_rows = set()
        super().__init__(apportioner, sites)
        self._primitive_rows = self._path_rows - self._tail_rows
        self._primitive_vars = self._path_aux - self._tail_vars
        for t in self.tm.all_trxns:
            if isinstance(t, PathTrxn) and not t.is_slack:
                for p in self.tm.ordered_paths[t.id]:
                    self.committed[f"{t.id}___{p.flow_id}"] = 0.0
        for site in self.sites:
            for cohort in site.cohorts:
                for m in cohort:
                    if not m.trxn.is_slack:
                        self.committed.update(
                            {m.before: 0.0, m.after: 0.0, m.loss: 0.0}
                        )

    def _make_evaluation_engine(self):
        # The current increment has its own path variables in the physical
        # model; no reconstruction from tentative final allocations is needed.
        return None

    def _new(self, label, lb=0, ub=None, binary=False):
        v = self.variable(label, lb=lb, ub=ub, binary=binary)
        self._tail_vars.add(v)
        return v

    def _constraint(self, label, coefficients, lb=0, ub=0):
        row = self.row(label, coefficients, lb, ub)
        self._tail_rows.add(row)
        return row

    def _groups(self, site):
        active, pending, residual = [], [], []
        for cohort in site.cohorts:
            if cohort[0].trxn.is_slack:
                residual.extend(cohort)
            else:
                active.extend(m for m in cohort if m.trxn.id in self.active_ids)
                pending.append(cohort)
        return (
            ([(active, True)] if active else [])
            + [(c, False) for c in pending]
            + ([(residual, False)] if residual else [])
        )

    def _pool(self, site):
        allocated = sum(
            m.item.factor * self.committed.get(f"{m.trxn.id}___{site.flow.id}", 0)
            for cohort in site.cohorts
            for m in cohort
            if not m.trxn.is_slack
        )
        return site.measured - allocated if self.method == "depletion" else allocated

    def _build_site_graph(self, site):
        old_rows, old_vars = set(self.engine.cons), set(self.engine.vars)
        graph_start = len(self._graphs)
        flow, loss = site.flow, site.loss
        positive, negative = self._flow_bounds[flow.id]
        direction = 1 if self.method == "buildup" else -1
        lower = -negative if direction > 0 else min(0, site.measured - positive)
        upper = (
            max(site.measured, positive) if direction > 0 else site.measured + negative
        )
        q = self._pool(site)
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
        initial_output = (
            self.remaining(loss, q, self.date)
            if site.side == "to_zone"
            else self.inverse(loss, q)
        )
        previous_pool = self._new("committed_pool", q, q)
        previous_output = self._new("committed_output", initial_output, initial_output)
        site.pools = [q if site.side == "to_zone" else initial_output]
        for members, active in self._groups(site):
            slack = members[0].trxn.is_slack
            rows_before = set(self.engine.cons)
            lo, hi = lower, upper
            final = site.measured if direction > 0 else 0.0
            pool = self._new(
                "increment_pool", final if slack else lo, final if slack else hi
            )
            balance = {pool: 1, previous_pool: -1}
            rhs = 0.0
            before, after, losses = {}, {}, {}
            for m in members:
                name = f"{m.trxn.id}___{flow.id}"
                if active:
                    balance[self.step_paths[name]] = -direction * m.item.factor
                else:
                    balance[name] = -direction * m.item.factor
                    rhs -= direction * m.item.factor * self.committed.get(name, 0)
                    if name in self.step_paths:
                        balance[self.step_paths[name]] = direction * m.item.factor
                for mapping, total in (
                    (before, m.before),
                    (after, m.after),
                    (losses, m.loss),
                ):
                    delta = self._new(
                        "increment_component", lb=None if mapping is losses else 0
                    )
                    if active:
                        self.step_components[total] = delta
                    else:
                        coefficients = {total: 1, delta: -1}
                        if total in self.step_components:
                            coefficients[self.step_components[total]] = -1
                        self._constraint(
                            "increment_offset",
                            coefficients,
                            self.committed.get(total, 0),
                            self.committed.get(total, 0),
                        )
                    mapping[m.trxn.id] = delta
                if active:
                    measured_delta = (
                        before[m.trxn.id]
                        if site.side == "to_zone"
                        else after[m.trxn.id]
                    )
                    self._constraint(
                        "increment_magnitude",
                        {measured_delta: 1, self.step_paths[name]: -abs(m.item.factor)},
                    )
                    self._constraint(
                        "increment_component_loss",
                        {
                            before[m.trxn.id]: m.sign,
                            after[m.trxn.id]: -m.sign,
                            losses[m.trxn.id]: -1,
                        },
                    )
            self._constraint("increment_pool_balance", balance, rhs, rhs)
            if site.side == "to_zone":
                transformed = self.remaining_graph(loss, pool, hi, flow.id, lower=lo)
                driver = pool
            else:
                transformed = self._new("increment_above", other_lo, other_hi)
                below = self.remaining_graph(
                    loss, transformed, other_hi, flow.id, lower=other_lo
                )
                self._constraint("increment_inverse", {below: 1, pool: -1})
                driver = transformed
            site.pools.append(driver)
            aggregate = {transformed: -direction, previous_output: direction}
            for m in members:
                aggregate[
                    after[m.trxn.id] if site.side == "to_zone" else before[m.trxn.id]
                ] = m.sign
            self._constraint("increment_delivery", aggregate)
            if active and len(members) > 1:
                # Weights belong to this allocation increment, not to its
                # resulting deliveries. Normalize over members at this site.
                total_loss = self._new("increment_loss", lb=None)
                self._constraint(
                    "increment_loss_sum",
                    {total_loss: -1, **{losses[m.trxn.id]: 1 for m in members}},
                )
                denominator = sum(self.allocation_weights[m.trxn.id] for m in members)
                for m in members:
                    weight = (
                        self.allocation_weights[m.trxn.id] / denominator
                        if denominator
                        else 0
                    )
                    self._constraint(
                        "fixed_increment_share",
                        {losses[m.trxn.id]: 1, total_loss: -weight},
                    )
            # Pending amounts are feasibility bounds, not allocations. Their
            # shares remain free in this relaxation; only an exact active
            # increment can enter the ledger. Parent reservations are checked
            # by replaying the child allocator with the same fixed-share rule.
            if slack and len(members) == 2:
                selector = self._new("residual_direction", binary=True)
                cap = max(upper - lower, other_hi - other_lo)
                for m in members:
                    for v in (m.before, m.after):
                        self._constraint(
                            "residual_sign",
                            {v: 1, selector: -cap if m.sign > 0 else cap},
                            lb=None,
                            ub=0 if m.sign > 0 else cap,
                        )
            if slack:
                self._evaluation_omit_rows.update(set(self.engine.cons) - rows_before)
            if active:
                self._step_rows.update(set(self.engine.cons) - rows_before)
            previous_pool, previous_output = pool, transformed
        self._tail_rows.update(set(self.engine.cons) - old_rows)
        self._tail_vars.update(set(self.engine.vars) - old_vars)
        self._tail_graphs.extend(self._graphs[graph_start:])

    def _build_step_paths(self):
        for t in self.tm.all_trxns:
            if not isinstance(t, PathTrxn) or t.id not in self.active_ids:
                continue
            for p in self.tm.ordered_paths[t.id]:
                name = f"{t.id}___{p.flow_id}"
                step = self._new(
                    "path_increment",
                    ub=max(
                        0, self.engine.vars[name].ub() - self.committed.get(name, 0)
                    ),
                )
                self.step_paths[name] = step
                self._constraint(
                    "path_increment_floor",
                    {name: 1, step: -1},
                    self.committed.get(name, 0),
                    None,
                )

    def _build_step_continuity(self):
        mapping = self.step_paths | self.step_components
        for t in self.tm.all_trxns:
            if not isinstance(t, PathTrxn) or t.id not in self.active_ids:
                continue
            path = self.tm.ordered_paths[t.id]
            for i, (first, second) in enumerate(zip(path, path[1:], strict=False)):
                if t.id in self._path_ids:
                    outgoing, _ = self._endpoint(t, first, entering=False)
                    incoming, entry_factor = self._endpoint(t, second, entering=True)
                    factor = (
                        entry_factor * (1 - first.loss_after) * (1 - second.loss_before)
                    )
                    coefficients = dict(incoming)
                    for v, c in outgoing.items():
                        coefficients[v] = coefficients.get(v, 0) - factor * c
                else:
                    coefficients = self.engine.cons[f"CONT_{t.id}_{i}"].coefficients
                self._step_rows.add(
                    self._constraint(
                        "step_continuity",
                        {mapping[v]: c for v, c in coefficients.items()},
                    )
                )

    def _group_capacities(self, groups):
        """Reserve only what the children can realize with incremental sharing.

        A final-delivery sharing witness can overstate a parent's capacity when
        a child stops early. Preview the child schedule on an isolated ledger;
        the parent reserves its aggregate, never the preview's loss assignments.
        Include sibling groups at the same priority so their shared children
        see the same proportional cohort even after one parent reaches a limit.
        """
        priorities = {g.priority for g in groups}
        roots = [
            t
            for t in self.tm.all_trxns
            if isinstance(t, TrxnGroup) and t.priority in priorities
        ]
        descendants = {
            t.id for g in roots for t in self.tm.traverse_vars(g.children_trxns)
        }
        for flow in self.flows:
            children = [
                t
                for t, _ in self.tm.lookup_flow_trxns[flow.id]
                if t.id in descendants and not t.is_slack
            ]
            if not children:
                continue
            latest = max(t.priority for t in children)
            for t, _ in self.tm.lookup_flow_trxns[flow.id]:
                if (
                    not t.is_slack
                    and t.id not in descendants
                    and min(priorities) <= t.priority <= latest
                ):
                    raise ValueError(
                        "Incremental parent reservations with interleaved "
                        f"priorities are not supported: outside transaction "
                        f"{t.id!r} changes loss site {flow.id!r} before the "
                        "group's children finish. This requires a joint model "
                        "of the future increments."
                    )
        engine = type(self.engine)(tolerance=self.engine.tolerance)
        for name, v in self.engine.vars.items():
            if name in self.engine.binary_variables:
                engine.add_binary_variable(name)
                engine.vars[name].SetBounds(v.lb(), v.ub())
            else:
                engine.add_variable(name, v.lb(), v.ub())
        for name, c in self.engine.cons.items():
            engine.add_constraint(name, c.lb(), c.ub())
            for v, coefficient in c.coefficients.items():
                engine.set_coefficient(name, v, coefficient)
        preview = deepcopy(self.a, {id(self.engine): engine})
        preview.generate_audit = False
        for g in roots:
            engine.vars[g.id].SetLb(0)
        all_trxns = preview.tm.all_trxns
        preview.tm.all_trxns = [t for t in all_trxns if t.id in descendants]
        schedule = preview.tm.build_schedule(self.date)
        preview.tm.all_trxns = all_trxns
        preview.calculate_apportionments(schedule)
        capacities = {
            g.id: sum(
                preview.loss_model.committed.get(self.tm.get_anchor_var(t), 0)
                for t in self.tm.traverse_vars(g.children_trxns)
                if isinstance(t, PathTrxn)
            )
            for g in groups
        }
        return {
            g.id: max(
                self.engine.vars[g.id].lb(),
                capacities[g.id],
                sum(
                    self.committed.get(self.tm.get_anchor_var(t), 0)
                    for t in self.tm.traverse_vars(g.children_trxns)
                    if isinstance(t, PathTrxn)
                ),
            )
            for g in groups
        }

    def prepare_increment(self, variables, factors=None):
        for name in self._proportion_rows:
            self.engine.cons[name].Clear()
            self.engine.cons[name].SetBounds(-inf, inf)
        self._proportion_rows = []
        if "INC_combined" in self.engine.vars:
            self.engine.vars["INC_combined"].SetBounds(0, 0)
        self.active_ids = {t.id for t in variables if isinstance(t, PathTrxn)}
        self.allocation_weights = {
            t.id: (factors or {}).get(self.tm.get_anchor_var(t), 1.0)
            for t in variables
            if isinstance(t, PathTrxn)
        }
        for name in self._tail_rows:
            self.engine.cons[name].Clear()
            self.engine.cons[name].SetBounds(-inf, inf)
        for name in self._tail_vars:
            self.engine.vars[name].SetBounds(0, 0)
        old_graphs = {driver for _, driver, _ in self._tail_graphs}
        self._graphs = [g for g in self._graphs if g[1] not in old_graphs]
        self._tail_rows, self._tail_vars = set(), set()
        self._tail_graphs = []
        self.step_paths, self.step_components = {}, {}
        self._step_rows = set()
        self._evaluation_omit_rows = set()
        self._build_step_paths()
        for site in self.sites:
            self._build_site_graph(site)
        self._build_step_continuity()
        groups = [t for t in variables if isinstance(t, TrxnGroup)]
        if groups:
            for name, capacity in self._group_capacities(groups).items():
                self._constraint("increment_group_capacity", {name: 1}, None, capacity)
        self._path_rows = self._primitive_rows | self._tail_rows
        self._path_aux = self._primitive_vars | self._tail_vars
        self._path_var_names = self._path_aux | {
            f"{t.id}___{p.flow_id}"
            for t in self._path_transactions
            for p in self.tm.ordered_paths[t.id]
        }

    def prepare_proportions(self, names, factors):
        if "INC_combined" not in self.engine.vars:
            self.engine.add_variable("INC_combined")
        self.engine.vars["INC_combined"].SetBounds(0, inf)
        self._proportion_initial = {v: self.engine.vars[v].lb() for v in names}
        for v in names:
            row = "INC_proportion_" + v
            if row not in self.engine.cons:
                self.engine.add_constraint(row)
            c = self.engine.cons[row]
            c.Clear()
            initial = 0 if v in self.step_paths else self._proportion_initial[v]
            c.SetBounds(initial, initial)
            c.SetCoefficient(self.engine.vars[self.step_paths.get(v, v)], 1)
            c.SetCoefficient(self.engine.vars["INC_combined"], -factors[v])
            self._proportion_rows.append(row)

    def _step_scale(self):
        """Trim numerical cap overshoot before replaying exact anchor increments.

        The replay rebuilds endpoint losses from the graph; endpoint assignments
        themselves must never be scaled across multiple curve segments.
        """
        scale = 1.0
        values = self.engine._last_solution_values
        mapping = self.step_paths | self.step_components
        for name, step in mapping.items():
            delta = values[step]
            if delta > 1e-7:
                scale = min(
                    scale,
                    max(0, self.engine.vars[name].ub() - self.committed.get(name, 0))
                    / delta,
                )
        # Nonnegative accounting rows (including a delivery gauge) give a hard
        # ceiling even with future allocations. Correct tolerated overshoot
        # before it becomes an irreversible delivery above that physical cap.
        # Signed rows are excluded: future counterflows may legitimately support
        # a gross committed component larger than the net measurement.
        for row in self.engine.cons.values():
            if (
                row.ub() == inf
                or not row.coefficients
                or any(
                    c < 0 or self.engine.vars[v].lb() < 0
                    for v, c in row.coefficients.items()
                )
            ):
                continue
            delta = sum(
                c * values[mapping[v]]
                for v, c in row.coefficients.items()
                if v in mapping
            )
            if delta > 1e-7:
                initial = sum(
                    c * self.committed.get(v, self.engine.vars[v].lb())
                    for v, c in row.coefficients.items()
                )
                scale = min(scale, max(0, row.ub() - initial) / delta)
        return scale

    def maximize_proportional(self, names, factors):
        _, values = self.engine.solve_objective(["INC_combined"])
        amount = values["INC_combined"] * self._step_scale()
        if any(v not in self.step_paths for v in names):
            # Reservations become lower bounds in independently solved models.
            # Keep rounding headroom there, without shrinking physical caps.
            amount = max(0, amount - 1e-7)
        return {
            v: max(
                self.engine.vars[v].lb(),
                min(
                    self.engine.vars[v].ub(),
                    self._proportion_initial[v] + factors[v] * amount,
                ),
            )
            for v in names
        }

    def maximize_variable(self, name):
        if name not in self.step_paths:
            initial = self.engine.vars[name].lb()
            _, values = self.engine.solve_objective([name])
            value = max(initial, values[name] - 1e-7)
            self.engine.vars[name].SetLb(value)
            return value
        initial = self.committed.get(name, 0)
        _, values = self.engine.solve_objective([self.step_paths[name]])
        value = min(
            self.engine.vars[name].ub(),
            initial + values[self.step_paths[name]] * self._step_scale(),
        )
        self.engine.vars[name].SetLb(value)
        return value

    def solve_increment_objective(self, names):
        objective, values = self.engine.solve_objective(
            [self.step_paths.get(v, v) for v in names]
        )
        return objective, {
            v: self.committed.get(v, 0) + values[self.step_paths[v]]
            if v in self.step_paths
            else values[v]
            for v in names
        }

    def _evaluate_increment(self, active):
        """Replay exact anchor increments with all physical constraints intact.

        A path-only replay can choose endpoint amounts inconsistent with a
        delivery gauge when the same anchors admit multiple path solutions.
        Keep the full model here; only the current increment enters the ledger.
        """
        bounds = {}
        try:
            for t in active:
                anchor = self.tm.get_anchor_var(t)
                variable = self.engine.vars[self.step_paths[anchor]]
                bounds[variable.name()] = (variable.lb(), variable.ub())
                amount = max(
                    0,
                    self.a.cur_trxn_value.get(anchor, 0)
                    - self.committed.get(anchor, 0),
                )
                variable.SetBounds(amount, amount)
            self.engine.solve_objective(
                list(self.step_paths.values()), maximization=False
            )
            return dict(self.engine._last_solution_values)
        except LPSolverError as exc:
            raise RuntimeError(
                "Committed anchors do not admit a physically feasible loss increment"
            ) from exc
        finally:
            for name, (lo, hi) in bounds.items():
                self.engine.vars[name].SetBounds(lo, hi)

    @contextmanager
    def committed_solution(self):
        """Residual/spill solves reconcile the ledger, never allocate new rights."""
        bounds = {
            v: (self.engine.vars[v].lb(), self.engine.vars[v].ub())
            for v in self.committed
        }
        try:
            for v, value in self.committed.items():
                self.engine.vars[v].SetBounds(value, value)
            yield
        finally:
            for v, (lo, hi) in bounds.items():
                self.engine.vars[v].SetBounds(lo, hi)

    def commit_cohort(self, changes):
        active = [t for t, _ in changes if isinstance(t, PathTrxn)]
        if active:
            values = self._evaluate_increment(active)
            path_vars = list(self.step_paths)
            before_commit = dict(self.committed)
            old_pools = {(s.flow.id, s.side): self._pool(s) for s in self.sites}
            for name in path_vars:
                self.committed[name] = self.committed.get(name, 0) + max(
                    0, values[self.step_paths[name]]
                )
                self.a.cur_trxn_value[name] = self.committed[name]
            for name, step in self.step_components.items():
                self.committed[name] = self.committed.get(name, 0) + values[step]
            # Parent reservations may have been rounded slightly below the
            # attainable total. Once children are committed, their actual sum
            # is the parent's floor for subsequent proportional/spill passes.
            for g in self.tm.all_trxns:
                if not isinstance(g, TrxnGroup):
                    continue
                amount = sum(
                    self.committed.get(self.tm.get_anchor_var(t), 0)
                    for t in self.tm.traverse_vars(g.children_trxns)
                    if isinstance(t, PathTrxn)
                )
                bound = max(
                    self.engine.vars[g.id].lb(),
                    min(self.engine.vars[g.id].ub(), amount),
                )
                self.engine.vars[g.id].SetLb(bound)
                self.a.cur_trxn_value[g.id] = bound
            # Validate sharing on this increment, not on accumulated totals.
            if any(
                abs(self.committed.get(v, 0) - before_commit.get(v, 0)) > 1e-7
                for v in path_vars
            ):
                self._increment_sequence += 1
            for site in self.sites:
                members = [
                    m for c in site.cohorts for m in c if m.trxn.id in self.active_ids
                ]

                def delta(v):
                    return self.committed.get(v, 0) - before_commit.get(v, 0)

                total = sum(delta(m.loss) for m in members)
                denominator = sum(self.allocation_weights[m.trxn.id] for m in members)
                for m in members:
                    expected = (
                        total * self.allocation_weights[m.trxn.id] / denominator
                        if denominator
                        else 0
                    )
                    if abs(delta(m.loss) - expected) > 1e-5:
                        raise RuntimeError(
                            f"Increment loss sharing failed for {m.trxn.id}"
                        )
                if self.a.generate_audit and not site.loss.is_constant_fraction(
                    self.date
                ):
                    old, new = old_pools[(site.flow.id, site.side)], self._pool(site)
                    if site.side == "from_zone":
                        old, new = (
                            self.inverse(site.loss, old),
                            self.inverse(site.loss, new),
                        )
                    for m in members:
                        if abs(delta(m.before)) + abs(delta(m.after)) > 1e-7:
                            self.increments.append(
                                SolverOutputLossIncrement(
                                    self.date,
                                    m.trxn.id,
                                    site.flow.id,
                                    site.side,
                                    m.sign * delta(m.before),
                                    m.sign * delta(m.after),
                                    delta(m.loss),
                                    self._increment_sequence,
                                    old,
                                    new,
                                    self.allocation_weights[m.trxn.id] / denominator
                                    if denominator
                                    else 0,
                                )
                            )
            for t in active:
                source = self.tm.get_nf_zone_id(t)
                if source is not None:
                    amount = sum(
                        c * self.committed[v]
                        for v, c in self.source_expression(t).items()
                    )
                    self.nfc.apply_committed_allocation(
                        source, amount - self._source_values.get(t.id, 0)
                    )
                    self._source_values[t.id] = amount
        drivers = {
            (s.flow.id, s.side): self._pool(s)
            if s.side == "to_zone"
            else self.inverse(s.loss, self._pool(s))
            for s in self.sites
        }
        self._record_events(",".join(t.id for t, _ in changes), drivers)

    def validate_solution(self):
        super().validate_solution()
        values = self.engine._last_solution_values
        for site in self.sites:
            for cohort in site.cohorts:
                for m in cohort:
                    if not m.trxn.is_slack:
                        for name in (m.before, m.after, m.loss):
                            if abs(values[name] - self.committed.get(name, 0)) > 1e-5:
                                raise RuntimeError(
                                    f"Uncommitted loss component remains for {m.trxn.id}"
                                )
