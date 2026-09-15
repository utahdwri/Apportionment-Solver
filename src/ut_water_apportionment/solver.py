"""Public v2 solve API plus shared accounting-day helpers.

This branch intentionally has no whole-day LP solve mode.  ``solve`` prepares
and executes the frozen v2 compiled-formula plan.  The day helper remains the
common LP-problem builder used during v2 preparation and execution.
"""

from typing import Generator

from .models import SolverInput, SolverOutput, PathTrxn, TrxnPathItem, ZoneTypes
from .graph_manager import GraphManager
from .timeseries_manager import DailyDataManager
from .trxn_schedule import TrxnSchedule
from .apportioner import Apportioner


def solve(
    input: SolverInput,
    *,
    check_expected_values: bool = False,
    max_daily_apportionment: float | None = None,
    compilation_options=None,
) -> SolverOutput:
    """Compile and execute the frozen v2 formula solver.

    The production LP remains the problem definition, but there is no public
    whole-day LP execution path on this branch.  Difficult residual objectives
    are handled by v2 reduced LP kernels.
    """
    from .compiled_v2 import compile_solver_input_v2

    plan = compile_solver_input_v2(
        input,
        options=compilation_options,
        max_daily_apportionment=max_daily_apportionment,
    )
    return plan.solve(check_expected_values=check_expected_values)


def _solve_day(graph_manager, trxn_manager, data_manager, natural_flow_calculator,
               factory, generate_audit, date):
    """One tentative day. Only the caller commits cross-day account state."""
    apportioner = Apportioner(
        graph_manager, trxn_manager, data_manager, natural_flow_calculator,
        lp_solver_factory=factory, generate_audit=generate_audit,
    )
    apportioner.update_daily_bounds()
    schedule = trxn_manager.build_schedule(date)
    apportioner.apply_nf_mass_balance_constraints(date)
    apportioner.calculate_apportionments(schedule)
    apportioner.calculate_spills()
    # Even a zero spill can free storage deliveries: retain main's second pass.
    apportioner.calculate_apportionments(schedule)
    apportioner.solve_for_nonpath_vars()
    return apportioner

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
        if type(t) == PathTrxn:
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

