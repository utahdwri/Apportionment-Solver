"""Independent expectations for breakpoint increments and immutable past losses."""

from collections import defaultdict
from copy import deepcopy

import pytest

from benchmarks.benchmark_piecewise import make_path_input
from tests.test_piecewise_losses import path_problem, value
from tests.test_signed_losses import losses_at, signed_site
from ut_water_apportionment import (
    FlowMeasurement,
    InterzoneFlow,
    LossCurvePoint,
    LossDefinition,
    MeasurementCollection,
    MeasurementSeries,
    TrxnGroup,
    TrxnPathItem,
    Zone,
    ZoneTypes,
    solve,
)
from ut_water_apportionment.apportioner import Apportioner


@pytest.fixture(autouse=True)
def exact_accounting(monkeypatch):
    pytest.importorskip("highspy")

    def unexpected_fallback(self):
        raise AssertionError(
            "This analytical case must not relax accounting constraints"
        )

    monkeypatch.setattr(Apportioner, "feasibility_fallback", unexpected_fallback)


def delivery_limited(method):
    problem = path_problem()
    problem.loss_attribution_method = method
    graph = problem.accounting_graph
    graph.zones.extend([Zone("U0", ZoneTypes.USE), Zone("U1", ZoneTypes.USE)])
    graph.interzone_flows = [f for f in graph.interzone_flows if f.id != "B>U"]
    for i, t in enumerate(problem.txns):
        t.priority = 1
        t.path[-1] = TrxnPathItem(f"B>U{i}")
        graph.interzone_flows.append(
            InterzoneFlow(
                f"B>U{i}", "B", f"U{i}", flow_measurements=[FlowMeasurement(f"BU{i}")]
            )
        )
    problem.measurements = MeasurementCollection(
        beg_date=problem.beg_date,
        end_date=problem.end_date,
        series=[
            MeasurementSeries("IA", [100]),
            MeasurementSeries("AB", [100]),
            MeasurementSeries("BU0", [10]),
            MeasurementSeries("BU1", [46]),
        ],
    )
    return problem


@pytest.mark.parametrize(
    "method,input0,output1,count0",
    [
        ("depletion", 100 / 9, 239 / 9, 1),
        ("buildup", 100 / 3, 13, 1),
    ],
)
def test_member_stops_without_reassigning_its_past_loss(
    method, input0, output1, count0
):
    result = solve(delivery_limited(method))
    assert value(result, "T0", "I>A") == pytest.approx(input0)
    assert value(result, "T0", "B>U0") == pytest.approx(10)
    assert value(result, "T1", "I>A") == pytest.approx(30)
    assert value(result, "T1", "B>U1") == pytest.approx(output1)
    first = [r for r in result.loss_increments if r.txn_id == "T0"]
    second = [r for r in result.loss_increments if r.txn_id == "T1"]
    assert len(first) == count0
    assert sum(r.loss for r in first) == pytest.approx(input0 - 10)
    assert max(r.sequence for r in first) < max(r.sequence for r in second)
    totals = losses_at(result)
    assert totals["T0"].loss / totals["T0"].remaining != pytest.approx(
        totals["T1"].loss / totals["T1"].remaining
    )


@pytest.mark.parametrize("method", ["buildup", "depletion"])
def test_audit_replays_fixed_weights_across_breakpoints(method):
    result = solve(delivery_limited(method))
    cohorts, members = defaultdict(list), defaultdict(list)
    for r in result.loss_increments:
        cohorts[r.sequence].append(r)
        members[r.txn_id].append(r)
        assert r.inflow - r.remaining == pytest.approx(r.loss)
    for records in cohorts.values():
        loss, delivery = sum(r.loss for r in records), sum(r.remaining for r in records)
        if delivery > 1e-7:
            for r in records:
                assert r.loss == pytest.approx(loss * r.allocation_weight, abs=1e-6)
    for t, records in members.items():
        total = losses_at(result)[t]
        assert sum(r.inflow for r in records) == pytest.approx(total.inflow)
        assert sum(r.remaining for r in records) == pytest.approx(total.remaining)
        assert sum(r.loss for r in records) == pytest.approx(total.loss)


@pytest.mark.parametrize("method", ["buildup", "depletion"])
def test_shared_parent_reservation_can_cross_breakpoints_in_one_increment(method):
    problem = path_problem()
    problem.loss_attribution_method = method
    for t in problem.txns:
        t.priority = 1
    problem.txns = [
        TrxnGroup("group", priority=0, upper_limit=50, children_trxns=problem.txns)
    ]
    result = solve(problem)
    assert value(result, "T0", "I>A") == pytest.approx(100 / 3)
    assert value(result, "T1", "I>A") == pytest.approx(50 / 3)
    assert {round(r.driver_after, 6) for r in result.loss_increments} == {50}
    assert len({r.sequence for r in result.loss_increments}) == 1


@pytest.mark.parametrize("method", ["depletion", "buildup"])
@pytest.mark.parametrize("topology", ["parent", "nested", "equal_parents"])
def test_parent_reserves_an_aggregate_its_delivery_limited_children_can_realize(
    method, topology
):
    problem = delivery_limited(method)
    if topology == "equal_parents":
        problem.txns = [
            TrxnGroup(
                f"group{i}",
                priority=0,
                upper_limit=t.upper_limit,
                children_trxns=[t],
            )
            for i, t in enumerate(problem.txns)
        ]
    else:
        problem.txns = [
            TrxnGroup(
                "group",
                priority=0.1,
                upper_limit=50,
                children_trxns=problem.txns,
            )
        ]
        if topology == "nested":
            problem.txns = [
                TrxnGroup(
                    "outer",
                    priority=0,
                    upper_limit=50,
                    children_trxns=problem.txns,
                )
            ]
    result = solve(problem)
    expected0 = 100 / 9 if method == "depletion" else 100 / 3
    expected1 = 50 / 3 if method == "buildup" and topology != "equal_parents" else 30
    assert value(result, "T0", "I>A") == pytest.approx(expected0)
    assert value(result, "T1", "I>A") == pytest.approx(expected1)
    assert value(result, "T0", "B>U0") == pytest.approx(10)
    # Reservations and recursive previews do not add audit allocations.
    assert sum(
        r.inflow for r in result.loss_increments if r.txn_id == "T0"
    ) == pytest.approx(expected0)


@pytest.mark.parametrize("method", ["depletion", "buildup"])
def test_interleaved_parent_reservation_is_reported_explicitly(method):
    problem = delivery_limited(method)
    outside = deepcopy(problem.txns[0])
    outside.id = "outside"
    outside.priority = 0.5
    problem.txns = [
        TrxnGroup("group", priority=0, upper_limit=50, children_trxns=problem.txns),
        outside,
    ]
    with pytest.raises(ValueError, match="interleaved.*outside"):
        solve(problem)


@pytest.mark.parametrize("method", ["buildup", "depletion"])
def test_multiple_loss_sites_cross_breakpoints_in_one_increment(method):
    problem = make_path_input(reaches=3, rights=2, days=1, sites=2)
    problem.loss_attribution_method = method
    for t in problem.txns:
        t.priority = 1
    result = solve(problem)
    assert value(result, "T0", "P0") == pytest.approx(1)
    assert value(result, "T1", "P0") == pytest.approx(1)
    definitions = {
        f.id: f.loss_to_zone for f in problem.accounting_graph.interzone_flows
    }
    assert len({r.sequence for r in result.loss_increments}) == 1
    crossings = 0
    for r in result.loss_increments:
        points = [s.min_driver_flow for s in definitions[r.interzone_flow_id].segments]
        lo, hi = sorted((r.driver_before, r.driver_after))
        crossings += sum(lo + 1e-7 < p < hi - 1e-7 for p in points)
    assert crossings > 0


def test_signed_pool_reverses_direction_and_crosses_zero_after_a_member_drops():
    problem = signed_site("buildup", priorities=(1, 1), amounts=(60, 20))
    problem.accounting_graph.zones.append(Zone("I", ZoneTypes.IMPORT))
    problem.accounting_graph.interzone_flows.append(
        InterzoneFlow("I>B", "I", "B", flow_measurements=[FlowMeasurement("IB")])
    )
    problem.txns[0].path.insert(0, TrxnPathItem("I>B"))
    problem.measurements = MeasurementCollection(
        beg_date=problem.beg_date,
        end_date=problem.end_date,
        series=[MeasurementSeries("AB", [40]), MeasurementSeries("IB", [10])],
    )
    result = solve(problem)
    assert value(result, "reverse", "I>B") == pytest.approx(10)
    assert value(result, "forward", "A>B") == pytest.approx(20)
    records = losses_at(result)
    assert records["reverse"].loss == pytest.approx(0)
    assert records["forward"].loss == pytest.approx(10)
    forward = [r for r in result.loss_increments if r.txn_id == "forward"]
    assert any(r.driver_after < -1 for r in forward)
    assert any(r.driver_before < -1 and r.driver_after > 0 for r in forward)


def test_increment_audit_is_optional_and_does_not_change_allocations():
    problem = delivery_limited("buildup")
    audited, quiet = solve(problem), solve(problem, generate_audit=False)
    assert audited.loss_increments
    assert quiet.loss_increments == []
    assert audited.apportionments == quiet.apportionments
    assert audited.loss_allocations == quiet.loss_allocations


def test_scaled_anchor_uses_allocation_weights_not_resulting_deliveries():
    problem = path_problem()
    for t in problem.txns:
        t.priority = 1
    problem.txns[0].path[0].factor = 2
    result = solve(problem, solver_backend="highspy")
    records = losses_at(result)
    # Raw anchor proportions 60:30; physical inputs become 80:20.
    # The revised rule splits loss 44 using 2:1, not physical input 4:1.
    assert records["T0"].inflow == pytest.approx(80)
    assert records["T1"].inflow == pytest.approx(20)
    assert records["T0"].loss == pytest.approx(44 * 2 / 3)
    assert records["T1"].loss == pytest.approx(44 / 3)
    assert records["T0"].remaining == pytest.approx(80 - 44 * 2 / 3)
    assert records["T1"].remaining == pytest.approx(20 - 44 / 3)


def test_different_upstream_losses_preserve_fixed_loss_weights():
    problem = path_problem(limits=(30, 30))
    problem.loss_attribution_method = "buildup"
    for t in problem.txns:
        t.priority = 1
    problem.accounting_graph.zones.append(Zone("J", ZoneTypes.IMPORT))
    problem.accounting_graph.interzone_flows.append(
        InterzoneFlow(
            "J>A",
            "J",
            "A",
            flow_measurements=[FlowMeasurement("JA")],
            loss_to_zone=LossDefinition.piecewise_linear(
                [
                    LossCurvePoint(0, 0),
                    LossCurvePoint(10, 10),
                ]
            ),
        )
    )
    problem.txns[1].path[0] = TrxnPathItem("J>A")
    problem.measurements = MeasurementCollection(
        beg_date=problem.beg_date,
        end_date=problem.end_date,
        series=[
            MeasurementSeries(name, [amount])
            for name, amount in [("IA", 30), ("JA", 30), ("AB", 50), ("BU", 15)]
        ],
    )
    result = solve(problem, solver_backend="highspy")
    records = losses_at(result)
    # The upstream loss makes arrivals 30:20, while anchor weights stay 1:1.
    assert records["T0"].inflow == pytest.approx(30)
    assert records["T1"].inflow == pytest.approx(20)
    for t in ("T0", "T1"):
        assert records[t].loss == pytest.approx(35 / 2)
    assert records["T0"].remaining == pytest.approx(12.5)
    assert records["T1"].remaining == pytest.approx(2.5)


def test_default_shared_solver_does_not_import_scip(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "pyscipopt", None)
    monkeypatch.setitem(sys.modules, "ut_water_apportionment.lp_solver_SCIP", None)
    result = solve(delivery_limited("depletion"))
    assert result.solver_backend == "highspy"
    assert value(result, "T1", "I>A") == pytest.approx(30)


@pytest.mark.parametrize("method", ["depletion", "buildup"])
def test_optional_scip_backend_uses_no_quadratic_rows(method, monkeypatch):
    pytest.importorskip("pyscipopt")
    from ut_water_apportionment.lp_solver_SCIP import LPSolver

    def forbid_quadratic(*args, **kwargs):
        raise AssertionError("Fixed allocation weights require no quadratic rows")

    monkeypatch.setattr(LPSolver, "add_quadratic_constraint", forbid_quadratic)
    problem = signed_site(method, priorities=(1, 1))
    result = solve(problem, solver_backend="scip")
    assert losses_at(result)["reverse"].loss == pytest.approx(7.5)
    assert losses_at(result)["forward"].loss == pytest.approx(22.5)
