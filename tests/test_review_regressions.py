"""Proposed correctness regressions for the reviewed src(6).zip snapshot.

These tests intentionally expose bugs in the reviewed snapshot. They are NOT
an implementation patch, and they are expected to fail until the corresponding
issues in apportionment_solver_review.md are fixed.

Copy into the project's tests directory and run:
    python -m unittest tests.test_review_regressions -v
"""

from copy import deepcopy
from dataclasses import replace
import unittest

from ut_water_apportionment import (
    AccountingGraph, AccountingLimit, AccountingLimitInterval,
    FlowComponentsTypes, FlowMeasurement, InterzoneFlow, LossDefinition,
    MeasurementCollection, MeasurementSeries, NaturalFlowMode, PathTrxn,
    SolverInput, TrxnGroup, TrxnPathItem, Zone, ZoneTypes, compile,
    CompileOptions, solve,
)


def transaction(name, priority=1, limit: float | AccountingLimit | None=10, flow="D"):
    return PathTrxn(
        id=name, priority=priority, upper_limit=limit,
        path=[TrxnPathItem(flow)],
    )


def diversion_problem(txns, values=(10,), split=False):
    begin, end = "2025-01-01", f"2025-01-{len(values):02d}"
    zones = [
        Zone("S", ZoneTypes.SYSTEM_GAIN_LOSS),
        Zone("R", ZoneTypes.STREAM),
        Zone("U", ZoneTypes.USE),
    ]
    flows = [
        InterzoneFlow("D", "R", "U", flow_measurements=[FlowMeasurement("Q")]),
        InterzoneFlow(
            "G", "S", "R", bidirectional=True,
            flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
        ),
    ]
    series = [MeasurementSeries("Q", list(values))]
    if split:
        zones.append(Zone("U2", ZoneTypes.USE))
        flows.append(InterzoneFlow(
            "D2", "R", "U2", flow_measurements=[FlowMeasurement("Q2")],
        ))
        series.append(MeasurementSeries("Q2", [8.] * len(values)))
    return SolverInput(
        AccountingGraph(zones, flows), txns,
        MeasurementCollection(series, begin, end), begin, end,
    )


def allocation(output, txn_id, flow_id=None):
    return [
        a.value for a in output.apportionments
        if a.txn_id == txn_id
        and (flow_id is None or a.interzone_flow_id == flow_id)
    ]


class ReviewRegressions(unittest.TestCase):
    def test_replay_counterflow_when_nested(self):
        problem = SolverInput(
            AccountingGraph(
                zones=[
                    Zone("S", ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone("R", ZoneTypes.STREAM),
                    Zone("RES", ZoneTypes.STORAGE),
                    Zone("U", ZoneTypes.USE)],
                interzone_flows=[
                    InterzoneFlow("X", "R", "RES", bidirectional=True, flow_measurements=[FlowMeasurement("X")]),
                    InterzoneFlow("D", "R", "U", flow_measurements=[FlowMeasurement("D")]),
                    InterzoneFlow("G", "S", "R", bidirectional=True)
                ],
            ),
            [
                PathTrxn(id="FILL", priority=1, upper_limit=10, path=[TrxnPathItem("X", expected_values=[10])]),
                TrxnGroup(
                    "P", priority=2, upper_limit=0, children_trxns=[
                        PathTrxn("RELEASE", priority=3, upper_limit=10, path=[TrxnPathItem("X", factor=-1), TrxnPathItem("D", expected_values=[10])])
                    ],
                )
            ],
            MeasurementCollection(
                [
                    MeasurementSeries("X", [0]),
                    MeasurementSeries("D", [10])
                ],
                "2025-01-01", "2025-01-01",
            ),
            "2025-01-01", "2025-01-01",
        )
        solve(problem, check_expected_values=True)


    def test_source_endpoint_loss_is_charged_to_natural_flow(self):
        problem = diversion_problem([transaction("A")])
        diversion, gain = problem.accounting_graph.interzone_flows
        diversion.loss_from_zone = LossDefinition.linear(0.5)
        gain.natural_flow_mode = NaturalFlowMode.SPECIFIED
        gain.nf_measurements = [FlowMeasurement("NF")]
        problem.measurements = replace(
            problem.measurements,
            series=[*problem.measurements.series, MeasurementSeries("NF", [4])],
        )
        output = compile(problem).solve()
        # Four units at the source can supply only two beyond a 50% entry loss.
        self.assertEqual(allocation(output, "A"), [2.0])

    def test_transaction_ids_do_not_become_unsafe_python_locals(self):
        for name in ("state", "class", "min", "isfinite", "TOL"):
            with self.subTest(name=name):
                output = compile(diversion_problem([transaction(name)])).solve()
                self.assertEqual(allocation(output, name), [10.0])

    def test_renaming_a_flow_does_not_change_allocations(self):
        problem = diversion_problem(
            [transaction("NATURAL", priority=1, flow="D-1"),
             PathTrxn("STORAGE", priority=2, upper_limit=10,
                      path=[TrxnPathItem("I"), TrxnPathItem("D-1")])],
            split=True,
        )
        flows = problem.accounting_graph.interzone_flows
        flows[0].id = "D-1"
        flows[0].loss_from_zone = LossDefinition.linear(0.5)
        flows[2].id = "D_1"
        problem.accounting_graph.zones.append(Zone("RES", ZoneTypes.STORAGE))
        flows.append(InterzoneFlow(
            "I", "RES", "R", flow_measurements=[FlowMeasurement("I")],
        ))
        problem.measurements = replace(
            problem.measurements,
            series=[*problem.measurements.series, MeasurementSeries("I", [10])],
        )
        renamed = deepcopy(problem)
        renamed.accounting_graph.interzone_flows[2].id = "D_OTHER"
        before = compile(problem).solve()
        after = compile(renamed).solve()
        self.assertEqual(allocation(before, "NATURAL"), allocation(after, "NATURAL"))

    def test_negative_observations_are_normalized_before_residuals(self):
        problem = diversion_problem(
            [transaction("A", flow="D2")], values=(-5,), split=True,
        )
        output = compile(problem).solve()
        # Under the enabled coalescing policy, the two diversions are 0 and 8.
        self.assertEqual(allocation(output, "A"), [8.0])

    def test_bidirectional_stream_slack_does_not_cancel_natural_flow(self):
        problem = SolverInput(
            AccountingGraph(
                [Zone("S", ZoneTypes.SYSTEM_GAIN_LOSS),
                 Zone("R", ZoneTypes.STREAM), Zone("B", ZoneTypes.STREAM)],
                [InterzoneFlow("G", "S", "R", bidirectional=True),
                 InterzoneFlow("F", "R", "B", bidirectional=True,
                               flow_measurements=[FlowMeasurement("Q")])],
            ), [],
            MeasurementCollection([MeasurementSeries("Q", [10])],
                                  "2025-01-01", "2025-01-01"),
            "2025-01-01", "2025-01-01",
        )
        output = compile(problem).solve()
        nf_total = sum(
            a.value for a in output.apportionments
            if a.interzone_flow_id == "F" and a.txn_id.endswith("_NF")
        )
        self.assertEqual(nf_total, 10.0)

    def test_public_max_daily_apportionment_is_honored(self):
        problem = diversion_problem([transaction("A", limit=None)])
        output = solve(problem, CompileOptions(max_daily_apportionment=3))
        self.assertEqual(allocation(output, "A"), [3.0])


if __name__ == "__main__":
    unittest.main()
