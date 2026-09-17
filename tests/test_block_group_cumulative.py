import unittest

from ut_water_apportionment import (
    AccountingGraph,
    FlowComponentsTypes,
    FlowMeasurement,
    InterzoneFlow,
    MeasurementCollection,
    MeasurementSeries,
    PathTrxn,
    SolverInput,
    TrxnGroup,
    TrxnPathItem,
    Zone,
    ZoneTypes,
    compile,
)


def _problem(group: TrxnGroup, values=(10.0, 10.0, 10.0)) -> SolverInput:
    end_day = len(values)
    return SolverInput(
        beg_date='2025-01-01',
        end_date=f'2025-01-{end_day:02d}',
        accounting_graph=AccountingGraph(
            zones=[
                Zone('SYS', ZoneTypes.SYSTEM_GAIN_LOSS),
                Zone('R', ZoneTypes.STREAM),
                Zone('U', ZoneTypes.USE),
            ],
            interzone_flows=[
                InterzoneFlow(
                    'D', 'R', 'U',
                    flow_measurements=[FlowMeasurement('Q')],
                ),
                InterzoneFlow(
                    'G', 'SYS', 'R',
                    bidirectional=True,
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                ),
            ],
        ),
        txns=[group],
        measurements=MeasurementCollection(
            beg_date='2025-01-01',
            end_date=f'2025-01-{end_day:02d}',
            series=[MeasurementSeries('Q', list(values))],
        ),
    )


def _group(*, daily=4.0, cumulative=5.0, reset=None) -> TrxnGroup:
    child = PathTrxn(
        id='A',
        priority=2,
        upper_limit=10,
        path=[TrxnPathItem('D')],
    )
    return TrxnGroup(
        id='P',
        priority=1,
        upper_limit=daily,
        cumulative_limit=cumulative,
        cumulative_reset_before_MMDD=reset,
        children_trxns=[child],
    )


def _child_values(output):
    return [
        item.value
        for item in output.apportionments
        if item.txn_id == 'A' and item.interzone_flow_id == 'D'
    ]


class CumulativeGroupLimitTests(unittest.TestCase):

    def test_group_cumulative_limit_is_validated(self):
        with self.assertRaises(ValueError):
            _group(cumulative=-1.0)

    def test_group_cumulative_remaining_becomes_daily_variable_limit(self):
        plan = compile(_problem(_group()))
        self.assertEqual(_child_values(plan.solve()), [4.0, 1.0, 0.0])

        # A new execution of the same compiled plan starts with fresh
        # cross-day runtime state.
        self.assertEqual(_child_values(plan.solve()), [4.0, 1.0, 0.0])

    def test_group_cumulative_limit_can_be_only_finite_limit(self):
        plan = compile(_problem(_group(daily=None, cumulative=5.0)))
        self.assertEqual(_child_values(plan.solve()), [5.0, 0.0, 0.0])

    def test_group_cumulative_reset_occurs_before_that_days_solve(self):
        plan = compile(_problem(_group(reset='0103')))
        self.assertEqual(_child_values(plan.solve()), [4.0, 1.0, 4.0])


if __name__ == '__main__':
    unittest.main()
