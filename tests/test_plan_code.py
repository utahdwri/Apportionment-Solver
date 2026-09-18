"""The displayed plan source is the exact Python program used for execution."""
import builtins
from types import SimpleNamespace
from unittest import TestCase

from ut_water_apportionment.compile import compile
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
        self.assertIn('def _pass1_block_0(state):', source)
        self.assertIn("# Direct formula for 'TRXN_1'", source)
        self.assertIn('_allocation = _upper if _objective > 0 else _lower', source)
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

        source = generate_plan_source([LPKernel(model)], [], layout).source

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
        self.assertIn('return _PASS1_FALLBACK_0.execute(state)', source)

        # The explanatory comments are part of the same executable source.
        builtins.compile(source, '<generated plan>', 'exec')

