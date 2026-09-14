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
    TrxnPathItem,
    Zone,
    ZoneTypes,
    compile_solver_input_v2,
    solve,
)


def _simple_input() -> SolverInput:
    return SolverInput(
        beg_date="2000-01-01",
        end_date="2000-01-01",
        accounting_graph=AccountingGraph(
            zones=[
                Zone(id="RIVER", type=ZoneTypes.STREAM),
                Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                Zone(id="USER", type=ZoneTypes.USE),
            ],
            interzone_flows=[
                InterzoneFlow(
                    id="RIVER>USER",
                    from_zone="RIVER",
                    to_zone="USER",
                    flow_measurements=[FlowMeasurement(measurement_id="1")],
                ),
                InterzoneFlow(
                    id="SYS>RIVER",
                    from_zone="SYS",
                    to_zone="RIVER",
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                    bidirectional=True,
                ),
            ],
        ),
        measurements=MeasurementCollection(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            series=[MeasurementSeries(id="1", values=[12])],
        ),
        txns=[
            PathTrxn(
                id="TRXN_1",
                priority=1,
                upper_limit=3,
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
            PathTrxn(
                id="TRXN_2",
                priority=2,
                upper_limit=6,
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
            PathTrxn(
                id="TRXN_3",
                priority=3,
                upper_limit=12,
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
            PathTrxn(
                id="TRXN_4",
                priority=4,
                upper_limit=4,
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
        ],
    )


def _reservoir_input() -> SolverInput:
    return SolverInput(
        beg_date="2000-01-01",
        end_date="2000-01-03",
        accounting_graph=AccountingGraph(
            zones=[
                Zone(id="RIVER", type=ZoneTypes.STREAM),
                Zone(id="STO", type=ZoneTypes.STORAGE, storage_meas_ids=["STO"]),
                Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                Zone(id="USER", type=ZoneTypes.USE),
            ],
            interzone_flows=[
                InterzoneFlow(
                    id="RIVER>STO",
                    from_zone="RIVER",
                    to_zone="STO",
                    bidirectional=True,
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                ),
                InterzoneFlow(
                    id="RIVER>USER",
                    from_zone="RIVER",
                    to_zone="USER",
                    flow_measurements=[FlowMeasurement(measurement_id="1")],
                ),
                InterzoneFlow(
                    id="SYS>RIVER",
                    from_zone="SYS",
                    to_zone="RIVER",
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                    bidirectional=True,
                ),
            ],
        ),
        measurements=MeasurementCollection(
            beg_date="1999-12-31",
            end_date="2000-01-03",
            series=[
                MeasurementSeries(id="1", values=[12, 12, 3, 10]),
                MeasurementSeries(id="STO", values=[1200, 1200, 1200, 1200]),
            ],
        ),
        txns=[
            PathTrxn(
                id="TRXN_1",
                priority=1,
                upper_limit=3,
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
            PathTrxn(
                id="TRXN_2",
                priority=2,
                upper_limit=6,
                path=[
                    TrxnPathItem(flow_id="RIVER>STO", factor=-1),
                    TrxnPathItem(flow_id="RIVER>USER"),
                ],
            ),
        ],
    )


def _result_map(result):
    return {
        (row.date, row.txn_id, row.interzone_flow_id): row.value
        for row in result.apportionments
    }


class CompiledV2Tests(unittest.TestCase):
    def assert_outputs_close(self, expected, actual):
        expected_map = _result_map(expected)
        actual_map = _result_map(actual)
        self.assertEqual(expected_map.keys(), actual_map.keys())
        for key, expected_value in expected_map.items():
            self.assertAlmostEqual(expected_value, actual_map[key], places=8, msg=str(key))

    def test_simple_v2_matches_lp_without_whole_day_fallback(self):
        problem = _simple_input()
        expected = solve(problem, solver_backend="scipy", generate_audit=False)
        plan = compile_solver_input_v2(problem)
        actual = plan.solve()

        self.assert_outputs_close(expected, actual)
        report = plan.report()
        self.assertEqual(report["whole_day_lp_fallbacks"], 0)
        self.assertGreater(report["program_count"], 0)

    def test_simple_v2_exposes_spreadsheet_style_remaining_constraints(self):
        problem = _simple_input()
        plan = compile_solver_input_v2(problem)
        plan.solve()
        text = plan.formulas()

        self.assertIn("V2P1: DIRECT MAXIMIZE TRXN_1___RIVER>USER", text)
        self.assertIn("constraint[MEAS_RIVER>USER].remaining_upper", text)
        self.assertIn("constraint[NF_ZONE_RIVER].remaining_upper", text)
        self.assertIn("TRXN_2___RIVER>USER := 0 (monotone lower bound)", text)

    def test_v2_collapses_path_continuity_before_residual_kernel(self):
        problem = _reservoir_input()
        plan = compile_solver_input_v2(problem)
        plan.solve()
        text = plan.formulas()

        self.assertIn("zero-RHS equality CONT_TRXN_2_0", text)
        self.assertIn(
            "TRXN_2___RIVER>STO := TRXN_2___RIVER>USER",
            text,
        )

    def test_reservoir_v2_matches_lp(self):
        problem = _reservoir_input()
        expected = solve(problem, solver_backend="scipy", generate_audit=False)
        plan = compile_solver_input_v2(problem)
        actual = plan.solve()

        self.assert_outputs_close(expected, actual)
        self.assertEqual(plan.report()["whole_day_lp_fallbacks"], 0)
        self.assertGreater(plan.report()["equality_eliminated"], 0)


if __name__ == "__main__":
    unittest.main()
