import unittest

from ut_water_apportionment import (
    AccountingGraph, FlowMeasurement, InterzoneFlow, MeasurementCollection,
    MeasurementSeries, PathTrxn, SolverInput, TrxnPathItem, Zone, ZoneTypes,
    compile,
)


class TestBlockReverse(unittest.TestCase):
    def test_reverse_transaction_uses_negative_measurement_capacity(self):
        problem = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id='A', type=ZoneTypes.USE),
                    Zone(id='B', type=ZoneTypes.USE),
                ],
                interzone_flows=[
                    InterzoneFlow(
                        id='A>B', from_zone='A', to_zone='B', bidirectional=True,
                        flow_measurements=[FlowMeasurement(measurement_id='A>B')],
                    ),
                ],
            ),
            measurements=MeasurementCollection(
                beg_date='2000-01-01', end_date='2000-01-01',
                series=[MeasurementSeries(id='A>B', values=[-10])],
            ),
            txns=[
                PathTrxn(
                    id='REV', priority=1, upper_limit=7,
                    path=[TrxnPathItem(flow_id='A>B', factor=-1, expected_values=[-7])],
                )
            ],
        )
        result = compile(problem).solve(check_expected_values=True)
        values = {(a.txn_id, a.interzone_flow_id): a.value for a in result.apportionments}
        self.assertAlmostEqual(values['REV', 'A>B'], -7.0)

    def test_reverse_path_continuity_uses_directional_endpoint_losses(self):
        from ut_water_apportionment.loss_models import LossDefinition

        problem = SolverInput(
            beg_date='2000-01-01', end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id='A', type=ZoneTypes.USE),
                    Zone(id='B', type=ZoneTypes.USE),
                    Zone(id='C', type=ZoneTypes.USE),
                ],
                interzone_flows=[
                    InterzoneFlow(
                        id='A>B', from_zone='A', to_zone='B', bidirectional=True,
                        flow_measurements=[FlowMeasurement(measurement_id='A>B')],
                        loss_from_zone=LossDefinition.linear(0.20),
                        loss_to_zone=LossDefinition.linear(0.10),
                    ),
                    InterzoneFlow(
                        id='B>C', from_zone='B', to_zone='C', bidirectional=True,
                        flow_measurements=[FlowMeasurement(measurement_id='B>C')],
                        loss_from_zone=LossDefinition.linear(0.30),
                        loss_to_zone=LossDefinition.linear(0.25),
                    ),
                ],
            ),
            measurements=MeasurementCollection(
                beg_date='2000-01-01', end_date='2000-01-01',
                series=[
                    MeasurementSeries(id='A>B', values=[-6.3]),
                    MeasurementSeries(id='B>C', values=[-10]),
                ],
            ),
            txns=[
                PathTrxn(
                    id='REV', priority=1, upper_limit=10,
                    path=[
                        TrxnPathItem(flow_id='B>C', factor=-1, expected_values=[-10]),
                        TrxnPathItem(flow_id='A>B', factor=-1, expected_values=[-6.3]),
                    ],
                )
            ],
        )
        compile(problem).solve(check_expected_values=True)

    def test_reverse_component_on_non_bidirectional_flow_can_be_balanced_by_forward_slack(self):
        problem = SolverInput(
            beg_date="2000-01-01", end_date="2000-01-01",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                ],
                interzone_flows=[
                    InterzoneFlow(
                        id="A>B", from_zone="A", to_zone="B",
                        flow_measurements=[FlowMeasurement(measurement_id="Q")],
                    )
                ],
            ),
            measurements=MeasurementCollection(
                beg_date="2000-01-01", end_date="2000-01-01",
                series=[MeasurementSeries(id="Q", values=[5.0])],
            ),
            txns=[PathTrxn(
                id="REV", priority=1, upper_limit=2.0,
                path=[TrxnPathItem(flow_id="A>B", factor=-1)],
            )],
        )
        result = compile(problem).solve()
        value = next(a.value for a in result.apportionments if a.txn_id == "REV")
        self.assertAlmostEqual(value, -2.0)


if __name__ == '__main__':
    unittest.main()
