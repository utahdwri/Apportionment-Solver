"""The displayed plan source is the exact Python program used for execution."""
import builtins
from types import SimpleNamespace
from unittest import TestCase

from ut_water_apportionment.compile import CompiledOperation, compile
from ut_water_apportionment.compile.codegen import generate_plan_source
from ut_water_apportionment.compile.kernel import LPKernel
from ut_water_apportionment.compile.lp import BlockLP, Constraint, Proportional, Slot, Variable
from ut_water_apportionment.models import (
    AccountingGraph,
    FlowComponentsTypes,
    FlowMeasurement,
    InterzoneFlow,
    MeasurementCollection,
    MeasurementSeries,
    PathTrxn,
    SolverInput,
    TrxnPathItem,
    Zone,
    ZoneTypes,
)


class PlanCodeTests(TestCase):
    def test_natural_flow_is_propagated_inline_in_topological_order(self):
        problem = SolverInput(
            beg_date='2000-01-01', end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[Zone('C', ZoneTypes.STREAM), Zone('B', ZoneTypes.STREAM),
                       Zone('A', ZoneTypes.STREAM)],
                interzone_flows=[
                    InterzoneFlow('B>C', 'B', 'C',
                                  flow_measurements=[FlowMeasurement('BC')]),
                    InterzoneFlow('A>B', 'A', 'B',
                                  flow_measurements=[FlowMeasurement('AB')]),
                ],
            ),
            measurements=MeasurementCollection(
                beg_date='2000-01-01', end_date='2000-01-01',
                series=[MeasurementSeries('AB', [10]), MeasurementSeries('BC', [10])],
            ),
            txns=[],
            external_natural_flows={'A>B': {'2000-01-01': 10}},
        )
        plan = compile(problem)
        source = plan.code()
        self.assertNotIn('def _nf_propagate_', source)
        self.assertNotIn('def _nf_apply_flow_', source)
        self.assertIn("# 'B'", source[source.index('    # Propagate calculated flows once'):])
        layout = plan.state_layout
        state = layout.new_day('2000-01-01', layout.data.clone_runtime(),
                               layout.schedule.clone_runtime())
        plan.executor(state)
        self.assertAlmostEqual(state[layout.flow_natural['B>C'].index], 10)
        self.assertAlmostEqual(state[layout.natural_at_zone['C'].index], 10)

    def test_external_boundary_can_cut_a_structural_cycle(self):
        problem = SolverInput(
            beg_date='2000-01-01', end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[Zone('A', ZoneTypes.STREAM), Zone('B', ZoneTypes.STREAM)],
                interzone_flows=[
                    InterzoneFlow('AB', 'A', 'B',
                                  flow_measurements=[FlowMeasurement('q1')]),
                    InterzoneFlow('BA', 'B', 'A',
                                  flow_measurements=[FlowMeasurement('q2')]),
                ],
            ),
            measurements=MeasurementCollection(
                beg_date='2000-01-01', end_date='2000-01-01',
                series=[MeasurementSeries('q1', [3]), MeasurementSeries('q2', [0])],
            ),
            txns=[], external_natural_flows={'AB': {'2000-01-01': 3}},
        )
        plan = compile(problem)
        self.assertNotIn('def _nf_propagate_', plan.code())
        layout = plan.state_layout
        state = layout.new_day('2000-01-01', layout.data.clone_runtime(),
                               layout.schedule.clone_runtime())
        plan.executor(state)
        self.assertEqual(state[layout.flow_natural['BA'].index], 3)

    def test_code_is_human_readable_executable_formula_program(self):
        problem = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-03',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id='RIVER', type=ZoneTypes.STREAM),
                    Zone(id='SYS', type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id='USER', type=ZoneTypes.USE),
                ],
                interzone_flows=[
                    InterzoneFlow(
                        id='RIVER>USER', from_zone='RIVER', to_zone='USER',
                        flow_measurements=[FlowMeasurement(measurement_id='1')],
                    ),
                    InterzoneFlow(
                        id='SYS>RIVER', from_zone='SYS', to_zone='RIVER',
                        flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                        bidirectional=True,
                    ),
                ],
            ),
            measurements=MeasurementCollection(
                beg_date='2000-01-01', end_date='2000-01-03',
                series=[MeasurementSeries(id='1', values=[12, 3, 10])],
            ),
            txns=[
                PathTrxn(
                    id='TRXN_1', priority=1, upper_limit=3,
                    path=[TrxnPathItem(flow_id='RIVER>USER')],
                ),
                PathTrxn(
                    id='TRXN_2', priority=2, upper_limit=6,
                    path=[TrxnPathItem(flow_id='RIVER>USER')],
                ),
            ],
        )

        plan = compile(problem)
        source = plan.code()
        self.assertIn('def _block_0(state):', source)
        self.assertIn('def _block_0_direct(state):', source)
        self.assertIn("# Direct formula for 'TRXN_1'", source)
        self.assertIn('amount = checked_nonnegative_increment(amount)', source)
        self.assertIn('state[S_REMAINING_MEASURED_FORWARD_RIVER_USER]', source)
        self.assertIn("def execute(state):", source)
        self.assertNotIn('compile_direct_kernel(', source)
        self.assertNotIn('kernels = (', source)

        values = [
            (row.date, row.txn_id, row.value)
            for row in plan.solve().apportionments
            if row.txn_id in {'TRXN_1', 'TRXN_2'}
        ]
        self.assertEqual(values, [
            ('2000-01-01', 'TRXN_1', 3.0),
            ('2000-01-01', 'TRXN_2', 6.0),
            ('2000-01-02', 'TRXN_1', 3.0),
            ('2000-01-02', 'TRXN_2', 0.0),
            ('2000-01-03', 'TRXN_1', 3.0),
            ('2000-01-03', 'TRXN_2', 6.0),
        ])

    def test_lp_fallback_is_rendered_as_readable_comments(self):
        limit_a = Slot(0, "remaining_limit['A']")
        limit_b = Slot(1, "remaining_limit['B']")
        measured = Slot(2, "remaining_measured['RIVER>RES']")
        reserved = Slot(3, "remaining_group['P']")
        allocated_a = Slot(4, "allocated['A']")

        model = BlockLP(
            variables={
                'A': Variable(0.0, limit_a),
                'B': Variable(0.0, limit_b),
                'W': Variable(0.0, None),
            },
            constraints=[
                Constraint(
                    "measurement['RIVER>RES']",
                    {'A': 1.0, 'B': 1.0, 'W': -1.0},
                    upper=measured,
                ),
                Constraint(
                    "reservation['P']",
                    {'A': 1.0, 'W': -1.0},
                    lower=reserved,
                    upper=reserved,
                ),
            ],
            rule=Proportional({'A': 1.0, 'B': 2.0}),
            updates={'A': {allocated_a: 1.0}},
        )
        layout = SimpleNamespace(
            slots={
                slot.name: slot
                for slot in (limit_a, limit_b, measured, reserved, allocated_a)
            }
        )

        source = generate_plan_source([CompiledOperation(LPKernel(model))], layout).source

        self.assertIn('# Numerical LP fallback', source)
        self.assertIn('# VARIABLES', source)
        self.assertIn("#     0.0 <= A <= remaining_limit['A']", source)
        self.assertIn('# ALLOCATION RULE', source)
        self.assertIn('#     PROPORTIONAL', source)
        self.assertIn('# CONSTRAINTS', source)
        self.assertIn("#     measurement['RIVER>RES']:", source)
        self.assertIn("#         <= remaining_measured['RIVER>RES']", source)
        self.assertIn('# COMMITTED STATE UPDATES', source)
        self.assertIn("#         allocated['A'] += (1.0) * A", source)
        self.assertIn('return _BLOCK_0_DIRECT_FALLBACK.execute(state)', source)

        # The explanatory comments are part of the same executable source.
        builtins.compile(source, '<generated plan>', 'exec')

