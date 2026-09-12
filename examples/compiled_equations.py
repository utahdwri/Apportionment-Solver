"""Run from an installed checkout: python examples/compiled_equations.py."""

from pathlib import Path

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
    compile_solver_input,
)

problem = SolverInput(
    beg_date="2000-01-01",
    end_date="2000-01-03",
    accounting_graph=AccountingGraph(
        zones=[
            Zone("river", ZoneTypes.STREAM),
            Zone("farm", ZoneTypes.USE),
            Zone("system", ZoneTypes.SYSTEM_GAIN_LOSS),
        ],
        interzone_flows=[
            InterzoneFlow(
                "diversion",
                "river",
                "farm",
                flow_measurements=[FlowMeasurement("gage")],
            ),
            InterzoneFlow(
                "gain",
                "system",
                "river",
                bidirectional=True,
                flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
            ),
        ],
    ),
    measurements=MeasurementCollection(
        beg_date="2000-01-01",
        end_date="2000-01-03",
        series=[MeasurementSeries("gage", [9, 6, 12])],
    ),
    txns=[
        PathTrxn(
            "right_a", priority=1, upper_limit=4, path=[TrxnPathItem("diversion")]
        ),
        PathTrxn(
            "right_b", priority=1, upper_limit=8, path=[TrxnPathItem("diversion")]
        ),
    ],
)

plan = compile_solver_input(problem)
# Preparation already exposes individual upper bounds. Executing also records
# the encountered equal-priority, counterflow, and finalization programs.
result = plan.solve()
for record in result.apportionments:
    if record.txn_id in {"right_a", "right_b"}:
        print(record.date, record.txn_id, round(record.value, 6))
print(plan.report())

Path("compiled-formulas.txt").write_text(plan.formulas(), encoding="utf-8")
Path("compiled-programs.py").write_text(plan.code(), encoding="utf-8")
Path("compiled-execution.txt").write_text(plan.execution_outline(), encoding="utf-8")
