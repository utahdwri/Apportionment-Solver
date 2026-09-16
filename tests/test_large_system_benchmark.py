"""Large-system V2 vs. production SciPy benchmark.

This benchmark is intentionally opt-in because its wall-clock timings are too
slow and hardware-sensitive for the ordinary unit-test suite.

Run it with::

    RUN_LARGE_SYSTEM_BENCHMARK=1 \
      python -m unittest tests.test_large_system_benchmark -v

The default fixture contains 10 connected stream zones and 500 allocation
transactions (50 shared priority cohorts of 10 transactions each).  The V2
compiler is timed separately from repeated daily execution.  The comparison
solver uses the same production LP formulation with the internal SciPy/HiGHS
engine and no V2 compilation.
"""

from __future__ import annotations

from copy import deepcopy
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
    SolverOutput,
    TrxnPathItem,
    Zone,
    ZoneTypes,
    compile_solver_input_v2,
)
from ut_water_apportionment.compiled_v2.lp_engine import LPSolver as ScipyLPSolver
from ut_water_apportionment.graph_manager import GraphManager
from ut_water_apportionment.lag_utils import unlag_apportionments
from ut_water_apportionment.natural_flow_calculator import NaturalFlowCalculator
from ut_water_apportionment.solver import _loop_through_date_range, _solve_day
from ut_water_apportionment.timeseries_manager import DailyDataManager
from ut_water_apportionment.trxn_schedule import TrxnSchedule


BENCHMARK_ENV = "RUN_LARGE_SYSTEM_BENCHMARK"


@dataclass(frozen=True)
class BenchmarkResult:
    stream_zones: int
    transactions: int
    production_variables: int
    production_constraints: int
    scipy_lp_solves_per_day: int
    compiled_programs: int
    v2_compile_seconds: float
    v2_daily_seconds: float
    scipy_daily_seconds: float
    v2_solve_repeats: int = 1
    scipy_solve_repeats: int = 1

    @property
    def daily_speedup(self) -> float:
        """SciPy daily time / V2 daily time (>1 means V2 is faster)."""
        return self.scipy_daily_seconds / self.v2_daily_seconds

    @property
    def first_day_speedup(self) -> float:
        """SciPy first day / (V2 compile + first day)."""
        return self.scipy_daily_seconds / (
            self.v2_compile_seconds + self.v2_daily_seconds
        )

    def render(self) -> str:
        daily_label = (
            f"{self.daily_speedup:.2f}x faster"
            if self.daily_speedup >= 1.0
            else f"{1.0 / self.daily_speedup:.2f}x slower"
        )
        first_day_label = (
            f"{self.first_day_speedup:.2f}x faster"
            if self.first_day_speedup >= 1.0
            else f"{1.0 / self.first_day_speedup:.2f}x slower"
        )
        return "\n".join(
            [
                "",
                "=== LARGE SYSTEM BENCHMARK ===",
                f"stream zones:              {self.stream_zones}",
                f"transactions:              {self.transactions}",
                f"production LP variables:   {self.production_variables}",
                f"production LP constraints: {self.production_constraints}",
                f"SciPy LP solves/day:        {self.scipy_lp_solves_per_day}",
                f"V2 compiled programs:       {self.compiled_programs}",
                f"V2 compilation:             {self.v2_compile_seconds:.6f} s",
                f"V2 daily solve:             {self.v2_daily_seconds:.6f} s "
                f"(median of {self.v2_solve_repeats})",
                f"SciPy daily solve:          {self.scipy_daily_seconds:.6f} s "
                f"(median of {self.scipy_solve_repeats})",
                f"V2 daily vs SciPy:          {daily_label}",
                f"V2 compile+first day:       {first_day_label}",
                "==============================",
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
    fixture real network topology without making every right a long path.  The
    With ``shared_priorities=True``, transaction priorities are aligned across
    reaches, producing 50 equal-priority cohorts of 10 members in the default
    500-right problem.  With ``shared_priorities=False``, all 500 transactions
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

        # The nth right at every reach has the same priority in the shared
        # benchmark.  The unique-priority variant keeps the same transactions
        # and network but assigns a globally distinct lexicographic priority.
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


def _scipy_factory(tolerance: float | None = None) -> ScipyLPSolver:
    return ScipyLPSolver(tolerance=tolerance)


def solve_with_production_scipy(problem: SolverInput) -> tuple[SolverOutput, dict[str, int]]:
    """Execute the ordinary production LP directly with SciPy/HiGHS.

    This intentionally bypasses the public ``solve`` function because that API
    is V2-only on this branch.  It exercises the same ``Apportioner`` LP and
    daily two-pass algorithm, but every objective is solved against the full
    production LP instead of a compiled residual kernel.
    """

    problem = deepcopy(problem)
    graph_manager = GraphManager(deepcopy(problem.accounting_graph))
    natural_flow_calculator = NaturalFlowCalculator(graph_manager)
    data_manager = DailyDataManager(
        graph_manager,
        problem.measurements,
        problem.external_natural_flows,
    )
    trxn_manager = TrxnSchedule(graph_manager, problem.txns, None)

    apportionments = []
    solve_count = 0
    production_variables = 0
    production_constraints = 0

    for date in _loop_through_date_range(problem.beg_date, problem.end_date):
        data_manager.set_day(date)
        trxn_manager.begin_day(date)
        apportioner = _solve_day(
            graph_manager,
            trxn_manager,
            data_manager,
            natural_flow_calculator,
            _scipy_factory,
            False,
            date,
        )
        trxn_manager.commit_day(apportioner.cur_trxn_value)
        apportionments.extend(apportioner.get_variables(date))
        solve_count += apportioner.engine.solve_count
        production_variables = len(apportioner.engine.vars)
        production_constraints = len(apportioner.engine.cons)

    output = SolverOutput(
        apportionments=unlag_apportionments(apportionments, data_manager.flow_lags),
        solve_steps=[],
        solver_backend="scipy-production-benchmark",
        solve_method="scipy-highs-ds",
    )
    return output, {
        "solve_count": solve_count,
        "production_variables": production_variables,
        "production_constraints": production_constraints,
    }


def _result_map(output: SolverOutput) -> dict[tuple[str, str, str], float]:
    return {
        (row.date, row.txn_id, row.interzone_flow_id): row.value
        for row in output.apportionments
    }


def _assert_outputs_close(
    testcase: unittest.TestCase,
    left: SolverOutput,
    right: SolverOutput,
    *,
    places: int = 7,
) -> None:
    left_map = _result_map(left)
    right_map = _result_map(right)
    testcase.assertEqual(set(left_map), set(right_map))
    for key in left_map:
        testcase.assertAlmostEqual(left_map[key], right_map[key], places=places, msg=key)


def _median_seconds(fn, repeats: int, *, collect: bool = False):
    values = []
    result = None
    for _ in range(repeats):
        start = perf_counter()
        current = fn()
        values.append(perf_counter() - start)
        result = current
        # Production comparison solves create many short-lived solver objects
        # and native HiGHS arrays, so explicit collection is useful there. Do
        # not collect while a large frozen V2 plan is live: traversing that
        # intentionally retained IR can cost far more than the daily solve.
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
        solve_repeats = int(os.environ.get("BENCHMARK_SOLVE_REPEATS", "3"))
        v2_solve_repeats = int(
            os.environ.get("BENCHMARK_V2_SOLVE_REPEATS", str(solve_repeats))
        )
        scipy_solve_repeats = int(
            os.environ.get("BENCHMARK_SCIPY_SOLVE_REPEATS", "1")
        )

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

        # Measure the production solver before retaining the frozen V2 plan.
        # Large unique-priority plans intentionally trade memory/compile work
        # for fast daily execution; timing repeated production deep-copies while
        # that plan is resident can create memory-pressure noise unrelated to
        # either solver's actual daily algorithm.
        scipy_warm, scipy_stats = solve_with_production_scipy(problem)
        scipy_daily_seconds, scipy_result = _median_seconds(
            lambda: solve_with_production_scipy(problem),
            scipy_solve_repeats,
            collect=True,
        )
        assert scipy_result is not None
        scipy_output, scipy_stats = scipy_result

        compile_seconds, plan = _median_seconds(
            lambda: compile_solver_input_v2(problem),
            compile_repeats,
        )
        assert plan is not None

        # Warm V2 once so one-time SciPy/HiGHS loading in any conservative
        # kernel is not charged to its repeated daily timing.
        v2_warm = plan.solve()
        _assert_outputs_close(self, v2_warm, scipy_warm)

        v2_daily_seconds, v2_output = _median_seconds(
            plan.solve, v2_solve_repeats
        )
        assert v2_output is not None
        _assert_outputs_close(self, v2_output, scipy_output)

        report = plan.report()
        self.assertFalse(report["runtime_compilation"])
        self.assertEqual(report["whole_day_lp_fallbacks"], 0)
        if not shared_priorities:
            # The ordinary sequential benchmark should take the early sparse-
            # column compiler all the way to one direct MIN program per right.
            self.assertEqual(
                report["early_direct_sequential_programs"], len(problem.txns)
            )
            self.assertEqual(report["reduced_lp_kernels"], 0)
            self.assertEqual(report["prepared_unguarded_variants"], 0)

        benchmark = BenchmarkResult(
            stream_zones=actual_stream_zones,
            transactions=len(problem.txns),
            production_variables=scipy_stats["production_variables"],
            production_constraints=scipy_stats["production_constraints"],
            scipy_lp_solves_per_day=scipy_stats["solve_count"],
            compiled_programs=report["program_count"],
            v2_compile_seconds=compile_seconds,
            v2_daily_seconds=v2_daily_seconds,
            scipy_daily_seconds=scipy_daily_seconds,
            v2_solve_repeats=v2_solve_repeats,
            scipy_solve_repeats=scipy_solve_repeats,
        )
        layout = "SHARED PRIORITIES" if shared_priorities else "UNIQUE PRIORITIES"
        print(f"\n--- {layout} ---", flush=True)
        print(benchmark.render(), flush=True)

        # The public benchmark wrapper runs each large layout in its own child
        # process, so process teardown releases the large frozen plan without an
        # expensive full gc traversal while it is still live.
        return benchmark

    def test_v2_shared_priority_compile_and_daily_solve_vs_production_scipy(self):
        self._run_benchmark(shared_priorities=True)

    def test_v2_unique_priority_compile_and_daily_solve_vs_production_scipy(self):
        self._run_benchmark(shared_priorities=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
