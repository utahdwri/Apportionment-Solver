"""Exact curve, incremental accounting, and solver-level regressions."""

import pytest

from ut_water_apportionment import (
    AccountingGraph,
    FlowComponentsTypes,
    FlowMeasurement,
    InterzoneFlow,
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
from ut_water_apportionment.loss_models import (
    LossCurvePoint,
    LossDefinition,
    LossInterval,
    ResolvedLossRelation,
)


@pytest.fixture(autouse=True)
def require_highspy():
    pytest.importorskip("highspy")


def curve():
    return LossDefinition.piecewise_linear(
        [
            LossCurvePoint(0, 0),
            LossCurvePoint(20, 20),
            LossCurvePoint(60, 40),
            LossCurvePoint(100, 44),
        ]
    )


def value(result, txn, flow, day="2000-01-01"):
    return sum(
        r.value for r in result.get_result_value(trxn_id=txn, flow_id=flow, date=day)
    )


def routed_diversions(loss=None, priorities=(1, 2), days=1):
    loss = loss or curve()
    end = f"2000-01-{days:02}"
    flows = [
        InterzoneFlow(
            "A>B",
            "A",
            "B",
            flow_measurements=[FlowMeasurement("AB")],
            loss_to_zone=loss,
        ),
        InterzoneFlow("A>U", "A", "U", flow_measurements=[FlowMeasurement("AU")]),
        InterzoneFlow("B>V", "B", "V", flow_measurements=[FlowMeasurement("BV")]),
        InterzoneFlow(
            "GA",
            "SYS",
            "A",
            bidirectional=True,
            flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
        ),
        InterzoneFlow(
            "GB",
            "SYS",
            "B",
            bidirectional=True,
            flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
        ),
    ]
    return SolverInput(
        accounting_graph=AccountingGraph(
            [
                Zone("A", ZoneTypes.STREAM),
                Zone("B", ZoneTypes.STREAM),
                Zone("U", ZoneTypes.USE),
                Zone("V", ZoneTypes.USE),
                Zone("SYS", ZoneTypes.SYSTEM_GAIN_LOSS),
            ],
            flows,
        ),
        txns=[
            PathTrxn(
                "upstream",
                priority=priorities[0],
                upper_limit=90,
                path=[TrxnPathItem("A>U")],
            ),
            PathTrxn(
                "downstream",
                priority=priorities[1],
                upper_limit=5,
                path=[TrxnPathItem("B>V")],
            ),
        ],
        measurements=MeasurementCollection(
            beg_date="2000-01-01",
            end_date=end,
            series=[
                MeasurementSeries("AB", [10] * days),
                MeasurementSeries("AU", [90] * days),
                MeasurementSeries("BV", [5] * days),
            ],
        ),
        beg_date="2000-01-01",
        end_date=end,
    )


def path_problem(limits=(60, 30), flow=100, loss_from=None, loss_to=None, days=1):
    from_loss, to_loss = loss_from or LossDefinition(), loss_to or curve()
    mid = from_loss.transform_total_flow(flow, date="2000-01-01")
    out = to_loss.transform_total_flow(mid, date="2000-01-01")
    end = f"2000-01-{days:02}"
    return SolverInput(
        accounting_graph=AccountingGraph(
            [
                Zone("I", ZoneTypes.IMPORT),
                Zone("A", ZoneTypes.STREAM),
                Zone("B", ZoneTypes.STREAM),
                Zone("U", ZoneTypes.USE),
                Zone("SYS", ZoneTypes.SYSTEM_GAIN_LOSS),
            ],
            [
                InterzoneFlow(
                    "I>A", "I", "A", flow_measurements=[FlowMeasurement("IA")]
                ),
                InterzoneFlow(
                    "A>B",
                    "A",
                    "B",
                    flow_measurements=[FlowMeasurement("AB")],
                    loss_from_zone=from_loss,
                    loss_to_zone=to_loss,
                ),
                InterzoneFlow(
                    "B>U", "B", "U", flow_measurements=[FlowMeasurement("BU")]
                ),
                InterzoneFlow(
                    "GA",
                    "SYS",
                    "A",
                    bidirectional=True,
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                ),
                InterzoneFlow(
                    "GB",
                    "SYS",
                    "B",
                    bidirectional=True,
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                ),
            ],
        ),
        txns=[
            PathTrxn(
                f"T{i}",
                priority=i,
                upper_limit=limit,
                path=[TrxnPathItem("I>A"), TrxnPathItem("A>B"), TrxnPathItem("B>U")],
            )
            for i, limit in enumerate(limits)
        ],
        measurements=MeasurementCollection(
            beg_date="2000-01-01",
            end_date=end,
            series=[
                MeasurementSeries("IA", [flow] * days),
                MeasurementSeries("AB", [mid] * days),
                MeasurementSeries("BU", [out] * days),
            ],
        ),
        beg_date="2000-01-01",
        end_date=end,
    )


def test_increment_integrates_multiple_segments():
    loss = curve()
    assert loss.transform_component_increment(-90, driver_flow=100) == pytest.approx(
        -56
    )
    assert loss.transform_component_increment(90, driver_flow=10) == pytest.approx(56)
    assert loss.transform_component_increment(-40, driver_flow=100) == pytest.approx(
        -36
    )


def test_natural_flow_withdrawal_crosses_all_loss_segments():
    result = solve(routed_diversions(), solver_backend="highspy")
    assert value(result, "upstream", "A>U") == pytest.approx(90)
    assert value(result, "downstream", "B>V") == pytest.approx(5)
    steps = [
        s
        for s in result.solve_steps
        if any(v.variable_name == "upstream___A>U" for v in s.variables)
    ]
    assert steps[0].remaining_natural_flow == pytest.approx({"A": 10, "B": 5})


def test_fixed_boundary_uses_exact_difference_and_retains_scipy_support():
    problem = routed_diversions()
    problem.external_natural_flows = {"A>B": {"2000-01-01": 100}}
    result = solve(problem, solver_backend="scipy")
    before = next(s for s in result.solve_steps if s.reason == "NF Calculations")
    after = next(s for s in result.solve_steps if s.reason == "NF Boundary Adjustments")
    # The external area already removed 90 at the gauge: downstream impact
    # is R(100) - R(10) = 56, not R(90) = 47.
    assert before.remaining_natural_flow["B"] == pytest.approx(61)
    assert after.remaining_natural_flow["B"] == pytest.approx(5)
    assert value(result, "downstream", "B>V") == pytest.approx(5)


def test_equal_priority_diversions_cross_breakpoints_together():
    result = solve(routed_diversions(priorities=(1, 1)), solver_backend="highspy")
    assert value(result, "upstream", "A>U") == pytest.approx(90)
    assert value(result, "downstream", "B>V") == pytest.approx(5)


def test_path_incremental_loss_preserves_senior_attribution():
    result = solve(path_problem(), solver_backend="highspy")
    assert value(result, "T0", "I>A") == pytest.approx(60)
    assert value(result, "T0", "B>U") == pytest.approx(46)
    assert value(result, "T1", "I>A") == pytest.approx(30)
    assert value(result, "T1", "B>U") == pytest.approx(10)
    losses = {x.txn_id: x.loss for x in result.loss_allocations}
    assert losses == pytest.approx({"T0": 14, "T1": 20, "SLACK_A_TO_B_A>B": 10})
    assert sum(losses.values()) == pytest.approx(44)


def test_from_and_to_losses_with_intercepts():
    before = LossDefinition.piecewise_linear(
        [
            LossCurvePoint(0, 0),
            LossCurvePoint(10, 2),
            LossCurvePoint(30, 10),
            LossCurvePoint(100, 17),
        ]
    )
    result = solve(
        path_problem(
            limits=(10,), flow=40, loss_from=before, loss_to=LossDefinition.linear(0.2)
        ),
        solver_backend="highspy",
    )
    assert value(result, "T0", "I>A") == pytest.approx(10)
    assert value(result, "T0", "A>B") == pytest.approx(9)
    assert value(result, "T0", "B>U") == pytest.approx(7.2)


def test_post_loss_anchor_consumes_the_full_source_withdrawal():
    before = LossDefinition.piecewise_linear(
        [
            LossCurvePoint(0, 0),
            LossCurvePoint(10, 2),
            LossCurvePoint(30, 10),
            LossCurvePoint(100, 17),
        ]
    )
    problem = path_problem(
        limits=(10,), flow=40, loss_from=before, loss_to=LossDefinition.linear(0.2)
    )
    problem.accounting_graph.interzone_flows.pop(0)
    problem.txns[0].path.pop(0)
    result = solve(problem, solver_backend="highspy")
    assert value(result, "T0", "A>B") == pytest.approx(10)
    assert value(result, "T0", "B>U") == pytest.approx(8)
    allocation = next(x for x in result.loss_allocations if x.txn_id == "T0")
    # The gauge falls from 29 to 19; the pre-loss pool falls from 40 to 28 1/3.
    assert allocation.inflow == pytest.approx(35 / 3)
    step = next(
        s
        for s in result.solve_steps
        if any(v.variable_name == "T0___A>B" for v in s.variables)
    )
    assert step.remaining_natural_flow["A"] == pytest.approx(85 / 3)


def test_integer_lag_loss_records_match_apportionment_dates():
    problem = path_problem(days=2)
    problem.beg_date = problem.end_date = "2000-01-02"
    problem.accounting_graph.interzone_flows[1].lag_to_zone = 1
    result = solve(problem, solver_backend="highspy")
    assert {x.date for x in result.loss_allocations} == {"2000-01-01"}
    assert value(result, "T0", "A>B", "2000-01-01") == pytest.approx(60)
    assert value(result, "T0", "B>U", "2000-01-02") == pytest.approx(46)


def test_fractional_lag_requires_coupled_time_model():
    problem = path_problem(days=2)
    problem.beg_date = problem.end_date = "2000-01-02"
    problem.accounting_graph.interzone_flows[1].lag_to_zone = 0.5
    with pytest.raises(ValueError, match="Fractional lags"):
        solve(problem, solver_backend="highspy")


def test_endogenous_curve_requires_native_highspy():
    with pytest.raises(ValueError, match="native highspy"):
        solve(routed_diversions(), solver_backend="scipy")


def test_piecewise_pre_loss_gauge_ambiguity_is_rejected():
    with pytest.raises(ValueError, match="100% marginal-loss"):
        solve(path_problem(loss_from=curve()), solver_backend="highspy")


def test_bidirectional_site_with_positive_physical_flow_is_supported():
    problem = routed_diversions()
    problem.accounting_graph.interzone_flows[0].bidirectional = True
    result = solve(problem, solver_backend="highspy")
    assert value(result, "upstream", "A>U") == pytest.approx(90)
    assert value(result, "downstream", "B>V") == pytest.approx(5)


def test_shared_site_equal_priority_uses_highs():
    problem = path_problem()
    problem.txns[1].priority = problem.txns[0].priority
    result = solve(problem, solver_backend="highspy")
    assert result.solver_backend == "highspy"
    assert value(result, "T0", "I>A") == pytest.approx(60)
    assert value(result, "T1", "I>A") == pytest.approx(30)


def test_discontinuous_raw_segments_rejected():
    with pytest.raises(ValueError, match="continuous"):
        LossDefinition(
            segments=(
                ResolvedLossRelation(0, 0, 10, 0.2, 0),
                ResolvedLossRelation(1, 10, None, 0, 3),
            )
        )


def test_spill_credit_reopens_nonlinear_upstream_supply():
    problem = routed_diversions(priorities=(2, 1))
    problem.accounting_graph.zones.append(Zone("I", ZoneTypes.IMPORT))
    problem.accounting_graph.interzone_flows.append(
        InterzoneFlow("I>B", "I", "B", flow_measurements=[FlowMeasurement("IB")])
    )
    problem.measurements = MeasurementCollection(
        series=problem.measurements.series + [MeasurementSeries("IB", [30])],
        beg_date=problem.beg_date,
        end_date=problem.end_date,
    )
    problem.measurements.series[2].values = [35]
    problem.txns[1].upper_limit = 35
    result = solve(problem, solver_backend="highspy")
    steps = [
        s
        for s in result.solve_steps
        if any(v.variable_name == "upstream___A>U" for v in s.variables)
    ]
    first = next(
        v.value_after for v in steps[0].variables if v.variable_name == "upstream___A>U"
    )
    # R(q) = .9q-34 on [60,100]; downstream needs 35-5 = 30.
    assert first == pytest.approx(100 - 64 / 0.9)
    assert value(result, "upstream", "A>U") == pytest.approx(90)
    assert value(result, "downstream", "B>V") == pytest.approx(35)
    assert any(s.reason == "Spills" for s in result.solve_steps)


def test_time_varying_piecewise_to_constant_resets_each_day():
    changing = LossDefinition.time_varying_piecewise_linear(
        [
            LossInterval("2000-01-01", "2000-01-01", curve()),
            LossInterval("2000-01-02", "2000-01-02", LossDefinition.linear(0.2)),
        ]
    )
    problem = path_problem(limits=(60,), loss_to=changing, days=2)
    problem.measurements.series[2].values[1] = 80
    result = solve(problem, solver_backend="highspy")
    assert value(result, "T0", "B>U", "2000-01-01") == pytest.approx(46)
    assert value(result, "T0", "B>U", "2000-01-02") == pytest.approx(48)


@pytest.mark.parametrize("priority_pair", [(1, 2), (2, 1), (1, 1)])
def test_shared_parent_limit_with_piecewise_natural_flow(priority_pair):
    problem = routed_diversions(priorities=priority_pair)
    problem.txns = [
        TrxnGroup("group", priority=0, upper_limit=50, children_trxns=problem.txns)
    ]
    result = solve(problem, solver_backend="highspy")
    up, down = value(result, "upstream", "A>U"), value(result, "downstream", "B>V")
    assert up + down == pytest.approx(50)
    if priority_pair == (1, 2):
        assert up == pytest.approx(50)
    elif priority_pair == (2, 1):
        assert down == pytest.approx(5)
    else:
        assert up / down == pytest.approx(90 / 5)


def test_negative_marginal_loss_is_supported():
    decreasing = LossDefinition.piecewise_linear(
        [
            LossCurvePoint(0, 0),
            LossCurvePoint(20, 10),
            LossCurvePoint(40, 5),
            LossCurvePoint(100, 5),
        ]
    )
    result = solve(
        path_problem(limits=(20,), flow=40, loss_to=decreasing),
        solver_backend="highspy",
    )
    assert value(result, "T0", "B>U") == pytest.approx(25)
    allocation = next(x for x in result.loss_allocations if x.txn_id == "T0")
    assert allocation.loss == pytest.approx(-5)
    assert sum(x.loss for x in result.loss_allocations) == pytest.approx(5)


def test_capped_tail_and_exact_start_breakpoint():
    result = solve(path_problem(limits=(40, 40), flow=140), solver_backend="highspy")
    assert value(result, "T0", "B>U") == pytest.approx(
        40
    )  # Constant total loss above 100.
    assert value(result, "T1", "B>U") == pytest.approx(36)  # 100 -> 60, slope .1.


def test_two_piecewise_endpoints_compose_exactly():
    before = LossDefinition.piecewise_linear(
        [LossCurvePoint(0, 0), LossCurvePoint(20, 5), LossCurvePoint(100, 10)]
    )
    problem = path_problem(limits=(40, 30), loss_from=before)
    result = solve(problem, solver_backend="highspy")
    initial = 100
    mid = before.transform_total_flow(initial)
    senior_mid = mid - before.transform_total_flow(initial - 40)
    senior_end = curve().transform_total_flow(mid) - curve().transform_total_flow(
        mid - senior_mid
    )
    assert value(result, "T0", "A>B") == pytest.approx(senior_mid)
    assert value(result, "T0", "B>U") == pytest.approx(senior_end)
    assert sum(
        x.loss for x in result.loss_allocations if x.endpoint == "from_zone"
    ) == pytest.approx(10)
    assert sum(
        x.loss for x in result.loss_allocations if x.endpoint == "to_zone"
    ) == pytest.approx(curve().get_loss(mid))


def test_breakpoints_are_reported_without_becoming_allocation_limits():
    result = solve(routed_diversions(), solver_backend="highspy")
    events = [
        e
        for e in result.loss_events
        if e.objective_id == "upstream" and e.driver_kind == "remaining_natural_flow"
    ]
    assert len(events) == 1
    assert events[0].flow_before == pytest.approx(100)
    assert events[0].flow_after == pytest.approx(10)
    assert events[0].breakpoints == [60, 20]
    assert value(result, "upstream", "A>U") == pytest.approx(90)
    unaudited = solve(
        routed_diversions(), solver_backend="highspy", generate_audit=False
    )
    assert unaudited.loss_events == []
    assert unaudited.apportionments == result.apportionments


def test_many_curves_against_independent_segment_enumeration():
    import random

    rng = random.Random(7345)
    for _ in range(25):
        x = [0.0, 20.0, 60.0, 100.0]
        losses = [0.0, rng.uniform(0, 20)]
        losses += [rng.uniform(0, losses[-1] + 40)]
        losses += [rng.uniform(0, min(100, losses[-1] + 40))]
        loss = LossDefinition.piecewise_linear(
            [LossCurvePoint(q, amount) for q, amount in zip(x, losses, strict=True)]
        )
        imp = rng.uniform(0, 60)
        delivered_actual = 10 * (1 - losses[1] / 20)
        downstream_measurement = delivered_actual + 5 + imp
        downstream = min(downstream_measurement, 5 + 100 - losses[-1])
        target = max(0, downstream - 5)
        candidates = []
        # Enumerate affine pieces directly from input coordinates. This oracle
        # does not use the loss evaluator or the MIP representation.
        for j in range(3):
            r0, r1 = x[j] - losses[j], x[j + 1] - losses[j + 1]
            if target > r1 + 1e-9:
                continue
            if target <= r0:
                candidates.append(x[j])
            else:
                candidates.append(x[j] + (target - r0) * (x[j + 1] - x[j]) / (r1 - r0))
        expected = min(90, 100 - min(candidates))
        problem = routed_diversions(loss=loss, priorities=(2, 1))
        problem.accounting_graph.zones.append(Zone("I", ZoneTypes.IMPORT))
        problem.accounting_graph.interzone_flows.append(
            InterzoneFlow("I>B", "I", "B", flow_measurements=[FlowMeasurement("IB")])
        )
        problem.measurements.series[2].values = [downstream_measurement]
        problem.measurements = MeasurementCollection(
            series=problem.measurements.series + [MeasurementSeries("IB", [imp])],
            beg_date=problem.beg_date,
            end_date=problem.end_date,
        )
        problem.txns[1].upper_limit = downstream_measurement
        result = solve(problem, solver_backend="highspy")
        steps = [
            s
            for s in result.solve_steps
            if any(v.variable_name == "upstream___A>U" for v in s.variables)
        ]
        actual = next(
            v.value_after
            for v in steps[0].variables
            if v.variable_name == "upstream___A>U"
        )
        assert actual == pytest.approx(expected, abs=2e-6)
        assert value(result, "upstream", "A>U") == pytest.approx(90, abs=2e-6)
