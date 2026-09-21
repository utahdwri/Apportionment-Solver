"""Daily data binding and reporting; allocation lives in generated operations."""
from dataclasses import replace
from math import isfinite
from typing import Generator

from .kernel import DirectCalculationKernel, LPKernel, ProportionalCalculationKernel, ScalarFormulaKernel
from ..lag_utils import unlag_apportionments
from ..models import SolverInput, SolverOutput, SolverOutputApportionment, ZoneTypes, PathTrxn, TrxnPathItem
from ..graph_manager import GraphManager
from ..timeseries_manager import DailyDataManager
from ..trxn_schedule import TrxnSchedule



def solve_plan(plan, measurements, *, check_expected_values=False):
    layout = plan.state_layout
    problem = layout.input if measurements is None else replace(layout.input, measurements=measurements)
    data = layout.data.clone_runtime(measurements=problem.measurements)
    schedule = layout.schedule.clone_runtime()
    output = []
    lp_solves = 0
    days = 0
    for date in _loop_through_date_range(problem.beg_date, problem.end_date):
        # Parent allocations are feasibility caps, not permanently reserved
        # physical water. If later child conditions leave part of a parent
        # unused, tighten that parent's cap to actual use and replay the day.
        # Caps move only downward, so this correction is monotone.
        fixed_group_caps = {}
        for _unwind_iteration in range(50):
            state = layout.new_day(date, data, schedule)
            for group_id, cap in fixed_group_caps.items():
                slot = layout.limits[group_id]
                state[slot.index] = min(state[slot.index], cap)

            # The generated daily program owns natural-flow initialization,
            # the priority sweep (including counterflow completion), spill/import
            # NF credit, and the optional post-spill sweep.
            lp_solves += plan.executor(state)

            changed = False
            for group_id, remaining_slot in layout.groups.items():
                remaining = float(state[remaining_slot.index])
                if remaining <= 1e-7:
                    continue
                allocated = float(state[layout.allocated[group_id].index])
                used = max(0.0, allocated - remaining)
                prior = fixed_group_caps.get(group_id)
                if prior is None or used < prior - 1e-7:
                    fixed_group_caps[group_id] = used
                    changed = True
            if not changed:
                break
        else:
            raise RuntimeError(f"Group unwind did not converge on {date}")

        days += 1

        variable_values = {
            group_id: float(state[layout.allocated[group_id].index])
            for group_id in layout.groups
        }
        for txn in schedule.all_trxns:
            if not isinstance(txn, PathTrxn):
                continue
            if txn.is_slack:
                item = txn.path[0]
                residual = state[layout.measurements[item.flow_id].index]
                magnitude = max(0.0, float(residual * item.factor))
                variable_values[f'{txn.id}___{item.flow_id}'] = magnitude
            else:
                allocation = state[layout.allocated[txn.id].index]
                for item in txn.path:
                    coefficient = layout.flow_coefficients[txn.id, item.flow_id][0]
                    variable_values[f'{txn.id}___{item.flow_id}'] = float(allocation * state[coefficient.index] / item.factor)
        # Preserve existing output conventions, including stream-to-stream NF
        # and CPI reporting components. No reporting slack is an LP variable.
        for txn in schedule.all_trxns:
            if not isinstance(txn, PathTrxn):
                continue
            for item in txn.path:
                flow = layout.graph.get_flow_by_id(item.flow_id)
                amount = variable_values[f'{txn.id}___{item.flow_id}']
                if not isfinite(amount):
                    raise RuntimeError(f"Non-finite allocation for {txn.id!r}")
                stream_slack = (txn.is_slack
                    and layout.graph.get_zone_by_id(flow.from_zone).type == ZoneTypes.STREAM
                    and layout.graph.get_zone_by_id(flow.to_zone).type == ZoneTypes.STREAM)
                if stream_slack:
                    nf = float(state[layout.flow_natural[flow.id].index])
                    output.extend([
                        SolverOutputApportionment(date, flow.id, txn.id + '_NF', nf * item.factor, True),
                        SolverOutputApportionment(date, flow.id, txn.id + '_CPI', (amount - nf) * item.factor, amount > nf, ''),
                    ])
                else:
                    output.append(SolverOutputApportionment(date, flow.id, txn.id, amount * item.factor, item.factor > 0, ''))
        schedule.commit_day(variable_values)
    result = SolverOutput(
        apportionments=unlag_apportionments(output, data.flow_lags),
        solver_backend='scipy-highs-block-kernels', solve_method='block_lp',
        compilation_report={
            'priority_blocks': len(plan.operations),
            'lp_kernels': sum(
                isinstance(op, LPKernel)
                for op in (
                    *plan.operations,
                    *(op for op in plan.counterflow_operations if op is not None),
                )
            ),
            'direct_calculations': sum(
                isinstance(op, DirectCalculationKernel)
                for op in (
                    *plan.operations,
                    *(op for op in plan.counterflow_operations if op is not None),
                )
            ),
            'proportional_calculations': sum(
                isinstance(op, ProportionalCalculationKernel)
                for op in (
                    *plan.operations,
                    *(op for op in plan.counterflow_operations if op is not None),
                )
            ),
            'scalar_formulas': sum(
                isinstance(op, ScalarFormulaKernel)
                for op in (
                    *plan.operations,
                    *(op for op in plan.counterflow_operations if op is not None),
                )
            ),
            'maximum_formula_rows': max((
                op.maximum_intermediate_rows
                for op in (
                    *plan.operations,
                    *(op for op in plan.counterflow_operations if op is not None),
                )
                if isinstance(op, ScalarFormulaKernel)
            ), default=0),
            'runtime_slots': len(layout.slots),
            'execution_days': days, 'execution_lp_solves': lp_solves,
            'spill_replay_flows': len(layout.spill_credits),
            'maximum_kernel_variables': max((
                len(op.model.variables)
                for op in (
                    *plan.operations,
                    *(op for op in plan.counterflow_operations if op is not None),
                )
            ), default=0),
            'runtime_compilation': False,
        },
    )
    if check_expected_values:
        assert_apportionments_equal_expected(result, problem, layout.graph, data, schedule)
    return result



def assert_apportionments_equal_expected(
    results: SolverOutput,
    input: SolverInput,
    gm: GraphManager,
    dm: DailyDataManager,
    tm: TrxnSchedule
) -> None:
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

