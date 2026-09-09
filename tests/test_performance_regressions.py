"""Correctness contracts for routing caches and deferred HiGHS evidence."""
from unittest.mock import Mock, patch

import pytest

from benchmarks.benchmark_scaling import make_input
from ut_water_apportionment import PathTrxn, solve
from ut_water_apportionment.apportioner import Apportioner
from ut_water_apportionment.graph_manager import GraphManager
from ut_water_apportionment.loss_models import LossDefinition
from ut_water_apportionment.natural_flow_calculator import NaturalFlowCalculator
from ut_water_apportionment.timeseries_manager import DailyDataManager


def test_coefficients_are_reused_but_public_results_are_independent():
    problem = make_input(reaches=3, rights=2, days=2)
    gm = GraphManager(problem.accounting_graph)
    dm = DailyDataManager(gm, problem.measurements, problem.external_natural_flows)
    nfc = NaturalFlowCalculator(gm)
    dm.set_day("2000-01-01")
    nfc.calculate(date=dm.cur_date, daily_flows=dm.cur_flows_by_id,
                  specified_values={}, boundary_values={})
    expected = nfc.get_nf_constraint_coefficients("R0")
    assert expected == {"R0": 1, "R1": 1, "R2": 1}
    with patch.object(LossDefinition, "get_fraction", side_effect=AssertionError("cache miss")):
        returned = nfc.get_nf_constraint_coefficients("R0")
        returned["R1"] = -999
        assert nfc.get_nf_constraint_coefficients("R0") == expected
        nfc.source_is_exhausted("R0")
        nfc.apply_committed_allocation("R0", 0.1)

    # Recalculation on the SAME date must also invalidate (e.g. new boundaries).
    gm.get_flow_by_id("C0").loss_to_zone = LossDefinition.linear(0.25)
    dm.set_day("2000-01-01")
    nfc.calculate(date=dm.cur_date, daily_flows=dm.cur_flows_by_id,
                  specified_values={}, boundary_values={})
    assert nfc.get_nf_constraint_coefficients("R0") == {"R0": 1, "R1": .75, "R2": .75}
    gm.get_flow_by_id("C0").loss_to_zone = LossDefinition.linear(0.5)
    dm.set_day("2000-01-02")
    nfc.calculate(date=dm.cur_date, daily_flows=dm.cur_flows_by_id,
                  specified_values={}, boundary_values={})
    assert nfc.get_nf_constraint_coefficients("R0") == {"R0": 1, "R1": .5, "R2": .5}


@pytest.fixture
def engine():
    pytest.importorskip("highspy")
    from ut_water_apportionment.lp_solver_HIGHSPY import LPSolver
    return LPSolver()


def test_evidence_survives_row_changes_and_new_rows(engine):
    engine.add_variable("x", ub=10)
    engine.add_variable("y", ub=10)
    engine.add_constraint("capacity", ub=6)
    engine.set_coefficient("capacity", "x", 1)
    engine.set_coefficient("capacity", "y", 1)
    engine.solve_objective(["x"])
    old = engine.get_last_solve_constraint_evidence("x")
    assert old[0]["activity"] == pytest.approx(6)
    engine.update_constraint_ub("capacity", 8)
    engine.set_coefficient("capacity", "x", 2)
    engine.set_coefficient("capacity", "x", 0)
    engine.add_constraint("new", ub=1)
    engine.set_coefficient("new", "x", 1)
    engine.add_variable("new_variable")
    engine.set_coefficient("capacity", "new_variable", 1)
    assert engine.get_last_solve_constraint_evidence("x") == old
    assert engine.get_last_solve_constraint_evidence("new_variable") == []
    engine.solve_objective(["x"])
    evidence = engine.get_last_solve_constraint_evidence("x")
    assert [row["constraint_name"] for row in evidence] == ["new"]
    assert evidence[0]["upper_bound"] == 1


def test_proportional_evidence_survives_temporary_row_cleanup(engine):
    engine.add_variable("x", ub=4)
    engine.add_variable("y", ub=8)
    result = engine.maximize_group_by_proportions(["x", "y"], {"x": 1, "y": 2})
    assert result == pytest.approx({"x": 4, "y": 8})
    evidence = engine.get_last_solve_constraint_evidence("x")
    row = next(row for row in evidence if row["constraint_name"] == "combined_x")
    assert row["lower_bound"] == 0
    assert row["coefficient"] == 1
    assert row["is_tight"]
    assert engine.cons["combined_x"].coefficients == {}


@pytest.mark.parametrize("backend", ["highspy", "glop", "scipy"])
def test_fixed_variable_shortcut_matches_audited_allocation(backend):
    pytest.importorskip({"highspy": "highspy", "glop": "ortools", "scipy": "scipy"}[backend])
    from ut_water_apportionment.lp_solver import (
        SolverBackendUnavailableError,
        resolve_solver_backend,
    )
    try:
        resolve_solver_backend(backend)
    except SolverBackendUnavailableError as exc:
        pytest.skip(str(exc))
    problem = make_input(reaches=3, rights=4, days=3)
    audited = solve(problem, solver_backend=backend, generate_audit=True)
    unaudited = solve(problem, solver_backend=backend, generate_audit=False)
    assert unaudited.apportionments == audited.apportionments


@pytest.mark.parametrize("upper,audit,should_solve", [
    (1.0, False, False),
    (1.0 + 1e-8, False, True),
    (1.0, True, True),
])
def test_fixed_shortcut_requires_exact_bounds_and_retains_audit(upper, audit, should_solve):
    apportioner = Apportioner.__new__(Apportioner)
    apportioner.loss_model = None
    apportioner.tm = Mock()
    apportioner.tm.get_anchor_var.return_value = "x"
    apportioner.engine = Mock()
    apportioner.engine.get_variable_bounds.return_value = (1.0, upper)
    apportioner.engine.maximize_and_update_variable.return_value = upper
    apportioner.cur_trxn_value = {"x": 1.0}
    apportioner.generate_audit = audit
    apportioner._minimize_minus_vars = Mock(return_value={})
    apportioner._reset_minus_vars = Mock()
    apportioner._apply_natural_flow_change = Mock()
    apportioner._capture_solve_step_data = Mock(return_value=(None, None))
    apportioner._record_audit_iteration = Mock()
    apportioner._with_feasibility_fallback = lambda description, operation: operation()
    apportioner._maximize_var(PathTrxn(id="test"))
    assert apportioner.engine.maximize_and_update_variable.called == should_solve
