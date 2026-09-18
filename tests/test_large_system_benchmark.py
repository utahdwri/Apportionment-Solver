"""Large-system benchmark for the block/formula compiler.

This benchmark is intentionally opt-in because wall-clock timings are hardware
sensitive and the default fixture contains 500 transactions.

Run it with::

    RUN_LARGE_SYSTEM_BENCHMARK=1 \
      python -m unittest tests.test_large_system_benchmark -v

The default fixture contains 10 connected stream zones and 500 allocation
transactions. Two layouts are benchmarked:

* shared priorities: 50 equal-priority cohorts of 10 transactions each;
* unique priorities: all 500 transactions have distinct priorities.

Compilation and repeated execution are timed separately. The benchmark reports
which compiled kernel types were produced, whether any numerical LP solves were
needed at runtime, and the size of the generated executable Python program.
"""

from __future__ import annotations

from dataclasses import dataclass
import gc
import os
from statistics import median
from time import perf_counter
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
    compile,
)


BENCHMARK_ENV = "RUN_LARGE_SYSTEM_BENCHMARK"


@dataclass(frozen=True)
class BenchmarkResult:
    layout: str
    stream_zones: int
    transactions: int
    priority_blocks: int
    compile_seconds: float
    solve_seconds: float
    solve_repeats: int
    generated_source_bytes: int
    runtime_slots: int
    direct_calculations: int
    proportional_calculations: int
    scalar_formulas: int
    lp_kernels: int
    execution_lp_solves: int
    maximum_formula_rows: int
    maximum_kernel_variables: int
    spill_replay_flows: int

    @property
    def transactions_per_second(self) -> float:
        if self.solve_seconds <= 0.0:
            return float("inf")
        return self.transactions / self.solve_seconds

    @property
    def compile_plus_solve_seconds(self) -> float:
        return self.compile_seconds + self.solve_seconds

    def render(self) -> str:
        return "\n".join(
            [
                "",
                f"=== LARGE SYSTEM BENCHMARK: {self.layout} ===",
                f"stream zones:               {self.stream_zones}",
                f"transactions:               {self.transactions}",
                f"priority blocks:            {self.priority_blocks}",
                f"runtime slots:              {self.runtime_slots}",
                f"generated Python:           {self.generated_source_bytes:,} bytes",
                "",
                f"direct calculations:        {self.direct_calculations}",
                f"proportional calculations:  {self.proportional_calculations}",
                f"scalar formulas:            {self.scalar_formulas}",
                f"LP kernels:                 {self.lp_kernels}",
                f"runtime LP solves:          {self.execution_lp_solves}",
                f"maximum formula rows:       {self.maximum_formula_rows}",
                f"maximum kernel variables:   {self.maximum_kernel_variables}",
                f"spill/replay flows:          {self.spill_replay_flows}",
                "",
                f"compilation:                {self.compile_seconds:.6f} s",
                f"solve:                      {self.solve_seconds:.6f} s "
                f"(median of {self.solve_repeats})",
                f"compile + one solve:        {self.compile_plus_solve_seconds:.6f} s",
                f"transaction throughput:     {self.transactions_per_second:,.0f} txn/s",
                "===============================================",
            ]
        )


def build_large_system_benchmark_input(
    *,
    stream_zone_count: int = 10,
    transactions_per_zone: int = 50,
    shared_priorities: bool = True,
) -> SolverInput:
    """Build a deterministic connected large-system benchmark.

    Each stream reach has a measured diversion and a system gain/loss residual.
    Adjacent stream zones are connected by measured mainstem flows, giving the
    fixture real network topology without making every right a long path.

    With ``shared_priorities=True``, the nth right at every reach has the same
    priority, producing 50 equal-priority cohorts of 10 members in the default
    500-right problem. With ``shared_priorities=False``, all 500 transactions
    have distinct priorities while the physical network remains identical.
    """

    if stream_zone_count < 10:
        raise ValueError("Benchmark requires at least 10 stream zones")
    if stream_zone_count * transactions_per_zone < 500:
        raise ValueError("Benchmark requires at least 500 transactions")

    zones = [Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS)]
    flows: list[InterzoneFlow] = []
    measurements: list[MeasurementSeries] = []
    txns: list[PathTrxn] = []

    for zone_index in range(stream_zone_count):
        stream_id = f"R{zone_index:02d}"
        use_id = f"U{zone_index:02d}"
        diversion_id = f"{stream_id}>DIV"
        diversion_measurement_id = f"DIV{zone_index:02d}"

        zones.extend(
            [
                Zone(id=stream_id, type=ZoneTypes.STREAM),
                Zone(id=use_id, type=ZoneTypes.USE),
            ]
        )
        flows.extend(
            [
                InterzoneFlow(
                    id=diversion_id,
                    from_zone=stream_id,
                    to_zone=use_id,
                    flow_measurements=[
                        FlowMeasurement(measurement_id=diversion_measurement_id)
                    ],
                ),
                InterzoneFlow(
                    id=f"SYS>{stream_id}",
                    from_zone="SYS",
                    to_zone=stream_id,
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                    bidirectional=True,
                ),
            ]
        )
        measurements.append(
            MeasurementSeries(id=diversion_measurement_id, values=[275.0])
        )

        for priority_index in range(transactions_per_zone):
            priority = (
                float(priority_index + 1)
                if shared_priorities
                else float(priority_index * stream_zone_count + zone_index + 1)
            )
            txns.append(
                PathTrxn(
                    id=f"T{zone_index:02d}_{priority_index:03d}",
                    priority=priority,
                    upper_limit=10.0,
                    path=[TrxnPathItem(flow_id=diversion_id)],
                )
            )

        if zone_index < stream_zone_count - 1:
            next_stream_id = f"R{zone_index + 1:02d}"
            mainstem_id = f"{stream_id}>{next_stream_id}"
            mainstem_measurement_id = f"MAIN{zone_index:02d}"
            flows.append(
                InterzoneFlow(
                    id=mainstem_id,
                    from_zone=stream_id,
                    to_zone=next_stream_id,
                    flow_measurements=[
                        FlowMeasurement(measurement_id=mainstem_measurement_id)
                    ],
                )
            )
            measurements.append(
                MeasurementSeries(id=mainstem_measurement_id, values=[1000.0])
            )

    return SolverInput(
        beg_date="2000-01-01",
        end_date="2000-01-01",
        accounting_graph=AccountingGraph(
            zones=zones,
            interzone_flows=flows,
        ),
        measurements=MeasurementCollection(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            series=measurements,
        ),
        txns=txns,
    )


def _median_seconds(fn, repeats: int, *, collect: bool = False):
    values = []
    result = None
    for _ in range(repeats):
        start = perf_counter()
        current = fn()
        values.append(perf_counter() - start)
        result = current
        if collect:
            gc.collect()
    return median(values), result


@unittest.skipUnless(
    os.environ.get(BENCHMARK_ENV) == "1",
    f"set {BENCHMARK_ENV}=1 to run the large-system timing benchmark",
)
class LargeSystemBenchmarkTests(unittest.TestCase):
    def _run_benchmark(self, *, shared_priorities: bool) -> BenchmarkResult:
        stream_zones = int(os.environ.get("BENCHMARK_STREAM_ZONES", "10"))
        transactions_per_zone = int(
            os.environ.get("BENCHMARK_TRANSACTIONS_PER_ZONE", "50")
        )
        compile_repeats = int(os.environ.get("BENCHMARK_COMPILE_REPEATS", "1"))
        solve_repeats = int(os.environ.get("BENCHMARK_SOLVE_REPEATS", "10"))

        problem = build_large_system_benchmark_input(
            stream_zone_count=stream_zones,
            transactions_per_zone=transactions_per_zone,
            shared_priorities=shared_priorities,
        )

        actual_stream_zones = sum(
            zone.type == ZoneTypes.STREAM
            for zone in problem.accounting_graph.zones
        )
        self.assertGreaterEqual(actual_stream_zones, 10)
        self.assertGreaterEqual(len(problem.txns), 500)

        compile_seconds, plan = _median_seconds(
            lambda: compile(problem),
            compile_repeats,
            collect=True,
        )
        assert plan is not None

        # Warm once before timing repeated execution. This also gives us the
        # compiler/execution report used below.
        warm_output = plan.solve()
        report = warm_output.compilation_report

        solve_seconds, output = _median_seconds(
            plan.solve,
            solve_repeats,
        )
        assert output is not None

        # The benchmark fixture should compile completely to formulas. If this
        # changes, the report makes the reason immediately visible.
        self.assertFalse(report["runtime_compilation"])
        self.assertEqual(report["execution_days"], 1)
        self.assertEqual(report["execution_lp_solves"], 0)
        self.assertEqual(report["lp_kernels"], 0)

        if shared_priorities:
            self.assertGreater(report["proportional_calculations"], 0)
        else:
            # One direct calculation for each transaction in Pass 1 and replay.
            self.assertEqual(
                report["direct_calculations"],
                2 * len(problem.txns),
            )
            self.assertEqual(report["proportional_calculations"], 0)

        benchmark = BenchmarkResult(
            layout="SHARED PRIORITIES" if shared_priorities else "UNIQUE PRIORITIES",
            stream_zones=actual_stream_zones,
            transactions=len(problem.txns),
            priority_blocks=report["priority_blocks"],
            compile_seconds=compile_seconds,
            solve_seconds=solve_seconds,
            solve_repeats=solve_repeats,
            generated_source_bytes=len(plan.code().encode("utf-8")),
            runtime_slots=report["runtime_slots"],
            direct_calculations=report["direct_calculations"],
            proportional_calculations=report["proportional_calculations"],
            scalar_formulas=report["scalar_formulas"],
            lp_kernels=report["lp_kernels"],
            execution_lp_solves=report["execution_lp_solves"],
            maximum_formula_rows=report["maximum_formula_rows"],
            maximum_kernel_variables=report["maximum_kernel_variables"],
            spill_replay_flows=report["spill_replay_flows"],
        )
        print(benchmark.render(), flush=True)
        return benchmark

    def test_shared_priority_compile_and_solve(self):
        self._run_benchmark(shared_priorities=True)

    def test_unique_priority_compile_and_solve(self):
        self._run_benchmark(shared_priorities=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
