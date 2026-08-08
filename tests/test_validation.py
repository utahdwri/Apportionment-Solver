"""Validation tests for public input models and graph/schedule relationships.

These tests intentionally avoid calling solve().  Validation should fail before
an LP backend is needed, which keeps failures focused and makes this test file
independent of which solver backends are installed.
"""

import math
import unittest

from ut_water_apportionment import (
    AccountingGraph,
    AccountingLimit,
    AccountingLimitInterval,
    FlowComponentsTypes,
    FlowMeasurement,
    InterzoneFlow,
    MeasurementCollection,
    MeasurementSeries,
    SolverInput,
    Trxn,
    TrxnGroup,
    TrxnPathItem,
    Zone,
    ZoneTypes,
)
from ut_water_apportionment.graph_manager import GraphManager
from ut_water_apportionment.models import DEFAULT_TRXN_PRIORITY
from ut_water_apportionment.trxn_schedule import TrxnSchedule


def _simple_graph() -> AccountingGraph:
    return AccountingGraph(
        zones=[
            Zone("A", ZoneTypes.USE),
            Zone("B", ZoneTypes.USE),
        ],
        interzone_flows=[
            InterzoneFlow(
                id="A>B",
                from_zone="A",
                to_zone="B",
            )
        ],
    )


class TransactionModelValidationTests(unittest.TestCase):

    def test_transaction_rejects_negative_upper_limit(self):
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            Trxn(
                id="T1",
                path=[],
                upper_limit=-1.0,
            )

    def test_transaction_group_rejects_negative_upper_limit(self):
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            TrxnGroup(
                id="G1",
                children_trxns=[],
                wrnum=None,
                upper_limit=-1.0,
            )

    def test_transaction_rejects_negative_priority(self):
        with self.assertRaisesRegex(ValueError, "priority must be"):
            Trxn(
                id="T1",
                path=[],
                upper_limit=None,
                priority=-1,
            )

    def test_transaction_rejects_priority_above_maximum(self):
        with self.assertRaisesRegex(ValueError, "priority must be"):
            Trxn(
                id="T1",
                path=[],
                upper_limit=None,
                priority=DEFAULT_TRXN_PRIORITY + 1,
            )

    def test_transaction_group_rejects_invalid_priority(self):
        with self.assertRaisesRegex(ValueError, "priority must be"):
            TrxnGroup(
                id="G1",
                children_trxns=[],
                wrnum=None,
                priority=-1,
            )


class TransactionPathItemValidationTests(unittest.TestCase):

    def test_zero_factor_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "finite, non-zero"):
            TrxnPathItem(
                flow_id="A>B",
                factor=0,
            )

    def test_non_finite_factor_is_rejected(self):
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "finite, non-zero"):
                    TrxnPathItem(
                        flow_id="A>B",
                        factor=value,
                    )

    def test_negative_factor_is_allowed(self):
        item = TrxnPathItem(
            flow_id="A>B",
            factor=-1,
        )
        self.assertEqual(item.factor, -1)

    def test_loss_before_must_be_fraction(self):
        for value in (-0.01, 1.01):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "loss_before"):
                    TrxnPathItem(
                        flow_id="A>B",
                        loss_before=value,
                    )

    def test_loss_after_must_be_fraction(self):
        for value in (-0.01, 1.01):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "loss_after"):
                    TrxnPathItem(
                        flow_id="A>B",
                        loss_after=value,
                    )


class AccountingLimitValidationTests(unittest.TestCase):

    def test_negative_interval_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            AccountingLimitInterval(
                beg_date="2026-01-01",
                end_date="2026-02-01",
                value=-1,
            )

    def test_invalid_interval_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            AccountingLimitInterval(
                beg_date="2026-02-30",
                end_date="2026-03-01",
                value=1,
            )

    def test_interval_requires_begin_before_end(self):
        with self.assertRaisesRegex(ValueError, "beg_date must be before end_date"):
            AccountingLimitInterval(
                beg_date="2026-02-01",
                end_date="2026-02-01",
                value=1,
            )

    def test_overlapping_intervals_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "intervals overlap"):
            AccountingLimit(
                intervals=[
                    AccountingLimitInterval(
                        beg_date="2026-01-01",
                        end_date="2026-03-01",
                        value=10,
                    ),
                    AccountingLimitInterval(
                        beg_date="2026-02-01",
                        end_date="2026-04-01",
                        value=20,
                    ),
                ]
            )

    def test_adjacent_intervals_are_allowed(self):
        limit = AccountingLimit(
            intervals=[
                AccountingLimitInterval(
                    beg_date="2026-01-01",
                    end_date="2026-02-01",
                    value=10,
                ),
                AccountingLimitInterval(
                    beg_date="2026-02-01",
                    end_date="2026-03-01",
                    value=20,
                ),
            ]
        )
        self.assertEqual(len(limit.intervals), 2)


class InterzoneFlowValidationTests(unittest.TestCase):

    def test_fractional_lag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "whole number of days"):
            InterzoneFlow(
                id="A>B",
                from_zone="A",
                to_zone="B",
                lag_to_zone=1.5,
            )

    def test_negative_lag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            InterzoneFlow(
                id="A>B",
                from_zone="A",
                to_zone="B",
                lag_from_zone=-1,
            )

    def test_boolean_lag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "whole number of days"):
            InterzoneFlow(
                id="A>B",
                from_zone="A",
                to_zone="B",
                lag_to_zone=True,
            )


class MeasurementCollectionValidationTests(unittest.TestCase):

    def test_end_date_before_begin_date_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "before beg_date"):
            MeasurementCollection(
                beg_date="2026-01-02",
                end_date="2026-01-01",
                series=[],
            )

    def test_invalid_collection_date_is_rejected(self):
        with self.assertRaises(ValueError):
            MeasurementCollection(
                beg_date="2026-02-30",
                end_date="2026-03-01",
                series=[],
            )

    def test_duplicate_measurement_ids_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate measurement id"):
            MeasurementCollection(
                beg_date="2026-01-01",
                end_date="2026-01-01",
                series=[
                    MeasurementSeries("M1", [1.0]),
                    MeasurementSeries("M1", [2.0]),
                ],
            )

    def test_wrong_series_length_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "expected 2"):
            MeasurementCollection(
                beg_date="2026-01-01",
                end_date="2026-01-02",
                series=[
                    MeasurementSeries("M1", [1.0]),
                ],
            )

    def test_unknown_measurement_id_is_rejected(self):
        measurements = MeasurementCollection(
            beg_date="2026-01-01",
            end_date="2026-01-01",
            series=[
                MeasurementSeries("M1", [1.0]),
            ],
        )

        with self.assertRaisesRegex(ValueError, "not recognized"):
            measurements.get(
                "DOES_NOT_EXIST",
                "2026-01-01",
            )


class AccountingGraphValidationTests(unittest.TestCase):

    def test_duplicate_zone_ids_are_rejected(self):
        graph = AccountingGraph(
            zones=[
                Zone("A", ZoneTypes.USE),
                Zone("A", ZoneTypes.STREAM),
            ],
            interzone_flows=[],
        )

        with self.assertRaisesRegex(ValueError, "duplicate zone IDs"):
            GraphManager(graph)

    def test_duplicate_flow_ids_are_rejected(self):
        graph = AccountingGraph(
            zones=[
                Zone("A", ZoneTypes.USE),
                Zone("B", ZoneTypes.USE),
            ],
            interzone_flows=[
                InterzoneFlow("F", "A", "B"),
                InterzoneFlow("F", "A", "B"),
            ],
        )

        with self.assertRaisesRegex(ValueError, "duplicate interzone-flow IDs"):
            GraphManager(graph)

    def test_unknown_from_zone_is_rejected(self):
        graph = AccountingGraph(
            zones=[Zone("B", ZoneTypes.USE)],
            interzone_flows=[
                InterzoneFlow("F", "MISSING", "B"),
            ],
        )

        with self.assertRaisesRegex(ValueError, "unknown from_zone"):
            GraphManager(graph)

    def test_unknown_to_zone_is_rejected(self):
        graph = AccountingGraph(
            zones=[Zone("A", ZoneTypes.USE)],
            interzone_flows=[
                InterzoneFlow("F", "A", "MISSING"),
            ],
        )

        with self.assertRaisesRegex(ValueError, "unknown to_zone"):
            GraphManager(graph)

    def test_multiple_residual_gain_routes_are_rejected(self):
        graph = AccountingGraph(
            zones=[
                Zone("SYS1", ZoneTypes.SYSTEM_GAIN_LOSS),
                Zone("SYS2", ZoneTypes.SYSTEM_GAIN_LOSS),
                Zone("A", ZoneTypes.STREAM),
            ],
            interzone_flows=[
                InterzoneFlow(
                    id="SYS1>A",
                    from_zone="SYS1",
                    to_zone="A",
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                    flow_measurements=[FlowMeasurement("dummy-1")],
                    residual_for_gains=True,
                    residual_for_losses=True,
                ),
                InterzoneFlow(
                    id="SYS2>A",
                    from_zone="SYS2",
                    to_zone="A",
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                    flow_measurements=[FlowMeasurement("dummy-2")],
                    residual_for_gains=True,
                    residual_for_losses=False,
                ),
            ],
        )

        with self.assertRaisesRegex(ValueError, "exactly one residual gain route"):
            GraphManager(graph)

    def test_missing_residual_loss_route_is_rejected(self):
        graph = AccountingGraph(
            zones=[
                Zone("SYS", ZoneTypes.SYSTEM_GAIN_LOSS),
                Zone("A", ZoneTypes.STREAM),
            ],
            interzone_flows=[
                InterzoneFlow(
                    id="SYS>A",
                    from_zone="SYS",
                    to_zone="A",
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                    flow_measurements=[FlowMeasurement("dummy")],
                    residual_for_gains=True,
                    residual_for_losses=False,
                ),
            ],
        )

        with self.assertRaisesRegex(ValueError, "exactly one residual loss route"):
            GraphManager(graph)


class TransactionScheduleValidationTests(unittest.TestCase):

    def test_duplicate_transaction_id_inside_group_is_rejected(self):
        gm = GraphManager(_simple_graph())

        trxns = [
            TrxnGroup(
                id="DUPLICATE",
                wrnum=None,
                children_trxns=[
                    Trxn(
                        id="DUPLICATE",
                        path=[TrxnPathItem("A>B")],
                        upper_limit=10,
                    )
                ],
            )
        ]

        with self.assertRaisesRegex(ValueError, "Duplicate transaction id"):
            TrxnSchedule(gm, trxns) # type: ignore

    def test_bad_flow_reference_inside_group_is_rejected(self):
        gm = GraphManager(_simple_graph())

        trxns = [
            TrxnGroup(
                id="GROUP",
                wrnum=None,
                children_trxns=[
                    Trxn(
                        id="T1",
                        path=[TrxnPathItem("DOES_NOT_EXIST")],
                        upper_limit=10,
                    )
                ],
            )
        ]

        with self.assertRaisesRegex(ValueError, "unknown interzone-flow"):
            TrxnSchedule(gm, trxns)# type: ignore

    def test_disconnected_transaction_path_is_rejected(self):
        graph = AccountingGraph(
            zones=[
                Zone("A", ZoneTypes.USE),
                Zone("B", ZoneTypes.USE),
                Zone("C", ZoneTypes.USE),
                Zone("D", ZoneTypes.USE),
            ],
            interzone_flows=[
                InterzoneFlow("A>B", "A", "B"),
                InterzoneFlow("C>D", "C", "D"),
            ],
        )
        gm = GraphManager(graph)

        with self.assertRaisesRegex(ValueError, "does not form one continuous path"):
            TrxnSchedule(
                gm,
                [
                    Trxn(
                        id="T1",
                        path=[
                            TrxnPathItem("A>B"),
                            TrxnPathItem("C>D"),
                        ],
                        upper_limit=10,
                    )
                ],
            )

    def test_branched_transaction_path_is_rejected(self):
        graph = AccountingGraph(
            zones=[
                Zone("A", ZoneTypes.USE),
                Zone("B", ZoneTypes.USE),
                Zone("C", ZoneTypes.USE),
            ],
            interzone_flows=[
                InterzoneFlow("A>B", "A", "B"),
                InterzoneFlow("A>C", "A", "C"),
            ],
        )
        gm = GraphManager(graph)

        with self.assertRaisesRegex(ValueError, "branches at zone"):
            TrxnSchedule(
                gm,
                [
                    Trxn(
                        id="T1",
                        path=[
                            TrxnPathItem("A>B"),
                            TrxnPathItem("A>C"),
                        ],
                        upper_limit=10,
                    )
                ],
            )

    def test_cyclic_transaction_path_is_rejected(self):
        graph = AccountingGraph(
            zones=[
                Zone("A", ZoneTypes.USE),
                Zone("B", ZoneTypes.USE),
            ],
            interzone_flows=[
                InterzoneFlow("A>B", "A", "B"),
                InterzoneFlow("B>A", "B", "A"),
            ],
        )
        gm = GraphManager(graph)

        with self.assertRaisesRegex(ValueError, "does not form one continuous path"):
            TrxnSchedule(
                gm,
                [
                    Trxn(
                        id="T1",
                        path=[
                            TrxnPathItem("A>B"),
                            TrxnPathItem("B>A"),
                        ],
                        upper_limit=10,
                    )
                ],
            )


class SolverInputValidationTests(unittest.TestCase):

    def test_solver_input_rejects_invalid_date(self):
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            SolverInput(
                accounting_graph=AccountingGraph([], []),
                txns=[],
                measurements=MeasurementCollection(
                    beg_date="2026-01-01",
                    end_date="2026-01-10",
                    series=[],
                ),
                beg_date="2026-01-XX",
                end_date="2026-01-05",
            )


    def test_solver_input_rejects_reversed_dates(self):
        with self.assertRaisesRegex(ValueError, "before beg_date"):
            SolverInput(
                accounting_graph=AccountingGraph([], []),
                txns=[],
                measurements=MeasurementCollection(
                    beg_date="2026-01-01",
                    end_date="2026-01-10",
                    series=[],
                ),
                beg_date="2026-01-06",
                end_date="2026-01-05",
            )


    def test_solver_input_requires_measurement_coverage(self):
        with self.assertRaisesRegex(
            ValueError,
            "contained within the measurement date range",
        ):
            SolverInput(
                accounting_graph=AccountingGraph([], []),
                txns=[],
                measurements=MeasurementCollection(
                    beg_date="2026-01-01",
                    end_date="2026-01-10",
                    series=[],
                ),
                beg_date="2026-01-05",
                end_date="2026-01-11",
            )


if __name__ == "__main__":
    unittest.main()
