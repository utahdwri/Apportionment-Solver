import unittest

from ut_water_apportionment import (
    AccountingGraph,
    AccountingLimit,
    AccountingLimitInterval,
    FlowMeasurement,
    InterzoneFlow,
    MeasurementCollection,
    MeasurementSeries,
    SolverInput,
    PathTrxn,
    TrxnPathItem,
    Zone,
    ZoneAccount,
    ZoneTypes,
)
from ut_water_apportionment.solver import solve
from ut_water_apportionment.lp_solver import SolverBackend
from ut_water_apportionment.graph_manager import GraphManager
from ut_water_apportionment.trxn_schedule import TrxnSchedule


def make_input(*, beg_date, end_date, measured, txns, source_accounts=None, dest_accounts=None):
    source_accounts = source_accounts or []
    dest_accounts = dest_accounts or []
    return SolverInput(
        accounting_graph=AccountingGraph(
            zones=[
                Zone(id='SOURCE', type=ZoneTypes.IMPORT, accounts=source_accounts),
                Zone(id='DEST', type=ZoneTypes.USE, accounts=dest_accounts),
            ],
            interzone_flows=[
                InterzoneFlow(
                    id='SOURCE>DEST',
                    from_zone='SOURCE',
                    to_zone='DEST',
                    flow_measurements=[FlowMeasurement('Q')],
                )
            ],
        ),
        txns=txns,
        measurements=MeasurementCollection(
            series=[MeasurementSeries(id='Q', values=measured)],
            beg_date=beg_date,
            end_date=end_date,
        ),
        beg_date=beg_date,
        end_date=end_date,
    )


def values_for(results, txn_id):
    return [
        row.value
        for row in results.apportionments
        if row.txn_id == txn_id and row.interzone_flow_id == 'SOURCE>DEST'
    ]


class ZoneAccountTests(unittest.TestCase):
    def test_outgoing_account_floor_tracks_balance_across_days(self):
        input = make_input(
            beg_date='2000-01-01',
            end_date='2000-01-02',
            measured=[4, 4],
            source_accounts=[ZoneAccount(id='A', starting_balance=5, balance_floor=0)],
            txns=[
                PathTrxn(
                    id='T',
                    path=[TrxnPathItem('SOURCE>DEST')],
                    upper_limit=10,
                    priority=1,
                    from_account='A',
                )
            ],
        )

        results = solve(input, solver_backend=SolverBackend.SCIPY)
        self.assertEqual(values_for(results, 'T'), [4.0, 1.0])

    def test_incoming_account_ceiling_tracks_balance_across_days(self):
        input = make_input(
            beg_date='2000-01-01',
            end_date='2000-01-02',
            measured=[4, 4],
            dest_accounts=[ZoneAccount(id='A', starting_balance=0, balance_ceiling=5)],
            txns=[
                PathTrxn(
                    id='T',
                    path=[TrxnPathItem('SOURCE>DEST')],
                    upper_limit=10,
                    priority=1,
                    to_account='A',
                )
            ],
        )

        results = solve(input, solver_backend=SolverBackend.SCIPY)
        self.assertEqual(values_for(results, 'T'), [4.0, 1.0])

    def test_shared_account_limit_is_shared_across_transactions(self):
        input = make_input(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            measured=[10],
            source_accounts=[ZoneAccount(id='A', starting_balance=6, balance_floor=0)],
            txns=[
                PathTrxn(
                    id='T1',
                    path=[TrxnPathItem('SOURCE>DEST')],
                    upper_limit=10,
                    priority=1,
                    from_account='A',
                ),
                PathTrxn(
                    id='T2',
                    path=[TrxnPathItem('SOURCE>DEST')],
                    upper_limit=10,
                    priority=1,
                    from_account='A',
                ),
            ],
        )

        results = solve(input, solver_backend=SolverBackend.SCIPY)
        self.assertAlmostEqual(values_for(results, 'T1')[0], 3.0, places=6)
        self.assertAlmostEqual(values_for(results, 'T2')[0], 3.0, places=6)

    def test_account_ids_only_need_to_be_unique_within_zone(self):
        graph = AccountingGraph(
            zones=[
                Zone(id='A', type=ZoneTypes.IMPORT, accounts=[ZoneAccount('SAME')]),
                Zone(id='B', type=ZoneTypes.USE, accounts=[ZoneAccount('SAME')]),
            ],
            interzone_flows=[InterzoneFlow(id='F', from_zone='A', to_zone='B')],
        )
        GraphManager(graph)  # no exception

    def test_duplicate_account_ids_in_same_zone_are_rejected(self):
        graph = AccountingGraph(
            zones=[
                Zone(
                    id='A',
                    type=ZoneTypes.IMPORT,
                    accounts=[ZoneAccount('DUP'), ZoneAccount('DUP')],
                ),
                Zone(id='B', type=ZoneTypes.USE),
            ],
            interzone_flows=[InterzoneFlow(id='F', from_zone='A', to_zone='B')],
        )
        with self.assertRaisesRegex(ValueError, 'duplicate ZoneAccount IDs'):
            GraphManager(graph)

    def test_account_reference_must_exist_in_correct_endpoint_zone(self):
        gm = GraphManager(
            AccountingGraph(
                zones=[
                    Zone(id='A', type=ZoneTypes.IMPORT),
                    Zone(id='B', type=ZoneTypes.USE, accounts=[ZoneAccount('X')]),
                ],
                interzone_flows=[InterzoneFlow(id='F', from_zone='A', to_zone='B')],
            )
        )
        with self.assertRaisesRegex(ValueError, 'source zone'):
            TrxnSchedule(
                gm,
                [PathTrxn(id='T', path=[TrxnPathItem('F')], upper_limit=1, from_account='X')],
            )


class TransactionLimitTests(unittest.TestCase):
    def test_cumulative_limit_tracks_usage_and_resets(self):
        input = make_input(
            beg_date='2000-01-01',
            end_date='2000-01-04',
            measured=[10, 10, 10, 10],
            txns=[
                PathTrxn(
                    id='T',
                    path=[TrxnPathItem('SOURCE>DEST')],
                    upper_limit=10,
                    priority=1,
                    cumulative_limit=12,
                    cumulative_reset_before_MMDD='0103',
                )
            ],
        )

        results = solve(input, solver_backend=SolverBackend.SCIPY)
        self.assertEqual(values_for(results, 'T'), [10.0, 2.0, 10.0, 2.0])
        self.assertTrue(any('cumulative limit' in (step.reason or '') for step in results.solve_steps))

    def test_call_limit_governs_when_lower_than_other_limits(self):
        input = make_input(
            beg_date='2000-01-01',
            end_date='2000-01-02',
            measured=[10, 10],
            txns=[
                PathTrxn(
                    id='T',
                    path=[TrxnPathItem('SOURCE>DEST')],
                    upper_limit=10,
                    priority=1,
                    cumulative_limit=20,
                    call_limit=3,
                )
            ],
        )

        results = solve(input, solver_backend=SolverBackend.SCIPY)
        self.assertEqual(values_for(results, 'T'), [3.0, 3.0])
        self.assertTrue(any('call limit' in (step.reason or '') for step in results.solve_steps))

    def test_time_varying_call_limit(self):
        input = make_input(
            beg_date='2000-01-01',
            end_date='2000-01-02',
            measured=[10, 10],
            txns=[
                PathTrxn(
                    id='T',
                    path=[TrxnPathItem('SOURCE>DEST')],
                    upper_limit=10,
                    priority=1,
                    call_limit=AccountingLimit(intervals=[
                        AccountingLimitInterval('2000-01-01', '2000-01-02', 2),
                        AccountingLimitInterval('2000-01-02', '2000-01-03', 5),
                    ]),
                )
            ],
        )

        results = solve(input, solver_backend=SolverBackend.SCIPY)
        self.assertEqual(values_for(results, 'T'), [2.0, 5.0])

    def test_invalid_reset_mmdd_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'cumulative_reset_MMDD'):
            PathTrxn(
                id='T',
                path=[TrxnPathItem('F')],
                upper_limit=1,
                cumulative_limit=10,
                cumulative_reset_before_MMDD='1332',
            )


if __name__ == '__main__':
    unittest.main()
