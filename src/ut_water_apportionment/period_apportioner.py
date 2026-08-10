"""One sparse LP spanning the complete solve period.

Transaction/path-component variables in this model are indexed in *real-world*
measurement time.  Fractional lags are represented directly in temporal
continuity/natural-flow constraints, so no post-solve deconvolution is needed.

For a flow having absolute lag ``whole + fraction``, its value in accounting
time ``d`` is represented by

    (1 - fraction) * x[d - whole]
        + fraction * x[d - whole - 1]

where ``x`` is the real-world transaction component on that flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from math import floor, isclose, isfinite
import logging

from .apportioner import (
    Apportioner,
    PREFIX_CONT,
    PREFIX_MEASURE,
    PREFIX_NF_ZONE,
    PREFIX_PARENT,
    SLACK_TRXN_PRIORITY,
    SOLVER_TOL,
)
from .graph_manager import GraphManager
from .lp_solver import LPSolverFactory, LPSolverProtocol
from .models import (
    CorePropSchedule,
    CoreSeqSchedule,
    FlowComponentsTypes,
    InterzoneFlow,
    SolverOutputApportionment,
    SolverOutputSolveStepEvidence,
    Trxn,
    TrxnGroup,
    TrxnPathItem,
    ZoneTypes,
)
from .timeseries_manager import (
    COALESCE_MISSING_FLOWS_TO_ZERO,
    COALESCE_NEGATIVE_FLOWS_TO_ZERO,
    DailyDataManager,
)
from .trxn_schedule import TrxnSchedule


logger = logging.getLogger(__name__)

_DATE_TOKEN = "___DATE_"


@dataclass(frozen=True)
class _DailySnapshot:
    measured_by_flow: dict[str, float]
    natural_by_flow: dict[str, float | None]
    available_natural_by_zone: dict[str, float]


def _date_range(beg_date: str, end_date: str) -> list[str]:
    beg = date.fromisoformat(beg_date)
    end = date.fromisoformat(end_date)
    output = []
    current = beg
    while current <= end:
        output.append(current.isoformat())
        current += timedelta(days=1)
    return output


class PeriodApportioner(Apportioner):
    """Build and solve one LP containing every day in the solve period.

    The existing :class:`Apportioner` still owns the lexicographic/equal-priority
    solving machinery, feasibility fallback, and audit summarization.  This
    subclass changes the model from one variable per transaction/path item to
    one variable per transaction/path item/real-world date.
    """

    def __init__(
        self,
        gm: GraphManager,
        tm: TrxnSchedule,
        dm: DailyDataManager,
        beg_date: str,
        end_date: str,
        lp_solver_factory: LPSolverFactory | None = None,
    ):
        self.accounting_dates = _date_range(beg_date, end_date)
        self._snapshots = self._snapshot_daily_data(gm, dm)
        self._flow_dates, self._lag_terms = self._build_time_index(dm)
        self.real_dates = sorted(
            {
                real_date
                for dates in self._flow_dates.values()
                for real_date in dates
            }
        )
        self._objective_date: str | None = None
        self._schedules: dict[str, CoreSeqSchedule] = {}

        super().__init__(
            gm,
            tm,
            dm,
            lp_solver_factory=lp_solver_factory,
        )

        self._schedules = {
            real_date: self.tm.build_schedule(real_date)
            for real_date in self.real_dates
        }

    # ------------------------------------------------------------------
    # Time indexing
    # ------------------------------------------------------------------

    def _snapshot_daily_data(
        self,
        gm: GraphManager,
        dm: DailyDataManager,
    ) -> dict[str, _DailySnapshot]:
        """Resolve physical/natural-flow state once for every accounting day."""

        snapshots: dict[str, _DailySnapshot] = {}

        for accounting_date in self.accounting_dates:
            dm.set_day(accounting_date)
            dm.calc_natural_flows()

            snapshots[accounting_date] = _DailySnapshot(
                measured_by_flow={
                    flow.id: float(dm.cur_flows_by_id[flow.id].measured or 0.0)
                    for flow in gm.graph.interzone_flows
                },
                natural_by_flow={
                    flow.id: dm.cur_flows_by_id[flow.id].natural
                    for flow in gm.graph.interzone_flows
                },
                available_natural_by_zone={
                    zone.id: dm.get_available_natural_at_zone(zone.id)
                    for zone in gm.graph.zones
                    if zone.type == ZoneTypes.STREAM
                },
            )

        return snapshots

    def _build_time_index(
        self,
        dm: DailyDataManager,
    ) -> tuple[
        dict[str, list[str]],
        dict[tuple[str, str], list[tuple[str, float]]],
    ]:
        """Return real-world dates and interpolation terms for each flow.

        ``terms[(flow_id, accounting_date)]`` contains ``(real_date, weight)``
        pairs.  The weights are exactly the same interpolation used by
        ``MeasurementCollection.get()``.
        """

        flow_dates: dict[str, set[str]] = {
            flow.id: set()
            for flow in dm.gm.graph.interzone_flows
        }
        terms: dict[tuple[str, str], list[tuple[str, float]]] = {}

        for flow in dm.gm.graph.interzone_flows:
            lag = dm.flow_lags[flow.id]
            whole = floor(lag)
            fraction = lag - whole

            if isclose(fraction, 0.0, abs_tol=1e-12):
                whole = int(round(lag))
                fraction = 0.0

            for accounting_date in self.accounting_dates:
                accounting_day = date.fromisoformat(accounting_date)

                newer_date = (
                    accounting_day - timedelta(days=whole)
                ).isoformat()

                current_terms = [(newer_date, 1.0 - fraction)]
                flow_dates[flow.id].add(newer_date)

                if fraction > 0:
                    older_date = (
                        accounting_day - timedelta(days=whole + 1)
                    ).isoformat()
                    current_terms.append((older_date, fraction))
                    flow_dates[flow.id].add(older_date)

                terms[(flow.id, accounting_date)] = current_terms

        return (
            {
                flow_id: sorted(dates)
                for flow_id, dates in flow_dates.items()
            },
            terms,
        )

    @staticmethod
    def _dated_name(base_name: str, real_date: str) -> str:
        return f"{base_name}{_DATE_TOKEN}{real_date}"

    def _path_var_name(
        self,
        trxn: Trxn,
        flow_id: str,
        real_date: str,
    ) -> str:
        return self._dated_name(
            f"{trxn.id}___{flow_id}",
            real_date,
        )

    def _group_var_name(
        self,
        group: TrxnGroup,
        real_date: str,
    ) -> str:
        return self._dated_name(group.id, real_date)

    def _target_var(
        self,
        var: Trxn | TrxnGroup,
        real_date: str | None = None,
    ) -> str | None:
        real_date = real_date or self._objective_date
        if real_date is None:
            raise ValueError("No objective date has been selected.")

        if type(var) == Trxn:
            ordered = self.tm.ordered_paths.get(var.id, [])
            if not ordered:
                return None
            target = self._path_var_name(
                var,
                ordered[0].flow_id,
                real_date,
            )
        else:
            target = self._group_var_name(var, real_date)

        if not self.engine.has_variable(target):
            return None

        return target

    def _aligned_var_terms(
        self,
        trxn: Trxn,
        flow_id: str,
        accounting_date: str,
    ) -> list[tuple[str, float]]:
        return [
            (
                self._path_var_name(trxn, flow_id, real_date),
                weight,
            )
            for real_date, weight
            in self._lag_terms[(flow_id, accounting_date)]
        ]

    @staticmethod
    def _add_coefficient(
        coefficients: dict[str, float],
        variable_name: str,
        coefficient: float,
    ) -> None:
        coefficients[variable_name] = (
            coefficients.get(variable_name, 0.0)
            + coefficient
        )

    def _add_aligned_expression(
        self,
        coefficients: dict[str, float],
        trxn: Trxn,
        path_item: TrxnPathItem,
        accounting_date: str,
        scale: float,
    ) -> None:
        for variable_name, weight in self._aligned_var_terms(
            trxn,
            path_item.flow_id,
            accounting_date,
        ):
            self._add_coefficient(
                coefficients,
                variable_name,
                scale * weight,
            )

    def _set_row_coefficients(
        self,
        engine: LPSolverProtocol,
        constraint_name: str,
        coefficients: dict[str, float],
    ) -> None:
        for variable_name, coefficient in coefficients.items():
            if not isclose(coefficient, 0.0, abs_tol=1e-14):
                engine.set_coefficient(
                    constraint_name,
                    variable_name,
                    coefficient,
                )

    # ------------------------------------------------------------------
    # Physical measurement values
    # ------------------------------------------------------------------

    def _raw_observed_value(
        self,
        flow: InterzoneFlow,
        real_date: str,
    ) -> float:
        """Return the observed net flow on an actual measurement date."""

        total = 0.0

        for measurement in flow.flow_measurements:
            value = self.dm.measurements.get(
                measurement.measurement_id,
                real_date,
                0,
            )

            if value is None:
                if COALESCE_MISSING_FLOWS_TO_ZERO:
                    value = 0.0
                else:
                    raise ValueError(
                        f"Measurement {measurement.measurement_id} "
                        f"is undefined on {real_date}."
                    )

            total += value * measurement.adjustment_factor

        if not isfinite(total):
            raise ValueError(
                f"Measured flow for {flow.id!r} on {real_date} "
                "must be finite."
            )

        if total < 0 and not flow.bidirectional:
            if COALESCE_NEGATIVE_FLOWS_TO_ZERO:
                total = 0.0
            else:
                raise ValueError(
                    f"Net flow is negative for {flow.id!r} "
                    f"on {real_date}."
                )

        return total

    # ------------------------------------------------------------------
    # LP construction
    # ------------------------------------------------------------------

    def _build_linear_equations(self) -> LPSolverProtocol:
        engine = self._lp_solver_factory(tolerance=SOLVER_TOL)

        self._add_variables(engine)
        self._add_measurement_constraints(engine)
        self._add_path_continuity_constraints(engine)
        self._add_natural_flow_constraints(engine)
        self._add_group_constraints(engine)

        return engine

    def _add_variables(self, engine: LPSolverProtocol) -> None:
        for trxn in self.all_trxns:
            if type(trxn) == Trxn:
                ordered = self.tm.ordered_paths.get(trxn.id, [])
                anchor_flow_id = (
                    ordered[0].flow_id
                    if ordered
                    else None
                )

                for path_item in trxn.path:
                    for real_date in self._flow_dates[path_item.flow_id]:
                        variable_name = self._path_var_name(
                            trxn,
                            path_item.flow_id,
                            real_date,
                        )

                        if engine.has_variable(variable_name):
                            continue

                        upper_limit = None
                        if (
                            path_item.flow_id == anchor_flow_id
                            and not trxn.is_slack
                        ):
                            upper_limit = (
                                self.tm.get_transaction_upper_limit(
                                    trxn,
                                    real_date,
                                )
                            )

                        engine.add_variable(
                            name=variable_name,
                            lb=0,
                            ub=upper_limit,
                        )

            elif type(trxn) == TrxnGroup:
                for real_date in self.real_dates:
                    engine.add_variable(
                        name=self._group_var_name(
                            trxn,
                            real_date,
                        ),
                        lb=0,
                        ub=self.tm.get_transaction_upper_limit(
                            trxn,
                            real_date,
                        ),
                    )

    def _add_measurement_constraints(
        self,
        engine: LPSolverProtocol,
    ) -> None:
        """Tie transaction components to physical flow measurements.

        Observed flows are constrained in real-world time.  Calculated/residual
        flows are known only in aligned accounting time, so their transaction
        decomposition is constrained through the lag interpolation expression.
        """

        for flow in self.gm.graph.interzone_flows:
            flow_trxns = self.tm.lookup_flow_trxns[flow.id]

            if flow.flow_type == FlowComponentsTypes.OBSERVATION:
                for real_date in self._flow_dates[flow.id]:
                    constraint_name = self._dated_name(
                        PREFIX_MEASURE + flow.id,
                        real_date,
                    )
                    measured = self._raw_observed_value(
                        flow,
                        real_date,
                    )

                    engine.add_constraint(
                        name=constraint_name,
                        lb=measured,
                        ub=measured,
                    )

                    coefficients: dict[str, float] = {}
                    for trxn, path_item in flow_trxns:
                        self._add_coefficient(
                            coefficients,
                            self._path_var_name(
                                trxn,
                                flow.id,
                                real_date,
                            ),
                            path_item.factor,
                        )

                    self._set_row_coefficients(
                        engine,
                        constraint_name,
                        coefficients,
                    )

            else:
                for accounting_date in self.accounting_dates:
                    constraint_name = self._dated_name(
                        PREFIX_MEASURE + flow.id,
                        accounting_date,
                    )
                    measured = self._snapshots[
                        accounting_date
                    ].measured_by_flow[flow.id]

                    engine.add_constraint(
                        name=constraint_name,
                        lb=measured,
                        ub=measured,
                    )

                    coefficients: dict[str, float] = {}
                    for trxn, path_item in flow_trxns:
                        self._add_aligned_expression(
                            coefficients,
                            trxn,
                            path_item,
                            accounting_date,
                            path_item.factor,
                        )

                    self._set_row_coefficients(
                        engine,
                        constraint_name,
                        coefficients,
                    )

    def _add_path_continuity_constraints(
        self,
        engine: LPSolverProtocol,
    ) -> None:
        """Enforce path continuity in common/aligned accounting time."""

        for trxn in self.all_trxns:
            if type(trxn) != Trxn:
                continue

            ordered_path = self.tm.ordered_paths.get(
                trxn.id,
                [],
            )

            for i in range(len(ordered_path) - 1):
                leg1 = ordered_path[i]
                leg2 = ordered_path[i + 1]

                flow1 = self.gm.get_flow_by_id(leg1.flow_id)
                flow2 = self.gm.get_flow_by_id(leg2.flow_id)

                loss1_exit = (
                    flow1.loss_to_zone
                    if leg1.factor > 0
                    else flow1.loss_from_zone
                )
                loss2_enter = (
                    flow2.loss_from_zone
                    if leg2.factor > 0
                    else flow2.loss_to_zone
                )

                for accounting_date in self.accounting_dates:
                    remaining_factor = (
                        (
                            1.0
                            - loss1_exit.get_fraction(
                                accounting_date
                            )
                        )
                        * (1.0 - leg1.loss_after)
                        * (
                            1.0
                            - loss2_enter.get_fraction(
                                accounting_date
                            )
                        )
                        * (1.0 - leg2.loss_before)
                    )

                    constraint_name = self._dated_name(
                        f"{PREFIX_CONT}{trxn.id}_{i}",
                        accounting_date,
                    )
                    engine.add_constraint(
                        name=constraint_name,
                        lb=0,
                        ub=0,
                    )

                    coefficients: dict[str, float] = {}

                    self._add_aligned_expression(
                        coefficients,
                        trxn,
                        leg2,
                        accounting_date,
                        1.0,
                    )
                    self._add_aligned_expression(
                        coefficients,
                        trxn,
                        leg1,
                        accounting_date,
                        -remaining_factor,
                    )

                    self._set_row_coefficients(
                        engine,
                        constraint_name,
                        coefficients,
                    )

    def _add_natural_flow_constraints(
        self,
        engine: LPSolverProtocol,
    ) -> None:
        stream_zone_ids = {
            zone.id
            for zone in self.gm.graph.zones
            if zone.type == ZoneTypes.STREAM
        }

        for accounting_date in self.accounting_dates:
            for zone_id in stream_zone_ids:
                engine.add_constraint(
                    name=self._dated_name(
                        PREFIX_NF_ZONE + zone_id,
                        accounting_date,
                    ),
                    lb=0,
                    ub=None,
                )

        for trxn in self.all_trxns:
            if type(trxn) != Trxn or trxn.is_slack:
                continue

            ordered_path = self.tm.ordered_paths.get(
                trxn.id,
                [],
            )
            if not ordered_path:
                continue

            first_item = ordered_path[0]
            first_flow = self.gm.get_flow_by_id(
                first_item.flow_id
            )

            from_zone = self.gm.get_zone_by_id(
                first_flow.from_zone
            )
            if first_item.factor < 0:
                from_zone = self.gm.get_zone_by_id(
                    first_flow.to_zone
                )

            if from_zone.type != ZoneTypes.STREAM:
                continue

            affected_zone_ids = {from_zone.id}
            for downstream_flow in self.gm.traverse_downstream(
                from_zone.id
            ):
                downstream_zone = self.gm.get_zone_by_id(
                    downstream_flow.to_zone
                )
                if downstream_zone.type == ZoneTypes.STREAM:
                    affected_zone_ids.add(
                        downstream_zone.id
                    )

            for accounting_date in self.accounting_dates:
                coefficients: dict[str, float] = {}
                self._add_aligned_expression(
                    coefficients,
                    trxn,
                    first_item,
                    accounting_date,
                    1.0,
                )

                for zone_id in affected_zone_ids:
                    constraint_name = self._dated_name(
                        PREFIX_NF_ZONE + zone_id,
                        accounting_date,
                    )

                    # Multiple transactions contribute to the same row.
                    for variable_name, coefficient in (
                        coefficients.items()
                    ):
                        # set_coefficient replaces a coefficient.  This
                        # variable is unique to this transaction, so no
                        # cross-transaction accumulation is needed.
                        engine.set_coefficient(
                            constraint_name,
                            variable_name,
                            coefficient,
                        )

    def _add_group_constraints(
        self,
        engine: LPSolverProtocol,
    ) -> None:
        for group in self.all_trxns:
            if type(group) != TrxnGroup:
                continue

            for real_date in self.real_dates:
                constraint_name = self._dated_name(
                    PREFIX_PARENT + group.id,
                    real_date,
                )
                engine.add_constraint(
                    name=constraint_name,
                    lb=0,
                    ub=0,
                )

                engine.set_coefficient(
                    constraint_name,
                    self._group_var_name(
                        group,
                        real_date,
                    ),
                    1.0,
                )

                for child in group.children_trxns:
                    if type(child) == Trxn:
                        ordered = self.tm.ordered_paths.get(
                            child.id,
                            [],
                        )
                        target = (
                            self._path_var_name(
                                child,
                                ordered[0].flow_id,
                                real_date,
                            )
                            if ordered
                            else None
                        )
                    else:
                        target = self._group_var_name(
                            child,
                            real_date,
                        )

                    if (
                        target is not None
                        and engine.has_variable(target)
                    ):
                        engine.set_coefficient(
                            constraint_name,
                            target,
                            -1.0,
                        )

    # ------------------------------------------------------------------
    # Natural-flow phases
    # ------------------------------------------------------------------

    def apply_nf_mass_balance_constraints(self) -> None:
        for accounting_date in self.accounting_dates:
            snapshot = self._snapshots[accounting_date]

            for zone in self.gm.graph.zones:
                if zone.type != ZoneTypes.STREAM:
                    continue

                constraint_name = self._dated_name(
                    PREFIX_NF_ZONE + zone.id,
                    accounting_date,
                )

                self.engine.update_constraint_lb(
                    constraint_name,
                    0,
                )
                self.engine.update_constraint_ub(
                    constraint_name,
                    snapshot.available_natural_by_zone.get(
                        zone.id,
                        0.0,
                    ),
                )

    def remove_nf_mass_balance_constraints(self) -> None:
        for accounting_date in self.accounting_dates:
            for zone in self.gm.graph.zones:
                if zone.type != ZoneTypes.STREAM:
                    continue

                constraint_name = self._dated_name(
                    PREFIX_NF_ZONE + zone.id,
                    accounting_date,
                )
                self.engine.update_constraint_lb(
                    constraint_name,
                    0,
                )
                self.engine.update_constraint_ub(
                    constraint_name,
                    None,
                )

    # ------------------------------------------------------------------
    # Period-wide lexicographic solving
    # ------------------------------------------------------------------

    def calculate_period_apportionments(self) -> None:
        """Solve priorities across all dates on the one persistent LP.

        Priority dominates date: every real-world date for a senior priority is
        solved/fixed before any junior priority is considered.  Within one
        priority, dates are processed chronologically.
        """

        priorities = sorted(
            {
                item.priority
                for schedule in self._schedules.values()
                for item in schedule.series
                if item.priority < SLACK_TRXN_PRIORITY
            }
        )

        for priority in priorities:
            for real_date in self.real_dates:
                schedule = self._schedules[real_date]

                matching_items = [
                    item
                    for item in schedule.series
                    if item.priority == priority
                ]
                if not matching_items:
                    continue

                self._objective_date = real_date
                # Existing audit helpers use dm.cur_date.
                self.dm.cur_date = real_date

                self.calculate_apportionments(
                    CoreSeqSchedule(
                        series=matching_items
                    )
                )

    def _maximize_var(
        self,
        var: Trxn | TrxnGroup,
    ) -> None:
        target_var = self._target_var(var)
        if not target_var:
            return

        original_ub = self._minimize_minus_vars([var])

        value_before = self.cur_trxn_value.get(
            target_var,
            0.0,
        )
        new_value = (
            self.engine.maximize_and_update_variable(
                target_var
            )
        )
        self.cur_trxn_value[target_var] = new_value

        solve_step, audit_context = (
            self._snapshot_audit_step(
                var=var,
                target_var=target_var,
                value_before=value_before,
                value_after=new_value,
            )
        )

        audit_record = self._record_audit_iteration(
            steps=[solve_step],
            contexts=[audit_context],
            limiting_txn_ids=[var.id],
            is_proportional=False,
        )

        reason = (
            audit_record.reason
            or "No limiting constraint identified"
        )
        self.cur_trxn_reasons[
            self._reason_key(var.id, self._objective_date)
        ] = reason

        self._reset_minus_vars(original_ub)

    def maximize_series(
        self,
        series: CorePropSchedule | CoreSeqSchedule,
    ) -> None:
        """Date-aware version of Apportioner's proportional solve."""

        maxed_vars: list[Trxn | TrxnGroup] = []
        deferred_vars: list[Trxn | TrxnGroup] = []

        vars_list, factors = self._get_next_iter(
            series,
            maxed_vars,
        )

        while vars_list:
            # Schedules contain every transaction, but a transaction's
            # anchor date range can differ from another flow's range.
            active: list[
                tuple[Trxn | TrxnGroup, float, str]
            ] = []

            for var, factor in zip(vars_list, factors):
                target = self._target_var(var)
                if target is None:
                    if var not in maxed_vars:
                        maxed_vars.append(var)
                    continue
                active.append((var, factor, target))

            if not active:
                vars_list, factors = self._get_next_iter(
                    series,
                    maxed_vars,
                )
                continue

            var_names = [
                target
                for _, _, target in active
            ]
            vars_by_name = {
                target: var
                for var, _, target in active
            }
            proportion_factors = {
                target: factor
                for _, factor, target in active
            }

            has_tiny_factor = False
            for target, factor in list(
                proportion_factors.items()
            ):
                if 0 < factor < 0.000001:
                    var = vars_by_name[target]
                    if var not in deferred_vars:
                        deferred_vars.append(var)
                    if var not in maxed_vars:
                        maxed_vars.append(var)
                    has_tiny_factor = True

            if has_tiny_factor:
                vars_list, factors = self._get_next_iter(
                    series,
                    maxed_vars,
                )
                continue

            if sum(proportion_factors.values()) == 0:
                for target in proportion_factors:
                    self.engine.update_variable_bounds(
                        target,
                        lb=0,
                    )
                    self.cur_trxn_value[target] = 0
                break

            active_vars = [
                var
                for var, _, _ in active
            ]
            original_ub = self._minimize_minus_vars(
                active_vars
            )

            values_before = {
                target: self.cur_trxn_value.get(
                    target,
                    0.0,
                )
                for target in var_names
            }

            var_values = (
                self.engine.maximize_group_by_proportions(
                    var_names,
                    proportion_factors,
                )
            )

            member_steps = []
            audit_contexts = []

            for target, value in var_values.items():
                if abs(value) < SOLVER_TOL:
                    value = 0.0

                self.engine.update_variable_bounds(
                    target,
                    lb=value,
                )
                self.cur_trxn_value[target] = value

                var = vars_by_name.get(target)
                if var is None:
                    continue

                step, context = self._snapshot_audit_step(
                    var=var,
                    target_var=target,
                    value_before=values_before[target],
                    value_after=value,
                    proportion_factor=(
                        proportion_factors[target]
                    ),
                )
                member_steps.append(step)
                audit_contexts.append(context)

            newly_maxed = self._get_newly_maxed_vars(
                active_vars
            )

            audit_record = self._record_audit_iteration(
                steps=member_steps,
                contexts=audit_contexts,
                limiting_txn_ids=[
                    var.id
                    for var in newly_maxed
                ],
                is_proportional=True,
            )
            reason = (
                audit_record.reason
                or "Equal-priority proportional allocation"
            )

            for target, var in vars_by_name.items():
                if target in var_values:
                    self.cur_trxn_reasons[
                        self._reason_key(
                            var.id,
                            self._objective_date,
                        )
                    ] = reason

            for var in newly_maxed:
                if var not in maxed_vars:
                    maxed_vars.append(var)

            old_count = len(vars_list)
            vars_list, factors = self._get_next_iter(
                series,
                maxed_vars,
            )
            if len(vars_list) >= old_count:
                raise RuntimeError(
                    "Circular loop detected! Problem "
                    "identifying any equal-priority "
                    "variables to drop!"
                )

            self._reset_minus_vars(original_ub)

        for var in deferred_vars:
            self._maximize_var(var)

    def _get_newly_maxed_vars(
        self,
        vars: list[Trxn | TrxnGroup],
    ) -> list[Trxn | TrxnGroup]:
        maxed_ids: set[str] = set()
        remaining: dict[
            str,
            Trxn | TrxnGroup,
        ] = {}
        current_values: dict[str, float] = {}

        for var in vars:
            target = self._target_var(var)

            if not target:
                maxed_ids.add(var.id)
                continue

            current_value = self.cur_trxn_value.get(
                target,
                0.0,
            )
            _, upper_bound = (
                self.engine.get_variable_bounds(target)
            )

            if (
                upper_bound != float("inf")
                and isclose(
                    current_value,
                    upper_bound,
                    abs_tol=SOLVER_TOL,
                )
            ):
                maxed_ids.add(var.id)
                continue

            remaining[target] = var
            current_values[target] = current_value

        while remaining:
            targets = list(remaining.keys())
            _, solved_values = self.engine.solve_objective(
                targets,
                maximization=True,
            )

            increasable = [
                target
                for target in targets
                if (
                    solved_values[target]
                    > current_values[target] + SOLVER_TOL
                )
            ]

            if not increasable:
                maxed_ids.update(
                    var.id
                    for var in remaining.values()
                )
                break

            for target in increasable:
                del remaining[target]

        return [
            var
            for var in vars
            if var.id in maxed_ids
        ]

    # ------------------------------------------------------------------
    # Spill handling / final ambiguity cleanup
    # ------------------------------------------------------------------

    def _minimize_minus_vars(
        self,
        vars: list[Trxn | TrxnGroup],
    ) -> dict[str, float]:
        """Minimize relevant storage dump/spill vars over the whole period."""

        original_ub: dict[str, float] = {}
        minus_vars = self.tm.get_minus_vars(vars)

        targets: list[str] = []

        for minus_var in minus_vars:
            ordered = self.tm.ordered_paths.get(
                minus_var.id,
                [],
            )
            if not ordered:
                continue

            anchor_flow_id = ordered[0].flow_id

            for real_date in self._flow_dates[
                anchor_flow_id
            ]:
                target = self._target_var(
                    minus_var,
                    real_date,
                )
                if (
                    target
                    and target not in original_ub
                ):
                    _, ub = self.engine.get_variable_bounds(
                        target
                    )
                    original_ub[target] = ub
                    targets.append(target)

        if targets:
            _, solved_values = self.engine.solve_objective(
                targets,
                maximization=False,
            )

            for target in targets:
                value = solved_values[target]
                if abs(value) < SOLVER_TOL:
                    value = 0.0
                self.engine.update_variable_bounds(
                    target,
                    ub=value,
                )

        return original_ub

    def lock_spill_variables(self) -> None:
        natural_zones = {
            ZoneTypes.STREAM,
            ZoneTypes.SYSTEM_GAIN_LOSS,
        }

        spill_vars: list[Trxn] = []

        for trxn in self.all_trxns:
            if (
                type(trxn) != Trxn
                or not trxn.is_slack
            ):
                continue

            ordered = self.tm.ordered_paths.get(
                trxn.id,
                [],
            )
            if len(ordered) != 1:
                continue

            path_item = ordered[0]
            flow = self.gm.get_flow_by_id(
                path_item.flow_id
            )
            from_zone = self.gm.get_zone_by_id(
                flow.from_zone
            )
            to_zone = self.gm.get_zone_by_id(
                flow.to_zone
            )

            if path_item.factor < 0:
                from_zone, to_zone = (
                    to_zone,
                    from_zone,
                )

            if (
                from_zone.type not in natural_zones
                and to_zone.type in natural_zones
            ):
                spill_vars.append(trxn)

        targets: list[str] = []

        for spill in spill_vars:
            anchor_flow_id = (
                self.tm.ordered_paths[spill.id][0].flow_id
            )

            for real_date in self._flow_dates[
                anchor_flow_id
            ]:
                target = self._target_var(
                    spill,
                    real_date,
                )
                if target:
                    targets.append(target)

        if not targets:
            return

        objective_value, _ = self.engine.solve_objective(
            targets,
            maximization=False,
        )

        constraint_name = "lock-slacks-period"
        self.engine.add_constraint(
            name=constraint_name,
            lb=0,
            ub=objective_value,
        )
        for target in targets:
            self.engine.set_coefficient(
                constraint_name,
                target,
                1.0,
            )

    def solve_for_nonpath_vars(self) -> None:
        targets: list[str] = []

        for trxn in self.all_trxns:
            if type(trxn) == Trxn:
                for path_item in trxn.path:
                    for real_date in self._flow_dates[
                        path_item.flow_id
                    ]:
                        target = self._path_var_name(
                            trxn,
                            path_item.flow_id,
                            real_date,
                        )
                        if target not in targets:
                            targets.append(target)

            elif type(trxn) == TrxnGroup:
                for real_date in self.real_dates:
                    targets.append(
                        self._group_var_name(
                            trxn,
                            real_date,
                        )
                    )

        if not targets:
            return

        _, solved_values = self.engine.solve_objective(
            targets,
            maximization=False,
        )

        for target, value in solved_values.items():
            if abs(value) < SOLVER_TOL:
                value = 0.0
            self.cur_trxn_value[target] = value

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    @staticmethod
    def _reason_key(
        txn_id: str,
        real_date: str | None,
    ) -> str:
        return f"{txn_id}{_DATE_TOKEN}{real_date}"

    def _snapshot_audit_step(
        self,
        var: Trxn | TrxnGroup,
        target_var: str,
        value_before: float,
        value_after: float,
        proportion_factor: float | None = None,
    ) -> tuple[SolverOutputSolveStepEvidence, dict]:
        evidence_variable_names = [target_var]

        if type(var) == TrxnGroup:
            def add_descendants(group: TrxnGroup) -> None:
                for child in group.children_trxns:
                    if type(child) == Trxn:
                        target = self._target_var(child)
                        if target:
                            evidence_variable_names.append(
                                target
                            )
                    elif type(child) == TrxnGroup:
                        add_descendants(child)

            add_descendants(var)

        constraints = []
        seen = set()

        for variable_name in evidence_variable_names:
            for constraint in (
                self.engine.get_last_solve_constraint_evidence(
                    variable_name,
                    SOLVER_TOL,
                )
            ):
                name = constraint["constraint_name"]
                if name in seen:
                    continue
                seen.add(name)
                constraints.append(dict(constraint))

        upper_limit = self.tm.get_transaction_upper_limit(
            var,
            self._objective_date,
        )

        context = {
            "txn_id": var.id,
            "var": var,
            "upper_limit_reached": (
                upper_limit is not None
                and isclose(
                    value_after,
                    upper_limit,
                    abs_tol=SOLVER_TOL,
                )
            ),
            "constraints": constraints,
        }

        return (
            SolverOutputSolveStepEvidence(
                variable_name=target_var,
                value_before=value_before,
                value_after=value_after,
                proportion_factor=proportion_factor,
            ),
            context,
        )

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def get_variables(self) -> list[SolverOutputApportionment]:
        """Return real-world-time transaction component values directly."""

        output: list[SolverOutputApportionment] = []

        for trxn in self.all_trxns:
            if type(trxn) != Trxn:
                continue

            ordered = self.tm.ordered_paths.get(
                trxn.id,
                [],
            )

            is_stream_slack = False
            if trxn.is_slack and len(ordered) == 1:
                flow = self.gm.get_flow_by_id(
                    ordered[0].flow_id
                )
                is_stream_slack = (
                    self.gm.get_zone_by_id(
                        flow.from_zone
                    ).type == ZoneTypes.STREAM
                    and self.gm.get_zone_by_id(
                        flow.to_zone
                    ).type == ZoneTypes.STREAM
                )

            for path_item in trxn.path:
                flow = self.gm.get_flow_by_id(
                    path_item.flow_id
                )
                lag = self.dm.flow_lags[flow.id]
                integer_lag = round(lag)
                has_integer_lag = isclose(
                    lag,
                    integer_lag,
                    abs_tol=1e-12,
                )

                for real_date in self._flow_dates[
                    path_item.flow_id
                ]:
                    variable_name = self._path_var_name(
                        trxn,
                        path_item.flow_id,
                        real_date,
                    )
                    value = self.cur_trxn_value.get(
                        variable_name,
                        0.0,
                    )

                    reason = self.cur_trxn_reasons.get(
                        self._reason_key(
                            trxn.id,
                            real_date,
                        ),
                        "Unsolved / Unconstrained",
                    )

                    # Preserve the current NF/CPI split where the mapping
                    # between real time and accounting time is exact.
                    if (
                        is_stream_slack
                        and has_integer_lag
                        and len(trxn.path) == 1
                    ):
                        accounting_date = (
                            date.fromisoformat(real_date)
                            + timedelta(days=int(integer_lag))
                        ).isoformat()

                        snapshot = self._snapshots.get(
                            accounting_date
                        )
                        natural_flow = (
                            snapshot.natural_by_flow.get(
                                flow.id
                            )
                            if snapshot is not None
                            else None
                        )

                        if natural_flow is not None:
                            output.append(
                                SolverOutputApportionment(
                                    date=real_date,
                                    interzone_flow_id=flow.id,
                                    txn_id=trxn.id + "_NF",
                                    value=(
                                        natural_flow
                                        * path_item.factor
                                    ),
                                    is_forward=True,
                                )
                            )
                            output.append(
                                SolverOutputApportionment(
                                    date=real_date,
                                    interzone_flow_id=flow.id,
                                    txn_id=trxn.id + "_CPI",
                                    value=(
                                        (value - natural_flow)
                                        * path_item.factor
                                    ),
                                    is_forward=(
                                        value > natural_flow
                                    ),
                                    reason=reason,
                                )
                            )
                            continue

                    output.append(
                        SolverOutputApportionment(
                            date=real_date,
                            interzone_flow_id=flow.id,
                            txn_id=trxn.id,
                            value=value * path_item.factor,
                            is_forward=path_item.factor > 0,
                            reason=reason,
                        )
                    )

        output.sort(
            key=lambda row: (
                row.date,
                row.interzone_flow_id,
                row.txn_id,
            )
        )
        return output
