"""

TODO - Add lags, unlags
NOTE: Lag stuff is incomplete. What lags do we care about being consistent, and which not?

TODO - ensure the API names & saves the slack transactions correctly.

NOTE: This version defines Natural Flow at the zone, not the interzone-flow. Is that what I want?


# TODO - #1: Fix losses so results satisfy measurement mass balance (see failing test)
# TODO - #2: I suspect that when a storage delivery transaction has no priority,
            it is being maximized in the very last step -- and whether that transaction
            or 'unauthorized' picks up the slack is arbitrary.
            e.g., see: 2021, Duchesne R2 → Knights Canal

            Fixed???

"""

from typing import Generator
from dataclasses import dataclass
from math import isclose
from collections.abc import Iterator

from .models import (
    AccountingGraph, AccountingLimit, CorePropSchedule, CorePropScheduleItem,
    CoreScheduleVariable, CoreSeqSchedule, CoreSeqScheduleItem,
    FlowComponents, FlowComponentsTypes, InterzoneFlow,
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
    data_manager = DailyDataManager(graph_manager, input.measurements, input.measurement_beg_date, input.measurement_beg_date, input.external_natural_flows)

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

    # The measured flow should always match the input measurements.
    measured: float = 0

    # The natural flow includes any upstream-boundary natural flow specified in
    # the input data.
    natural: float = 0

    # This is the portion of the natural flow that excludes upstream-boundary
    # natural flow specified in the input data.
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
    """Adds slack transactions to ensure the problem is feasible. This will
    add an extra transaction for each interzone flow (or two if it's
    bidirectional). These slack variables represent things like unauthorized
    diversions to a user from a stream, water spilled from a reservoir or an
    import to the natural system, etc."""
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

    def set_implied_calculated_flow_boundaries(self):
        """Previous versions of the general solver assumed that a residual
        calculation was neccessary when no flow measurements were specified.
        This function explicitly creates those calculation specifications so
        test code built using the old way will still work.

        In the future, it may be best to require a full, explicit definition
        and not depend on this function.
        """
        for z in self.graph.zones:

            # 1. Reservoirs connected to a reach with a bi-directional flow
            if z.type == ZoneTypes.STORAGE:
                for f in self.get_zone_inflows(z.id):
                    if len(f.pos_flow_components + f.neg_flow_components) == 0:
                        f.pos_flow_components.append(FlowComponents(
                            flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                            gain_factor=1,
                            loss_factor=1
                        ))
                    if len(f.pos_flow_components) == 1 and len(f.neg_flow_components) == 0 and f.bidirectional:
                        if f.pos_flow_components[0].flow_type == FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE:
                            f.pos_flow_components[0].gain_factor = 1
                            f.pos_flow_components[0].loss_factor = 1

            # 2. Stream zones connected to a gain or loss zone.
            elif z.type == ZoneTypes.STREAM:
                for f in self.get_zone_inflows(z.id):
                    from_z = self.get_zone_by_id(f.from_zone)
                    is_gain = from_z.type == ZoneTypes.SYSTEM_GAIN_LOSS
                    if is_gain and len(f.pos_flow_components + f.neg_flow_components)==0:
                        f.pos_flow_components.append(FlowComponents(
                            flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                            gain_factor=1,
                            loss_factor=(1 if f.bidirectional else 0)
                        ))
                for f in self.get_zone_outflows(z.id):
                    to_z = self.get_zone_by_id(f.to_zone)
                    is_loss = to_z.type == ZoneTypes.SYSTEM_GAIN_LOSS
                    if is_loss and len(f.pos_flow_components + f.neg_flow_components)==0:
                        f.pos_flow_components.append(FlowComponents(
                            flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                            gain_factor=(1 if f.bidirectional else 0),
                            loss_factor=1
                        ))


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
            to_zone = self.gm.get_flow_by_id(flow_id).to_zone
            outflows = self.gm.get_zone_outflows(to_zone)
            for f in outflows:
                if (self.gm.get_zone_by_id(f.from_zone).type == ZoneTypes.STREAM and
                    self.gm.get_zone_by_id(f.to_zone).type == ZoneTypes.STREAM):

                    outflow_data = self.cur_flows_by_id[f.id]

                    outflow_data.natural += impact
                    # Don't allow the natural flow to be reduced below zero.
                    if outflow_data.natural < 0:
                        outflow_data.natural = 0

                    if is_available:
                        outflow_data.available_natural += impact
                        # Don't allow the natural flow to be reduced below zero.
                        if outflow_data.available_natural < 0:
                            outflow_data.available_natural = 0

                    recursive_apply_impact_downstream(f.id, impact)

        # 1. First, look at the upstream boundaries with specified natural flow
        #    values. We want to propigate this natural flow downstream, but
        #    also propigate the fact that some portion of the flows have been
        #    utilized.
        for f_id in self.external_natural_flows:
            if f_id in self.cur_flows_by_id:

                flow_data = self.cur_flows_by_id[f_id]

                if flow_data.natural != 0:
                    recursive_apply_impact_downstream(f_id, flow_data.natural, False)


        # 2. Now look at the local reach gains, and propigate that to increase
        #    the natural flow all the way downstream.
        for f in self.gm.graph.interzone_flows:
            is_gain_to_stream =  (
                self.gm.get_zone_by_id(f.from_zone).type == ZoneTypes.SYSTEM_GAIN_LOSS and
                self.gm.get_zone_by_id(f.to_zone).type == ZoneTypes.STREAM)

            if is_gain_to_stream:
                flow_data = self.cur_flows_by_id[f.id]
                flow_data.natural = flow_data.measured
                flow_data.available_natural = flow_data.measured

                # Now route that flow downstream.
                recursive_apply_impact_downstream(f.id, flow_data.measured)

    def _get_lag_by_flowline_id(self) -> dict[str, float]:
        """Traverse the graph to set the absolute lag (time offset) for each
        interzone-flow."""

        # The thing to populate, figure out.
        flow_lags = {}

        def set_flow_lag(f: InterzoneFlow, lag: float):
            if f.id in flow_lags:
                if flow_lags[f.id] != lag:
                    raise ValueError(f'Computed absolute lag times of {lag} ' +
                        f'for interzone-flow id: "{f.id}" is inconsistent '+
                        f'with existing value of {flow_lags[f.id]}!')
            else:
                # Set the lag for this interzone-flow.
                flow_lags[f.id] = lag

                # Loop through the connected interzone-flows, and set their
                # lags too.
                iter_over_zone(f.to_zone, lag - f.lag_to_zone)
                iter_over_zone(f.from_zone, lag - f.lag_from_zone)

        def iter_over_zone(zone_id: str, lag: float):
            """Iterate through each flow to and from the given zone."""
            z = self.gm.get_zone_by_id(zone_id)
            #if z.external:                                                    # TODO - this was active in the origional code. May require more work ...
            #    return

            for flow in self.gm.get_zone_inflows(zone_id):
                set_flow_lag(flow, lag + flow.lag_to_zone)
            for flow in self.gm.get_zone_outflows(zone_id):
                set_flow_lag(flow, lag + flow.lag_from_zone)

        # Traverse the graph and assign a lag to each flow...
        for f in self.gm.graph.interzone_flows:
            if f.id not in flow_lags:
                set_flow_lag(f, 0)

        # Normalize so there are no negative zone lags. (Adjust reference so
        # the downstream zone has a lag of zero.)
        min_lag = min(flow_lags.values()) if flow_lags else 0
        flow_lags = {f_id: lag - min_lag for f_id, lag in flow_lags.items()}

        return flow_lags

    def _get_interzone_flow_values(self, date: str) -> dict[str, float]:
        """Given the class input, determine the total flow values for each
        interzone flow. This involves summing flow components, applying lags,
        and calculating residual flows.

        Returns a dictionary of flows values by interzone-flow-id.
        """

        total_flow_by_id: dict[str, float] = {}

        # 1. Measurements
        for f in self.gm.graph.interzone_flows:
            lag = self._flow_lags[f.id]
            total_measured = 0
            for comp, fact in ([(i,  1) for i in f.pos_flow_components] +
                               [(i, -1) for i in f.neg_flow_components]):
                if comp.measurement_id is not None:
                    val = self._get_flow_from_meas(comp.measurement_id, date, int(lag))
                    if val is None:
                        if COALESCE_MISSING_FLOWS_TO_ZERO:
                            val = 0
                        else:
                            raise ValueError(f"Measurement {comp.measurement_id} undefined on {date}")
                    total_measured += val * fact
            total_flow_by_id[f.id] = total_measured

        # 2. Calculate Residuals
        for zone_id, calcs in self._determine_residual_calc_order():
            residual_flow = sum(total_flow_by_id[f.id] for f in self.gm.get_zone_outflows(zone_id))
            storage_chg = self._get_storage_change(self.gm.get_zone_by_id(zone_id), date)
            if storage_chg is not None:
                residual_flow += storage_chg
            residual_flow -= sum(total_flow_by_id[f.id] for f in self.gm.get_zone_inflows(zone_id))

            # Now assign the residual flow to the the interzone-flow term(s)
            for f, calc in calcs:
                factor = calc.gain_factor if residual_flow >= 0 else calc.loss_factor
                if factor != 0:
                    total_flow_by_id[f.id] += factor * residual_flow

        # 3. Checks
        for f in self.gm.graph.interzone_flows:
            if total_flow_by_id[f.id] < 0 and not f.bidirectional:
                if COALESCE_NEGATIVE_FLOWS_TO_ZERO:
                    total_flow_by_id[f.id] = 0
                else:
                    raise ValueError(f"Net flow negative for {f.id} on {date}")

        return total_flow_by_id

    def _determine_residual_calc_order(self) -> list[tuple[str, list[tuple[InterzoneFlow, FlowComponents]]]]:
        """In order to obtain the values for Calculated Flows, we first need
        to know the order that these flows must be calculated in, since one
        calculation may depend on another. This is what this function
        provides."""

        zone_calc_order :list[str] = []
        flow_balance_calcs :dict[str, list[str]] = {}
        residual_flows_by_zone:dict[str, list[tuple[InterzoneFlow, FlowComponents]]] = {}

        def add_flow_balance_calc(zone_id:str, required_by_zone_id:str, f:InterzoneFlow, flow_component:FlowComponents):
            if zone_id not in flow_balance_calcs:
                flow_balance_calcs[zone_id] = []
                residual_flows_by_zone[zone_id] = []
            flow_balance_calcs[zone_id].append(required_by_zone_id)
            residual_flows_by_zone[zone_id].append((f, flow_component))

        for f in self.gm.graph.interzone_flows:
            for comp, _ in ([(i, 1) for i in f.pos_flow_components] +
                            [(i,-1) for i in f.neg_flow_components]):
                if comp.flow_type == FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE:
                    add_flow_balance_calc(f.to_zone, f.from_zone, f, comp)
                elif comp.flow_type == FlowComponentsTypes.FLOW_BALANCE_OF_SOURCE_ZONE:
                    add_flow_balance_calc(f.from_zone, f.to_zone, f, comp)

        # Find the keys that are not dependencies.
        while len(flow_balance_calcs) > 0:
            dependencies = {dep for deps in flow_balance_calcs.values() for dep in deps}
            numdel = 0
            for key in list(flow_balance_calcs.keys()):
                if key not in dependencies:
                    zone_calc_order.append(key)
                    del flow_balance_calcs[key]
                    numdel += 1

            # If no next item can be identified, raise an error instead of
            # infinite looping
            if numdel == 0:
                raise ValueError("Circular dependency in residual calculations!")

        return [(z, residual_flows_by_zone[z]) for z in zone_calc_order]

    def _get_storage_change(self, z: Zone, date: str):
        storage_chg = None
        if z.type in (ZoneTypes.STREAM, ZoneTypes.STORAGE):
            for mid in z.storage_meas_ids:
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

        # Convert the date to the list index.
        beg_date = datetime.strptime(self.measurement_beg_date, "%Y-%m-%d").date()
        this_date = datetime.strptime(date, "%Y-%m-%d").date()
        day_idx = (this_date - beg_date).days

        # Retrieve the value.
        idx = day_idx - lag
        if 0 <= idx < len(measurements):
            return measurements[idx]
        return None

    def _get_storage_change_from_meas(self, meas_id: str | None, date: str, lag: int = 0):
        """
        storage_change for today = [todays storage] - [yesterdays storage]
        The assumption is that these are end-of-day storage volumes.
        """
        from datetime import datetime
        measurements = self.measurements[str(meas_id)]

        # Convert the date to the list index.
        beg_date = datetime.strptime(self.measurement_beg_date, "%Y-%m-%d").date()
        this_date = datetime.strptime(date, "%Y-%m-%d").date()
        day_idx = (this_date - beg_date).days

        if meas_id is not None:
            idx_yesterday = day_idx - 1 - lag
            idx_today = day_idx - lag

            if idx_yesterday >= 0 and idx_today < len(measurements):
                # Retrieve the values and calculate the change.
                storage_a = measurements[idx_yesterday]
                storage_b = measurements[idx_today]
                if storage_a is not None and storage_b is not None:
                    # Note that units should have already been converted from
                    # ac-ft/day to cfs
                    return storage_b - storage_a

        return None


class TrxnManager:
    """Makes it easier to manipulate transaction data."""

    def __init__(self, gm: GraphManager, txns:list[Trxn | TrxnGroup]):
        self.gm = gm
        self._ensure_children_after_parent(txns)
        self.all_trxns = list(traverse_vars(txns))
        self.lookup_flow_trxns = self._build_flow_trxns_lookup()


    def _ensure_children_after_parent(self, txns: list['Trxn | TrxnGroup'], parent_priority: float | None = None):
        """Recursively shifts child priorities to ensure they solve immediately after their parent."""
        for t in txns:
            # Shift the child priority if it is less than or equal to the parent's priority
            if parent_priority is not None and t.priority is not None and t.priority <= parent_priority:
                new_priority = parent_priority + 1e-5
                print(f"WARNING: Child transaction '{t.id}' priority ({t.priority}) "
                        f"equals or precedes parent priority ({parent_priority}). "
                        f"Adjusting '{t.id}' priority to {new_priority}.")
                t.priority = new_priority


            # Recurse for nested groups using the established priority
            if type(t) == TrxnGroup:
                self._ensure_children_after_parent(t.children_trxns, t.priority)

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

        #log(f"TRXN UPPER LIMIT: {t.id} = {upper_limit}")
        return upper_limit

    def get_minus_vars(self, vars: list[Trxn | TrxnGroup]) -> list[Trxn]:

        def get_from(v:Trxn) -> tuple[Zone, InterzoneFlow]:
            first_item = v.path[0]
            f0 = self.gm.get_flow_by_id(first_item.flow_id)
            if first_item.factor >= 0:
                from_zone = self.gm.get_zone_by_id(f0.from_zone)
            else:
                from_zone = self.gm.get_zone_by_id(f0.to_zone)
            return from_zone, f0

        def get_to(v:Trxn) -> tuple[Zone, InterzoneFlow]:
            last_item = v.path[-1]
            fl = self.gm.get_flow_by_id(last_item.flow_id)
            if last_item.factor < 0:
                to_zone = self.gm.get_zone_by_id(fl.from_zone)
            else:
                to_zone = self.gm.get_zone_by_id(fl.to_zone)
            return to_zone, fl

        output: list[Trxn]  = []

        for v in vars:
            if type(v) == Trxn:
                if len(v.path) > 0:

                    # If the variable starts at a storage zone, we need to look
                    # for slack variables flowing into that storage zone.
                    from_zone, from_flow = get_from(v)
                    if from_zone.type == ZoneTypes.STORAGE:
                        for trxn, path_item in self.lookup_flow_trxns[from_flow.id]:
                            if trxn.is_slack and len(trxn.path) == 1:
                                if get_to(trxn)[0] == from_zone:
                                    output.append(trxn)

                    # If the variable ends at a storage zone, we need to look
                    # for slack variables flowing from that storage zone.
                    to_zone, to_flow = get_to(v)
                    if to_zone.type == ZoneTypes.STORAGE:
                        for trxn, path_item in self.lookup_flow_trxns[to_flow.id]:
                                #if trxn.is_slack and len(trxn.path) == 1:
                                if get_from(trxn)[0] == to_zone:
                                    output.append(trxn)

        return output

    def _is_spill(self, trxn: Trxn) -> bool:
        # A var has is_spill=True when it represents water under                   # TODO - get rid of is_spill!
        # the name of a user being released back to the natural
        # system, e.g. the slack variable representing reservoir
        # releases with no downstream diversion or imports with no
        # downstream diversion.
        if len(trxn.path) == 0:
            return False

        last_item = trxn.path[-1]
        last_flow = self.gm.get_flow_by_id(last_item.flow_id)
        to_zone = self.gm.get_zone_by_id(last_flow.to_zone)
        if last_item.factor < 0:
            to_zone = self.gm.get_zone_by_id(last_flow.from_zone)

        first_item = trxn.path[0]
        first_flow = self.gm.get_flow_by_id(first_item.flow_id)
        from_zone = self.gm.get_zone_by_id(first_flow.from_zone)
        if first_item.factor < 0:
            from_zone = self.gm.get_zone_by_id(first_flow.to_zone)

        # If the flow variable goes from a non-source to a source, set
        # the spill flag.
        from_a_nonsource = from_zone.type not in (ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS)
        to_a_source = to_zone.type == ZoneTypes.STREAM

        return from_a_nonsource and to_a_source

    def build_schedule(self, date:str) -> CoreSeqSchedule:
        # Convert the paths dictionary to an ordered schedule list by sorting the
        # paths by priority while grouping paths with the same priority.
        vars = self.all_trxns

        output_list: list[CoreSeqScheduleItem] = []

        varsByPriority: dict[float,list['Trxn | TrxnGroup']]  = {}
        for v in vars:
            p = v.priority if v.priority is not None else -1
            if p not in varsByPriority:
                varsByPriority[p] = []
            varsByPriority[p].append(v)

        # Get a list of the distinct priority values sorted from smallest to
        # largest.
        priorities = sorted(varsByPriority.keys())

        # Now add the variables to the schedule, in priority order.
        for p in priorities:
            pvars = varsByPriority[p]

            # If there is only one item with this priority, it must be either a
            # variable or a sequential subseries:
            if len(pvars) == 1:
                #if type(pvars[0]) == Trxn:
                #    item = CoreScheduleVariable(var=pvars[0])
                #    output_list.append(CoreSeqScheduleItem(priority=p, item=item))
                #elif type(pvars[0]) == TrxnGroup:
                #    item = self.build_schedule(pvars[0].children_trxns, date)
                #    output_list.append(CoreSeqScheduleItem(priority=p, item=item))
                item = CoreScheduleVariable(var=pvars[0])
                output_list.append(CoreSeqScheduleItem(priority=p, item=item))


            # Otherwise, it must be a proportional subseries:
            elif len(pvars) > 1:
                nested_sched = None

                # Look for any variables that do not have an upper limit. Increase
                # these together in equal-proportion before increasing the other
                # variables that do have a limit.
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

                # Deal with variables that do have a limit.
                proportional_subseries: list[CorePropScheduleItem] = []
                cfs_sum = 0
                for v2 in pvars:
                    v2_ub = self.get_transaction_upper_limit(v2, date)
                    if v2_ub is not None:
                        cfs_sum += v2_ub
                        #if type(v2) == Trxn:
                        #    citem = CorePropScheduleItem(
                        #        factor=v2_ub,
                        #        item=CoreScheduleVariable(var=v2)
                        #    )
                        #    proportional_subseries.append(citem)
                        #elif type(v2) == TrxnGroup:
                        #    citem = CorePropScheduleItem(
                        #        factor=v2_ub,
                        #        item=self.build_schedule(v2.children_trxns, date)
                        #    )
                        #    proportional_subseries.append(citem)

                        # Apply CoreScheduleVariable uniformly without checking type
                        citem = CorePropScheduleItem(
                            factor=v2_ub,
                            item=CoreScheduleVariable(var=v2)
                        )
                        proportional_subseries.append(citem)

                # normalize the factor
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
        """Return the path-item object for the given trxn passing the given
        interzone flow."""

        # Strip suffixes from dynamically generated stream slack splits
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
        """Add the variables and constraints to the linear solver engine."""

        engine = LPSolver(tolerance=SOLVER_TOL)

        # Add Variables (Bounds will be updated daily)
        for trxn in self.all_trxns:
            engine.add_variable(name=trxn.id, lb=0, ub=None)

        # Add Interzone Flow Measurements
        for f in self.gm.graph.interzone_flows:
            con_name = PREFIX_MEASURE + f.id
            engine.add_constriant(name=con_name, lb=None, ub=None)

        # Add Natural Flow Constraints (Streams)
        for z in self.gm.graph.zones:
            if z.type == ZoneTypes.STREAM:
                con_name = PREFIX_NF_ZONE + z.id
                engine.add_constriant(name=con_name, lb=None, ub=None)

        # Tie variables to flow constraints (Coefficients NEVER change)
        for trxn in self.all_trxns:
            if type(trxn) == Trxn:
                # Measurement Coefficients
                for x in trxn.path:
                    con_name = PREFIX_MEASURE + x.flow_id
                    engine.set_coeficient(con_name, trxn.id, x.factor)

                # Natural Flow Coefficients
                if not trxn.is_slack and len(trxn.path)>0:
                    first_item = trxn.path[0]
                    first_flow = self.gm.get_flow_by_id(first_item.flow_id)
                    from_zone = self.gm.get_zone_by_id(first_flow.from_zone)
                    if first_item.factor < 0:
                        from_zone = self.gm.get_zone_by_id(first_flow.to_zone)

                    if from_zone.type == ZoneTypes.STREAM:
                        engine.set_coeficient(PREFIX_NF_ZONE + from_zone.id, trxn.id, 1)
                        for f in self.gm.traverse_downstream(from_zone.id):
                            engine.set_coeficient(PREFIX_NF_ZONE + f.to_zone, trxn.id, 1)

        # Group Limits
        for trxn in self.all_trxns:
            if type(trxn) == TrxnGroup:
                con_name = PREFIX_PARENT + trxn.id
                engine.add_constriant(name=con_name, lb=0, ub=0)
                engine.set_coeficient(con_name, trxn.id, 1)
                for v2 in trxn.children_trxns:
                    engine.set_coeficient(con_name, v2.id, -1)

        return engine

    def update_daily_bounds(self):
        """Updates the limits on variables and measurements for the current day."""
        # Update Variable Limits
        for trxn in self.all_trxns:
            upper_limit = self.tm.get_transaction_upper_limit(trxn, self.dm.cur_date)
            if type(trxn) == Trxn and trxn.is_slack:
                upper_limit = None
            self.engine.update_variable_bounds(trxn.id, lb=trxn.lower_limit, ub=upper_limit)

        # Update Measurement Constraints
        for f in self.gm.graph.interzone_flows:
            flow = self.dm.cur_flows_by_id[f.id].measured
            if flow is not None:
                self.engine.update_constraint_lb(name=PREFIX_MEASURE + f.id, lb=flow)
                self.engine.update_constraint_ub(name=PREFIX_MEASURE + f.id, ub=flow)

    def apply_nf_mass_balance_constraints(self):
        """Sets the upper bound on the pre-existing NF constraints."""
        for z in self.gm.graph.zones:
            if z.type == ZoneTypes.STREAM:
                con_name = PREFIX_NF_ZONE + z.id
                nf_available = 0
                for f in self.gm.get_zone_inflows(z.id):
                    from_zone = self.gm.get_zone_by_id(f.from_zone)
                    if from_zone.type in (ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS):
                        nf_available += self.dm.cur_flows_by_id[f.id].available_natural

                self.engine.update_constraint_lb(name=con_name, lb=0)
                self.engine.update_constraint_ub(name=con_name, ub=max(0, nf_available))

    def remove_nf_mass_balance_constraints(self):
        for z in self.gm.graph.zones:
            if z.type == ZoneTypes.STREAM:
                self.engine.update_constraint_lb(name=PREFIX_NF_ZONE + z.id, lb=0)
                self.engine.update_constraint_ub(name=PREFIX_NF_ZONE + z.id, ub=None)

    def lock_spill_variables(self):
        """Find all the variables that flow to a stream, and add a constraint
        to prevent them from increasing. """

        natural_zones = {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}

        # Find all the spill vars...
        spill_vars:list[Trxn] = []
        for t in self.all_trxns:
            if type(t) == Trxn and t.is_slack:
                if len(t.path) == 1:
                    path_item = t.path[0]
                    flow = self.gm.get_flow_by_id(path_item.flow_id)
                    from_zone = self.gm.get_zone_by_id(flow.from_zone)
                    to_zone = self.gm.get_zone_by_id(flow.to_zone)

                    if path_item.factor < 0:
                        from_zone, to_zone = to_zone, from_zone

                    if from_zone.type not in natural_zones and to_zone.type in natural_zones:
                        spill_vars.append(t)

        # Minimize the spill vars.
        obj_value, solved_values = self.engine.solve_objective([x.id for x in spill_vars], maximization=False)

        # Lock the spill vars to their min value.
        con_name = 'lock-slacks'
        self.engine.add_constriant(name=con_name, lb=0, ub=obj_value)
        for t in spill_vars:
            self.engine.set_coeficient(con_name, t.id, 1)


    def calculate_apportionments(self, schedule: CoreSeqSchedule, start_priority=None, stop_priority=None):
        """Solves the variables in the order defined by the schedule."""


        for x in schedule.series:
            priority = x.priority
            item = x.item

            if start_priority is not None and start_priority > priority: continue
            if stop_priority is not None and stop_priority < priority: continue
            if priority >= SLACK_TRXN_PRIORITY: return

            log(f"\nPriority: {priority}")

            try:

                #raise RuntimeError('test error...')

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
        # NOTE: I get away with not bothering to seperate the non-path variables
        #       from the path variables in this code because the path variables
        #       have already been maximized and updated so they can not be less
        #       than their max values.
        all_variables = [v.id for v in self.all_trxns]
        _, variable_values = self.engine.solve_objective(all_variables,
                                                         maximization=False)
        for var_name, solved_value in variable_values.items():
            self.cur_trxn_value[var_name] = solved_value
            log(f' - maxed {var_name} to {solved_value}')

    def _minimize_minus_vars(self, vars: list[Trxn | TrxnGroup]) -> dict[str, float]:
        origional_ub: dict[str, float] = {}
        minus_vars = self.tm.get_minus_vars(vars)

        if not minus_vars:
            return origional_ub

        var_names = []
        for minus_var in minus_vars:
            if minus_var.id not in origional_ub:
                _, ub = self.engine.get_variable_bounds(minus_var.id)
                origional_ub[minus_var.id] = ub
                var_names.append(minus_var.id)

        if var_names:
            # Minimize ALL dump slacks simultaneously
            _, solved_values = self.engine.solve_objective(var_names, maximization=False)

            # Lock their upper bounds to the newly found minimums
            for v_name in var_names:
                min_val = solved_values[v_name]

                # Clean up floating point noise to prevent ABNORMAL status
                if abs(min_val) < SOLVER_TOL:
                    min_val = 0.0

                self.engine.update_variable_bounds(v_name, ub=min_val)

        return origional_ub

    def _reset_minus_vars(self, origional_ub: dict[str, float]):
        for minus_var, ub in origional_ub.items():
            self.engine.update_variable_bounds(minus_var, ub=ub)

    def _maximize_var(self, var: Trxn | TrxnGroup):
        """My earlier version of this function used _minimize_minus_vars and
        _reset_minus_vars to prevent apportionments from forcing a reservoir
        spill to increase the divertible natural flow."""
        origional_ub = self._minimize_minus_vars([var])

        new_value = self.engine.maximize_and_update_variable(var.id)
        self.cur_trxn_value[var.id] = new_value

        reason = self._determine_reason(var, new_value)
        self.cur_trxn_reasons[var.id] = self._determine_reason(var, new_value)

        log(f' - maxed {var.id} to {new_value} (Reason: {reason})')
        self._reset_minus_vars(origional_ub)

    def _maximize_var_TEST(self, var: Trxn | TrxnGroup):
        # 1. Get the minus vars (spills) that we want to penalize
        minus_vars = self.tm.get_minus_vars([var])

        # 2. Build our objective variables and weights
        objective_vars = [var.id] + [m.id for m in minus_vars]

        weights = {var.id: 1.0}
        for m in minus_vars:
            weights[m.id] = -2.0  # Penalty MUST be stronger than the +1.0 reward

        # 3. Solve in ONE step. The penalty prevents the loop
        obj_value, solved_values = self.engine.solve_objective(
            objective_vars,
            maximization=True,
            weights=weights
        )

        new_value = solved_values[var.id]

        # 4. Lock the target variable into place
        self.engine.update_variable_bounds(var.id, lb=new_value)
        self.cur_trxn_value[var.id] = new_value

        reason = self._determine_reason(var, new_value)
        self.cur_trxn_reasons[var.id] = reason

        log(f' - maxed {var.id} to {new_value} (Reason: {reason})')

    def maximize_series(self, series: CorePropSchedule | CoreSeqSchedule):
        """Maximize the given series of variables (either a sequential or
        proportional series) until all the variables in the series are
        maximized.
        Tiny factor variables are caught, warned, and executed
        sequentially immediately following the equal-priority series.
        """
        maxed_vs: list[Trxn | TrxnGroup] = []
        deferred_vars: list[Trxn | TrxnGroup] = []  # Track variables with tiny proportion factors

        vars_list, factors = self._get_next_iter(series, maxed_vs)
        while vars_list:
            var_names = [v.id for v in vars_list]
            log(f'*** var_names, factors: {var_names}, {factors}')

            proportion_factors = {var_name: factors[i] for i, var_name in enumerate(var_names)}

            # 1. Identify and extract any variables with dangerously small factors
            has_tiny_factor = False
            for var_name, f in list(proportion_factors.items()):
                if 0 < f < 0.000001:
                    log(f"WARNING: Proportion factor for Variable {var_name} ({f:.8f}) is too small. "
                          f"Moving to sequential execution immediately after this series.")

                    var_obj = next((v for v in vars_list if v.id == var_name), None)
                    if var_obj and var_obj not in deferred_vars:
                        deferred_vars.append(var_obj)
                        maxed_vs.append(var_obj)  # Mark as "maxed" to drop from current prop iterations
                    has_tiny_factor = True

            # If any tiny factors were filtered out, re-evaluate the next iteration parameters
            if has_tiny_factor:
                vars_list, factors = self._get_next_iter(series, maxed_vs)
                continue

            factors_sum = sum(proportion_factors.values())

            # Deal with the edge case where every proportion_factor is zero.
            if factors_sum == 0:
                for var_name in proportion_factors:
                    self.engine.update_variable_bounds(var_name, lb=0)
                    self.cur_trxn_value[var_name] = 0
                    log(f' - initialized {var_name} to 0')
                break

            ## minimize all minus vars
            origional_ub = self._minimize_minus_vars(vars_list)

            # Solve
            var_values = self.engine.maximize_group_by_proportions(var_names, proportion_factors)

            # Update the variables. Set only the lb for now, since we might
            # be able to increase this variable further in a future iteration.
            for var_name, var_value in var_values.items():

                # Prevent floating-point noise from creating negative lower bounds
                if var_value < 1e-6:
                    var_value = 0.0

                '''# Added 6/9/2026
                # --- Floating-Point Relaxation Buffer ---
                # Subtract a microscopic buffer before locking the lower bound.
                # This prevents "Infeasible" errors caused by floating-point noise
                # when this variable is later constrained by strict equalities (=).
                #
                # The dynamic calculation balances two strict mathematical limits:
                # 1. Infinite Loop Prevention: We divide 1e-5 by len(var_names) so the
                #    total "poolable" buffer across all variables never exceeds 1e-5.
                #    If it exceeds our `_is_var_maxed` tolerance (1e-4), the solver
                #    will endlessly steal buffer to artificially grow single variables.
                # 2. Solver Zero-Tolerance: We use max(..., 1e-8) to ensure massive arrays
                #    (e.g., 1000+ variables) don't shrink the individual buffer below the
                #    solver's internal zero-tolerance limit, which would render it useless.
                else:
                    buffer = max((1e-5) / len(var_names), 1e-8)
                    var_value = var_value - buffer'''

                self.engine.update_variable_bounds(var_name, lb=var_value)
                self.cur_trxn_value[var_name] = var_value

                var_obj = next((v for v in vars_list if v.id == var_name), None) # i.e. get the first item in the list with the specified id
                reason = self._determine_reason(var_obj, var_value) if var_obj else "Proportional Allocation"
                self.cur_trxn_reasons[var_name] = reason

                log(f' - maxed {var_name} to {var_value} (Reason: {reason})')

            # Get a list of the variables that are now maximized.
            maxed_vs.extend(self._get_newly_maxed_vars(vars_list))

            # This function will check to see if the series can further be
            # maximized, possibly by dropping a constrained variable (in a
            # proportional series) or by replacing a constrained variable by the
            # next in line (for a sequential series). It returns the info needed
            # to continue on into another loop iteration.
            vars_list, factors = self._get_next_iter(series, maxed_vs)

            ## reset all minus vars
            self._reset_minus_vars(origional_ub)

        # 2. Process the tiny-factor variables sequentially right after the group closes
        for var_obj in deferred_vars:
            log(f"Processing deferred tiny-factor variable sequentially: {var_obj.id}")
            self._maximize_var(var_obj)


    def _get_newly_maxed_vars(self, vars: list[Trxn | TrxnGroup]):
        """Return a list of which of the given variables are now maximized."""
        return [v for v in vars if self._is_var_maxed(v)]

    def _is_var_maxed(self, var: Trxn | TrxnGroup):
        """
        Checks if the variable can be increased further.
        """
        obj_value, _ = self.engine.solve_objective([var.id], maximization=True)
        return isclose(self.cur_trxn_value[var.id], obj_value, abs_tol=SOLVER_TOL)

    def _get_next_iter(self, schedule: CorePropSchedule | CoreSeqSchedule, maxed_vars: list[Trxn | TrxnGroup]):
        """Returns two lists for the next iteration.
        If there are no remaining variables to maximize, returns two empty
        lists.
        """
        var_names: list[Trxn | TrxnGroup] = []
        factors: list[float] = []

        # If it is a sequential series, return the params for the next item.
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

        # If it is a proportional series, return the list of paths and a list
        # of each's proportion. If the proportional series has any sub-series,
        # this will involve identifying which variable(s) from the subseries
        # need to be considered and their factor(s).
        elif type(schedule) is CorePropSchedule:
            for x in schedule.series:
                item, factor = x.item, x.factor

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
        """Formats the solved values into the output structure."""
        vars_output: list[SolverOutputApportionment] = []

        for v in self.all_trxns:

            # Skip non-transaction objects (like TrxnGroups)
            if type(v) != Trxn:
                continue

            var_value = self.cur_trxn_value[v.id]
            reason = self.cur_trxn_reasons.get(v.id, "Unsolved / Unconstrained")

            # Check if this variable is a special single-hop stream-to-stream slack variable
            is_stream_slack = False

            natural_set = set([ZoneTypes.STREAM])

            if v.is_slack and len(v.path) == 1:

                flowobj = self.gm.get_flow_by_id(v.path[0].flow_id)

                # If this variable represents a slack variable for a stream to stream
                # connection,
                from_zone_type = self.gm.get_zone_by_id(flowobj.from_zone).type
                to_zone_type = self.gm.get_zone_by_id(flowobj.to_zone).type

                if from_zone_type == ZoneTypes.STREAM and to_zone_type == ZoneTypes.STREAM:
                    is_stream_slack = True

            if is_stream_slack:
                flowobj = self.gm.get_flow_by_id(v.path[0].flow_id)


                # Get the natural flow.
                natural_flow = self.dm.cur_flows_by_id[flowobj.id].natural
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

                    if var_value is None:
                        raise ValueError(f'value for {v.id} is None')
                    vars_output.append(SolverOutputApportionment(
                        date=date,
                        interzone_flow_id=a.flow_id,
                        txn_id=v.id,
                        value=var_value * a.remaining_factor * a.factor,
                        is_forward=a.factor > 0,
                        reason=reason
                    ))

        return vars_output


    def _determine_reason(self, var: Trxn | TrxnGroup, value: float) -> str:
        """Heuristically determines the limiting factor for a transaction's value."""
        if var is None:
            return "Unknown"

        # 2. Check if it hit the Water Right Upper Limit (Rule 2 & 3)
        upper_limit = self.tm.get_transaction_upper_limit(var, self.dm.cur_date)
        if upper_limit is not None and isclose(value, upper_limit, abs_tol=SOLVER_TOL):
            return "Water Right Limit"

        # 4. Rigorous Constraint Check
        if type(var) == Trxn:
            # A. Check Measurement Constraints (Destination Demand limitations)
            # Traces the path to see if any flow destination is fully satisfied
            for path_item in var.path:
                con_name = PREFIX_MEASURE + path_item.flow_id
                if self.engine.is_constraint_tight(con_name, var.id):
                    return f"No remaining demand at destination (Measured flow reached at '{path_item.flow_id}')"

            if len(var.path):
                # B. Check Natural Flow Constraints (Source Water limitations)
                first_item = var.path[0]
                first_flow = self.gm.get_flow_by_id(first_item.flow_id)
                from_zone = self.gm.get_zone_by_id(first_flow.from_zone)

                # Adjust if flowing backwards
                if first_item.factor < 0:
                    from_zone = self.gm.get_zone_by_id(first_flow.to_zone)

                if from_zone.type == ZoneTypes.STREAM:
                    # Check the immediate source zone
                    if self.engine.is_constraint_tight(PREFIX_NF_ZONE + from_zone.id, var.id):
                        return f"No remaining divertible Natural Flow at source '{from_zone.id}'"

                    # Check downstream zones (which can also bottleneck upstream diversions)
                    for f in self.gm.traverse_downstream(from_zone.id):
                        if self.engine.is_constraint_tight(PREFIX_NF_ZONE + f.to_zone, var.id):
                            return f"No remaining divertible Natural Flow at downstream reach '{f.to_zone}'"

        return "Other"


    def feasibility_fallback(self):

        self.feasibility_slacks = self.add_feasibility_vars(self.feasibility_slacks)

        self.engine.set_perminant_minus_var('FEAS_SUM')


    def add_feasibility_vars(self, feasibility_slacks) -> list[str]:
        """Update the constraints to include true slack variables that will
        ensure the system is feasible, even if we have floating point rounding
        issues that can occur from the equal priority scalling stuff.

        The side effect of adding these is that we have to minimize them before
        solving for variables.
        """

        #feasibility_slacks = []
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

        # Add vars to each constraint:
        for con_name in engine.get_constraint_names():
            if con_name == 'FEAS_TOTAL':
                continue
            add_feasibility_slack(con_name, 1)
            add_feasibility_slack(con_name, -1)

        return feasibility_slacks



#
#

def system_report_str(results:SolverOutput, day_idx:int, date:str, gm:GraphManager, dm:DailyDataManager, tm:TrxnManager) -> str:

    def warn_if_value_is_incorrect(path_item:TrxnPathItem, value:float|None):
        if path_item.expected_values is not None:
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

            storage_change =  dm.cur_storage_chg_by_id[n.id].measured # n.storage_chg

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
    """Check if each of the apportionment results match the expected value
    to 4 decimal places.

    If a values does not match what is expected, it will include
    the system report string.

    Skips apportionment results that don't have a defined expected value.

    Raises an exception if no apportionment results have an expected value.
    """

    message:str = ''

    cnt = 0
    for t in traverse_vars(input.txns):
        if type(t) == Trxn:
            for p in t.path:
                if p.expected_values is not None:
                    idx = 0
                    for date in loop_through_date_range(input.beg_date,
                            input.end_date):

                        expected_value = p.expected_values[idx]
                        computed_values = results.get_result_value(date=date,
                                trxn_id=t.id, flow_id=p.flow_id)

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

