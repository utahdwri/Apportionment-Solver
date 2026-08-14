from copy import deepcopy
from typing import Generator
import logging
from .models import (
    SolverInput, SolverOutput, Trxn, TrxnPathItem, ZoneTypes
)
from .graph_manager import GraphManager
from .natural_flow_calculator import NaturalFlowCalculator
from .timeseries_manager import DailyDataManager
from .trxn_schedule import TrxnSchedule
from .apportioner import Apportioner
from .lp_solver import SolverBackend, resolve_solver_backend
from .lag_utils import unlag_apportionments

logger = logging.getLogger(__name__)



# --- Public API ---
def solve(
    input: SolverInput,
    *,
    check_expected_values: bool = False,
    solver_backend: SolverBackend | str = SolverBackend.AUTO,
    max_daily_apportionment: float | None = None,
) -> SolverOutput:
    """Build and solve the apportionment model.

    ``solver_backend`` may be ``"auto"``, ``"highspy"``, ``"glop"``, or
    ``"scipy"``. Automatic selection prefers native HiGHS, then GLOP, then
    SciPy's HiGHS interface. An explicitly requested unavailable backend raises
    an error rather than silently using a different implementation.
    """
    resolved_backend = resolve_solver_backend(solver_backend)
    logger.info("Using LP backend: %s", resolved_backend.name.value)

    apportionment_results = []
    apportionments_audit = []

    # 1. Initialize Network Topology
    graph_manager = GraphManager(deepcopy(input.accounting_graph))


    # 2. Initialize daily data and natural-flow services.
    natural_flow_calculator = NaturalFlowCalculator(graph_manager)

    data_manager = DailyDataManager(
        graph_manager,
        input.measurements,
        input.external_natural_flows,
    )

    # 3.
    trxn_manager = TrxnSchedule(graph_manager, input.txns, max_daily_apportionment)

    # 4. Run for each day.
    for date in _loop_through_date_range(input.beg_date, input.end_date):
        logger.info(f'Starting {date} ...')

        # A. Setup the state for the day
        data_manager.set_day(date)

        #
        apportioner = Apportioner(
            graph_manager,
            trxn_manager,
            data_manager,
            natural_flow_calculator,
            lp_solver_factory=resolved_backend.factory,
        )

        # B. Update Daily Bounds
        apportioner.update_daily_bounds()



        # C. Rebuild Schedule & Solve
        schedule = trxn_manager.build_schedule(date)
        logger.debug(f"\nSchedule: {schedule}")

        # Solve sequentially with and without NF mass balance limits
        #apportioner.solve_phase = 'NATURAL_FLOW'
        apportioner.apply_nf_mass_balance_constraints(date)
        apportioner.calculate_apportionments(schedule)

        #apportioner.solve_phase = 'SPILL_REALLOCATION'
        #apportioner.remove_nf_mass_balance_constraints()
        total_spill = apportioner.calculate_spills()

        # NOTE - I would like to make this 2nd pass conditional, but even if
        # there is no spill released, there may be some storage delivery that
        # is freed up. If I make the following conditional,
        # test_storage_deliveries_and_diversions_and_losses fails.
        if total_spill > 0 or True:
            apportioner.calculate_apportionments(schedule)

        # D. Finalize unconstrained (nonpath) vars
        apportioner.solve_for_nonpath_vars()


        # Collect results for this day
        apportionment_results.extend(apportioner.get_variables(date))
        apportionments_audit.extend(apportioner.apportionments_audit)


    unlagged_apportionments = unlag_apportionments(
        apportionment_results,
        data_manager.flow_lags,
    )

    results = SolverOutput(
        apportionments=unlagged_apportionments,
        solve_steps=apportionments_audit,
        solver_backend=resolved_backend.name.value,
    )

    if check_expected_values:

        results.print_solve_steps()

        assert_apportionments_equal_expected(results, input, graph_manager, data_manager, trxn_manager)

    return results


def assert_apportionments_equal_expected(results: SolverOutput, input: SolverInput, gm:GraphManager, dm:DailyDataManager, tm:TrxnSchedule) -> None:
    """Check if each of the apportionment results match the expected value
    to 4 decimal places.

    If a values does not match what is expected, it will include
    the system report string.

    Skips apportionment results that don't have a defined expected value.

    Raises an exception if no apportionment results have an expected value.
    """

    message:str = ''

    cnt = 0
    for t in tm.traverse_vars(input.txns):
        if type(t) == Trxn:
            for p in t.path:
                if p.expected_values is not None:
                    idx = 0
                    for date in _loop_through_date_range(input.beg_date,
                                                        input.end_date):

                        expected_value = p.expected_values[idx]
                        computed_values = results.get_result_value(date=date,
                                trxn_id=t.id, flow_id=p.flow_id)

                        if not computed_values:
                            raise ValueError(f'(date, trxn_id, flow_id) of {(date, t.id, p.flow_id)} not found.')
                        elif len(computed_values) > 1:
                            raise ValueError('Multiple results found')
                        computed_value = computed_values[0].value

                        if expected_value is not None:
                            cnt += 1
                            if abs(expected_value - computed_value) >= 1e-4:
                                msg = (message +
                                    f'Var "{t.id}": computed ({computed_value}) != ' +
                                    f'expected ({expected_value}) on {date}\n' +
                                    (system_report_str(results, idx, date, gm, dm, tm) if input is not None else '')
                                )
                                raise AssertionError(msg)
                        idx += 1
    if cnt == 0:
        raise Exception('No trxn path-items were given an expected_value!')


# --- Helper Methods


def _loop_through_date_range(beg_date: str, end_date: str) -> Generator[str, None, None]:
    """Iterate through each date from beg_date to end_date inclusive."""
    from datetime import datetime, timedelta
    a_date = datetime.strptime(beg_date, "%Y-%m-%d").date()
    b_date = datetime.strptime(end_date, "%Y-%m-%d").date()

    current_date = a_date
    while current_date <= b_date:
        yyyy_mm_dd = current_date.isoformat()
        yield yyyy_mm_dd
        current_date += timedelta(days=1)



def system_report_str(
        results:SolverOutput,
        day_idx:int,
        date:str,
        gm:GraphManager,
        dm:DailyDataManager,
        tm:TrxnSchedule
        ) -> str:
    """Displays the inflow and outflow totals and apportionments for stream
    zones, comparing the apportionments to the expected values. Useful for
    debuging."""
    def warn_if_value_is_incorrect(path_item:TrxnPathItem, value:float|None):
        if path_item.expected_values is not None:
            expected_value = path_item.expected_values[day_idx]
            if expected_value is not None and value is not None:
                if abs(expected_value - value) > 1e-4:
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

