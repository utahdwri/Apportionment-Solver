"""Analytical acceptance tests for signed conventions and joint loss sharing."""

from dataclasses import asdict

import pytest

from tests.test_piecewise_losses import curve, path_problem, value
from ut_water_apportionment import (
    AccountingGraph,
    FlowMeasurement,
    InterzoneFlow,
    LossCurvePoint,
    LossDefinition,
    LossInterval,
    MeasurementCollection,
    MeasurementSeries,
    PathTrxn,
    SolverInput,
    TrxnGroup,
    TrxnPathItem,
    Zone,
    ZoneTypes,
    solve,
)


@pytest.fixture(autouse=True)
def dependencies(caplog):
    pytest.importorskip("highspy")
    yield
    assert "Adding feasibility slacks" not in caplog.text


def losses_at(result, flow="A>B", endpoint="to_zone"):
    return {
        r.txn_id: r
        for r in result.loss_allocations
        if r.interzone_flow_id == flow and r.endpoint == endpoint
    }


def test_fixed_positive_graph_domain_excludes_disjoint_segments():
    from ut_water_apportionment.lp_solver_HIGHSPY import LPSolver
    from ut_water_apportionment.signed_losses import SignedLossModel

    model = object.__new__(SignedLossModel)
    model.engine = LPSolver()
    model.date = "2000-01-01"
    model._serial = 0
    model._tracking_nf = False
    model._path_binaries = set()
    model._graphs = []
    driver = model.variable("fixed_driver", 50, 50)
    remaining = model.remaining_graph(curve(), driver, 50, "fixed", lower=50)
    _, values = model.engine.solve_objective([remaining])
    assert values[remaining] == pytest.approx(15)


def signed_site(
    method="depletion",
    priorities=(1, 2),
    amounts=(20, 60),
    physical=40,
    reverse_flag=False,
):
    """One fixed physical site; rights enter/leave imports/uses, so NF is separate."""
    # IMPORT/USE endpoints permit accounting in either direction, while the
    # measured physical flow is positive. Endpoints both IMPORT make NF ZERO.
    return SolverInput(
        beg_date="2000-01-01",
        end_date="2000-01-01",
        loss_attribution_method=method,
        accounting_graph=AccountingGraph(
            [Zone("A", ZoneTypes.IMPORT), Zone("B", ZoneTypes.IMPORT)],
            [
                InterzoneFlow(
                    "A>B",
                    "A",
                    "B",
                    bidirectional=reverse_flag,
                    flow_measurements=[FlowMeasurement("AB")],
                    loss_to_zone=curve(),
                )
            ],
        ),
        txns=[
            PathTrxn(
                "reverse",
                priority=priorities[0],
                upper_limit=amounts[0],
                path=[TrxnPathItem("A>B", factor=-1)],
            ),
            PathTrxn(
                "forward",
                priority=priorities[1],
                upper_limit=amounts[1],
                path=[TrxnPathItem("A>B")],
            ),
        ],
        measurements=MeasurementCollection(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            series=[MeasurementSeries("AB", [physical])],
        ),
    )


@pytest.mark.parametrize(
    "method,senior,junior", [("depletion", 46, 10), ("buildup", 20, 27)]
)
def test_forward_priorities_use_selected_reference(method, senior, junior):
    problem = path_problem()
    problem.loss_attribution_method = method
    result = solve(problem, solver_backend="highspy")
    assert value(result, "T0", "B>U") == pytest.approx(senior)
    assert value(result, "T1", "B>U") == pytest.approx(junior)
    assert sum(r.loss for r in losses_at(result).values()) == pytest.approx(44)


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize(
    "method,reverse_loss,forward_loss", [("buildup", 0, 30), ("depletion", -10, 40)]
)
def test_reverse_senior_and_forward_junior(method, reverse_loss, forward_loss, flag):
    result = solve(signed_site(method, reverse_flag=flag), solver_backend="highspy")
    records = losses_at(result)
    assert value(result, "reverse", "A>B") == pytest.approx(-20)
    assert value(result, "forward", "A>B") == pytest.approx(60)
    assert records["reverse"].inflow == pytest.approx(-20)
    assert records["reverse"].loss == pytest.approx(reverse_loss)
    assert records["forward"].loss == pytest.approx(forward_loss)
    assert sum(r.loss for r in records.values()) == pytest.approx(30)
    assert sum(r.remaining for r in records.values()) == pytest.approx(10)


@pytest.mark.parametrize("method", ["buildup", "depletion"])
def test_equal_priority_forward_cohort_is_delivery_weighted(method):
    problem = path_problem()
    problem.loss_attribution_method = method
    for t in problem.txns:
        t.priority = 1
    result = solve(problem)
    assert result.solver_backend == "highspy"
    records = losses_at(result)
    total = 34 if method == "depletion" else 43
    assert records["T0"].loss == pytest.approx(total * 2 / 3, abs=1e-5)
    assert records["T1"].loss == pytest.approx(total / 3, abs=1e-5)
    assert records["T0"].remaining / records["T1"].remaining == pytest.approx(2)
    assert sum(r.loss for r in records.values()) == pytest.approx(44)


@pytest.mark.parametrize("method", ["buildup", "depletion"])
def test_mixed_direction_equal_priority_cohort(method):
    result = solve(signed_site(method, priorities=(1, 1)))
    records = losses_at(result)
    # One increment uses the predetermined anchor ratio 20:60.
    reverse_loss = 30 / 4
    forward_delivery = 60 - 30 * 3 / 4
    assert records["reverse"].loss == pytest.approx(reverse_loss, abs=1e-5)
    assert records["forward"].loss == pytest.approx(30 - reverse_loss, abs=1e-5)
    assert records["reverse"].remaining == pytest.approx(-20 - reverse_loss, abs=1e-5)
    assert records["forward"].remaining == pytest.approx(forward_delivery, abs=1e-5)


@pytest.mark.parametrize("method", ["buildup", "depletion"])
def test_zero_net_cohort_has_no_loss_before_residual(method):
    result = solve(signed_site(method, priorities=(1, 1), amounts=(20, 20)))
    records = losses_at(result)
    assert records["reverse"].loss == pytest.approx(0, abs=1e-6)
    assert records["forward"].loss == pytest.approx(0, abs=1e-6)
    assert sum(r.loss for r in records.values()) == pytest.approx(30)


def test_all_lost_cohort_has_zero_delivery_and_conserves_each_input():
    problem = path_problem(limits=(5, 10))
    problem.loss_attribution_method = "buildup"
    for t in problem.txns:
        t.priority = 1
    result = solve(problem)
    records = losses_at(result)
    assert records["T0"].remaining == pytest.approx(0, abs=1e-6)
    assert records["T1"].remaining == pytest.approx(0, abs=1e-6)
    assert records["T0"].loss == pytest.approx(5)
    assert records["T1"].loss == pytest.approx(10)


def test_cohort_sharing_is_independent_of_input_order():
    problem = path_problem(limits=(40, 20, 10))
    for t in problem.txns:
        t.priority = 1
    a = losses_at(solve(problem))
    problem.txns.reverse()
    b = losses_at(solve(problem))
    for name in a:
        assert b[name].loss == pytest.approx(a[name].loss, abs=1e-5)
        assert b[name].remaining == pytest.approx(a[name].remaining, abs=1e-5)


def test_scaled_component_uses_physical_flow_units():
    problem = path_problem(limits=(10,))
    problem.txns[0].path[1].factor = 2
    result = solve(problem, solver_backend="highspy")
    assert value(result, "T0", "A>B") == pytest.approx(10)
    assert value(result, "T0", "B>U") == pytest.approx(9)
    assert losses_at(result)["T0"].inflow == pytest.approx(10)


def test_setting_validates_and_serializes():
    problem = path_problem()
    assert asdict(problem)["loss_attribution_method"] == "depletion"
    problem.loss_attribution_method = "buildup"
    assert asdict(problem)["loss_attribution_method"] == "buildup"
    problem.loss_attribution_method = "typo"
    with pytest.raises(ValueError, match="loss_attribution_method"):
        solve(problem)


def test_delivery_cap_preserves_the_completed_increment():
    problem = path_problem(limits=(60, 30))
    graph = problem.accounting_graph
    graph.zones.extend([Zone("U0", ZoneTypes.USE), Zone("U1", ZoneTypes.USE)])
    graph.interzone_flows = [f for f in graph.interzone_flows if f.id != "B>U"]
    for i, t in enumerate(problem.txns):
        t.priority = 1
        t.path[-1] = TrxnPathItem(f"B>U{i}")
        graph.interzone_flows.append(
            InterzoneFlow(
                f"B>U{i}",
                "B",
                f"U{i}",
                flow_measurements=[FlowMeasurement(f"BU{i}")],
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
    result = solve(problem)
    # The first increment has 10% loss. T0 stops at its delivery cap and
    # keeps that loss while T1 continues through the next breakpoint.
    assert value(result, "T0", "I>A") == pytest.approx(100 / 9, abs=1e-5)
    assert value(result, "T1", "I>A") == pytest.approx(30)
    assert value(result, "T0", "B>U0") == pytest.approx(10)
    records = losses_at(result)
    assert records["T0"].loss == pytest.approx(10 / 9)
    assert value(result, "T1", "B>U1") == pytest.approx(239 / 9)


def test_buildup_spill_credits_the_incremental_residual_delivery():
    from ut_water_apportionment import FlowComponentsTypes

    problem = SolverInput(
        beg_date="2000-01-01",
        end_date="2000-01-01",
        loss_attribution_method="buildup",
        accounting_graph=AccountingGraph(
            [
                Zone("I", ZoneTypes.IMPORT),
                Zone("A", ZoneTypes.STREAM),
                Zone("U", ZoneTypes.USE),
                Zone("SYS", ZoneTypes.SYSTEM_GAIN_LOSS),
            ],
            [
                InterzoneFlow(
                    "I>A",
                    "I",
                    "A",
                    flow_measurements=[FlowMeasurement("IA")],
                    loss_to_zone=curve(),
                ),
                InterzoneFlow(
                    "A>U", "A", "U", flow_measurements=[FlowMeasurement("AU")]
                ),
                InterzoneFlow(
                    "GA",
                    "SYS",
                    "A",
                    bidirectional=True,
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                ),
            ],
        ),
        txns=[
            PathTrxn(
                "imported",
                priority=1,
                upper_limit=60,
                path=[TrxnPathItem("I>A"), TrxnPathItem("A>U")],
            ),
            PathTrxn("natural", priority=2, upper_limit=36, path=[TrxnPathItem("A>U")]),
        ],
        measurements=MeasurementCollection(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            series=[MeasurementSeries("IA", [100]), MeasurementSeries("AU", [56])],
        ),
    )
    result = solve(problem, solver_backend="highspy")
    assert value(result, "imported", "A>U") == pytest.approx(20)
    assert value(result, "natural", "A>U") == pytest.approx(36)
    spill = next(s for s in result.solve_steps if s.reason == "Spills")
    assert spill.remaining_natural_flow["A"] == pytest.approx(36)


def test_unbounded_counterflow_is_rejected_without_an_invented_cap():
    with pytest.raises(ValueError, match="finite counterflow bound"):
        solve(signed_site(amounts=(None, None)), solver_backend="highspy")


def test_mixed_cohort_gross_delivery_can_exceed_its_net_delivery():
    problem = signed_site(priorities=(1, 1), amounts=(1, 100), physical=1000)
    problem.accounting_graph.interzone_flows[
        0
    ].loss_to_zone = LossDefinition.piecewise_linear(
        [LossCurvePoint(0, 0), LossCurvePoint(2000, 1800)]
    )
    result = solve(problem)
    records = losses_at(result)
    forward_delivery = 100 - 0.9 * (100 - 1) * 100 / 101
    assert value(result, "forward", "A>B") == pytest.approx(100)
    assert value(result, "reverse", "A>B") == pytest.approx(-1)
    assert records["forward"].remaining == pytest.approx(forward_delivery, abs=1e-5)
    assert records["forward"].remaining + records["reverse"].remaining == pytest.approx(
        9.9
    )


@pytest.mark.parametrize("method,entry", [("buildup", 20), ("depletion", 10)])
def test_reverse_multileg_path_uses_directional_endpoint_delivery(method, entry):
    problem = signed_site(method)
    graph = problem.accounting_graph
    graph.zones.extend([Zone("I", ZoneTypes.IMPORT), Zone("U", ZoneTypes.USE)])
    graph.interzone_flows.extend(
        [
            InterzoneFlow("I>B", "I", "B", flow_measurements=[FlowMeasurement("IB")]),
            InterzoneFlow("A>U", "A", "U", flow_measurements=[FlowMeasurement("AU")]),
        ]
    )
    problem.txns[0].path = [
        TrxnPathItem("I>B"),
        TrxnPathItem("A>B", factor=-1),
        TrxnPathItem("A>U"),
    ]
    problem.txns[0].upper_limit = entry
    problem.measurements = MeasurementCollection(
        beg_date=problem.beg_date,
        end_date=problem.end_date,
        series=[
            MeasurementSeries("AB", [40]),
            MeasurementSeries("IB", [entry]),
            MeasurementSeries("AU", [20]),
        ],
    )
    result = solve(problem, solver_backend="highspy")
    assert value(result, "reverse", "I>B") == pytest.approx(entry)
    assert value(result, "reverse", "A>B") == pytest.approx(-20)
    assert value(result, "reverse", "A>U") == pytest.approx(20)
    assert value(result, "forward", "A>B") == pytest.approx(60)


@pytest.mark.parametrize(
    "method,forward_loss,reverse_loss", [("depletion", 30, 0), ("buildup", 40, -10)]
)
def test_forward_senior_can_deplete_below_zero(method, forward_loss, reverse_loss):
    result = solve(signed_site(method, priorities=(2, 1)), solver_backend="highspy")
    records = losses_at(result)
    assert value(result, "forward", "A>B") == pytest.approx(60)
    assert records["forward"].loss == pytest.approx(forward_loss)
    assert records["reverse"].loss == pytest.approx(reverse_loss)


@pytest.mark.parametrize("method", ["buildup", "depletion"])
@pytest.mark.parametrize("shared", [False, True])
def test_signed_post_loss_gauge_uses_the_inverse_curve(method, shared):
    problem = signed_site(method, priorities=(1, 1) if shared else (1, 2))
    flow = problem.accounting_graph.interzone_flows[0]
    flow.loss_to_zone = LossDefinition()
    flow.loss_from_zone = LossDefinition.piecewise_linear(
        [
            LossCurvePoint(0, 0),
            LossCurvePoint(100, 20),
        ]
    )
    result = solve(problem)
    records = losses_at(result, endpoint="from_zone")
    assert sum(r.inflow for r in records.values()) == pytest.approx(50)
    assert sum(r.loss for r in records.values()) == pytest.approx(10)
    if shared:
        # Fixed anchor weights 20:60 split aggregate loss 10 as 2.5:7.5.
        d = 20 - 10 / 4
        assert records["reverse"].inflow == pytest.approx(-d, abs=1e-5)
        assert records["reverse"].loss == pytest.approx(20 - d, abs=1e-5)
    else:
        assert records["reverse"].loss == pytest.approx(
            0 if method == "buildup" else -5
        )


@pytest.mark.parametrize("method,total_loss", [("buildup", 10), ("depletion", 9)])
def test_joint_cohort_composes_both_endpoint_losses(method, total_loss):
    before = LossDefinition.piecewise_linear(
        [
            LossCurvePoint(0, 0),
            LossCurvePoint(10, 2),
            LossCurvePoint(30, 10),
            LossCurvePoint(100, 17),
        ]
    )
    problem = path_problem(
        limits=(10, 20), flow=40, loss_from=before, loss_to=LossDefinition.linear(0.2)
    )
    problem.loss_attribution_method = method
    for t in problem.txns:
        t.priority = 1
    result = solve(problem)
    records = losses_at(result, endpoint="from_zone")
    assert records["T0"].loss == pytest.approx(total_loss / 3, abs=1e-5)
    assert records["T1"].loss == pytest.approx(total_loss * 2 / 3, abs=1e-5)
    assert value(result, "T0", "B>U") == pytest.approx(
        (10 - total_loss / 3) * 0.8, abs=1e-5
    )
    assert value(result, "T1", "B>U") == pytest.approx(
        (20 - total_loss * 2 / 3) * 0.8, abs=1e-5
    )


@pytest.mark.parametrize("method", ["buildup", "depletion"])
def test_shared_limit_bounds_counterflow_without_individual_caps(method):
    problem = signed_site(method, priorities=(1, 1), amounts=(None, None))
    problem.txns = [
        TrxnGroup("group", priority=0, upper_limit=80, children_trxns=problem.txns)
    ]
    result = solve(problem)
    assert abs(value(result, "reverse", "A>B")) + value(
        result, "forward", "A>B"
    ) == pytest.approx(80)
    records = losses_at(result)
    assert sum(r.inflow for r in records.values()) == pytest.approx(40)
    assert sum(r.loss for r in records.values()) == pytest.approx(30)


def test_automatic_backend_covers_later_piecewise_dates_and_audit_modes():
    problem = path_problem(days=2)
    problem.accounting_graph.interzone_flows[
        1
    ].loss_to_zone = LossDefinition.time_varying_piecewise_linear(
        [LossInterval("2000-01-01", "2000-01-01", LossDefinition.linear(0.2))],
        default=curve(),
    )
    for t in problem.txns:
        t.priority = 1
    problem.measurements = MeasurementCollection(
        beg_date=problem.beg_date,
        end_date=problem.end_date,
        series=[
            MeasurementSeries("IA", [100, 100]),
            MeasurementSeries("AB", [100, 100]),
            MeasurementSeries("BU", [80, 56]),
        ],
    )
    audited = solve(problem)
    unaudited = solve(problem, generate_audit=False)
    assert audited.solver_backend == unaudited.solver_backend == "highspy"
    assert value(audited, "T0", "B>U") == pytest.approx(48)
    assert value(audited, "T0", "B>U", "2000-01-02") == pytest.approx(112 / 3)
    assert audited.apportionments == unaudited.apportionments
    assert audited.loss_allocations == unaudited.loss_allocations
    assert not unaudited.solve_steps and not unaudited.loss_events
