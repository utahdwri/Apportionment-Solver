"""
TODO - Add lags, unlags
NOTE: Lag stuff is incomplete. What lags do we care about being consistent, and which not?
"""

from typing import Generator
from dataclasses import dataclass
from math import isclose
from collections.abc import Iterator

from .models import (
    AccountingGraph, AccountingLimit, CorePropSchedule, CorePropScheduleItem,
    CoreScheduleVariable, CoreSeqSchedule, CoreSeqScheduleItem,
    FlowComponentsTypes, InterzoneFlow,
    SolverInput, SolverOutput, SolverOutputApportionment,
    Trxn, TrxnGroup, TrxnPathItem, Zone, ZoneTypes
)
from .solve_lp_with_GLOP import LPSolver, LPSolverError

# --- Configuration Constants ---
COALESCE_MISSING_FLOWS_TO_ZERO = True
COALESCE_NEGATIVE_FLOWS_TO_ZERO = True
DEFAULT_TRXN_LIMIT = 1e3
DEFAULT_TRXN_PRIORITY = 999999999
SLACK_TRXN_PRIORITY = 1e100
SOLVER_TOL = 1e-6

SHOW_LOG = False

PREFIX_MEASURE = 'MEAS_'
PREFIX_NF_ZONE = 'NF_ZONE_'
PREFIX_PARENT = 'PARENT_'
PREFIX_CONT = 'CONT_'

# --- Public API ---
def solve(input: SolverInput, check_expected_values: bool = False) -> SolverOutput:
    """Orchestrates the setup, data loading, equation building, and solving loops."""
    apportionment_results = []

    # 1. Initialize Network Topology
    graph_manager = GraphManager(input.accounting_graph)
    graph_manager.set_implied_calculated_flow_boundaries()

    # Add slack transactions to the input
    _add_slack_trxns(input, graph_manager)

    # 2. Initialize Data and Lags
    data_manager = DailyDataManager(graph_manager, input.measurements, input.measurement_beg_date, input.measurement_end_date, input.external_natural_flows)

    # 3.
    trxn_manager = TrxnManager(graph_manager, input.txns)

    # 4. Run for each day.
    for date in loop_through_date_range(input.beg_date, input.end_date):
        print(date, '...')

        # A. Setup the state for the day
        data_manager.set_day(date)
        data_manager.calc_natural_flows()
        #
        apportioner = Apportioner(graph_manager, trxn_manager, data_manager)

        # B. Update Daily Bounds
        apportioner.update_daily_bounds()

        # C. Rebuild Schedule & Solve
        schedule = trxn_manager.build_schedule(date)
        log(f"\nSchedule: {schedule}")

        # Solve sequentially with and without NF mass balance limits
        apportioner.apply_nf_mass_balance_constraints()
        apportioner.calculate_apportionments(schedule)

        apportioner.remove_nf_mass_balance_constraints()
        apportioner.lock_spill_variables()
        apportioner.calculate_apportionments(schedule)

        # D. Finalize unconstrained (nonpath) vars
        apportioner.solve_for_nonpath_vars()

        # Collect results for this day
        apportionment_results.extend(apportioner.get_variables(date))

    results = SolverOutput(apportionments=apportionment_results)

    if check_expected_values:
        assert_apportionments_equal_expected(results, input, graph_manager, data_manager, trxn_manager)

    return results

# --- Helper Methods & Classes ---

@dataclass
class CurFlowInfo:
    """Stores the total flow, natural flow, and total apportioned flow for an interzone-flow."""
    measured: float = 0
    natural: float = 0
    available_natural: float = 0


def log(message: str):
    if SHOW_LOG:
        print('LOG', message)


def traverse_vars(vars: list[Trxn | TrxnGroup]) -> Iterator[Trxn | TrxnGroup]:
    """Recursively yields all transactions, including nested children."""
    for v in vars:
        yield v
        if type(v) == TrxnGroup:
            yield from traverse_vars(v.children_trxns)


def loop_through_date_range(beg_date: str, end_date: str) -> Generator[str, None, None]:
    """Iterate through each date from beg_date to end_date inclusive."""
    from datetime import datetime, timedelta
    a_date = datetime.strptime(beg_date, "%Y-%m-%d").date()
    b_date = datetime.strptime(end_date, "%Y-%m-%d").date()
    current_date = a_date
    while current_date <= b_date:
        yyyy_mm_dd = current_date.isoformat()
        yield yyyy_mm_dd
        current_date += timedelta(days=1)


def _add_slack_trxns(input: SolverInput, gm: 'GraphManager'):
    """Adds slack transactions to ensure the problem is feasible."""
    for f in gm.graph.interzone_flows:
        from_z = gm.get_zone_by_id(f.from_zone)
        flow_var_name = f'SLACK_{f.from_zone}_TO_{f.to_zone}_{f.id}'

        slackvar = Trxn(
            id=flow_var_name,
            path=[TrxnPathItem(flow_id=f.id, factor=1)],
            upper_limit=None,
            is_slack=True
        )
        input.txns.append(slackvar)

        if f.bidirectional:
            flow_var_name2 = f'SLACK_{f.to_zone}_TO_{f.from_zone}_{f.id}'
            slackvar2 = Trxn(
                id=flow_var_name2,
                path=[TrxnPathItem(flow_id=f.id, factor=-1)],
                upper_limit=None,
                is_slack=True
            )
            input.txns.append(slackvar2)


class GraphManager:
    """Manages the static structure and traversal of the accounting graph."""
    def __init__(self, graph: AccountingGraph):
        self.graph = graph
        self.lookup_zones_by_id = {z.id: z for z in graph.zones}
        self.lookup_flows_by_id = {f.id: f for f in graph.interzone_flows}
        self.lookup_zone_outflows = {z.id: [] for z in graph.zones}
        self.lookup_zone_inflows = {z.id: [] for z in graph.zones}

        for f in graph.interzone_flows:
            self.lookup_zone_outflows[f.from_zone].append(f)
            self.lookup_zone_inflows[f.to_zone].append(f)

    def get_zone_by_id(self, zone_id: str) -> Zone:
        if zone_id not in self.lookup_zones_by_id:
            raise ValueError(f'Cannot find zone id {zone_id}')
        return self.lookup_zones_by_id[zone_id]

    def get_flow_by_id(self, flow_id: str) -> InterzoneFlow:
        if flow_id not in self.lookup_flows_by_id:
            raise ValueError(f'Cannot find interzone-flow id {flow_id}')
        return self.lookup_flows_by_id[flow_id]

    def get_zone_outflows(self, zone_id: str) -> list[InterzoneFlow]:
        return self.lookup_zone_outflows.get(zone_id, [])

    def get_zone_inflows(self, zone_id: str) -> list[InterzoneFlow]:
        return self.lookup_zone_inflows.get(zone_id, [])

    def traverse_downstream(self, zone_id: str) -> Generator[InterzoneFlow, None, None]:
        """Loops through all downstream interzone-flows, only following streams."""
        stream_outflow = None
        next_zone_id = None
        for f in self.get_zone_outflows(zone_id):
            to_zone = self.get_zone_by_id(f.to_zone)
            if to_zone.type == ZoneTypes.STREAM:
                if stream_outflow is not None:
                    raise ValueError('Cannot traverse downstream: stream network diverges.')
                stream_outflow = f
                next_zone_id = to_zone.id
        if stream_outflow is not None and next_zone_id is not None:
            yield stream_outflow
            yield from self.traverse_downstream(next_zone_id)

    def get_loss_route(self, zone_id: str) -> str:
        """Finds the dynamically designated physical loss route for a given zone."""
        candidates = []
        for f in self.get_zone_outflows(zone_id):
            if getattr(f, 'residual_for_losses', False) and self.get_zone_by_id(f.to_zone).type == ZoneTypes.SYSTEM_GAIN_LOSS:
                candidates.append(f)
        if len(candidates) == 1:
            return candidates[0].id
        elif len(candidates) > 1:
            raise ValueError(f"Multiple loss routes found for zone {zone_id} with residual_for_losses=True")
        else:
            raise ValueError(f"No loss route found for zone {zone_id} with residual_for_losses=True")

    def set_implied_calculated_flow_boundaries(self):
        """Creates explicitly calculated flow boundaries."""
        for z in self.graph.zones:
            if z.type == ZoneTypes.STORAGE:
                for f in self.get_zone_inflows(z.id):
                    if len(f.flow_measurements) == 0:
                        f.flow_type = FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE
                        f.residual_for_gains = True
                        f.residual_for_losses = True
            elif z.type == ZoneTypes.STREAM:
                for f in self.get_zone_inflows(z.id):
                    from_z = self.get_zone_by_id(f.from_zone)
                    is_gain = from_z.type == ZoneTypes.SYSTEM_GAIN_LOSS
                    if is_gain and len(f.flow_measurements)==0:
                        f.flow_type = FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE
                        f.residual_for_gains = True
                        f.residual_for_losses = (f.bidirectional)
                for f in self.get_zone_outflows(z.id):
                    to_z = self.get_zone_by_id(f.to_zone)
                    is_loss = to_z.type == ZoneTypes.SYSTEM_GAIN_LOSS
                    if is_loss and len(f.flow_measurements)==0:
                        f.flow_type = FlowComponentsTypes.FLOW_BALANCE_OF_SOURCE_ZONE
                        f.residual_for_gains = (f.bidirectional)
                        f.residual_for_losses = True


class DailyDataManager:
    """Manages flow/storage states, data lagging, and natural flow calculations for a specific day."""
    def __init__(self, gm: GraphManager, measurements, measurement_beg_date:str, measurement_end_date:str, external_natural_flows: dict|None = None):
        self.gm = gm
        self.measurements = measurements
        self.measurement_beg_date = measurement_beg_date
        self.measurement_end_date = measurement_end_date
        self.cur_date: str | None = None
        self.cur_flows_by_id: dict[str, CurFlowInfo] = {}
        self.cur_storage_chg_by_id: dict[str, CurFlowInfo] = {}
        self._flow_lags = self._get_lag_by_flowline_id()
        self.external_natural_flows = external_natural_flows or {}
        self.zone_residuals: dict[str, float] = {}

    def set_day(self, date: str):
        self.cur_date = date
        self.cur_flows_by_id = {f.id: CurFlowInfo() for f in self.gm.graph.interzone_flows}

        # Set Flows for this day:
        observed_values = self._get_interzone_flow_values(date)
        for f, value in observed_values.items():
            self.cur_flows_by_id[f].measured = value

        # Inject external natural flow value for today if it exists
        for f_id, vals in self.external_natural_flows.items():
            if f_id in self.cur_flows_by_id and date in vals:
                self.cur_flows_by_id[f_id].natural = vals[date]

        # Set storage change of zones:
        for z in self.gm.graph.zones:
            self.cur_storage_chg_by_id[z.id] = CurFlowInfo(measured=self._get_storage_change(z, date))

    def calc_natural_flows(self):
        """Calculates natural flow for paths and propagates it downstream."""
        def recursive_apply_impact_downstream(flow_id: str, impact: float, is_available=True):
            f_in = self.gm.get_flow_by_id(flow_id)
            to_zone = f_in.to_zone
            outflows = self.gm.get_zone_outflows(to_zone)

            for f_out in outflows:
                if (self.gm.get_zone_by_id(f_out.from_zone).type == ZoneTypes.STREAM and
                    self.gm.get_zone_by_id(f_out.to_zone).type == ZoneTypes.STREAM):

                    l_from = getattr(f_out, 'loss_from_zone', 0)
                    l_to = getattr(f_out, 'loss_to_zone', 0)

                    line_impact = impact * (1 - l_from)
                    downstream_impact = line_impact * (1 - l_to)

                    outflow_data = self.cur_flows_by_id[f_out.id]
                    outflow_data.natural += line_impact
                    if outflow_data.natural < 0:
                        outflow_data.natural = 0

                    if is_available:
                        outflow_data.available_natural += line_impact
                        if outflow_data.available_natural < 0:
                            outflow_data.available_natural = 0

                    recursive_apply_impact_downstream(f_out.id, downstream_impact) # TODO - review if we should pass in is_available value??

        for f_id in self.external_natural_flows:
            if f_id in self.cur_flows_by_id:
                flow_data = self.cur_flows_by_id[f_id]
                if flow_data.natural != 0:
                    recursive_apply_impact_downstream(f_id, flow_data.natural, False)

        for f in self.gm.graph.interzone_flows:
            is_gain_to_stream =  (
                self.gm.get_zone_by_id(f.from_zone).type == ZoneTypes.SYSTEM_GAIN_LOSS and
                self.gm.get_zone_by_id(f.to_zone).type == ZoneTypes.STREAM)

            if is_gain_to_stream:
                flow_data = self.cur_flows_by_id[f.id]
                flow_data.natural = flow_data.measured
                flow_data.available_natural = flow_data.measured
                recursive_apply_impact_downstream(f.id, flow_data.measured)

    def _get_lag_by_flowline_id(self) -> dict[str, float]:
        flow_lags = {}
        def set_flow_lag(f: InterzoneFlow, lag: float):
            if f.id in flow_lags:
                if flow_lags[f.id] != lag:
                    raise ValueError(f'Computed absolute lag times of {lag} for interzone-flow id: "{f.id}" is inconsistent!')
            else:
                flow_lags[f.id] = lag
                iter_over_zone(f.to_zone, lag - getattr(f, 'lag_to_zone', 0))
                iter_over_zone(f.from_zone, lag - getattr(f, 'lag_from_zone', 0))

        def iter_over_zone(zone_id: str, lag: float):
            for flow in self.gm.get_zone_inflows(zone_id):
                set_flow_lag(flow, lag + getattr(flow, 'lag_to_zone', 0))
            for flow in self.gm.get_zone_outflows(zone_id):
                set_flow_lag(flow, lag + getattr(flow, 'lag_from_zone', 0))

        for f in self.gm.graph.interzone_flows:
            if f.id not in flow_lags:
                set_flow_lag(f, 0)

        min_lag = min(flow_lags.values()) if flow_lags else 0
        flow_lags = {f_id: lag - min_lag for f_id, lag in flow_lags.items()}
        return flow_lags

    def _get_interzone_flow_values(self, date: str) -> dict[str, float]:
        total_flow_by_id: dict[str, float] = {}

        for f in self.gm.graph.interzone_flows:
            lag = self._flow_lags[f.id]
            total_measured = 0
            if getattr(f, 'flow_type', None) == FlowComponentsTypes.OBSERVATION:
                for fm in getattr(f, 'flow_measurements', []):
                    val = self._get_flow_from_meas(fm.measurement_id, date, int(lag))
                    if val is None:
                        if COALESCE_MISSING_FLOWS_TO_ZERO:
                            val = 0
                        else:
                            raise ValueError(f"Measurement {fm.measurement_id} undefined on {date}")
                    total_measured += val * getattr(fm, 'adjustment_factor', 1)
            total_flow_by_id[f.id] = total_measured

        # Explicitly route physical losses into designated loss pathways BEFORE the residual calculation
        for f in self.gm.graph.interzone_flows:
            l_from = getattr(f, 'loss_from_zone', 0)
            l_to = getattr(f, 'loss_to_zone', 0)

            if l_from > 0:
                loss_f_id = self.gm.get_loss_route(f.from_zone)
                loss_amount = (total_flow_by_id[f.id] / (1 - l_from)) * l_from
                total_flow_by_id[loss_f_id] += loss_amount

            if l_to > 0:
                loss_f_id = self.gm.get_loss_route(f.to_zone)
                loss_amount = total_flow_by_id[f.id] * l_to
                total_flow_by_id[loss_f_id] += loss_amount

        for zone_id, calcs in self._determine_residual_calc_order():
            residual_flow = sum(total_flow_by_id[f.id] for f in self.gm.get_zone_outflows(zone_id))
            storage_chg = self._get_storage_change(self.gm.get_zone_by_id(zone_id), date)
            if storage_chg is not None:
                residual_flow += storage_chg
            residual_flow -= sum(total_flow_by_id[f.id] for f in self.gm.get_zone_inflows(zone_id))

            self.zone_residuals[zone_id] = residual_flow

            for f in calcs:
                is_outflow = f in self.gm.get_zone_outflows(zone_id)
                if residual_flow >= 0 and getattr(f, 'residual_for_gains', False):
                    if not is_outflow:
                        total_flow_by_id[f.id] += residual_flow
                    else:
                        total_flow_by_id[f.id] -= residual_flow
                if residual_flow < 0 and getattr(f, 'residual_for_losses', False):
                    if is_outflow:
                        total_flow_by_id[f.id] -= residual_flow
                    else:
                        total_flow_by_id[f.id] += residual_flow

        for f in self.gm.graph.interzone_flows:
            if total_flow_by_id[f.id] < 0 and not getattr(f, 'bidirectional', False):
                if COALESCE_NEGATIVE_FLOWS_TO_ZERO:
                    total_flow_by_id[f.id] = 0
                else:
                    raise ValueError(f"Net flow negative for {f.id} on {date}")
        return total_flow_by_id

    def _determine_residual_calc_order(self) -> list[tuple[str, list[InterzoneFlow]]]:
        zone_calc_order: list[str] = []
        flow_balance_calcs: dict[str, list[str]] = {}
        residual_flows_by_zone: dict[str, list[InterzoneFlow]] = {}

        def add_flow_balance_calc(zone_id: str, required_by_zone_id: str, f: InterzoneFlow):
            if zone_id not in flow_balance_calcs:
                flow_balance_calcs[zone_id] = []
                residual_flows_by_zone[zone_id] = []
            flow_balance_calcs[zone_id].append(required_by_zone_id)
            residual_flows_by_zone[zone_id].append(f)

        for f in self.gm.graph.interzone_flows:
            if getattr(f, 'flow_type', None) == FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE:
                add_flow_balance_calc(f.to_zone, f.from_zone, f)
            elif getattr(f, 'flow_type', None) == FlowComponentsTypes.FLOW_BALANCE_OF_SOURCE_ZONE:
                add_flow_balance_calc(f.from_zone, f.to_zone, f)

        while len(flow_balance_calcs) > 0:
            dependencies = {dep for deps in flow_balance_calcs.values() for dep in deps}
            numdel = 0
            for key in list(flow_balance_calcs.keys()):
                if key not in dependencies:
                    zone_calc_order.append(key)
                    del flow_balance_calcs[key]
                    numdel += 1
            if numdel == 0:
                raise ValueError("Circular dependency in residual calculations!")
        return [(z, residual_flows_by_zone[z]) for z in zone_calc_order]

    def _get_storage_change(self, z: Zone, date: str):
        storage_chg = None
        if z.type in (ZoneTypes.STREAM, ZoneTypes.STORAGE):
            for mid in getattr(z, 'storage_meas_ids', []):
                storage_chg = self._get_storage_change_from_meas(mid, date)
                if storage_chg is not None:
                    break
            if storage_chg is None:
                if z.type == ZoneTypes.STREAM or COALESCE_MISSING_FLOWS_TO_ZERO:
                    storage_chg = 0
                else:
                    raise ValueError(f"Storage change undefined on {date}")
        else:
            storage_chg = 0
        return storage_chg

    def _get_flow_from_meas(self, meas_id: str, date: str, lag: int = 0):
        from datetime import datetime
        measurements = self.measurements[str(meas_id)]
        beg_date = datetime.strptime(self.measurement_beg_date, "%Y-%m-%d").date()
        this_date = datetime.strptime(date, "%Y-%m-%d").date()
        day_idx = (this_date - beg_date).days
        idx = day_idx - lag
        if 0 <= idx < len(measurements):
            return measurements[idx]
        return None

    def _get_storage_change_from_meas(self, meas_id: str | None, date: str, lag: int = 0):
        from datetime import datetime
        measurements = self.measurements[str(meas_id)]
        beg_date = datetime.strptime(self.measurement_beg_date, "%Y-%m-%d").date()
        this_date = datetime.strptime(date, "%Y-%m-%d").date()
        day_idx = (this_date - beg_date).days
        if meas_id is not None:
            idx_yesterday = day_idx - 1 - lag
            idx_today = day_idx - lag
            if idx_yesterday >= 0 and idx_today < len(measurements):
                storage_a = measurements[idx_yesterday]
                storage_b = measurements[idx_today]
                if storage_a is not None and storage_b is not None:
                    return storage_b - storage_a
        return None


class TrxnManager:
    """Makes it easier to manipulate transaction data."""
    def __init__(self, gm: GraphManager, txns:list[Trxn | TrxnGroup]):
        self.gm = gm
        self._ensure_children_after_parent(txns)
        self.all_trxns = list(traverse_vars(txns))

        # Dynamically inject mathematical losses into transaction paths BEFORE evaluating the route
        for t in self.all_trxns:
            if type(t) == Trxn:
                self._expand_path_with_losses(t)

        self.ordered_paths: dict[str, list[TrxnPathItem]] = {}
        for t in self.all_trxns:
            if type(t) == Trxn:
                if len(t.path) > 0:
                    self.ordered_paths[t.id] = self._get_ordered_path(t)
                else:
                    self.ordered_paths[t.id] = []

        self.lookup_flow_trxns = self._build_flow_trxns_lookup()

    def _ensure_children_after_parent(self, txns: list['Trxn | TrxnGroup'], parent_priority: float | None = None):
        for t in txns:
            if parent_priority is not None and t.priority is not None and t.priority <= parent_priority:
                new_priority = parent_priority + 1e-5
                t.priority = new_priority
            if type(t) == TrxnGroup:
                self._ensure_children_after_parent(t.children_trxns, t.priority)

    def _expand_path_with_losses(self, trxn: Trxn):
        if not trxn.path:
            return

        expanded_path = []
        for x in trxn.path:
            expanded_path.append(x)
            flow = self.gm.get_flow_by_id(x.flow_id)
            l_from = getattr(flow, 'loss_from_zone', 0)
            l_to = getattr(flow, 'loss_to_zone', 0)

            if l_from > 0:
                loss_id = self.gm.get_loss_route(flow.from_zone)
                expanded_path.append(TrxnPathItem(flow_id=loss_id, factor=x.factor))

            if l_to > 0:
                loss_id = self.gm.get_loss_route(flow.to_zone)
                expanded_path.append(TrxnPathItem(flow_id=loss_id, factor=x.factor))

        trxn.path = expanded_path

    def _get_ordered_path(self, trxn: Trxn) -> list[TrxnPathItem]:
        if not trxn.path:
            return []

        # Ignore loss branches for building the primary mathematical sequence
        main_path = []
        for x in trxn.path:
            flow = self.gm.get_flow_by_id(x.flow_id)
            to_z = self.gm.get_zone_by_id(flow.to_zone)
            if to_z.type != ZoneTypes.SYSTEM_GAIN_LOSS:
                main_path.append(x)

        if len(main_path) <= 1:
            return main_path

        lookup_next = {}
        for x in main_path:
            flow = self.gm.get_flow_by_id(x.flow_id)
            if x.factor >= 0:
                lookup_next[flow.from_zone] = (flow.to_zone, x)
            else:
                lookup_next[flow.to_zone] = (flow.from_zone, x)

        starts = set(lookup_next.keys())
        ends = set(v[0] for v in lookup_next.values())
        root_candidates = list(starts - ends)

        if len(root_candidates) != 1:
            return main_path

        sorted_list = []
        current_key = root_candidates[0]
        while len(sorted_list) < len(main_path):
            if current_key not in lookup_next: break
            next_zone, path_item = lookup_next[current_key]
            sorted_list.append(path_item)
            current_key = next_zone

        if len(sorted_list) != len(main_path):
            return main_path
        return sorted_list

    def get_anchor_var(self, trxn: Trxn) -> str | None:
        path = self.ordered_paths.get(trxn.id, [])
        if path:
            return f"{trxn.id}___{path[0].flow_id}"
        return None

    def _build_flow_trxns_lookup(self) -> dict[str, list[tuple[Trxn, TrxnPathItem]]]:
        lookup = {f.id: [] for f in self.gm.graph.interzone_flows}
        for t in self.all_trxns:
            if type(t) == Trxn:
                for x in t.path:
                    lookup[x.flow_id].append((t, x))
        return lookup

    def get_transaction_upper_limit(self, t: Trxn | TrxnGroup, date:str|None) -> float | None:
        upper_limit = None
        if date is None:
            raise ValueError('date not valid')
        if type(t.upper_limit) == AccountingLimit:
            upper_limit = 0
            for intv in t.upper_limit.intervals:
                if date >= intv.beg_date and date < intv.end_date:
                    upper_limit = intv.value
                    break
        elif isinstance(t.upper_limit, (int, float)):
            upper_limit = float(t.upper_limit)
        elif t.upper_limit is None:
            upper_limit = None
        else:
            raise ValueError('upper_limit must be an AccountingLimit, int, float, or None!')

        if upper_limit is None:
            upper_limit = DEFAULT_TRXN_LIMIT
        return upper_limit

    def get_minus_vars(self, vars: list[Trxn | TrxnGroup]) -> list[Trxn]:
        def get_from(v: Trxn) -> tuple[Zone, InterzoneFlow]:
            # Use original v.path[0], but skip any dynamically added loss routes
            first_item = v.path[0]
            for item in v.path:
                f = self.gm.get_flow_by_id(item.flow_id)
                if self.gm.get_zone_by_id(f.to_zone).type != ZoneTypes.SYSTEM_GAIN_LOSS:
                    first_item = item
                    break

            f0 = self.gm.get_flow_by_id(first_item.flow_id)
            if first_item.factor >= 0:
                from_zone = self.gm.get_zone_by_id(f0.from_zone)
            else:
                from_zone = self.gm.get_zone_by_id(f0.to_zone)
            return from_zone, f0

        def get_to(v: Trxn) -> tuple[Zone, InterzoneFlow]:
            # Use original v.path[-1], but skip backwards over dynamically added loss routes
            last_item = v.path[-1]
            for item in reversed(v.path):
                f = self.gm.get_flow_by_id(item.flow_id)
                if self.gm.get_zone_by_id(f.to_zone).type != ZoneTypes.SYSTEM_GAIN_LOSS:
                    last_item = item
                    break

            fl = self.gm.get_flow_by_id(last_item.flow_id)
            if last_item.factor < 0:
                to_zone = self.gm.get_zone_by_id(fl.from_zone)
            else:
                to_zone = self.gm.get_zone_by_id(fl.to_zone)
            return to_zone, fl

        output: list[Trxn] = []
        for v in vars:
            if type(v) == Trxn:
                if len(v.path) > 0:
                    from_zone, from_flow = get_from(v)
                    if from_zone.type == ZoneTypes.STORAGE:
                        for trxn, path_item in self.lookup_flow_trxns[from_flow.id]:
                            ordered_trxn = self.ordered_paths.get(trxn.id, [])
                            # Use len(ordered_trxn) to ensure loss expansions don't hide the slack status
                            if getattr(trxn, 'is_slack', False) and len(ordered_trxn) == 1:
                                if get_to(trxn)[0] == from_zone:
                                    output.append(trxn)

                    to_zone, to_flow = get_to(v)
                    if to_zone.type == ZoneTypes.STORAGE:
                        for trxn, path_item in self.lookup_flow_trxns[to_flow.id]:
                            if get_from(trxn)[0] == to_zone:
                                output.append(trxn)
        return output

    def build_schedule(self, date:str) -> CoreSeqSchedule:
        vars = self.all_trxns
        output_list: list[CoreSeqScheduleItem] = []
        varsByPriority: dict[float,list['Trxn | TrxnGroup']]  = {}

        for v in vars:
            p = getattr(v, 'priority', -1)
            if p not in varsByPriority:
                varsByPriority[p] = []
            varsByPriority[p].append(v)

        priorities = sorted(varsByPriority.keys())

        for p in priorities:
            pvars = varsByPriority[p]
            if len(pvars) == 1:
                item = CoreScheduleVariable(var=pvars[0])
                output_list.append(CoreSeqScheduleItem(priority=p, item=item))
            elif len(pvars) > 1:
                nested_sched = None
                unlimited_vars:list[Trxn] = []
                for v in pvars:
                    ub = self.get_transaction_upper_limit(v, date)
                    if ub is None:
                        if type(v) != Trxn:
                            raise NotImplementedError('Trxn Groups with no upper limit are not supported!')
                        unlimited_vars.append(v)

                if len(unlimited_vars) > 0:
                    nested_sched = CoreSeqSchedule(series=[])
                    nested_sched.series.append(CoreSeqScheduleItem(
                        priority=1,
                        item=CorePropSchedule(
                            series=[CorePropScheduleItem(
                                factor=1,
                                item=CoreScheduleVariable(var=v)
                            ) for v in unlimited_vars]
                        )
                    ))

                proportional_subseries: list[CorePropScheduleItem] = []
                cfs_sum = 0
                for v2 in pvars:
                    v2_ub = self.get_transaction_upper_limit(v2, date)
                    if v2_ub is not None:
                        cfs_sum += v2_ub
                        citem = CorePropScheduleItem(
                            factor=v2_ub,
                            item=CoreScheduleVariable(var=v2)
                        )
                        proportional_subseries.append(citem)

                for citem in proportional_subseries:
                    if cfs_sum > 0:
                        citem.factor /= cfs_sum

                if nested_sched is None:
                    output_list.append(CoreSeqScheduleItem(
                        priority=p,
                        item=CorePropSchedule(series=proportional_subseries)
                    ))
                else:
                    nested_sched.series.append(CoreSeqScheduleItem(
                        priority=1,
                        item=CorePropSchedule(series=proportional_subseries)
                    ))
                    output_list.append(CoreSeqScheduleItem(
                        priority=p,
                        item=nested_sched
                    ))
        return CoreSeqSchedule(series=output_list)

    def get_path_item(self, trxn_id, interzone_flow_id):
        base_id = trxn_id
        if trxn_id.endswith('_NF'):
            base_id = trxn_id[:-3]
        elif trxn_id.endswith('_CPI'):
            base_id = trxn_id[:-4]

        for trxn, path_item in self.lookup_flow_trxns[interzone_flow_id]:
            if trxn.id == base_id:
                return path_item
        raise ValueError(f'path_item not found for trxn_id={trxn_id}, interzone_flow_id={interzone_flow_id}')


class Apportioner:
    """Orchestrates solving the equations."""
    def __init__(self, gm: GraphManager, tm: TrxnManager, dm: DailyDataManager):
        self.gm = gm
        self.tm = tm
        self.dm = dm
        self.cur_trxn_value: dict[str, float] = {}
        self.cur_trxn_reasons: dict[str, str] = {}
        self.all_trxns = tm.all_trxns
        self.engine = self._build_linear_equations()
        self.feasibility_slacks:list[str] = []

    def _build_linear_equations(self) -> LPSolver:
        """Add the variables and constraints to the linear solver engine using a variable-per-path-item approach."""
        engine = LPSolver(tolerance=SOLVER_TOL)

        # Add Variables
        for trxn in self.all_trxns:
            if type(trxn) == Trxn:
                lb = None if getattr(trxn, 'lower_limit', 0) < 0 else 0
                for x in trxn.path:
                    var_name = f"{trxn.id}___{x.flow_id}"
                    engine.add_variable(name=var_name, lb=lb, ub=None)
            elif type(trxn) == TrxnGroup:
                lb = None if getattr(trxn, 'lower_limit', 0) < 0 else 0
                engine.add_variable(name=trxn.id, lb=lb, ub=None)

        # Add Interzone Flow Measurements
        for f in self.gm.graph.interzone_flows:
            con_name = PREFIX_MEASURE + f.id
            engine.add_constriant(name=con_name, lb=None, ub=None)

        # Add Natural Flow Constraints (Streams)
        for z in self.gm.graph.zones:
            if z.type == ZoneTypes.STREAM:
                con_name = PREFIX_NF_ZONE + z.id
                engine.add_constriant(name=con_name, lb=None, ub=None)

        # Tie variables to constraints
        for trxn in self.all_trxns:
            if type(trxn) == Trxn:
                # 1. Measurement Constraints
                for x in trxn.path:
                    var_name = f"{trxn.id}___{x.flow_id}"
                    con_name = PREFIX_MEASURE + x.flow_id
                    engine.set_coeficient(con_name, var_name, x.factor)

                ordered_path = self.tm.ordered_paths.get(trxn.id, [])

                # 2. Main Path Continuity Constraints (Mass Balance pushing downstream)
                for i in range(len(ordered_path) - 1):
                    leg1 = ordered_path[i]
                    leg2 = ordered_path[i+1]
                    v1 = f"{trxn.id}___{leg1.flow_id}"
                    v2 = f"{trxn.id}___{leg2.flow_id}"
                    con_name = f"{PREFIX_CONT}{trxn.id}_{i}"

                    engine.add_constriant(name=con_name, lb=0, ub=0)

                    f1 = self.gm.get_flow_by_id(leg1.flow_id)
                    f2 = self.gm.get_flow_by_id(leg2.flow_id)

                    # Physical flowline losses
                    l_to = getattr(f1, 'loss_to_zone', 0)
                    l_from = getattr(f2, 'loss_from_zone', 0)

                    # Mathematical transaction-specific path losses
                    p_after = getattr(leg1, 'loss_after', 0)
                    p_before = getattr(leg2, 'loss_before', 0)

                    # Remaining flow after exiting leg1 and before entering leg2
                    rem_factor = (1 - l_to) * (1 - p_after) * (1 - l_from) * (1 - p_before)

                    engine.set_coeficient(con_name, v2, 1.0)
                    engine.set_coeficient(con_name, v1, -rem_factor)

                # 3. Branching Equations for Flowline Losses
                for leg in ordered_path:
                    flow = self.gm.get_flow_by_id(leg.flow_id)
                    l_from = getattr(flow, 'loss_from_zone', 0)
                    l_to = getattr(flow, 'loss_to_zone', 0)
                    V_main = f"{trxn.id}___{leg.flow_id}"

                    if l_from > 0:
                        loss_id = self.gm.get_loss_route(flow.from_zone)
                        V_loss_from = f"{trxn.id}___{loss_id}"
                        con_name = f"{PREFIX_CONT}{trxn.id}_{leg.flow_id}_loss_from"
                        engine.add_constriant(name=con_name, lb=0, ub=0)
                        engine.set_coeficient(con_name, V_loss_from, 1.0)
                        engine.set_coeficient(con_name, V_main, -(l_from / (1 - l_from)))

                    if l_to > 0:
                        loss_id = self.gm.get_loss_route(flow.to_zone)
                        V_loss_to = f"{trxn.id}___{loss_id}"
                        con_name = f"{PREFIX_CONT}{trxn.id}_{leg.flow_id}_loss_to"
                        engine.add_constriant(name=con_name, lb=0, ub=0)
                        engine.set_coeficient(con_name, V_loss_to, 1.0)
                        engine.set_coeficient(con_name, V_main, -l_to)

                # 4. Natural Flow Constraints (only tied to anchor variables)
                if not getattr(trxn, 'is_slack', False) and len(ordered_path) > 0:
                    first_item = ordered_path[0]
                    anchor_var = f"{trxn.id}___{first_item.flow_id}"

                    first_flow = self.gm.get_flow_by_id(first_item.flow_id)
                    from_zone = self.gm.get_zone_by_id(first_flow.from_zone)
                    if first_item.factor < 0:
                        from_zone = self.gm.get_zone_by_id(first_flow.to_zone)

                    if from_zone.type == ZoneTypes.STREAM:
                        engine.set_coeficient(PREFIX_NF_ZONE + from_zone.id, anchor_var, 1)
                        for f in self.gm.traverse_downstream(from_zone.id):
                            engine.set_coeficient(PREFIX_NF_ZONE + f.to_zone, anchor_var, 1)

        # Group Limits
        for trxn in self.all_trxns:
            if type(trxn) == TrxnGroup:
                con_name = PREFIX_PARENT + trxn.id
                engine.add_constriant(name=con_name, lb=0, ub=0)
                engine.set_coeficient(con_name, trxn.id, 1)

                for v2 in trxn.children_trxns:
                    if type(v2) == Trxn:
                        anchor_var = self.tm.get_anchor_var(v2)
                        if anchor_var:
                            engine.set_coeficient(con_name, anchor_var, -1)
                    elif type(v2) == TrxnGroup:
                        engine.set_coeficient(con_name, v2.id, -1)

        return engine

    def update_daily_bounds(self):
        """Updates limits on variables and constraints."""
        # Update Variable Limits
        for trxn in self.all_trxns:
            upper_limit = self.tm.get_transaction_upper_limit(trxn, self.dm.cur_date)

            if type(trxn) == Trxn:
                if getattr(trxn, 'is_slack', False):
                    upper_limit = None

                anchor_var = self.tm.get_anchor_var(trxn)
                if anchor_var:
                    # Apply upper_limit strictly to the anchor variable
                    self.engine.update_variable_bounds(anchor_var, lb=getattr(trxn, 'lower_limit', 0), ub=upper_limit)

                # Loop through all paths (including mathematically derived loss branches)
                for path_item in trxn.path:
                    var_name = f"{trxn.id}___{path_item.flow_id}"
                    if var_name != anchor_var:
                        dlb = None if getattr(trxn, 'lower_limit', 0) < 0 else 0
                        self.engine.update_variable_bounds(var_name, lb=dlb, ub=None)

            elif type(trxn) == TrxnGroup:
                self.engine.update_variable_bounds(trxn.id, lb=getattr(trxn, 'lower_limit', 0), ub=upper_limit)

        # Update Measurement Constraints
        for f in self.gm.graph.interzone_flows:
            flow = self.dm.cur_flows_by_id[f.id].measured
            if flow is not None:
                self.engine.update_constraint_lb(name=PREFIX_MEASURE + f.id, lb=flow)
                self.engine.update_constraint_ub(name=PREFIX_MEASURE + f.id, ub=flow)

    def apply_nf_mass_balance_constraints(self):
        for z in self.gm.graph.zones:
            if z.type == ZoneTypes.STREAM:
                con_name = PREFIX_NF_ZONE + z.id
                nf_available = 0
                for f in self.gm.get_zone_inflows(z.id):
                    from_zone = self.gm.get_zone_by_id(f.from_zone)
                    if from_zone.type in (ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS):
                        val = self.dm.cur_flows_by_id[f.id].available_natural
                        l_to = getattr(f, 'loss_to_zone', 0)
                        nf_available += val * (1 - l_to)

                reach_residual = self.dm.zone_residuals.get(z.id, 0)
                if reach_residual < 0:
                    nf_available += reach_residual

                self.engine.update_constraint_lb(name=con_name, lb=0)
                self.engine.update_constraint_ub(name=con_name, ub=max(0, nf_available))

    def remove_nf_mass_balance_constraints(self):
        for z in self.gm.graph.zones:
            if z.type == ZoneTypes.STREAM:
                self.engine.update_constraint_lb(name=PREFIX_NF_ZONE + z.id, lb=0)
                self.engine.update_constraint_ub(name=PREFIX_NF_ZONE + z.id, ub=None)

    def lock_spill_variables(self):
        natural_zones = {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}
        spill_vars = []
        for t in self.all_trxns:
            if type(t) == Trxn and getattr(t, 'is_slack', False):
                if len(t.path) == 1:
                    path_item = t.path[0]
                    flow = self.gm.get_flow_by_id(path_item.flow_id)
                    from_zone = self.gm.get_zone_by_id(flow.from_zone)
                    to_zone = self.gm.get_zone_by_id(flow.to_zone)
                    if path_item.factor < 0:
                        from_zone, to_zone = to_zone, from_zone
                    if from_zone.type not in natural_zones and to_zone.type in natural_zones:
                        spill_vars.append(t)

        anchor_spill_vars = [self.tm.get_anchor_var(t) for t in spill_vars if self.tm.get_anchor_var(t)]
        if anchor_spill_vars:
            obj_value, solved_values = self.engine.solve_objective(anchor_spill_vars, maximization=False)

            con_name = 'lock-slacks'
            self.engine.add_constriant(name=con_name, lb=0, ub=obj_value)
            for var_name in anchor_spill_vars:
                self.engine.set_coeficient(con_name, var_name, 1)

    def calculate_apportionments(self, schedule: CoreSeqSchedule, start_priority=None, stop_priority=None):
        for x in schedule.series:
            priority = getattr(x, 'priority', -1)
            item = x.item
            if start_priority is not None and start_priority > priority: continue
            if stop_priority is not None and stop_priority < priority: continue
            if priority >= SLACK_TRXN_PRIORITY: return

            log(f"\nPriority: {priority}")
            try:
                if type(item) is CorePropSchedule:
                    self.maximize_series(item)
                elif type(item) is CoreSeqSchedule:
                    self.calculate_apportionments(item, start_priority, stop_priority)
                elif type(item) is CoreScheduleVariable:
                    self._maximize_var(item.var)
            except Exception:
                print('Solver failed! Adding feasibility slacks...')
                self.feasibility_fallback()
                if type(item) is CorePropSchedule:
                    self.maximize_series(item)
                elif type(item) is CoreSeqSchedule:
                    self.calculate_apportionments(item, start_priority, stop_priority)
                elif type(item) is CoreScheduleVariable:
                    self._maximize_var(item.var)
            log(f"\nCompleted iteration for priority: {priority}")

    def solve_for_nonpath_vars(self):
        # Build a strict list of only legitimate transaction variables,
        # explicitly ignoring internal LP utility variables like 'combined'
        target_variables = []
        for trxn in self.all_trxns:
            if type(trxn) == Trxn:
                for x in trxn.path:
                    target_variables.append(f"{trxn.id}___{x.flow_id}")
            elif type(trxn) == TrxnGroup:
                target_variables.append(trxn.id)

        _, variable_values = self.engine.solve_objective(target_variables, maximization=False)
        for var_name, solved_value in variable_values.items():
            self.cur_trxn_value[var_name] = solved_value
            log(f' - resolved {var_name} to {solved_value}')

    def _minimize_minus_vars(self, vars: list[Trxn | TrxnGroup]) -> dict[str, float]:
        origional_ub: dict[str, float] = {}
        minus_vars = self.tm.get_minus_vars(vars)
        if not minus_vars:
            return origional_ub

        var_names = []
        for minus_var in minus_vars:
            anchor = self.tm.get_anchor_var(minus_var)
            if anchor and anchor not in origional_ub:
                _, ub = self.engine.get_variable_bounds(anchor)
                origional_ub[anchor] = ub
                var_names.append(anchor)

        if var_names:
            _, solved_values = self.engine.solve_objective(var_names, maximization=False)
            for v_name in var_names:
                min_val = solved_values[v_name]
                if abs(min_val) < SOLVER_TOL:
                    min_val = 0.0
                self.engine.update_variable_bounds(v_name, ub=min_val)
        return origional_ub

    def _reset_minus_vars(self, origional_ub: dict[str, float]):
        for minus_var, ub in origional_ub.items():
            self.engine.update_variable_bounds(minus_var, ub=ub)

    def _maximize_var(self, var: Trxn | TrxnGroup):
        if type(var) == Trxn:
            target_var = self.tm.get_anchor_var(var)
        else:
            target_var = var.id

        if not target_var:
            return

        origional_ub = self._minimize_minus_vars([var])
        new_value = self.engine.maximize_and_update_variable(target_var)
        self.cur_trxn_value[target_var] = new_value

        reason = self._determine_reason(var, new_value)
        self.cur_trxn_reasons[var.id] = reason

        log(f' - maxed {target_var} to {new_value} (Reason: {reason})')
        self._reset_minus_vars(origional_ub)

    def maximize_series(self, series: CorePropSchedule | CoreSeqSchedule):
        maxed_vs: list[Trxn | TrxnGroup] = []
        deferred_vars: list[Trxn | TrxnGroup] = []

        vars_list, factors = self._get_next_iter(series, maxed_vs)
        while vars_list:
            var_names = []
            for v in vars_list:
                if type(v) == Trxn:
                    anchor = self.tm.get_anchor_var(v)
                    if anchor: var_names.append(anchor)
                else:
                    var_names.append(v.id)

            proportion_factors = {var_names[i]: factors[i] for i in range(len(var_names))}

            has_tiny_factor = False
            for var_name, f in list(proportion_factors.items()):
                if 0 < f < 0.000001:
                    log(f"WARNING: Proportion factor for Variable {var_name} is too small. Moving to sequential execution.")
                    var_obj = next((v for v in vars_list if (self.tm.get_anchor_var(v) == var_name if type(v)==Trxn else v.id == var_name)), None)
                    if var_obj and var_obj not in deferred_vars:
                        deferred_vars.append(var_obj)
                        maxed_vs.append(var_obj)
                    has_tiny_factor = True

            if has_tiny_factor:
                vars_list, factors = self._get_next_iter(series, maxed_vs)
                continue

            factors_sum = sum(proportion_factors.values())
            if factors_sum == 0:
                for var_name in proportion_factors:
                    self.engine.update_variable_bounds(var_name, lb=0)
                    self.cur_trxn_value[var_name] = 0
                break

            origional_ub = self._minimize_minus_vars(vars_list)
            var_values = self.engine.maximize_group_by_proportions(var_names, proportion_factors)

            for var_name, var_value in var_values.items():
                if abs(var_value) < SOLVER_TOL:
                    var_value = 0.0

                self.engine.update_variable_bounds(var_name, lb=var_value)
                self.cur_trxn_value[var_name] = var_value

                var_obj = next((v for v in vars_list if (self.tm.get_anchor_var(v) == var_name if type(v)==Trxn else v.id == var_name)), None)
                reason = self._determine_reason(var_obj, var_value) if var_obj else "Proportional Allocation"
                if var_obj:
                    self.cur_trxn_reasons[var_obj.id] = reason
                log(f' - maxed {var_name} to {var_value} (Reason: {reason})')

            maxed_vs.extend(self._get_newly_maxed_vars(vars_list))
            vars_list, factors = self._get_next_iter(series, maxed_vs)
            self._reset_minus_vars(origional_ub)

        for var_obj in deferred_vars:
            log(f"Processing deferred tiny-factor variable sequentially: {var_obj.id}")
            self._maximize_var(var_obj)

    def _get_newly_maxed_vars(self, vars: list[Trxn | TrxnGroup]):
        return [v for v in vars if self._is_var_maxed(v)]

    def _is_var_maxed(self, var: Trxn | TrxnGroup):
        if type(var) == Trxn:
            target_var = self.tm.get_anchor_var(var)
        else:
            target_var = var.id
        if not target_var: return True
        obj_value, _ = self.engine.solve_objective([target_var], maximization=True)
        return isclose(self.cur_trxn_value.get(target_var, 0), obj_value, abs_tol=SOLVER_TOL)

    def _get_next_iter(self, schedule: CorePropSchedule | CoreSeqSchedule, maxed_vars: list[Trxn | TrxnGroup]):
        var_names: list[Trxn | TrxnGroup] = []
        factors: list[float] = []
        if type(schedule) is CoreSeqSchedule:
            for x in schedule.series:
                item = x.item
                if type(item) == CoreSeqSchedule or type(item) == CorePropSchedule:
                    var_names, factors = self._get_next_iter(item, maxed_vars)
                elif type(item) is CoreScheduleVariable:
                    if item.var not in maxed_vars:
                        var_names.append(item.var)
                        factors.append(1)
                if len(var_names)>0:
                    break
        elif type(schedule) is CorePropSchedule:
            for x in schedule.series:
                item, factor = getattr(x, 'item', None), getattr(x, 'factor', 1)
                if type(item) == CoreSeqSchedule or type(item) == CorePropSchedule:
                    svar_names, sfactors = self._get_next_iter(item, maxed_vars)
                    var_names.extend(svar_names)
                    sum_sfactors = sum(sfactors)
                    if sfactors:
                        x = 0
                        if sum_sfactors > 0:
                            x = factor / sum_sfactors
                        factors.extend([f * x for f in sfactors])
                elif type(item) is CoreScheduleVariable:
                    if item.var not in maxed_vars:
                        var_names.append(item.var)
                        factors.append(factor)
        return var_names, factors

    def get_variables(self, date: str) -> list[SolverOutputApportionment]:
        vars_output: list[SolverOutputApportionment] = []
        for v in self.all_trxns:
            if type(v) != Trxn:
                continue

            reason = self.cur_trxn_reasons.get(v.id, "Unsolved / Unconstrained")

            is_stream_slack = False
            natural_set = set([ZoneTypes.STREAM])
            if getattr(v, 'is_slack', False) and len(v.path) == 1:
                flowobj = self.gm.get_flow_by_id(v.path[0].flow_id)
                from_zone_type = self.gm.get_zone_by_id(flowobj.from_zone).type
                to_zone_type = self.gm.get_zone_by_id(flowobj.to_zone).type
                if from_zone_type == ZoneTypes.STREAM and to_zone_type == ZoneTypes.STREAM:
                    is_stream_slack = True

            if is_stream_slack:
                flowobj = self.gm.get_flow_by_id(v.path[0].flow_id)
                natural_flow = self.dm.cur_flows_by_id[flowobj.id].natural
                var_name = f"{v.id}___{v.path[0].flow_id}"
                var_value = self.cur_trxn_value.get(var_name, 0.0)

                if natural_flow is not None and var_value is not None:
                    vars_output.append(SolverOutputApportionment(
                        date=date,
                        interzone_flow_id=v.path[0].flow_id,
                        txn_id=v.id + '_NF',
                        value=natural_flow * v.path[0].factor,
                        is_forward=True
                    ))
                    vars_output.append(SolverOutputApportionment(
                        date=date,
                        interzone_flow_id=v.path[0].flow_id,
                        txn_id=v.id + '_CPI',
                        value=(var_value - natural_flow) * v.path[0].factor,
                        is_forward=(var_value > natural_flow),
                        reason=reason
                    ))
            else:
                for a in v.path:
                    var_name = f"{v.id}___{a.flow_id}"
                    var_value = self.cur_trxn_value.get(var_name, 0.0)

                    if var_value is None:
                        raise ValueError(f'value for {var_name} is None')

                    vars_output.append(SolverOutputApportionment(
                        date=date,
                        interzone_flow_id=a.flow_id,
                        txn_id=v.id,
                        value=var_value * a.factor,
                        is_forward=a.factor > 0,
                        reason=reason
                    ))
        return vars_output

    def _determine_reason(self, var: Trxn | TrxnGroup, value: float) -> str:
        if var is None:
            return "Unknown"

        upper_limit = self.tm.get_transaction_upper_limit(var, self.dm.cur_date)
        if upper_limit is not None and isclose(value, upper_limit, abs_tol=SOLVER_TOL):
            return "Water Right Limit"

        if type(var) == Trxn:
            for path_item in var.path:
                con_name = PREFIX_MEASURE + path_item.flow_id
                var_name = f"{var.id}___{path_item.flow_id}"
                if self.engine.is_constraint_tight(con_name, var_name):
                    return f"No remaining demand at destination (Measured flow reached at '{path_item.flow_id}')"

            ordered_path = self.tm.ordered_paths.get(var.id, [])
            if len(ordered_path):
                first_item = ordered_path[0]
                anchor_var = f"{var.id}___{first_item.flow_id}"

                first_flow = self.gm.get_flow_by_id(first_item.flow_id)
                from_zone = self.gm.get_zone_by_id(first_flow.from_zone)

                if first_item.factor < 0:
                    from_zone = self.gm.get_zone_by_id(first_flow.to_zone)

                if from_zone.type == ZoneTypes.STREAM:
                    if self.engine.is_constraint_tight(PREFIX_NF_ZONE + from_zone.id, anchor_var):
                        return f"No remaining divertible Natural Flow at source '{from_zone.id}'"
                    for f in self.gm.traverse_downstream(from_zone.id):
                        if self.engine.is_constraint_tight(PREFIX_NF_ZONE + f.to_zone, anchor_var):
                            return f"No remaining divertible Natural Flow at downstream reach '{f.to_zone}'"
        return "Other"

    def feasibility_fallback(self):
        self.feasibility_slacks = self.add_feasibility_vars(self.feasibility_slacks)
        self.engine.set_perminant_minus_var('FEAS_SUM')

    def add_feasibility_vars(self, feasibility_slacks) -> list[str]:
        engine = self.engine
        def add_feasibility_slack(con_name, coef):
            var_name = 'FEAS_SLACK_'+con_name+'_'+str(coef)
            if var_name not in feasibility_slacks:
                engine.add_variable(var_name, lb=0, ub=None)
                engine.set_coeficient(con_name, var_name, coef)
                engine.set_coeficient('FEAS_TOTAL', var_name, 1)
                feasibility_slacks.append(var_name)

        if 'FEAS_SUM' not in engine.vars:
            engine.add_variable('FEAS_SUM', lb=0, ub=None)
            engine.add_constriant('FEAS_TOTAL', lb=0, ub=0)
            engine.set_coeficient('FEAS_TOTAL', 'FEAS_SUM', -1)

        for con_name in engine.get_constraint_names():
            if con_name == 'FEAS_TOTAL':
                continue
            add_feasibility_slack(con_name, 1)
            add_feasibility_slack(con_name, -1)
        return feasibility_slacks


def system_report_str(results:SolverOutput, day_idx:int, date:str, gm:GraphManager, dm:DailyDataManager, tm:TrxnManager) -> str:
    def warn_if_value_is_incorrect(path_item:TrxnPathItem, value:float|None):
        if getattr(path_item, 'expected_values', None) is not None:
            expected_value = path_item.expected_values[day_idx]
            if expected_value is not None and value is not None:
                if abs(expected_value - value) > SOLVER_TOL:
                    return ('*** NOT EQUAL TO EXPECTED VALUE OF '
                        + f'{expected_value:9.4f}')
        return ''

    dm.set_day(date)
    out = ''
    for n in gm.graph.zones:
        if n.type == ZoneTypes.STREAM:
            storage_change = dm.cur_storage_chg_by_id[n.id].measured
            out += '\n' + n.id + f'(\u0394S={storage_change:9.4f})'

            for f in gm.get_zone_outflows(n.id):
                flow_value = dm.cur_flows_by_id[f.id].measured
                out += f'\n {flow_value:9.4f} >> {f.to_zone}'
                for i in results.get_result_value(date=date, flow_id=f.id):
                    path_item = tm.get_path_item(i.txn_id, f.id)
                    out += f'\n      {i.txn_id: <26} = {i.value:9.4f}   ({i.reason})'
                    out += warn_if_value_is_incorrect(path_item, i.value)

            for f in gm.get_zone_inflows(n.id):
                flow_value = dm.cur_flows_by_id[f.id].measured
                out += f'\n {flow_value:9.4f} << {f.from_zone}'
                for i in results.get_result_value(date=date, flow_id=f.id):
                    path_item = tm.get_path_item(i.txn_id, f.id)
                    out += f'\n      {i.txn_id: <26} = {i.value:9.4f}   ({i.reason})'
                    out += warn_if_value_is_incorrect(path_item, i.value)
    return out


def assert_apportionments_equal_expected(results: SolverOutput, input: SolverInput, gm:GraphManager, dm:DailyDataManager, tm:TrxnManager) -> None:
    message:str = ''
    cnt = 0
    for t in traverse_vars(input.txns):
        if type(t) == Trxn:
            for p in t.path:
                if getattr(p, 'expected_values', None) is not None:
                    idx = 0
                    for date in loop_through_date_range(input.beg_date, input.end_date):
                        expected_value = p.expected_values[idx]
                        computed_values = results.get_result_value(date=date, trxn_id=t.id, flow_id=p.flow_id)

                        if not computed_values:
                            print(results.get_result_value())
                            raise ValueError(f'(date, trxn_id, flow_id) of {(date, t.id, p.flow_id)} not found.')
                        elif len(computed_values) > 1:
                            raise ValueError('Multiple results found')

                        computed_value = computed_values[0].value
                        if expected_value is not None:
                            cnt += 1
                            msg = ( message +
                                f'Var "{t.id}": computed ({computed_value}) != ' +
                                f'expected ({expected_value}) on {date}\n' +
                                (system_report_str(results, idx, date, gm, dm, tm) if input is not None else '')
                            )
                            assert abs(expected_value - computed_value) < SOLVER_TOL, msg
                        idx += 1

    if cnt == 0:
        raise Exception('No trxn path-items were given an expected_value!')