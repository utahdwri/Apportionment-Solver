"""Compare constant and endogenous piecewise routing on a synthetic river.

Run from the repository root:
    python -m benchmarks.benchmark_piecewise --sites 1 --days 7 --repeat 3

Timings include model construction, both allocation passes, and result assembly.
The nonzero curves change the physics, so these are workload costs, not speedups.
"""

import argparse
import json
import logging
import platform
import statistics
import time
from dataclasses import asdict
from datetime import date, timedelta
from importlib.metadata import version

from benchmarks.benchmark_scaling import make_input
from ut_water_apportionment import (
    AccountingGraph,
    FlowComponentsTypes,
    FlowMeasurement,
    InterzoneFlow,
    LossCurvePoint,
    LossDefinition,
    MeasurementCollection,
    MeasurementSeries,
    PathTrxn,
    SolverInput,
    TrxnPathItem,
    Zone,
    ZoneTypes,
    solve,
)


def loss_curve(scale):
    return LossDefinition.piecewise_linear(
        [
            LossCurvePoint(0, 0),
            LossCurvePoint(0.2 * scale, 0.2 * scale),
            LossCurvePoint(0.6 * scale, 0.4 * scale),
            LossCurvePoint(scale, 0.44 * scale),
        ]
    )


def make_path_input(reaches, rights, days, sites):
    """All rights cross the same measured import-to-use path in priority order."""
    start = date(2000, 1, 1)
    end = start + timedelta(days=days - 1)
    zones = [
        Zone("I", ZoneTypes.IMPORT),
        Zone("U", ZoneTypes.USE),
        Zone("SYS", ZoneTypes.SYSTEM_GAIN_LOSS),
    ]
    zones.extend(Zone(f"R{i}", ZoneTypes.STREAM) for i in range(reaches))
    flows, measurements, path = [], [], []
    total = float(rights)
    for i in range(reaches + 1):
        fid = f"P{i}"
        configured = 0 < i <= sites
        loss = loss_curve(total) if configured else LossDefinition()
        flows.append(
            InterzoneFlow(
                fid,
                "I" if i == 0 else f"R{i - 1}",
                "U" if i == reaches else f"R{i}",
                flow_measurements=[FlowMeasurement(fid)],
                loss_to_zone=loss,
            )
        )
        measurements.append(MeasurementSeries(fid, [total] * days))
        path.append(TrxnPathItem(fid))
        total = loss.transform_total_flow(total)
    for i in range(reaches):
        flows.append(
            InterzoneFlow(
                f"G{i}",
                "SYS",
                f"R{i}",
                bidirectional=True,
                flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
            )
        )
    return SolverInput(
        beg_date=str(start),
        end_date=str(end),
        accounting_graph=AccountingGraph(zones, flows),
        measurements=MeasurementCollection(
            beg_date=str(start),
            end_date=str(end),
            series=measurements,
        ),
        txns=[
            PathTrxn(f"T{i}", priority=i, upper_limit=1, path=path)
            for i in range(rights)
        ],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workload", choices=["routing", "path"], default="routing")
    parser.add_argument("--reaches", type=int, default=20)
    parser.add_argument("--rights", type=int, default=10)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--sites", type=int, default=1)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--proportional", action="store_true")
    parser.add_argument(
        "--loss-attribution-method",
        choices=["depletion", "buildup"],
        default="depletion",
    )
    parser.add_argument(
        "--backend", choices=["auto", "highspy", "scip"], default="auto"
    )
    parser.add_argument("--output", help="Save all results from the final repetition")
    args = parser.parse_args()
    if not 0 <= args.sites < args.reaches:
        parser.error("--sites must be between 0 and reaches - 1")
    if min(args.reaches, args.rights, args.days, args.repeat) < 1:
        parser.error("reaches, rights, days and repeat must be positive")
    logging.disable(logging.CRITICAL)
    durations = []
    for _ in range(args.repeat):
        if args.workload == "path":
            problem = make_path_input(args.reaches, args.rights, args.days, args.sites)
            if args.proportional:
                for trxn in problem.txns:
                    trxn.priority = 1
        else:
            problem = make_input(
                args.reaches, args.rights, args.days, args.proportional
            )
            configured = 0
            for flow in problem.accounting_graph.interzone_flows:
                if flow.id.startswith("C") and configured < args.sites:
                    reach = int(flow.id[1:])
                    scale = args.rights * (args.reaches - reach)
                    flow.loss_to_zone = loss_curve(scale)
                    configured += 1
        problem.loss_attribution_method = args.loss_attribution_method
        started = time.perf_counter()
        result = solve(problem, solver_backend=args.backend, generate_audit=args.audit)
        durations.append(time.perf_counter() - started)
    if args.output:
        with open(args.output, "w") as stream:
            json.dump(asdict(result), stream, default=str)
    print(
        json.dumps(
            {
                **vars(args),
                "seconds": durations,
                "median_seconds": statistics.median(durations),
                "apportionments": len(result.apportionments),
                "python": platform.python_version(),
                "highspy": version("highspy"),
                "solver_backend": result.solver_backend,
                "pyscipopt": version("pyscipopt")
                if result.solver_backend == "scip"
                else None,
            }
        )
    )


if __name__ == "__main__":
    main()
