"""Deterministic end-to-end benchmark; run with PYTHONPATH=<checkout>/src.

Example: python benchmarks/benchmark_scaling.py --reaches 20 --rights 10 --days 30
Use --output to retain every apportionment for differential verification.
"""
import argparse
import cProfile
import json
import logging
import platform
import statistics
import time
from dataclasses import asdict
from datetime import date, timedelta
from functools import partial
from unittest.mock import patch

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
    solve,
)


def make_input(reaches=20, rights=10, days=30, proportional=False):
    start = date(2000, 1, 1)
    end = start + timedelta(days=days - 1)
    zones = [Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS)]
    flows, measurements, txns = [], [], []
    for i in range(reaches):
        river, user = f"R{i}", f"U{i}"
        zones.extend([Zone(id=river, type=ZoneTypes.STREAM),
                      Zone(id=user, type=ZoneTypes.USE)])
        flows.append(InterzoneFlow(
            id=f"G{i}", from_zone="SYS", to_zone=river, bidirectional=True,
            flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE))
        flows.append(InterzoneFlow(id=f"D{i}", from_zone=river, to_zone=user,
                                   flow_measurements=[FlowMeasurement(measurement_id=f"D{i}")]))
        measurements.append(MeasurementSeries(
            id=f"D{i}", values=[rights * (0.6 + (d % 7) / 20) for d in range(days)]))
        if i + 1 < reaches:
            flows.append(InterzoneFlow(id=f"C{i}", from_zone=river, to_zone=f"R{i+1}",
                                       flow_measurements=[FlowMeasurement(measurement_id=f"C{i}")]))
            measurements.append(MeasurementSeries(
                id=f"C{i}", values=[rights * (reaches - i) for _ in range(days)]))
        for j in range(rights):
            txns.append(PathTrxn(id=f"T{i}_{j}",
                                 priority=j if proportional else j * reaches + i,
                                 upper_limit=1.0 + (j % 3) * 0.1,
                                 path=[TrxnPathItem(flow_id=f"D{i}")]))
    return SolverInput(
        beg_date=str(start), end_date=str(end),
        accounting_graph=AccountingGraph(zones=zones, interzone_flows=flows),
        measurements=MeasurementCollection(beg_date=str(start), end_date=str(end), series=measurements),
        txns=txns)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reaches", type=int, default=20)
    parser.add_argument("--rights", type=int, default=10)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--backend", default="highspy")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--proportional", action="store_true")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output")
    parser.add_argument("--profile")
    parser.add_argument("--simplex-strategy", type=int)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    from ut_water_apportionment.lp_solver import (
        ResolvedSolverBackend,
        resolve_solver_backend,
    )
    resolved = resolve_solver_backend(args.backend)
    if args.simplex_strategy is not None:
        if args.backend != "highspy":
            parser.error("--simplex-strategy requires --backend highspy")
        resolved = ResolvedSolverBackend(
            name=resolved.name,
            factory=partial(resolved.factory, simplex_strategy=args.simplex_strategy))
    durations = []
    profiler = cProfile.Profile() if args.profile else None
    if profiler:
        profiler.enable()
    for _ in range(args.repeat):
        problem = make_input(args.reaches, args.rights, args.days, args.proportional)
        started = time.perf_counter()
        with patch("ut_water_apportionment.solver.resolve_solver_backend", return_value=resolved):
            result = solve(problem, solver_backend=args.backend, generate_audit=args.audit)
        durations.append(time.perf_counter() - started)
    if profiler:
        profiler.disable()
        profiler.dump_stats(args.profile)
    if args.output:
        with open(args.output, "w") as stream:
            json.dump(asdict(result), stream, default=str)
    print(json.dumps({**vars(args), "seconds": durations,
                      "median_seconds": statistics.median(durations),
                      "apportionments": len(result.apportionments),
                      "python": platform.python_version()}))


if __name__ == "__main__":
    main()
