"""Compiled objective math and parity with main's accounting orchestration."""

import unittest
from copy import deepcopy
from dataclasses import asdict
from unittest.mock import patch

import numpy as np

import tests.test_solver as solver_fixtures
import tests.test_zone_accounts as account_fixtures
from benchmarks.benchmark_scaling import make_input
from ut_water_apportionment import CompilationOptions, compile_solver_input, solve
from ut_water_apportionment.apportioner import Apportioner, TMP_LEX_OBJECTIVE
from ut_water_apportionment.compiled.projection import Bounds, CannotCompile
from ut_water_apportionment.compiled.runtime import CompilationSession, compiled_factory
from ut_water_apportionment.lp_solver_SCIPY import LPSolver


def assert_same(actual, expected):
    def keys(out):
        return [
            (a.date, a.txn_id, a.interzone_flow_id, a.is_forward)
            for a in out.apportionments
        ]

    if keys(actual) != keys(expected):
        raise AssertionError("apportionment keys differ")
    np.testing.assert_allclose(
        [a.value for a in actual.apportionments],
        [a.value for a in expected.apportionments],
        rtol=1e-7,
        atol=1e-6,
    )


# Run the existing main fixtures with the alternate method, preserving their
# inputs, and compare the *full* output to LP (the original suite checks expected values).
# This exercises signed paths, reservations, spill passes, accounts and lags
# without maintaining a second set of hand-transcribed accounting inputs.
CASES = []
for module in (solver_fixtures, account_fixtures):
    for cls in vars(module).values():
        if isinstance(cls, type) and cls.__module__ == module.__name__:
            for name in dir(cls):
                if name.startswith("test_") and not getattr(
                    getattr(cls, name), "__unittest_skip__", False
                ):
                    CASES.append((module, cls, name))


class CompiledSolverTests(unittest.TestCase):
    def test_bounds_interval_tolerates_tiny_float_inversion(self):
        bounds = Bounds(
            upper=((1.0,),),
            lower=((1.0 + 2.0e-16,),),
            conditions=(),
        )
        lo, hi = bounds.interval(np.asarray([1.0]))
        self.assertEqual(lo, hi)
        self.assertAlmostEqual(lo, 1.0)

    def test_bounds_interval_rejects_material_infeasibility(self):
        bounds = Bounds(
            upper=((1.0,),),
            lower=((1.001,),),
            conditions=(),
        )
        with self.assertRaisesRegex(
            CannotCompile, "compiled domain/interval check failed"
        ):
            bounds.interval(np.asarray([1.0]))

    def test_symbolic_bounds_reuse_new_rhs_and_coefficients(self):
        session = CompilationSession()
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("forward", ub=30)
        engine.add_variable("reverse", ub=8)
        engine.add_constraint("net", lb=10, ub=10)
        engine.set_coefficient("net", "forward", 1)
        engine.set_coefficient("net", "reverse", -1)
        with patch("scipy.optimize.linprog", side_effect=AssertionError("optimizer")):
            value, _ = engine.solve_objective(["forward"])
            self.assertEqual(value, 18)
            engine.update_constraint_ub("net", 12)
            engine.update_constraint_lb("net", 12)
            self.assertEqual(engine.solve_objective(["forward"])[0], 20)
            self.assertEqual(len(session.programs), 1)  # Changed daily data, same formulas.
            engine.set_coefficient("net", "reverse", -2)
            self.assertEqual(engine.solve_objective(["forward"])[0], 28)
            self.assertEqual(len(session.programs), 2)  # Matrix changes invalidate the key.
        self.assertIn("upper = MIN(", session.formulas())
        self.assertIn("constraint[net].remaining", session.formulas())

    def test_signed_target_variable_compiles_without_lp(self):
        session = CompilationSession()
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("signed", lb=-10, ub=10)
        engine.add_variable("counter", lb=0, ub=4)
        engine.add_constraint("net", lb=-2, ub=-2)
        engine.set_coefficient("net", "signed", 1)
        engine.set_coefficient("net", "counter", -1)

        with patch("scipy.optimize.linprog", side_effect=AssertionError("optimizer")):
            self.assertEqual(
                engine.solve_objective(["signed"]),
                (2.0, {"signed": 2.0}),
            )
            self.assertEqual(
                engine.solve_objective(["signed"], maximization=False),
                (-2.0, {"signed": -2.0}),
            )

    def test_priority_reduction_moves_seniors_and_juniors_to_rhs(self):
        session = CompilationSession(CompilationOptions(max_variables=1))
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("senior", lb=7, ub=10)
        engine.add_variable("target", lb=0, ub=10)
        for index in range(50):
            engine.add_variable(f"junior_{index}", lb=0, ub=10)
        engine.add_constraint("water", ub=8)
        engine.set_coefficient("water", "senior", 1)
        engine.set_coefficient("water", "target", 1)
        for index in range(50):
            engine.set_coefficient("water", f"junior_{index}", 1)

        with patch("scipy.optimize.linprog", side_effect=AssertionError("optimizer")):
            self.assertEqual(
                engine.solve_objective(["target"]),
                (1.0, {"target": 1.0}),
            )
            # The senior commitment is a live RHS parameter, not baked into
            # the formula. Changing it should reuse the same compiled program.
            engine.update_variable_bounds("senior", lb=6)
            self.assertEqual(
                engine.solve_objective(["target"]),
                (2.0, {"target": 2.0}),
            )
            self.assertEqual(len(session.programs), 1)

        reduction = session.report()["program_reductions"]["P1"]
        self.assertEqual(reduction["source_variables"], 52)
        self.assertEqual(reduction["active_variables"], 1)
        self.assertEqual(reduction["bound_eliminated"], 51)
        self.assertIn("constraint[water].remaining_upper", session.formulas())
        self.assertNotIn("variable[senior].lower", session.formulas())

    def test_mixed_sign_counterflow_stays_live_until_projection(self):
        from ut_water_apportionment.compiled.runtime import snapshot

        session = CompilationSession(CompilationOptions(max_variables=2))
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("target", lb=0, ub=20)
        engine.add_variable("counter", lb=0, ub=10)
        engine.add_constraint("delivery", ub=5)
        engine.set_coefficient("delivery", "target", 1)
        engine.set_coefficient("delivery", "counter", -1)
        engine.add_constraint("counter_limit", ub=2)
        engine.set_coefficient("counter_limit", "counter", 1)

        state = snapshot(engine, session.options, ["target"])
        self.assertEqual(set(state.names), {"target", "counter"})
        with patch("scipy.optimize.linprog", side_effect=AssertionError("optimizer")):
            self.assertEqual(
                engine.solve_objective(["target"]),
                (7.0, {"target": 7.0}),
            )

    def test_exported_code_executes_the_same_compiled_expressions(self):
        from ut_water_apportionment.compiled.runtime import snapshot

        session = CompilationSession()
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("x", ub=10)
        engine.add_variable("y", ub=3)
        engine.add_constraint("q", lb=8, ub=8)
        engine.set_coefficient("q", "x", 1)
        engine.set_coefficient("q", "y", 1)
        for targets in [["x"], ["x", "y"]]:
            try:
                engine.solve_objective(targets)
            except CannotCompile:
                pass  # Also check the exported ambiguity guard below.
        namespace = {}
        exec(session.code(), namespace)
        for program in session.programs.values():
            preferences = {
                target: (
                    "upper"
                    if (cost > 0) == program.maximization
                    else "lower"
                )
                for target, cost in zip(
                    program.targets,
                    program.costs,
                    strict=True,
                )
                if cost != 0
            }
            state = snapshot(
                engine,
                session.options,
                program.targets,
                target_preferences=preferences,
            )
            self.assertEqual(state.labels, program.labels)
            params = dict(zip(state.labels, state.parameters, strict=True))
            try:
                expected = program.evaluate(state.parameters)
            except CannotCompile:
                with self.assertRaises(namespace["CompiledFallback"]):
                    namespace[program.name](params)
            else:
                self.assertEqual(namespace[program.name](params), expected)

    def test_negative_and_zero_objective_weights(self):
        session = CompilationSession()
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("x", lb=2, ub=5)
        self.assertEqual(
            engine.solve_objective(["x"], weights={"x": -2}),
            (-4, {"x": 2}),
        )
        with self.assertRaisesRegex(CannotCompile, "nonunique"):
            engine.solve_objective(["x"], weights={"x": 0})

    def test_common_increment_uses_active_rows_without_subset_enumeration(self):
        session = CompilationSession()
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("a", ub=3)
        engine.add_variable("b", ub=12)
        engine.add_constraint("water", ub=10)
        engine.set_coefficient("water", "a", 1)
        engine.set_coefficient("water", "b", 1)
        self.assertEqual(
            engine.maximize_group_by_proportions(["a", "b"], {"a": 0.5, "b": 0.5}),
            {"a": 3, "b": 3},
        )
        engine.update_variable_bounds("a", lb=3)
        engine.update_variable_bounds("b", lb=3)
        self.assertEqual(
            engine.maximize_group_by_proportions(["b"], {"b": 1}),
            {"b": 7},
        )
        self.assertEqual(len(session.programs), 2)

    def test_ambiguous_components_cannot_silently_select_caps(self):
        session = CompilationSession()
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("a", ub=10)
        engine.add_variable("b", ub=10)
        engine.add_constraint("sum", lb=5, ub=5)
        engine.set_coefficient("sum", "a", 1)
        engine.set_coefficient("sum", "b", 1)
        with self.assertRaisesRegex(CannotCompile, "nonunique"):
            engine.solve_objective(["a", "b"], maximization=False)

    def test_objective_value_only_does_not_require_unique_components(self):
        session = CompilationSession()
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("a", lb=0, ub=10)
        engine.add_variable("b", lb=0, ub=10)
        engine.add_constraint("sum", lb=5, ub=5)
        engine.set_coefficient("sum", "a", 1)
        engine.set_coefficient("sum", "b", 1)

        with patch("scipy.optimize.linprog", side_effect=AssertionError("optimizer")):
            self.assertEqual(
                engine.solve_objective_value(["a", "b"], maximization=False),
                5.0,
            )

    def test_lexicographic_minimum_preserves_primary_sum_and_breaks_ties(self):
        session = CompilationSession()
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("a", lb=0, ub=10)
        engine.add_variable("b", lb=0, ub=10)
        engine.add_constraint("sum", lb=10)
        engine.set_coefficient("sum", "a", 1)
        engine.set_coefficient("sum", "b", 1)
        engine.add_constraint(TMP_LEX_OBJECTIVE, lb=None, ub=None)

        apportioner = Apportioner.__new__(Apportioner)
        apportioner.engine = engine
        apportioner.feasibility_slacks = []
        apportioner._lexicographic_constraint_vars = set()

        with patch("scipy.optimize.linprog", side_effect=AssertionError("optimizer")):
            values = apportioner._lexicographic_minimum(["a", "b"])

        self.assertEqual(values, {"a": 0.0, "b": 10.0})
        self.assertEqual(engine.get_variable_bounds("a"), (0.0, 10.0))
        self.assertEqual(engine.get_variable_bounds("b"), (0.0, 10.0))

    def test_common_increment_keeps_helpful_residuals_live(self):
        session = CompilationSession(CompilationOptions(max_variables=2))
        engine = compiled_factory(LPSolver, session)()
        engine.add_variable("a", lb=0, ub=10)
        engine.add_variable("b", lb=0, ub=10)
        engine.add_constraint("difference", lb=2)
        engine.set_coefficient("difference", "a", 1)
        engine.set_coefficient("difference", "b", -1)
        engine.add_constraint("water", ub=10)
        engine.set_coefficient("water", "a", 1)
        engine.set_coefficient("water", "b", 1)

        with patch("scipy.optimize.linprog", side_effect=AssertionError("optimizer")):
            self.assertEqual(
                engine.maximize_group_by_proportions(
                    ["a", "b"],
                    {"a": 0.5, "b": 0.5},
                ),
                {"a": 4.0, "b": 4.0},
            )

        reduction = session.report()["program_reductions"]["P1"]
        self.assertEqual(reduction["active_variables"], 2)

    def test_compile_interface_and_repeatable_optimizer_free_execution(self):
        problem = make_input(reaches=1, rights=2, days=3)
        original = deepcopy(problem)
        # Native model construction is allowed; optimization is not needed here.
        plan = compile_solver_input(problem, solver_backend="scipy")
        self.assertIn("MIN(", plan.formulas())
        self.assertIn("RESTART_DAY_WITH_LP", plan.formulas())
        self.assertIn("while active_members_remain", plan.execution_outline())
        self.assertIn("def P1(parameters)", plan.code())
        expected = solve(original, solver_backend="scipy", generate_audit=False)
        with patch(
            "ut_water_apportionment.lp_solver_SCIPY.linprog",
            side_effect=AssertionError("unexpected LP"),
        ), patch(
            "ut_water_apportionment.compiled.runtime.compile_objective",
            side_effect=AssertionError("runtime compilation"),
        ):
            first = plan.solve()
            self.assertEqual(first.compilation_report["compiled_days"], 3)
            second = plan.solve()
        assert_same(first, expected)
        assert_same(second, expected)
        self.assertEqual(problem, original)
        self.assertEqual(second.compilation_report["compiled_days"], 3)  # Per-run counters.

    def test_plan_eagerly_compiles_complete_residual_priority_routine(self):
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
        )

        problem = SolverInput(
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
                    path=[TrxnPathItem(flow_id="RIVER>USER", expected_values=[3])],
                ),
                PathTrxn(
                    id="TRXN_2",
                    priority=2,
                    upper_limit=6,
                    path=[TrxnPathItem(flow_id="RIVER>USER", expected_values=[6])],
                ),
                PathTrxn(
                    id="TRXN_3",
                    priority=3,
                    upper_limit=12,
                    path=[TrxnPathItem(flow_id="RIVER>USER", expected_values=[3])],
                ),
                PathTrxn(
                    id="TRXN_4",
                    priority=4,
                    upper_limit=4,
                    path=[TrxnPathItem(flow_id="RIVER>USER", expected_values=[0])],
                ),
            ],
        )

        plan = compile_solver_input(problem, solver_backend="scipy")
        formulas = plan.formulas()
        report = plan.report()

        # Compilation is complete before the first public execution. Every
        # transaction gets its own priority formula, including TRXN_4 whose
        # remaining natural flow is already zero.
        for transaction in ("TRXN_1", "TRXN_2", "TRXN_3", "TRXN_4"):
            self.assertIn(
                f"MAXIMIZE (1.0) * {transaction}___RIVER>USER",
                formulas,
            )
        self.assertTrue(report["frozen"])
        self.assertEqual(report["days"], [])
        self.assertEqual(report["preparation_days"][0]["method"], "compiled")

        # Senior values are represented by residual constraint state, not by
        # explicit variable[TRXN_n].value parameters in junior programs.
        p2 = formulas.split("PROGRAM P2:", 1)[1].split("PROGRAM P3:", 1)[0]
        p3 = formulas.split("PROGRAM P3:", 1)[1].split("PROGRAM P4:", 1)[0]
        p4 = formulas.split("PROGRAM P4:", 1)[1].split("PROGRAM P5:", 1)[0]
        self.assertIn("constraint[MEAS_RIVER>USER].remaining", p2)
        self.assertIn("constraint[NF_ZONE_RIVER].remaining_upper", p2)

        # Earlier transactions have disappeared completely from each junior
        # priority formula. Their effects exist only in the residual constraint
        # state that is updated between formula evaluations.
        self.assertNotIn("TRXN_1___RIVER>USER", p2)
        self.assertNotIn("TRXN_1___RIVER>USER", p3)
        self.assertNotIn("TRXN_2___RIVER>USER", p3)
        self.assertNotIn("TRXN_1___RIVER>USER", p4)
        self.assertNotIn("TRXN_2___RIVER>USER", p4)
        self.assertNotIn("TRXN_3___RIVER>USER", p4)

        program_count = report["program_count"]
        compile_seconds = report["compile_seconds"]
        with patch(
            "ut_water_apportionment.compiled.runtime.compile_objective",
            side_effect=AssertionError("runtime compilation"),
        ):
            actual = plan.solve(check_expected_values=True)
        self.assertEqual(actual.compilation_report["program_count"], program_count)
        self.assertEqual(actual.compilation_report["compile_seconds"], compile_seconds)
        self.assertEqual(actual.compilation_report.get("lp_days", 0), 0)

    def test_changed_measurements_use_fresh_parameters(self):
        problem = make_input(reaches=1, rights=2, days=3)
        plan = compile_solver_input(problem, solver_backend="scipy")
        replacement = deepcopy(problem.measurements)
        for series in replacement.series:
            series.values = [v * 0.8 if v is not None else None for v in series.values]
        changed = deepcopy(problem)
        changed.measurements = replacement
        assert_same(
            plan.solve(measurements=replacement),
            solve(changed, solver_backend="scipy", generate_audit=False),
        )

    def test_reduced_lp_kernel_reuses_fresh_measurement_parameters(self):
        problem = make_input(reaches=2, rights=2, days=3)
        plan = compile_solver_input(
            problem,
            options=CompilationOptions(max_rows=1),
            solver_backend="scipy",
        )
        replacement = deepcopy(problem.measurements)
        for series in replacement.series:
            series.values = [
                value * 0.99 if value is not None else None
                for value in series.values
            ]
        changed = deepcopy(problem)
        changed.measurements = replacement
        actual = plan.solve(measurements=replacement)
        expected = solve(
            changed,
            solver_backend="scipy",
            generate_audit=False,
        )
        assert_same(actual, expected)
        self.assertEqual(actual.compilation_report["lp_days"], 0)
        self.assertGreater(actual.compilation_report["hybrid_days"], 0)
        self.assertGreater(
            actual.compilation_report["reduced_lp_evaluations"],
            0,
        )

    def test_audit_fallback_retains_native_evidence(self):
        problem = make_input(reaches=1, rights=2, days=2)
        actual = solve(
            problem, method="compiled", solver_backend="scipy", generate_audit=True
        )
        expected = solve(problem, solver_backend="scipy", generate_audit=True)
        assert_same(actual, expected)
        self.assertEqual(
            [asdict(s) for s in actual.solve_steps],
            [asdict(s) for s in expected.solve_steps],
        )
        self.assertEqual(actual.compilation_report["lp_days"], 2)
        self.assertTrue(
            all("audit" in d["fallback_reason"] for d in actual.compilation_report["days"])
        )

    def test_large_equal_priority_group_compiles_as_common_increment(self):
        problem = make_input(reaches=1, rights=300, days=1)
        for transaction in problem.txns:
            transaction.priority = 1
        options = CompilationOptions(max_variables=32)
        plan = compile_solver_input(problem, options=options, solver_backend="scipy")
        actual = plan.solve()
        self.assertEqual(actual.compilation_report.get("lp_days", 0), 0)
        self.assertEqual(
            actual.compilation_report["compiled_days"]
            + actual.compilation_report["hybrid_days"],
            1,
        )
        # The common-increment allocation itself remains symbolic. A difficult
        # post-allocation classification objective may use a local reduced-LP
        # kernel without restarting the day.
        self.assertTrue(
            any(
                reduction["kind"] == "symbolic"
                and reduction["active_variables"] <= 2
                and reduction["source_variables"] >= 300
                for reduction in actual.compilation_report[
                    "program_reductions"
                ].values()
            )
        )
        # Identical transaction columns are classified through one
        # representative rather than compiling one headroom objective per
        # equal-priority member. Keep the cache small as the group grows.
        self.assertLessEqual(actual.compilation_report["program_count"], 8)
        self.assertGreaterEqual(
            actual.compilation_report.get(
                "proportional_common_increment_evaluations", 0
            ),
            1,
        )
        self.assertTrue(
            any(
                reduction["active_variables"] <= 2
                and reduction["source_variables"] >= 300
                for reduction in actual.compilation_report[
                    "program_reductions"
                ].values()
            )
        )
        assert_same(actual, solve(problem, solver_backend="scipy", generate_audit=False))

    def test_projection_budget_uses_reduced_lp_kernel_without_day_restart(self):
        problem = make_input(reaches=2, rights=2, days=2)
        actual = solve(
            problem,
            method="compiled",
            compilation_options=CompilationOptions(max_rows=1),
            solver_backend="scipy",
            generate_audit=False,
        )
        expected = solve(problem, solver_backend="scipy", generate_audit=False)
        assert_same(actual, expected)
        report = actual.compilation_report
        self.assertEqual(report["lp_days"], 0)
        self.assertEqual(report["hybrid_days"], 2)
        self.assertGreater(report["reduced_lp_program_count"], 0)
        self.assertGreater(report["reduced_lp_evaluations"], 0)
        self.assertTrue(
            all(day["method"] == "compiled+reduced_lp" for day in report["days"])
        )

    def test_budgets_produce_a_complete_lp_result(self):
        local_kernel_options = [
            CompilationOptions(max_rows=1),
            CompilationOptions(max_pairs=1),
            CompilationOptions(max_fraction_bits=1),
            CompilationOptions(max_total_compile_seconds=1e-12),
            CompilationOptions(max_program_coefficients=1),
        ]
        whole_day_options = [
            CompilationOptions(max_plans=1),
            CompilationOptions(max_total_coefficients=1),
        ]
        for options in local_kernel_options:
            with self.subTest(options=options):
                problem = make_input(reaches=2, rights=2, days=2)
                actual = solve(
                    problem,
                    method="compiled",
                    compilation_options=options,
                    solver_backend="scipy",
                    generate_audit=False,
                )
                assert_same(
                    actual,
                    solve(problem, solver_backend="scipy", generate_audit=False),
                )
                self.assertEqual(actual.compilation_report["lp_days"], 0)
                self.assertGreater(
                    actual.compilation_report["reduced_lp_program_count"], 0
                )

        for options in whole_day_options:
            with self.subTest(options=options):
                problem = make_input(reaches=2, rights=2, days=2)
                actual = solve(
                    problem,
                    method="compiled",
                    compilation_options=options,
                    solver_backend="scipy",
                    generate_audit=False,
                )
                assert_same(
                    actual,
                    solve(problem, solver_backend="scipy", generate_audit=False),
                )
                self.assertEqual(actual.compilation_report["lp_days"], 2)

    def test_failed_day_does_not_double_commit_account_or_cumulative_state(self):
        import tests.test_solver as module
        from tests.test_solver import F_TransactionLimits

        captured = []
        with patch.object(module, "solve", lambda problem, **kw: captured.append(problem)):
            F_TransactionLimits("test_trxn_cumulative_limit").test_trxn_cumulative_limit()
        problem = captured[0]
        # First formula succeeds; the next new program exhausts the budget, forcing
        # restart after a tentative allocation. Cumulative use must be committed once.
        actual = solve(
            problem,
            method="compiled",
            solver_backend="scipy",
            generate_audit=False,
            compilation_options=CompilationOptions(max_plans=1),
        )
        assert_same(actual, solve(problem, solver_backend="scipy", generate_audit=False))
        self.assertGreater(
            actual.compilation_report.get("discarded_formula_evaluations", 0),
            0,
        )

    def test_bad_method_and_budget_are_input_errors(self):
        problem = make_input(reaches=1, rights=1, days=1)
        with self.assertRaisesRegex(ValueError, "method"):
            solve(problem, method="typo")
        with self.assertRaises(ValueError):
            CompilationOptions(max_rows=-1)
        with self.assertRaises(ValueError):
            CompilationOptions(max_rows=3.5)

    def test_existing_accounting_fixture_with_alternate_method(self):
        original_solve = solve

        for module, cls, name in CASES:
            case_name = f"{cls.__name__}.{name}"
            with self.subTest(case=case_name):
                if not hasattr(module, "solve"):
                    continue

                def both(problem, **kwargs):
                    audit_requested = kwargs.pop("generate_audit", True)
                    kwargs.update(solver_backend="scipy")
                    expected = original_solve(
                        problem, generate_audit=audit_requested, **kwargs
                    )
                    actual = original_solve(
                        problem, method="compiled", generate_audit=False, **kwargs
                    )
                    assert_same(actual, expected)
                    if audit_requested:
                        audited = original_solve(
                            problem, method="compiled", generate_audit=True, **kwargs
                        )
                        assert_same(audited, expected)
                        if [asdict(s) for s in audited.solve_steps] != [
                            asdict(s) for s in expected.solve_steps
                        ]:
                            raise AssertionError("compiled and LP audit steps differ")
                        return audited
                    return actual

                with patch.object(module, "solve", both):
                    getattr(cls(name), name)()


if __name__ == "__main__":
    unittest.main()
