'''

Update documentation
Add time-lags
Specified natural flows
Track storage balances, and allow paths to be limited by them.
Better audit tracing of results
Is there anything left in the gemini file that could be useful to me? Any constraints?

'''


from typing import Generator
from dataclasses import dataclass
from math import isclose
from collections.abc import Iterator

from .models import (
    AccountingGraph, AccountingLimit, CorePropSchedule, CorePropScheduleItem,
    CoreScheduleVariable, CoreSeqSchedule, CoreSeqScheduleItem,
    FlowComponentsTypes, InterzoneFlow,
    SolverInput, SolverOutput, SolverOutputApportionment,
    SolverOutputConstraintEvidence, SolverOutputSolveGroupEvidence,
    SolverOutputSolveStepEvidence,
    Trxn, TrxnGroup, TrxnPathItem, Zone, ZoneTypes
)

#from .solve_lp_with_GLOP import LPSolver, LPSolverError
from .solve_lp_with_SCIPY import LPSolver, LPSolverError
#from .solve_lp_with_HIGHSPY import LPSolver, LPSolverError

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
    solve_step_results = []
    solve_group_results = []

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
        apportioner.solve_phase = 'NATURAL_FLOW'
        apportioner.apply_nf_mass_balance_constraints()
        apportioner.calculate_apportionments(schedule)

        apportioner.solve_phase = 'SPILL_REALLOCATION'
        apportioner.remove_nf_mass_balance_constraints()
        apportioner.lock_spill_variables()
        apportioner.calculate_apportionments(schedule)

        # D. Finalize unconstrained (nonpath) vars
        apportioner.solve_for_nonpath_vars()


        # Collect results for this day
        apportionment_results.extend(apportioner.get_variables(date))
        solve_step_results.extend(apportioner.solve_steps)
        solve_group_results.extend(apportioner.solve_groups)

        #print( apportioner.engine.lp_string() )

    results = SolverOutput(
        apportionments=apportionment_results,
        solve_steps=solve_step_results,
        solve_groups=solve_group_results
    )

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

    def get_loss_route(self, zone_id: str) -> str:
        """Finds the dynamically designated physical loss route for a given zone."""
        candidates = []
        # Check outflows (e.g., from REACH to SYSTEM)
        for f in self.get_zone_outflows(zone_id):
            if f.residual_for_losses and self.get_zone_by_id(f.to_zone).type == ZoneTypes.SYSTEM_GAIN_LOSS:
                candidates.append(f)

        # Check inflows (e.g., bidirectional from SYSTEM to REACH)
        for f in self.get_zone_inflows(zone_id):
            if f.residual_for_losses and self.get_zone_by_id(f.from_zone).type == ZoneTypes.SYSTEM_GAIN_LOSS:
                candidates.append(f)

        if len(candidates) == 1:
            return candidates[0].id
        elif len(candidates) > 1:
            raise ValueError(f"Multiple loss routes found for zone {zone_id} with residual_for_losses=True")
        else:
            raise ValueError(f"No loss route found for zone {zone_id} with residual_for_losses=True")

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
                    if len(f.flow_measurements) == 0:
                        f.flow_type = FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE
                        f.residual_for_gains = True
                        f.residual_for_losses = True
                    if f.flow_type == FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE:
                        f.residual_for_gains = True
                        f.residual_for_losses = True


            # 2. Stream zones connected to a gain or loss zone.
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
        self.cur_flows_by_id = {f.id: CurFlowInfo() for f in self.gm.graph.interzone_flows} # initializes with zero terms

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

        def recursive_apply_impact_downstream(downstream_of_flow_id: str, impact: float, is_available=True):
            to_zone = self.gm.get_flow_by_id(downstream_of_flow_id).to_zone
            outflows = self.gm.get_zone_outflows(to_zone)
            for f in outflows:
                if (self.gm.get_zone_by_id(f.from_zone).type == ZoneTypes.STREAM and
                    self.gm.get_zone_by_id(f.to_zone).type == ZoneTypes.STREAM):

                    outflow_data = self.cur_flows_by_id[f.id]

                    # Deduct the losses from the impact moving through the stream
                    flow_in_channel = impact * (1 - f.loss_from_zone)
                    flow_reaching_end = flow_in_channel * (1 - f.loss_to_zone)

                    outflow_data.natural += flow_in_channel

                    # Don't allow the natural flow to be reduced below zero.
                    if outflow_data.natural < 0:
                        outflow_data.natural = 0

                    if is_available:
                        outflow_data.available_natural += flow_in_channel
                        # Don't allow the natural flow to be reduced below zero.
                        if outflow_data.available_natural < 0:
                            outflow_data.available_natural = 0

                    recursive_apply_impact_downstream(f.id, flow_reaching_end, is_available)

        # 1. First, look at the upstream boundaries with specified natural flow
        #    values. We want to propigate this natural flow downstream, but
        #    also store the fact that some portion of the flows have been
        #    utilized.
        for f_id in self.external_natural_flows:
            if f_id in self.cur_flows_by_id:

                flow_data = self.cur_flows_by_id[f_id]

                if flow_data.natural != 0:
                    f_obj = self.gm.get_flow_by_id(f_id)
                    flow_reaching_end = flow_data.natural * (1 - f_obj.loss_to_zone)
                    # Pass in False to indicate that some portion has been utilized.
                    recursive_apply_impact_downstream(f_id, flow_reaching_end, False)


        # 2. Now look at the local reach gains.
        #    Calculate the portion of the net gain/loss that is natural flow.
        #    Propigate this portion downstream.
        for f in self.gm.graph.interzone_flows:
            is_gain_to_stream =  (
                self.gm.get_zone_by_id(f.from_zone).type == ZoneTypes.SYSTEM_GAIN_LOSS and
                self.gm.get_zone_by_id(f.to_zone).type == ZoneTypes.STREAM)

            # For now, natural flow only enters the system along these flows.
            # NOTE: In the future I may want to have specified natural flows along other types of interzone-flows.
            if is_gain_to_stream:
                flow_data = self.cur_flows_by_id[f.id]
                net_flow = flow_data.measured

                to_zone_losses = self._get_calculated_losses(f.to_zone, self.cur_flows_by_id)


                flow_data.natural = net_flow + to_zone_losses
                flow_data.available_natural = flow_data.natural

                # Now route that flow downstream.
                f_gain = self.gm.get_flow_by_id(f.id)
                flow_reaching_end = flow_data.natural * (1 - f_gain.loss_to_zone)
                recursive_apply_impact_downstream(f.id, flow_reaching_end)


        print('\n' + '\n'.join([f'{key}:{val}' for key, val in self.cur_flows_by_id.items()]))


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

    def _get_calculated_losses(self, zone_id:str, cur_flows_by_id:dict[str, CurFlowInfo]) -> float:
        """Sum up the calculated losses for the given zone. While fractions
        are specified on the interzone-flows, the loss actually occurs at the
        to- or from-zone"""

        total_loss = 0

        # 1st look for inflows that have a to_zone loss.
        for f in self.gm.get_zone_inflows(zone_id):
            if f.loss_to_zone != 0:
                total_loss += cur_flows_by_id[f.id].measured * f.loss_to_zone

        # 2nd look for outflow that have a from_zone loss.
        for f in self.gm.get_zone_outflows(zone_id):
            if f.loss_from_zone != 0:
                total_loss += cur_flows_by_id[f.id].measured * f.loss_from_zone / (1 - f.loss_from_zone)

        return total_loss



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

            if f.flow_type == FlowComponentsTypes.OBSERVATION:
                for fm in f.flow_measurements:
                    val = self._get_flow_from_meas(fm.measurement_id, date, int(lag))
                    if val is None:
                        if COALESCE_MISSING_FLOWS_TO_ZERO:
                            val = 0
                        else:
                            raise ValueError(f"Measurement {fm.measurement_id} undefined on {date}")
                    total_measured += val * fm.adjustment_factor

            total_flow_by_id[f.id] = total_measured


        # 2. Calculate Residuals
        for zone_id, calcs in self._determine_residual_calc_order():
            # RESIDUAL = [MEASURED OUTFLOWS] - [MEASURED INFLOWS]
            # [UNMEASURED INFLOW] - [UNMEASURED OUTFLOW] - CALCULATED LOSSES = RESIDUAL
            #
            residual_flow = sum(total_flow_by_id[f.id] for f in self.gm.get_zone_outflows(zone_id))
            storage_chg = self._get_storage_change(self.gm.get_zone_by_id(zone_id), date)
            if storage_chg is not None:
                residual_flow += storage_chg
            residual_flow -= sum(total_flow_by_id[f.id] for f in self.gm.get_zone_inflows(zone_id))

            # Now assign the residual flow to the the interzone-flow term(s)
            for f in calcs:

                factor = 0
                if f.to_zone == zone_id:
                    factor = 1
                elif f.from_zone == zone_id:
                    factor = -1
                else:
                    raise ValueError('Unexpected error - Cannot set the direction factor...')

                if residual_flow >= 0 and f.residual_for_gains:
                    total_flow_by_id[f.id] = residual_flow * factor

                if residual_flow < 0 and f.residual_for_losses:
                    total_flow_by_id[f.id] = residual_flow * factor

                print(f'RES: {f.id} = {total_flow_by_id[f.id]} .... l:{f.residual_for_losses} g:{f.residual_for_gains}')


        # 3. Checks
        for f in self.gm.graph.interzone_flows:
            #continue
            if total_flow_by_id[f.id] < 0 and not f.bidirectional:

                # TODO - This code may be related to the failing test - if I add the following condition then that test passes, but others fail.
                if f.flow_type != FlowComponentsTypes.OBSERVATION:
                    continue
                print(f'NEG! {f.from_zone}->{f.to_zone} or {f.id} = {total_flow_by_id[f.id]} ')


                if COALESCE_NEGATIVE_FLOWS_TO_ZERO:
                    total_flow_by_id[f.id] = 0
                else:
                    raise ValueError(f"Net flow negative for {f.id} on {date}")

        return total_flow_by_id

    def _determine_residual_calc_order(self) -> list[tuple[str, list[InterzoneFlow]]]:
        """In order to obtain the values for Calculated Flows, we first need
        to know the order that these flows must be calculated in, since one
        calculation may depend on another. This is what this function
        provides."""

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
            if f.flow_type == FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE:
                add_flow_balance_calc(f.to_zone, f.from_zone, f)
            elif f.flow_type == FlowComponentsTypes.FLOW_BALANCE_OF_SOURCE_ZONE:
                add_flow_balance_calc(f.from_zone, f.to_zone, f)

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

        #log(f"TRXN UPPER LIMIT: {t.id} = {upper_limit}")
        return upper_limit

    def get_minus_vars(self, vars: list[Trxn | TrxnGroup]) -> list[Trxn]:

        def get_from2(v:Trxn) -> tuple[Zone, InterzoneFlow]:
            first_item = v.path[0]
            f0 = self.gm.get_flow_by_id(first_item.flow_id)
            if first_item.factor >= 0:
                from_zone = self.gm.get_zone_by_id(f0.from_zone)
            else:
                from_zone = self.gm.get_zone_by_id(f0.to_zone)
            return from_zone, f0

        def get_from(v: Trxn) -> tuple[Zone, InterzoneFlow]:
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

                    if from_flow.bidirectional:
                        for trxn, path_item in self.lookup_flow_trxns[from_flow.id]:
                            ordered_trxn = self.ordered_paths.get(trxn.id, [])
                            # Use len(ordered_trxn) to ensure loss expansions don't hide the slack status
                            if trxn.is_slack and len(ordered_trxn) == 1:
                                if get_to(trxn)[0] == from_zone:
                                    output.append(trxn)

                    # If the variable ends at a storage zone, we need to look
                    # for slack variables flowing from that storage zone.
                    to_zone, to_flow = get_to(v)
                    if to_flow.bidirectional:
                        for trxn, path_item in self.lookup_flow_trxns[to_flow.id]:
                            if get_from(trxn)[0] == to_zone:
                                output.append(trxn)
        return output

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
        self.cur_trxn_solve_step_ids: dict[str, list[str]] = {}
        self.solve_steps: list[SolverOutputSolveStepEvidence] = []
        self.solve_groups: list[SolverOutputSolveGroupEvidence] = []
        self.solve_phase = 'UNSPECIFIED'
        self._solve_step_count = 0
        self._solve_group_count = 0
        self.all_trxns = tm.all_trxns
        self.engine = self._build_linear_equations()

        self.feasibility_slacks:list[str] = []


    def _build_linear_equations(self) -> LPSolver:
        """Add the variables and constraints to the linear solver engine using a variable-per-path-item approach."""
        engine = LPSolver(tolerance=SOLVER_TOL)

        # Add Variables
        for trxn in self.all_trxns:
            if type(trxn) == Trxn:
                lb = None if trxn.lower_limit < 0 else 0
                for x in trxn.path:
                    var_name = f"{trxn.id}___{x.flow_id}"
                    engine.add_variable(name=var_name, lb=lb, ub=None)
            elif type(trxn) == TrxnGroup:
                lb = None if trxn.lower_limit < 0 else 0
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

                # 2. Path Continuity Constraints (Mass Balance pushing downstream)
                for i in range(len(ordered_path) - 1):
                    leg1 = ordered_path[i]
                    leg2 = ordered_path[i+1]
                    v1 = f"{trxn.id}___{leg1.flow_id}"
                    v2 = f"{trxn.id}___{leg2.flow_id}"
                    con_name = f"{PREFIX_CONT}{trxn.id}_{i}"

                    engine.add_constriant(name=con_name, lb=0, ub=0)

                    f1 = self.gm.get_flow_by_id(leg1.flow_id)
                    f2 = self.gm.get_flow_by_id(leg2.flow_id)

                    # Account for reverse flow directions!
                    l1_exit = f1.loss_to_zone if leg1.factor > 0 else f1.loss_from_zone
                    l2_enter = f2.loss_from_zone if leg2.factor > 0 else f2.loss_to_zone
                    # TODO - Do we need to account for reverse flow direction???

                    rem_factor = (1 - f1.loss_to_zone) * (1 - leg1.loss_after) * (1 - f2.loss_from_zone) * (1 - leg2.loss_before)

                    engine.set_coeficient(con_name, v2, 1.0)
                    engine.set_coeficient(con_name, v1, -rem_factor)

                # 3. Natural Flow Constraints (only tied to anchor variables)
                if not trxn.is_slack and len(ordered_path) > 0:
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
        """Updates the limits on variables and measurements for the current day."""
        # Update Variable Limits
        for trxn in self.all_trxns:
            upper_limit = self.tm.get_transaction_upper_limit(trxn, self.dm.cur_date)

            if type(trxn) == Trxn:
                if trxn.is_slack:
                    upper_limit = None

                anchor_var = self.tm.get_anchor_var(trxn)
                if anchor_var:
                    # Apply upper_limit strictly to the anchor variable
                    self.engine.update_variable_bounds(anchor_var, lb=trxn.lower_limit, ub=upper_limit)

                # Loop through all paths (including mathematically derived loss branches)
                for path_item in trxn.path:
                    var_name = f"{trxn.id}___{path_item.flow_id}"
                    if var_name != anchor_var:
                        dlb = None if getattr(trxn, 'lower_limit', 0) < 0 else 0
                        self.engine.update_variable_bounds(var_name, lb=dlb, ub=None)

            elif type(trxn) == TrxnGroup:
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
                        val = self.dm.cur_flows_by_id[f.id].available_natural
                        nf_available += val * (1 - f.loss_to_zone)

                print(f'reach {z.id} NF = {nf_available}')

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
        anchor_spill_vars = [self.tm.get_anchor_var(t) for t in spill_vars if self.tm.get_anchor_var(t)]
        if anchor_spill_vars:
            obj_value, solved_values = self.engine.solve_objective(anchor_spill_vars, maximization=False)

        # Lock the spill vars to their min value.
            con_name = 'lock-slacks'
            self.engine.add_constriant(name=con_name, lb=0, ub=obj_value)
            for var_name in anchor_spill_vars:
                self.engine.set_coeficient(con_name, var_name, 1)

    def calculate_apportionments(self,
                                 schedule: CoreSeqSchedule,
                                 start_priority=None,
                                 stop_priority=None,
                                 report_priority: float | None = None):
        """Solves the variables in the order defined by the schedule."""


        for x in schedule.series:
            priority = x.priority
            item = x.item

            if start_priority is not None and start_priority > priority: continue
            if stop_priority is not None and stop_priority < priority: continue
            if priority >= SLACK_TRXN_PRIORITY: return

            evidence_priority = report_priority if report_priority is not None else priority

            log(f"\nPriority: {priority}")

            try:
                if type(item) is CorePropSchedule:
                    self.maximize_series(item, priority=evidence_priority)
                elif type(item) is CoreSeqSchedule:
                    self.calculate_apportionments(
                        item,
                        start_priority,
                        stop_priority,
                        report_priority=evidence_priority
                    )
                elif type(item) is CoreScheduleVariable:
                    self._maximize_var(item.var, priority=evidence_priority)


            except LPSolverError:
                print('Solver failed! Adding feasibility slacks...')

                self.feasibility_fallback()

                if type(item) is CorePropSchedule:
                    self.maximize_series(item, priority=evidence_priority)
                elif type(item) is CoreSeqSchedule:
                    self.calculate_apportionments(
                        item,
                        start_priority,
                        stop_priority,
                        report_priority=evidence_priority
                    )
                elif type(item) is CoreScheduleVariable:
                    self._maximize_var(item.var, priority=evidence_priority)

            log(f"\nCompleted iteration for priority: {priority}")

    def solve_for_nonpath_vars(self):
        # NOTE: I get away with not bothering to seperate the non-path variables
        #       from the path variables in this code because the path variables
        #       have already been maximized and updated so they can not be less
        #       than their max values.
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
            log(f' - maxed {var_name} to {solved_value}')

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

    def _maximize_var(self, var: Trxn | TrxnGroup, priority: float):
        """My earlier version of this function used _minimize_minus_vars and
        _reset_minus_vars to prevent apportionments from forcing a reservoir
        spill to increase the divertible natural flow."""
        if type(var) == Trxn:
            target_var = self.tm.get_anchor_var(var)
        else:
            target_var = var.id

        if not target_var:
            return

        origional_ub = self._minimize_minus_vars([var])
        value_before = self.cur_trxn_value.get(target_var, 0.0)
        solve_group_id = self._next_solve_group_id()
        new_value = self.engine.maximize_and_update_variable(target_var)
        self.cur_trxn_value[target_var] = new_value

        solve_step = self._record_solve_step(
            var=var,
            target_var=target_var,
            value_before=value_before,
            value_after=new_value,
            objective='MAXIMIZE_TRANSACTION',
            priority=priority,
            solve_group_id=solve_group_id
        )
        solve_group = self._record_solve_group(
            solve_group_id=solve_group_id,
            priority=priority,
            objective='MAXIMIZE_TRANSACTION',
            member_steps=[solve_step],
            limiting_txn_ids=[var.id]
        )
        reason = solve_group.reason or "No single limiting constraint identified"
        self.cur_trxn_reasons[var.id] = reason

        log(f' - maxed {target_var} to {new_value} (Reason: {reason})')
        self._reset_minus_vars(origional_ub)

    def maximize_series(self, series: CorePropSchedule | CoreSeqSchedule, priority: float):
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
            var_names = []
            vars_by_name = {}
            for v in vars_list:
                if type(v) == Trxn:
                    var_name = self.tm.get_anchor_var(v)
                else:
                    var_name = v.id

                if var_name:
                    var_names.append(var_name)
                    vars_by_name[var_name] = v

            proportion_factors = {var_names[i]: factors[i] for i in range(len(var_names))}

            has_tiny_factor = False
            for var_name, f in list(proportion_factors.items()):
                if 0 < f < 0.000001:
                    log(f"WARNING: Proportion factor for Variable {var_name} ({f:.8f}) is too small. "
                        f"Moving to sequential execution immediately after this series.")
                    # Match string back to var obj
                    var_obj = vars_by_name.get(var_name)
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
            values_before = {
                var_name: self.cur_trxn_value.get(var_name, 0.0)
                for var_name in var_names
            }

            # Solve
            solve_group_id = self._next_solve_group_id()
            var_values = self.engine.maximize_group_by_proportions(
                var_names,
                proportion_factors
            )
            member_steps = []

            # Update the variables. Set only the lb for now, since we might
            # be able to increase this variable further in a future iteration.
            for var_name, var_value in var_values.items():

                # Prevent floating-point noise from creating negative lower bounds
                # Use abs() to prevent zeroing out valid negative flows!
                if abs(var_value) < SOLVER_TOL:
                    var_value = 0.0

                self.engine.update_variable_bounds(var_name, lb=var_value)
                self.cur_trxn_value[var_name] = var_value

                var_obj = vars_by_name.get(var_name)
                if var_obj:
                    member_steps.append(self._record_solve_step(
                        var=var_obj,
                        target_var=var_name,
                        value_before=values_before[var_name],
                        value_after=var_value,
                        objective='MAXIMIZE_PROPORTIONAL_GROUP',
                        priority=priority,
                        proportion_factor=proportion_factors[var_name],
                        solve_group_id=solve_group_id
                    ))

            # Identify every member that can no longer increase. This uses
            # batched objectives so one solve can classify many variables.
            newly_maxed = self._get_newly_maxed_vars(vars_list)

            limiting_txn_ids = [v.id for v in newly_maxed]

            solve_group = self._record_solve_group(
                solve_group_id=solve_group_id,
                priority=priority,
                objective='MAXIMIZE_PROPORTIONAL_GROUP',
                member_steps=member_steps,
                limiting_txn_ids=limiting_txn_ids
            )
            reason = solve_group.reason or "Equal-priority proportional allocation"
            for solve_step in member_steps:
                self.cur_trxn_reasons[solve_step.txn_id] = reason
                log(f' - maxed {solve_step.target_variable} to {solve_step.value_after} (Reason: {reason})')

            # Get a list of the variables that are now maximized.
            maxed_vs.extend(newly_maxed)

            # This function will check to see if the series can further be
            # maximized, possibly by dropping a constrained variable (in a
            # proportional series) or by replacing a constrained variable by the
            # next in line (for a sequential series). It returns the info needed
            # to continue on into another loop iteration.
            old_cnt = len(vars_list)
            vars_list, factors = self._get_next_iter(series, maxed_vs)
            new_cnt = len(vars_list)
            if new_cnt >= old_cnt:
                raise RuntimeError('Circular loop detected! Problem identifying any equal-priority variables to drop!')

            ## reset all minus vars
            self._reset_minus_vars(origional_ub)

        # 2. Process the tiny-factor variables sequentially right after the group closes
        for var_obj in deferred_vars:
            log(f"Processing deferred tiny-factor variable sequentially: {var_obj.id}")
            self._maximize_var(var_obj, priority=priority)


    def _get_newly_maxed_vars(self, vars: list[Trxn | TrxnGroup]):
        """Return the variables that cannot be increased further.

        The active variables have already been fixed at their current values by
        lower bounds. Maximizing all unresolved variables together can therefore
        identify multiple non-maxed variables in one solve. If none of the
        unresolved variables increases, every remaining variable is maxed.
        """

        #Old Code:
        #return [v for v in vars if self._is_var_maxed(v)]


        maxed_ids: set[str] = set()
        remaining: dict[str, Trxn | TrxnGroup] = {}
        current_values: dict[str, float] = {}

        for var in vars:
            if type(var) == Trxn:
                target_var = self.tm.get_anchor_var(var)
            else:
                target_var = var.id

            if not target_var:
                maxed_ids.add(var.id)
                continue

            current_value = self.cur_trxn_value[target_var]
            _, upper_bound = self.engine.get_variable_bounds(target_var)

            if (
                upper_bound != float('inf')
                and isclose(current_value, upper_bound, abs_tol=SOLVER_TOL)
            ):
                maxed_ids.add(var.id)
                continue

            remaining[target_var] = var
            current_values[target_var] = current_value

        while remaining:
            target_vars = list(remaining.keys())
            _, solved_values = self.engine.solve_objective(
                target_vars,
                maximization=True
            )

            increasable_vars = [
                target_var
                for target_var in target_vars
                if solved_values[target_var]
                    > current_values[target_var] + SOLVER_TOL
            ]

            if not increasable_vars:
                maxed_ids.update(var.id for var in remaining.values())
                break

            # A variable that increased in this feasible solution is known not
            # to be maxed. Remove all such variables and test the unresolved
            # remainder together in the next batch.
            for target_var in increasable_vars:
                del remaining[target_var]

        return [var for var in vars if var.id in maxed_ids]

    def _is_var_maxed(self, var: Trxn | TrxnGroup):
        """
        Checks if the variable can be increased further.
        8/1/2026 - no longer used
        """
        if type(var) == Trxn:
            target_var = self.tm.get_anchor_var(var)
        else:
            target_var = var.id
        if not target_var: return True
        _, solved_values = self.engine.solve_objective([target_var], maximization=True)
        max_value = solved_values[target_var]
        return isclose(self.cur_trxn_value.get(target_var, 0), max_value, abs_tol=SOLVER_TOL)

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

            reason = self.cur_trxn_reasons.get(v.id, "Unsolved / Unconstrained")
            solve_step_ids = list(self.cur_trxn_solve_step_ids.get(v.id, []))

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
                        reason=reason,
                        solve_step_ids=solve_step_ids
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
                        reason=reason,
                        solve_step_ids=solve_step_ids
                    ))

        return vars_output

    def _next_solve_group_id(self) -> str:
        self._solve_group_count += 1
        return f"{self.dm.cur_date}:SOLVE:{self._solve_group_count}"

    def _record_solve_step(self,
                           var: Trxn | TrxnGroup,
                           target_var: str,
                           value_before: float,
                           value_after: float,
                           objective: str,
                           priority: float,
                           solve_group_id: str,
                           proportion_factor: float | None = None
                           ) -> SolverOutputSolveStepEvidence:
        constraints = []
        evidence_variable_names = [target_var]

        # A TrxnGroup variable only participates directly in its parent accounting
        # equality. Include the descendant transaction variables so the solve-group
        # explanation can reach the physical measurement and natural-flow rows that
        # actually limited the group allocation.
        if type(var) == TrxnGroup:
            def add_descendant_variables(group: TrxnGroup):
                for child in group.children_trxns:
                    if type(child) == Trxn:
                        anchor_var = self.tm.get_anchor_var(child)
                        if anchor_var:
                            evidence_variable_names.append(anchor_var)
                    elif type(child) == TrxnGroup:
                        add_descendant_variables(child)

            add_descendant_variables(var)

        seen_constraints = set()
        for evidence_variable_name in evidence_variable_names:
            for x in self.engine.get_last_solve_constraint_evidence(
                    evidence_variable_name, SOLVER_TOL):
                if x['constraint_name'] in seen_constraints:
                    continue
                seen_constraints.add(x['constraint_name'])
                constraints.append(SolverOutputConstraintEvidence(
                    constraint_name=x['constraint_name'],
                    constraint_type=self._get_constraint_type(x['constraint_name']),
                    coefficient=x['coefficient'],
                    activity=x['activity'],
                    lower_bound=x['lower_bound'],
                    upper_bound=x['upper_bound'],
                    lower_slack=x['lower_slack'],
                    upper_slack=x['upper_slack'],
                    is_tight=x['is_tight'],
                    blocks_direct_increase=x['blocks_direct_increase'],
                    dual_value=x['dual_value']
                ))

        upper_limit = self.tm.get_transaction_upper_limit(var, self.dm.cur_date)
        upper_limit_reached = (
            upper_limit is not None and
            isclose(value_after, upper_limit, abs_tol=SOLVER_TOL)
        )

        self._solve_step_count += 1
        step_id = f"{self.dm.cur_date}:{self._solve_step_count}"
        solve_step = SolverOutputSolveStepEvidence(
            step_id=step_id,
            solve_group_id=solve_group_id,
            date=self.dm.cur_date,
            phase=self.solve_phase,
            priority=priority,
            txn_id=var.id,
            target_variable=target_var,
            objective=objective,
            value_before=value_before,
            value_after=value_after,
            upper_limit=upper_limit,
            upper_limit_reached=upper_limit_reached,
            proportion_factor=proportion_factor,
            reduced_cost=self.engine.get_last_variable_reduced_cost(target_var),
            constraints=constraints
        )

        self.solve_steps.append(solve_step)
        if var.id not in self.cur_trxn_solve_step_ids:
            self.cur_trxn_solve_step_ids[var.id] = []
        self.cur_trxn_solve_step_ids[var.id].append(step_id)

        return solve_step

    def _record_solve_group(self,
                            solve_group_id: str,
                            priority: float,
                            objective: str,
                            member_steps: list[SolverOutputSolveStepEvidence],
                            limiting_txn_ids: list[str]
                            ) -> SolverOutputSolveGroupEvidence:
        solve_group = SolverOutputSolveGroupEvidence(
            solve_group_id=solve_group_id,
            date=self.dm.cur_date,
            phase=self.solve_phase,
            priority=priority,
            objective=objective,
            member_step_ids=[x.step_id for x in member_steps],
            member_txn_ids=[x.txn_id for x in member_steps],
            reason=self._determine_solve_group_reason(
                member_steps,
                limiting_txn_ids
            )
        )
        self.solve_groups.append(solve_group)
        return solve_group

    def _determine_solve_group_reason(
            self,
            member_steps: list[SolverOutputSolveStepEvidence],
            limiting_txn_ids: list[str]
            ) -> str:
        if not member_steps:
            return "No solve members were recorded"

        limiting_ids = set(limiting_txn_ids)
        limiting_steps = [
            step for step in member_steps
            if step.txn_id in limiting_ids
        ]
        if not limiting_steps:
            limiting_steps = member_steps

        reasons = []
        limit_members = [
            step.txn_id for step in limiting_steps
            if step.upper_limit_reached
        ]
        if limit_members:
            if len(limit_members) == len(limiting_steps):
                reasons.append("All limiting transactions reached their upper limits")
            else:
                reasons.append("Upper limit reached by " + ", ".join(limit_members))

        ignored_types = {'PATH_CONTINUITY', 'PROPORTIONAL_ALLOCATION'}
        selected_blockers = {}

        for step in limiting_steps:
            var_obj = next(
                (v for v in self.all_trxns if v.id == step.txn_id),
                None
            )
            own_group_constraint = (
                PREFIX_PARENT + step.txn_id
                if type(var_obj) == TrxnGroup
                else None
            )

            eligible = [
                constraint for constraint in step.constraints
                if constraint.blocks_direct_increase
                and constraint.constraint_type not in ignored_types
                and constraint.constraint_name != own_group_constraint
            ]

            # When a child transaction is limited by an already apportioned
            # transaction group, that inherited group allocation is the immediate
            # reason this solve stopped. Measurement and natural-flow constraints
            # explain the earlier parent-group solve and should not be repeated here.
            parent_group_blockers = [
                constraint for constraint in eligible
                if constraint.constraint_type == 'GROUP_LIMIT'
            ]
            if parent_group_blockers:
                eligible = parent_group_blockers

            dual_supported = [
                constraint for constraint in eligible
                if constraint.dual_value is not None
                and abs(constraint.dual_value) > SOLVER_TOL
            ]
            blockers = dual_supported if dual_supported else eligible

            for constraint in blockers:
                selected_blockers[constraint.constraint_name] = constraint

        descriptions = [
            self._constraint_reason_text(constraint)
            for constraint in selected_blockers.values()
        ]
        descriptions = list(dict.fromkeys(descriptions))
        if descriptions:
            reasons.append(
                "Group increase was limited by "
                + "; ".join(descriptions[:3])
            )

        if reasons:
            return ". ".join(reasons)

        if member_steps[0].objective == 'MAXIMIZE_PROPORTIONAL_GROUP':
            return ("Equal-priority proportional increment reached its maximum "
                    "feasible value; no single physical limiting constraint was identified")

        return "No single limiting constraint was identified"

    def _constraint_reason_text(self,
                                constraint: SolverOutputConstraintEvidence
                                ) -> str:
        if constraint.constraint_type == 'MEASUREMENT':
            return f"Measured flow constraint '{constraint.constraint_name[len(PREFIX_MEASURE):]}'"
        if constraint.constraint_type == 'NATURAL_FLOW':
            return f"Natural flow availability at '{constraint.constraint_name[len(PREFIX_NF_ZONE):]}'"
        if constraint.constraint_type == 'GROUP_LIMIT':
            return f"Transaction group limit '{constraint.constraint_name[len(PREFIX_PARENT):]}'"
        if constraint.constraint_type == 'SPILL_LOCK':
            return "Locked minimum spill"
        if constraint.constraint_type == 'FEASIBILITY':
            return f"Feasibility adjustment '{constraint.constraint_name}'"
        return f"Constraint '{constraint.constraint_name}'"

    def _get_constraint_type(self, constraint_name: str) -> str:
        if constraint_name.startswith(PREFIX_MEASURE):
            return 'MEASUREMENT'
        if constraint_name.startswith(PREFIX_NF_ZONE):
            return 'NATURAL_FLOW'
        if constraint_name.startswith(PREFIX_PARENT):
            return 'GROUP_LIMIT'
        if constraint_name.startswith(PREFIX_CONT):
            return 'PATH_CONTINUITY'
        if constraint_name.startswith('combined_'):
            return 'PROPORTIONAL_ALLOCATION'
        if constraint_name == 'lock-slacks':
            return 'SPILL_LOCK'
        if constraint_name.startswith('FEAS_'):
            return 'FEASIBILITY'
        return 'OTHER'


    def feasibility_fallback(self) -> float:
        """
        The intended fallback hierarchy is:
        1. minimize total constraint violation;
        2. among solutions with minimum violation, maximize the current transaction.
        """

        self.feasibility_slacks = self.add_feasibility_vars(self.feasibility_slacks)

        # A previous fallback may already have fixed FEAS_SUM. Release that lock
        # before finding the minimum feasible violation under the current bounds.
        self.engine.update_variable_bounds( 'FEAS_SUM', lb=0, ub=float('inf') )

        _, solved_values = self.engine.solve_objective(
            ['FEAS_SUM'],
            maximization=False
        )
        minimum_feasibility = solved_values['FEAS_SUM']

        # Lock the minimum violation before returning to the requested
        # transaction objective. This makes the fallback lexicographic:
        # first minimize constraint violations, then optimize apportionment.
        self.engine.update_variable_bounds(
            'FEAS_SUM',
            lb=minimum_feasibility,
            ub=minimum_feasibility
        )

        return minimum_feasibility


    def add_feasibility_vars(self, feasibility_slacks) -> list[str]:
        """Update the constraints to include true slack variables that will
        ensure the system is feasible, even if we have floating point rounding
        issues that can occur from the equal priority scalling stuff.

        The side effect of adding these is that we have to minimize them before
        solving for variables.
        """

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

def print_solve_steps(results: SolverOutput, constraint_mode: str = 'blocking'):
    """Print solve evidence grouped by the LP solve that produced it.

    constraint_mode may be 'blocking', 'tight', 'all', or 'none'.
    """
    from math import isinf

    valid_modes = {'blocking', 'tight', 'all', 'none'}
    if constraint_mode not in valid_modes:
        raise ValueError(f"constraint_mode must be one of {sorted(valid_modes)}")

    def fmt(value, width=11):
        if value is None:
            text = '-'
        elif isinstance(value, float):
            if isinf(value):
                text = 'inf' if value > 0 else '-inf'
            else:
                text = f'{value:.3f}'
        else:
            text = str(value)
        return f'{text:>{width}}'

    steps_by_id = {x.step_id: x for x in results.solve_steps}
    if not results.solve_groups:
        print('No solve-group evidence was recorded.')
        return

    line = '=' * 134
    for solve_number, group in enumerate(results.solve_groups, start=1):
        steps = [steps_by_id[x] for x in group.member_step_ids if x in steps_by_id]
        solve_type = ('EQUAL-PRIORITY PROPORTIONAL SOLVE'
                      if group.objective == 'MAXIMIZE_PROPORTIONAL_GROUP'
                      else 'TRANSACTION SOLVE')

        print()
        print(line)
        print(f'Solve:     {solve_number}')
        print(f'Group ID:  {group.solve_group_id}')
        print(f'Type:      {solve_type}')
        print(f'Date:      {group.date}')
        print(f'Phase:     {group.phase}')
        print(f'Priority:  {group.priority}')
        print(f'Objective: {group.objective}')
        print(f'Reason:    {group.reason or "-"}')
        if len(steps) > 1:
            print('Members:   ' + ', '.join(x.txn_id for x in steps))

        print()
        header = (
            f"{'Record':22}"
            f"{'Transaction':25}"
            f"{'Priority':>12}"
            f"{'Before':>11}"
            f"{'After':>11}"
            f"{'Increase':>11}"
            f"{'Factor':>11}"
            f"{'Limit':>11}"
            f"{'At limit':>10}"
        )
        print(header)
        print('-' * len(header))

        for step in steps:
            print(
                f'{step.step_id[:22]:22}'
                f'{step.txn_id[:25]:25}'
                f'{fmt(step.priority, 12)}'
                f'{fmt(step.value_before)}'
                f'{fmt(step.value_after)}'
                f'{fmt(step.value_after - step.value_before)}'
                f'{fmt(step.proportion_factor)}'
                f'{fmt(step.upper_limit)}'
                f"{('Y' if step.upper_limit_reached else ''):>10}"
            )

        if constraint_mode == 'none':
            continue

        for step in steps:
            if constraint_mode == 'blocking':
                constraints = [x for x in step.constraints if x.blocks_direct_increase]
            elif constraint_mode == 'tight':
                constraints = [x for x in step.constraints if x.is_tight]
            else:
                constraints = list(step.constraints)

            if not constraints:
                continue

            print()
            print(f'Constraint evidence for {step.txn_id} ({step.step_id})')
            constraint_header = (
                f"{'Type':20}"
                f"{'Constraint':37}"
                f"{'Coef':>9}"
                f"{'Activity':>11}"
                f"{'LB':>11}"
                f"{'UB':>11}"
                f"{'Dual':>11}"
                f"{'Tight':>8}"
                f"{'Blocks':>9}"
            )
            print(constraint_header)
            print('-' * len(constraint_header))

            for constraint in constraints:
                print(
                    f'{constraint.constraint_type[:20]:20}'
                    f'{constraint.constraint_name[:37]:37}'
                    f'{fmt(constraint.coefficient, 9)}'
                    f'{fmt(constraint.activity)}'
                    f'{fmt(constraint.lower_bound)}'
                    f'{fmt(constraint.upper_bound)}'
                    f'{fmt(constraint.dual_value)}'
                    f"{('Y' if constraint.is_tight else ''):>8}"
                    f"{('Y' if constraint.blocks_direct_increase else ''):>9}"
                )


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
                            if abs(expected_value - computed_value) >= SOLVER_TOL:
                                msg = (message +
                                    f'Var "{t.id}": computed ({computed_value}) != ' +
                                    f'expected ({expected_value}) on {date}\n' +
                                    (system_report_str(results, idx, date, gm, dm, tm) if input is not None else '')
                                )
                                raise AssertionError(msg)
                        idx += 1
    if cnt == 0:
        raise Exception('No trxn path-items were given an expected_value!')

