import unittest

from ut_water_apportionment import (
    AccountingGraph,
    FlowMeasurement,
    InterzoneFlow,
    MeasurementCollection,
    MeasurementSeries,
    PathTrxn,
    SolverInput,
    TrxnPathItem,
    Zone,
    ZoneAccount,
    ZoneTypes,
    compile,
)


class TestBlockAccounts(unittest.TestCase):

    def test_storage_account_floor_ceiling_and_cross_day_balance(self):
        problem = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id='IMPORT', type=ZoneTypes.IMPORT),
                    Zone(
                        id='RES',
                        type=ZoneTypes.STORAGE,
                        accounts=[
                            ZoneAccount(
                                id='A',
                                starting_balance=10,
                                balance_floor=0,
                                balance_ceiling=40,
                            )
                        ],
                    ),
                    Zone(id='USE', type=ZoneTypes.USE),
                ],
                interzone_flows=[
                    InterzoneFlow(
                        id='IN', from_zone='IMPORT', to_zone='RES',
                        flow_measurements=[FlowMeasurement(measurement_id='IN')],
                    ),
                    InterzoneFlow(
                        id='OUT', from_zone='RES', to_zone='USE',
                        flow_measurements=[FlowMeasurement(measurement_id='OUT')],
                    ),
                ],
            ),
            measurements=MeasurementCollection(
                beg_date='2000-01-01',
                end_date='2000-01-02',
                series=[
                    MeasurementSeries(id='IN', values=[100, 100]),
                    MeasurementSeries(id='OUT', values=[100, 100]),
                ],
            ),
            txns=[
                PathTrxn(
                    id='DEPOSIT', priority=1, upper_limit=100, to_account='A',
                    path=[TrxnPathItem(flow_id='IN', expected_values=[30, 10])],
                ),
                PathTrxn(
                    id='WITHDRAW', priority=2, upper_limit=100, from_account='A',
                    path=[TrxnPathItem(flow_id='OUT', expected_values=[10, 30])],
                ),
            ],
        )

        compile(problem).solve(check_expected_values=True)

    def test_account_deposit_uses_delivered_amount_after_losses(self):
        problem = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id='IMPORT', type=ZoneTypes.IMPORT),
                    Zone(id='MID', type=ZoneTypes.USE),
                    Zone(
                        id='RES',
                        type=ZoneTypes.STORAGE,
                        accounts=[ZoneAccount(id='A', starting_balance=0, balance_ceiling=9)],
                    ),
                ],
                interzone_flows=[
                    InterzoneFlow(
                        id='F1', from_zone='IMPORT', to_zone='MID',
                        flow_measurements=[FlowMeasurement(measurement_id='F1')],
                    ),
                    InterzoneFlow(
                        id='F2', from_zone='MID', to_zone='RES',
                        flow_measurements=[FlowMeasurement(measurement_id='F2')],
                    ),
                ],
            ),
            measurements=MeasurementCollection(
                beg_date='2000-01-01',
                end_date='2000-01-01',
                series=[
                    MeasurementSeries(id='F1', values=[100]),
                    MeasurementSeries(id='F2', values=[100]),
                ],
            ),
            txns=[
                PathTrxn(
                    id='DEPOSIT', priority=1, upper_limit=20, to_account='A',
                    path=[
                        TrxnPathItem(flow_id='F1', loss_after=0.1, expected_values=[10]),
                        TrxnPathItem(flow_id='F2', expected_values=[9]),
                    ],
                ),
            ],
        )

        compile(problem).solve(check_expected_values=True)


if __name__ == '__main__':
    unittest.main()
