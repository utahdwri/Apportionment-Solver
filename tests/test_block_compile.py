"""Accounting and execution contracts for the new block-first compiler."""
from copy import deepcopy
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch

from ut_water_apportionment import (
    AccountingGraph, AccountingLimit, AccountingLimitInterval, FlowComponentsTypes,
    FlowMeasurement, InterzoneFlow, LossDefinition, LossInterval,
    MeasurementCollection, MeasurementSeries, NaturalFlowMode, PathTrxn, SolverInput,
    TrxnGroup, TrxnPathItem, Zone, ZoneTypes, compile_solver_input_v2,
)
from ut_water_apportionment.compile import (
    UnsupportedBlockInput, build_block_lp, build_runtime_state_layout,
    compile, priority_blocks,
)
from ut_water_apportionment.compile.lp import Maximize, Proportional, Slot
from ut_water_apportionment.natural_flow_calculator import NaturalFlowCalculator


def transaction(name, priority=1, limit=10, flow='D', **kwargs):
    return PathTrxn(id=name, priority=priority, upper_limit=limit,
                    path=[TrxnPathItem(flow)], **kwargs)


def problem(txns, values=(10,), *, split=False):
    dates = ('2025-01-01', f'2025-01-{len(values):02d}')
    zones = [Zone('S', ZoneTypes.SYSTEM_GAIN_LOSS), Zone('R', ZoneTypes.STREAM),
             Zone('U', ZoneTypes.USE)]
    flows = [InterzoneFlow('D', 'R', 'U', flow_measurements=[FlowMeasurement('Q')]),
             InterzoneFlow('G', 'S', 'R', bidirectional=True,
                          flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE)]
    series = [MeasurementSeries('Q', list(values))]
    if split:
        zones.append(Zone('U2', ZoneTypes.USE))
        flows.append(InterzoneFlow('D2', 'R', 'U2', flow_measurements=[FlowMeasurement('Q2')]))
        series.append(MeasurementSeries('Q2', [8.] * len(values)))
    return SolverInput(AccountingGraph(zones, flows), txns,
                       MeasurementCollection(beg_date=dates[0], end_date=dates[1], series=series), *dates)


def results(output):
    return {(a.date, a.txn_id, a.interzone_flow_id, a.is_forward): a.value
            for a in output.apportionments}


class BlockCompilerTests(TestCase):
    def assert_reference(self, input):
        expected = results(compile_solver_input_v2(input).solve())
        plan = compile(input)
        actual = results(plan.solve())
        self.assertEqual(actual.keys(), expected.keys())
        for key in expected:
            self.assertAlmostEqual(actual[key], expected[key], places=6, msg=str(key))
        return plan, actual

    def test_sequential_blocks_use_only_target_and_preserve_residuals(self):
        input = problem([transaction('A', 1, 3), transaction('B', 2, 9)])
        plan, output = self.assert_reference(input)
        self.assertEqual([list(op.model.variables) for op in plan.operations], [['A'], ['B']])
        self.assertEqual(output['2025-01-01', 'A', 'D', True], 3)
        self.assertEqual(output['2025-01-01', 'B', 'D', True], 7)
        self.assertEqual(plan.solve().compilation_report['execution_lp_solves'], 2)

    def test_equal_priority_uses_effective_reference_cfs(self):
        plan, output = self.assert_reference(problem([transaction('A', limit=3), transaction('B', limit=7)], (5,)))
        self.assertIsInstance(plan.operations[0].model.rule, Proportional)
        self.assertAlmostEqual(output['2025-01-01', 'A', 'D', True], 1.5)
        self.assertAlmostEqual(output['2025-01-01', 'B', 'D', True], 3.5)

    def test_blocked_member_is_removed_and_other_continues(self):
        _, output = self.assert_reference(problem([transaction('A'), transaction('B', flow='D2')], (2,), split=True))
        self.assertEqual(output['2025-01-01', 'A', 'D', True], 2)
        self.assertEqual(output['2025-01-01', 'B', 'D2', True], 8)

    def test_unlimited_cohort_precedes_limited_members(self):
        _, output = self.assert_reference(problem([transaction('A', limit=None), transaction('B', limit=10)]))
        self.assertEqual(output['2025-01-01', 'A', 'D', True], 10)
        self.assertEqual(output['2025-01-01', 'B', 'D', True], 0)

    def test_parent_reservation_survives_outside_transaction(self):
        parent = TrxnGroup(id='P', priority=1, upper_limit=6,
                           children_trxns=[transaction('A', 3, 4), transaction('B', 5, 4)])
        plan, output = self.assert_reference(problem([parent, transaction('OUT', 2, 10)]))
        self.assertEqual(output['2025-01-01', 'OUT', 'D', True], 4)
        self.assertEqual(output['2025-01-01', 'A', 'D', True], 4)
        self.assertEqual(output['2025-01-01', 'B', 'D', True], 2)
        self.assertEqual(set(plan.operations[0].model.updates), {'P'})
        # A parent's optimizer witness must not commit either child early.
        layout = plan.state_layout
        data = layout.data.clone_runtime()
        schedule = layout.schedule.clone_runtime()
        state = layout.new_day('2025-01-01', data, schedule, NaturalFlowCalculator(layout.graph))
        plan.operations[0].execute(state)
        self.assertEqual(state[layout.measurements['D'].index], 10)
        self.assertEqual(state[layout.allocated['A'].index], 0)
        self.assertEqual(state[layout.groups['P'].index], 6)

    def test_nested_reservations(self):
        group = TrxnGroup(id='G1', priority=3, upper_limit=6,
                         children_trxns=[transaction('A', 5, 5), transaction('B', 7, 5)])
        parent = TrxnGroup(id='P', priority=1, upper_limit=6,
                          children_trxns=[group, transaction('C', 6, 3)])
        _, output = self.assert_reference(problem([parent, transaction('OUT', 2)]))
        self.assertEqual(output['2025-01-01', 'OUT', 'D', True], 4)
        self.assertEqual(output['2025-01-01', 'A', 'D', True], 5)
        self.assertEqual(output['2025-01-01', 'B', 'D', True], 1)
        self.assertEqual(output['2025-01-01', 'C', 'D', True], 0)

    def test_priority_adjustment_does_not_modify_input(self):
        input = problem([TrxnGroup(id='P', priority=2, upper_limit=5,
                                  children_trxns=[transaction('A', 1)])])
        before = deepcopy(input)
        blocks = priority_blocks(input)
        self.assertEqual([b.priority_order for b in blocks], [2, 2.00001])
        layout = build_runtime_state_layout(input)
        for block in blocks:
            build_block_lp(input, block, layout)
        self.assertEqual(input, before)

    def test_daily_limits_and_calls_are_slots_not_compile_day_constants(self):
        limit = AccountingLimit([AccountingLimitInterval('2025-01-01', '2025-01-02', 0),
                                 AccountingLimitInterval('2025-01-02', '2025-01-04', 8)])
        _, output = self.assert_reference(problem([transaction('A', 1, limit, call_limit=6), transaction('B', 2)], (10, 10, 3)))
        self.assertEqual([output[f'2025-01-0{i}', 'A', 'D', True] for i in (1, 2, 3)], [0, 6, 3])

    def test_cumulative_caps_reset_and_repeated_solves_start_fresh(self):
        input = problem([transaction('A', limit=4, cumulative_limit=5,
                                     cumulative_reset_before_MMDD='0103')], (10, 10, 10))
        plan, output = self.assert_reference(input)
        self.assertEqual([output[f'2025-01-0{i}', 'A', 'D', True] for i in (1, 2, 3)], [4, 1, 4])
        first, second = plan.solve(), plan.solve()
        self.assertEqual(results(first), results(second))
        self.assertEqual(first.compilation_report, second.compilation_report)

    def test_measurements_can_be_replaced_without_recompiling(self):
        plan = compile(problem([transaction('A', 1, 3), transaction('B', 2)]))
        replacement = MeasurementCollection(beg_date='2025-01-01', end_date='2025-01-01', series=[MeasurementSeries('Q', [2])])
        with patch('ut_water_apportionment.compile.compile.build_block_lp', side_effect=AssertionError('runtime compilation')):
            output = results(plan.solve(replacement))
        self.assertEqual(output['2025-01-01', 'A', 'D', True], 2)
        self.assertEqual(output['2025-01-01', 'B', 'D', True], 0)
        self.assertEqual(results(plan.solve())['2025-01-01', 'A', 'D', True], 3)

    def test_loss_coefficients_change_between_days(self):
        input = problem([PathTrxn(id='A', priority=1, upper_limit=10,
                                  path=[TrxnPathItem('D'), TrxnPathItem('LAST')])], (10, 10))
        input.accounting_graph.zones.append(Zone('END', ZoneTypes.USE))
        input.accounting_graph.interzone_flows.append(InterzoneFlow('LAST', 'U', 'END', flow_measurements=[FlowMeasurement('LAST')]))
        input.measurements = replace(input.measurements,
            series=[*input.measurements.series, MeasurementSeries('LAST', [10, 10])])
        input.accounting_graph.interzone_flows[0].loss_to_zone = LossDefinition(
            intervals=(LossInterval('2025-01-01', '2025-01-01', LossDefinition.linear(.2)),
                       LossInterval('2025-01-02', '2025-01-02', LossDefinition.linear(.5))))
        plan, output = self.assert_reference(input)
        self.assertEqual(output['2025-01-01', 'A', 'LAST', True], 8)
        self.assertEqual(output['2025-01-02', 'A', 'LAST', True], 5)
        self.assertTrue(any(isinstance(c, Slot) for row in plan.operations[0].model.constraints for c in row.coefficients.values()))

    def test_equal_priority_reference_cfs_refresh_each_day(self):
        cap = AccountingLimit([
            AccountingLimitInterval('2025-01-01', '2025-01-02', 3),
            AccountingLimitInterval('2025-01-02', '2025-01-03', 9),
        ])
        _, output = self.assert_reference(problem(
            [transaction('A', limit=cap), transaction('B', limit=3)], (6, 6)))
        self.assertEqual(output['2025-01-01', 'A', 'D', True], 3)
        self.assertEqual(output['2025-01-02', 'A', 'D', True], 4.5)
        self.assertEqual(output['2025-01-02', 'B', 'D', True], 1.5)

    def test_natural_flow_can_limit_below_physical_measurement(self):
        input = problem([transaction('A')])
        gain = input.accounting_graph.interzone_flows[1]
        gain.natural_flow_mode = NaturalFlowMode.SPECIFIED
        gain.nf_measurements = [FlowMeasurement('NF')]
        input.measurements = replace(input.measurements,
            series=[*input.measurements.series, MeasurementSeries('NF', [4])])
        _, output = self.assert_reference(input)
        self.assertEqual(output['2025-01-01', 'A', 'D', True], 4)
        self.assertEqual(output['2025-01-01', 'SLACK_R_TO_U_D', 'D', True], 6)

    def test_equal_priority_parent_reservations_with_distinct_bottlenecks(self):
        parents = [
            TrxnGroup(id='P', priority=1, upper_limit=10,
                      children_trxns=[transaction('A', 2)]),
            TrxnGroup(id='Q', priority=1, upper_limit=10,
                      children_trxns=[transaction('B', 2, flow='D2')]),
        ]
        _, output = self.assert_reference(problem(parents, (2,), split=True))
        self.assertEqual(output['2025-01-01', 'A', 'D', True], 2)
        self.assertEqual(output['2025-01-01', 'B', 'D2', True], 8)

    def test_compilation_does_not_read_a_representative_day(self):
        input = problem([transaction('A')])
        with patch('ut_water_apportionment.timeseries_manager.DailyDataManager.set_day',
                   side_effect=AssertionError('compile-time day binding')):
            plan = compile(input)
        self.assertEqual(results(plan.solve())['2025-01-01', 'A', 'D', True], 10)


    def test_no_transactions_reports_measurement_residuals(self):
        plan, output = self.assert_reference(problem([]))
        self.assertEqual(plan.operations, [])
        self.assertEqual(output['2025-01-01', 'SLACK_R_TO_U_D', 'D', True], 10)

    def test_unsupported_accounting_is_explicit(self):
        for feature in ('storage', 'reverse', 'unconstrained', 'piecewise'):
            with self.subTest(feature=feature):
                input = problem([transaction('A')])
                if feature == 'storage':
                    input.accounting_graph.zones[-1].type = ZoneTypes.STORAGE
                elif feature == 'reverse':
                    input.txns[0].path[0].factor = -1
                elif feature == 'unconstrained':
                    input.accounting_graph.interzone_flows[0].flow_type = FlowComponentsTypes.UNCONSTRAINED
                else:
                    from ut_water_apportionment import LossCurvePoint
                    input.accounting_graph.interzone_flows[0].loss_to_zone = LossDefinition.piecewise_linear(
                        [LossCurvePoint(0, 0), LossCurvePoint(10, 2), LossCurvePoint(20, 3)])
                with self.assertRaises(UnsupportedBlockInput):
                    compile(input)
