"""Daily data binding and reporting; allocation lives in generated operations."""
from dataclasses import replace
from math import isfinite

from ..lag_utils import unlag_apportionments
from ..models import PathTrxn, SolverOutput, SolverOutputApportionment, ZoneTypes
from ..natural_flow_calculator import NaturalFlowCalculator
from ..solver import _loop_through_date_range, assert_apportionments_equal_expected


def solve_plan(plan, measurements, *, check_expected_values=False):
    layout = plan.state_layout
    problem = layout.input if measurements is None else replace(layout.input, measurements=measurements)
    data = layout.data.clone_runtime(measurements=problem.measurements)
    schedule = layout.schedule.clone_runtime()
    natural = NaturalFlowCalculator(layout.graph)
    output = []
    lp_solves = 0
    days = 0
    for date in _loop_through_date_range(problem.beg_date, problem.end_date):
        state = layout.new_day(date, data, schedule, natural)
        lp_solves += plan.executor(state)
        days += 1
        for group, slot in layout.groups.items():
            if abs(state[slot.index]) > 1e-6:
                raise RuntimeError(f"Unfulfilled reservation {group!r} on {date}: {state[slot.index]}")
        variable_values = {}
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
                    nf = data.cur_flows_by_id[flow.id].natural
                    output.extend([
                        SolverOutputApportionment(date, flow.id, txn.id + '_NF', nf * item.factor, True),
                        SolverOutputApportionment(date, flow.id, txn.id + '_CPI', (amount - nf) * item.factor, amount > nf, ''),
                    ])
                else:
                    output.append(SolverOutputApportionment(date, flow.id, txn.id, amount * item.factor, item.factor > 0, ''))
        schedule.commit_day(variable_values)
    result = SolverOutput(
        apportionments=unlag_apportionments(output, data.flow_lags), solve_steps=[],
        solver_backend='scipy-highs-block-kernels', solve_method='block_lp',
        compilation_report={
            'priority_blocks': len(plan.operations), 'lp_kernels': len(plan.operations),
            'direct_calculations': 0, 'runtime_slots': len(layout.slots),
            'execution_days': days, 'execution_lp_solves': lp_solves,
            'maximum_kernel_variables': max((len(op.model.variables) for op in plan.operations), default=0),
            'runtime_compilation': False,
        },
    )
    if check_expected_values:
        assert_apportionments_equal_expected(result, problem, layout.graph, data, schedule)
    return result
