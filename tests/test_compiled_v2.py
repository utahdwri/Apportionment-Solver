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
    def test_simple_v2_solves_without_whole_day_fallback(self):
        plan = compile_solver_input_v2(_simple_input())
        actual = plan.solve()
        values = _result_map(actual)

        self.assertAlmostEqual(values[("2000-01-01", "TRXN_1", "RIVER>USER")], 3.0)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_2", "RIVER>USER")], 6.0)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_3", "RIVER>USER")], 3.0)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_4", "RIVER>USER")], 0.0)
        report = plan.report()
        self.assertEqual(report["whole_day_lp_fallbacks"], 0)
        self.assertGreater(report["program_count"], 0)

    def test_simple_v2_exposes_spreadsheet_style_remaining_constraints(self):
        problem = _simple_input()
        plan = compile_solver_input_v2(problem)
        plan.solve()
        text = plan.formulas()

        self.assertIn("V2P1: DIRECT MAXIMIZE TRXN_1___RIVER>USER", text)
        self.assertIn("constraint[MEAS_RIVER>USER].remaining", text)
        self.assertIn("constraint[NF_ZONE_RIVER].remaining_upper", text)
        self.assertIn("TRXN_2___RIVER>USER := 0 (monotone lower bound)", text)

    def test_v2_collapses_path_continuity_before_residual_kernel(self):
        problem = _reservoir_input()
        plan = compile_solver_input_v2(problem)
        plan.solve()
        text = plan.formulas()

        self.assertIn("zero-RHS equality CONT_TRXN_2_0", text)
        self.assertTrue(
            "TRXN_2___RIVER>STO := TRXN_2___RIVER>USER" in text
            or "TRXN_2___RIVER>USER := TRXN_2___RIVER>STO" in text
        )

    def test_reservoir_v2_uses_compiler_transformations(self):
        plan = compile_solver_input_v2(_reservoir_input())
        plan.solve()
        self.assertEqual(plan.report()["whole_day_lp_fallbacks"], 0)
        self.assertGreater(plan.report()["equality_eliminated"], 0)

    def test_v2_is_prepared_and_frozen_before_public_solve(self):
        plan = compile_solver_input_v2(_simple_input())
        report = plan.report()

        self.assertTrue(report["frozen"])
        self.assertFalse(report["runtime_compilation"])
        self.assertGreater(report["program_count"], 0)
        self.assertIn("FROZEN PARAMETERIZED IR", plan.formulas())

        program_count = report["program_count"]
        plan.solve()
        report = plan.report()
        self.assertEqual(report["program_count"], program_count)
        self.assertGreater(report["execution_cache_hits"], 0)
        self.assertEqual(report["execution_cache_misses"], 0)

    def test_frozen_v2_reuses_parameterized_bounds_for_new_measurements(self):
        problem = _simple_input()
        plan = compile_solver_input_v2(problem)
        program_count = plan.report()["program_count"]

        for flow in (8, 12, 20):
            with self.subTest(flow=flow):
                measurements = MeasurementCollection(
                    beg_date="2000-01-01",
                    end_date="2000-01-01",
                    series=[MeasurementSeries(id="1", values=[flow])],
                )
                actual = plan.solve(measurements=measurements)
                values = _result_map(actual)
                expected = {
                    8: (3.0, 5.0, 0.0, 0.0),
                    12: (3.0, 6.0, 3.0, 0.0),
                    20: (3.0, 6.0, 11.0, 0.0),
                }[flow]
                for index, expected_value in enumerate(expected, start=1):
                    self.assertAlmostEqual(
                        values[("2000-01-01", f"TRXN_{index}", "RIVER>USER")],
                        expected_value,
                    )
                self.assertEqual(plan.report()["program_count"], program_count)
                self.assertEqual(plan.report()["execution_cache_misses"], 0)

    def test_reservoir_days_reuse_frozen_parameterized_programs(self):
        plan = compile_solver_input_v2(_reservoir_input())
        program_count = plan.report()["program_count"]
        self.assertLess(program_count, 20)

        plan.solve()
        report = plan.report()
        self.assertEqual(report["program_count"], program_count)
        self.assertEqual(report["execution_cache_misses"], 0)
        self.assertGreaterEqual(report["execution_cache_hits"], 3 * program_count)


if __name__ == "__main__":
    unittest.main()
