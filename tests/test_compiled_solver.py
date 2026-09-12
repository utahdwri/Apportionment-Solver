"""Compiled objective math and parity with main's accounting orchestration."""

from copy import deepcopy
from dataclasses import asdict
from unittest.mock import patch

import numpy as np
import pytest

import tests.test_solver as solver_fixtures
import tests.test_zone_accounts as account_fixtures
from benchmarks.benchmark_scaling import make_input
from ut_water_apportionment import CompilationOptions, compile_solver_input, solve
from ut_water_apportionment.compiled.projection import CannotCompile
from ut_water_apportionment.compiled.runtime import CompilationSession, compiled_factory
from ut_water_apportionment.lp_solver_SCIPY import LPSolver


def assert_same(actual, expected):
    def keys(out):
        return [
            (a.date, a.txn_id, a.interzone_flow_id, a.is_forward)
            for a in out.apportionments
        ]

    assert keys(actual) == keys(expected)
    np.testing.assert_allclose(
        [a.value for a in actual.apportionments],
        [a.value for a in expected.apportionments],
        rtol=1e-7,
        atol=1e-6,
    )


def test_symbolic_bounds_reuse_new_rhs_and_coefficients():
    session = CompilationSession()
    engine = compiled_factory(LPSolver, session)()
    engine.add_variable("forward", ub=30)
    engine.add_variable("reverse", ub=8)
    engine.add_constraint("net", lb=10, ub=10)
    engine.set_coefficient("net", "forward", 1)
    engine.set_coefficient("net", "reverse", -1)
    with patch("scipy.optimize.linprog", side_effect=AssertionError("optimizer")):
        value, _ = engine.solve_objective(["forward"])
        assert value == 18
        engine.update_constraint_ub("net", 12)
        engine.update_constraint_lb("net", 12)
        assert engine.solve_objective(["forward"])[0] == 20
        assert len(session.programs) == 1  # Changed daily data, same formulas.
        engine.set_coefficient("net", "reverse", -2)
        assert engine.solve_objective(["forward"])[0] == 28
        assert len(session.programs) == 2  # Matrix changes invalidate the key.
    assert "upper = MIN(" in session.formulas()
    assert "constraint[net].value" in session.formulas()


def test_exported_code_executes_the_same_compiled_expressions():
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
    state = snapshot(engine, session.options)
    for program in session.programs.values():
        params = dict(zip(state.labels, state.parameters, strict=True))
        try:
            expected = program.evaluate(state.parameters)
        except CannotCompile:
            with pytest.raises(namespace["CompiledFallback"]):
                namespace[program.name](params)
        else:
            assert namespace[program.name](params) == expected


def test_negative_and_zero_objective_weights():
    session = CompilationSession()
    engine = compiled_factory(LPSolver, session)()
    engine.add_variable("x", lb=2, ub=5)
    assert engine.solve_objective(["x"], weights={"x": -2}) == (-4, {"x": 2})
    with pytest.raises(CannotCompile, match="nonunique"):
        engine.solve_objective(["x"], weights={"x": 0})


def test_common_increment_uses_active_rows_without_subset_enumeration():
    session = CompilationSession()
    engine = compiled_factory(LPSolver, session)()
    engine.add_variable("a", ub=3)
    engine.add_variable("b", ub=12)
    engine.add_constraint("water", ub=10)
    engine.set_coefficient("water", "a", 1)
    engine.set_coefficient("water", "b", 1)
    assert engine.maximize_group_by_proportions(["a", "b"], {"a": 0.5, "b": 0.5}) == {
        "a": 3,
        "b": 3,
    }
    engine.update_variable_bounds("a", lb=3)
    engine.update_variable_bounds("b", lb=3)
    assert engine.maximize_group_by_proportions(["b"], {"b": 1}) == {"b": 7}
    assert len(session.programs) == 2


def test_ambiguous_components_cannot_silently_select_caps():
    session = CompilationSession()
    engine = compiled_factory(LPSolver, session)()
    engine.add_variable("a", ub=10)
    engine.add_variable("b", ub=10)
    engine.add_constraint("sum", lb=5, ub=5)
    engine.set_coefficient("sum", "a", 1)
    engine.set_coefficient("sum", "b", 1)
    with pytest.raises(CannotCompile, match="nonunique"):
        engine.solve_objective(["a", "b"], maximization=False)


def test_compile_interface_and_repeatable_optimizer_free_execution():
    problem = make_input(reaches=1, rights=2, days=3)
    original = deepcopy(problem)
    # Native model construction is allowed; optimization is not needed here.
    plan = compile_solver_input(problem, solver_backend="scipy")
    assert "MIN(" in plan.formulas()
    assert "RESTART_DAY_WITH_LP" in plan.formulas()
    assert "while active_members_remain" in plan.execution_outline()
    assert "def P1(parameters)" in plan.code()
    expected = solve(original, solver_backend="scipy", generate_audit=False)
    with patch(
        "ut_water_apportionment.lp_solver_SCIPY.linprog",
        side_effect=AssertionError("unexpected LP"),
    ):
        first = plan.solve()
        assert first.compilation_report["compiled_days"] == 3
        with patch(
            "ut_water_apportionment.compiled.runtime.compile_objective",
            side_effect=AssertionError("recompiled"),
        ):
            second = plan.solve()
    assert_same(first, expected)
    assert_same(second, expected)
    assert problem == original
    assert second.compilation_report["compiled_days"] == 3  # Per-run counters.


def test_changed_measurements_use_fresh_parameters():
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


def test_audit_fallback_retains_native_evidence():
    problem = make_input(reaches=1, rights=2, days=2)
    actual = solve(
        problem, method="compiled", solver_backend="scipy", generate_audit=True
    )
    expected = solve(problem, solver_backend="scipy", generate_audit=True)
    assert_same(actual, expected)
    assert [asdict(s) for s in actual.solve_steps] == [
        asdict(s) for s in expected.solve_steps
    ]
    assert actual.compilation_report["lp_days"] == 2
    assert all(
        "audit" in d["fallback_reason"] for d in actual.compilation_report["days"]
    )


def test_large_equal_priority_group_falls_back_without_exponential_compilation():
    problem = make_input(reaches=1, rights=300, days=2)
    for t in problem.txns:
        t.priority = 1
    options = CompilationOptions(max_variables=32)
    with patch(
        "ut_water_apportionment.compiled.runtime.compile_objective",
        side_effect=AssertionError("large projection"),
    ):
        plan = compile_solver_input(problem, options=options, solver_backend="scipy")
        actual = plan.solve()
    assert actual.compilation_report["lp_days"] == 2
    assert actual.compilation_report["program_count"] == 0
    assert_same(actual, solve(problem, solver_backend="scipy", generate_audit=False))


@pytest.mark.parametrize(
    "options",
    [
        CompilationOptions(max_rows=1),
        CompilationOptions(max_pairs=1),
        CompilationOptions(max_plans=1),
        CompilationOptions(max_fraction_bits=1),
        CompilationOptions(max_total_compile_seconds=1e-12),
        CompilationOptions(max_program_coefficients=1),
        CompilationOptions(max_total_coefficients=1),
    ],
)
def test_budgets_produce_a_complete_lp_result(options):
    problem = make_input(reaches=2, rights=2, days=2)
    actual = solve(
        problem,
        method="compiled",
        compilation_options=options,
        solver_backend="scipy",
        generate_audit=False,
    )
    assert_same(actual, solve(problem, solver_backend="scipy", generate_audit=False))
    assert actual.compilation_report["lp_days"] == 2


def test_failed_day_does_not_double_commit_account_or_cumulative_state():
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
    assert actual.compilation_report.get("discarded_formula_evaluations", 0) > 0


def test_bad_method_and_budget_are_input_errors():
    problem = make_input(reaches=1, rights=1, days=1)
    with pytest.raises(ValueError, match="method"):
        solve(problem, method="typo")
    with pytest.raises(ValueError):
        CompilationOptions(max_rows=-1)
    with pytest.raises(ValueError):
        CompilationOptions(max_rows=3.5)


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


@pytest.mark.parametrize(
    "module,cls,name", CASES, ids=[f"{cls.__name__}.{name}" for _, cls, name in CASES]
)
def test_existing_accounting_fixture_with_alternate_method(module, cls, name):
    original_solve = solve

    def both(problem, **kwargs):
        audit_requested = kwargs.pop("generate_audit", True)
        kwargs.update(solver_backend="scipy")
        expected = original_solve(problem, generate_audit=audit_requested, **kwargs)
        actual = original_solve(
            problem, method="compiled", generate_audit=False, **kwargs
        )
        assert_same(actual, expected)
        if audit_requested:
            audited = original_solve(
                problem, method="compiled", generate_audit=True, **kwargs
            )
            assert_same(audited, expected)
            assert [asdict(s) for s in audited.solve_steps] == [
                asdict(s) for s in expected.solve_steps
            ]
            return audited
        return actual

    if not hasattr(module, "solve"):
        pytest.skip("fixture does not use solve")
    with patch.object(module, "solve", both):
        getattr(cls(name), name)()
