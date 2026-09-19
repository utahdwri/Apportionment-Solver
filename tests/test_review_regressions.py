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


if __name__ == "__main__":
    unittest.main()
