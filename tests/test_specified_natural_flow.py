"""Acceptance tests for specified natural-flow support.

Specified natural flows are supplied exclusively through an InterzoneFlow's
``nf_measurements`` list.  The tests exercise the public
SolverInput/InterzoneFlow/solve API rather than NaturalFlowCalculator internals,
so the calculator can be refactored without rewriting the behavioral tests.
"""

import unittest

import ut_water_apportionment.models as models
from ut_water_apportionment.loss_models import LossDefinition, LossCurvePoint
from ut_water_apportionment import (
    solve,
    AccountingGraph,
    FlowComponentsTypes,
    FlowMeasurement,
    InterzoneFlow,
    SolverInput,
    Trxn,
    TrxnPathItem,
    Zone,
    ZoneTypes,
    MeasurementSeries,
    MeasurementCollection
)


def _mode(name: str):
    """Resolve a natural-flow mode while retaining collection-time diagnostics."""
    mode_type = getattr(models, "NaturalFlowMode", None)
    if mode_type is None:
        return name
    return mode_type[name]


def _configure_natural_flow(
    flow: InterzoneFlow,
    mode: str,
    *,
    measurements: list[FlowMeasurement] | None = None,
) -> InterzoneFlow:
    """Attach natural-flow mode and optional NF measurement sources."""
    flow.natural_flow_mode = _mode(mode) # type: ignore
    if measurements is not None:
        flow.nf_measurements = measurements
    return flow


def _residual_gain_loss(
    flow_id: str,
    system_zone: str,
    stream_zone: str,
) -> InterzoneFlow:
    return InterzoneFlow(
        id=flow_id,
        from_zone=system_zone,
        to_zone=stream_zone,
        flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
        bidirectional=True,
    )


def _natural_flow_value(results, flow_id: str, date: str = "2000-01-01") -> float:
    values = [
        result.value
        for result in results.get_result_value(date=date, flow_id=flow_id)
        if result.txn_id.endswith("_NF")
    ]
    if not values:
        raise AssertionError(
            f"No natural-flow output was produced for {flow_id} on {date}."
        )
    return sum(values)


class SpecifiedNaturalFlowContractTests(unittest.TestCase):

    def test_specified_import_routes_downstream(self):
        """A locally specified import is routed like ordinary natural flow."""
        import_flow = _configure_natural_flow(
            InterzoneFlow(
                id="IMPORT>B",
                from_zone="IMPORT",
                to_zone="B",
                flow_measurements=[FlowMeasurement("IMPORT>B")],
            ),
            "SPECIFIED",
            measurements=[FlowMeasurement("IMPORT-NF")],
        )

        solver_input = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone("IMPORT", ZoneTypes.IMPORT),
                    Zone("B", ZoneTypes.STREAM),
                    Zone("C", ZoneTypes.STREAM),
                    Zone("SYS-B", ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone("SYS-C", ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    import_flow,
                    InterzoneFlow(
                        id="B>C",
                        from_zone="B",
                        to_zone="C",
                        flow_measurements=[FlowMeasurement("B>C")],
                    ),
                    _residual_gain_loss("SYS-B>B", "SYS-B", "B"),
                    _residual_gain_loss("SYS-C>C", "SYS-C", "C"),
                ],
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="IMPORT>B",  values=[5]),
                MeasurementSeries(id="IMPORT-NF", values=[5]),
                MeasurementSeries(id="B>C",       values=[5]),
            ]),
            txns=[],
        )

        results = solve(solver_input)

        self.assertAlmostEqual(
            _natural_flow_value(results, "B>C"),
            5.0,
            places=7,
        )

    def test_specified_import_is_available_in_natural_flow_phase(self):
        """Local specified natural flow is allocatable before spill reallocation."""
        import_flow = _configure_natural_flow(
            InterzoneFlow(
                id="IMPORT>B",
                from_zone="IMPORT",
                to_zone="B",
                flow_measurements=[FlowMeasurement("IMPORT>B")],
            ),
            "SPECIFIED",
            measurements=[FlowMeasurement("IMPORT-NF")],
        )

        solver_input = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone("IMPORT", ZoneTypes.IMPORT),
                    Zone("B", ZoneTypes.STREAM),
                    Zone("DIV", ZoneTypes.USE),
                    Zone("SYS-B", ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    import_flow,
                    InterzoneFlow(
                        id="B>DIV",
                        from_zone="B",
                        to_zone="DIV",
                        flow_measurements=[FlowMeasurement("B>DIV")],
                    ),
                    _residual_gain_loss("SYS-B>B", "SYS-B", "B"),
                ],
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="IMPORT>B",  values=[5]),
                MeasurementSeries(id="IMPORT-NF", values=[5]),
                MeasurementSeries(id="B>DIV",     values=[5]),
            ]),
            txns=[
                Trxn(
                    id="TRXN_DIV",
                    priority=1,
                    upper_limit=5.0,
                    path=[TrxnPathItem(flow_id="B>DIV")],
                )
            ],
        )

        results = solve(solver_input)
        transaction_steps = [
            step
            for audit_record in results.apportionments_audit
            for step in audit_record.steps
            if step.variable_name.startswith("TRXN_DIV___")
        ]

        self.assertTrue(
            transaction_steps,
            "No apportionment audit step was recorded for TRXN_DIV.",
        )
        self.assertAlmostEqual(
            max(step.value_after for step in transaction_steps),
            5.0,
            places=7,
        )

    def test_timeseries_specified_diversion_reduces_downstream_natural_flow(self):
        """Daily nf_measurements are resolved and deducted from stream balance."""
        diversion = _configure_natural_flow(
            InterzoneFlow(
                id="A>DIV",
                from_zone="A",
                to_zone="DIV",
                flow_measurements=[FlowMeasurement("DIV")],
            ),
            "SPECIFIED",
            measurements=[
                FlowMeasurement("DIV-NF-A"),
                FlowMeasurement("DIV-NF-B", adjustment_factor=0.5),
            ],
        )

        solver_input = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-02",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone("SYS-A", ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone("A", ZoneTypes.STREAM),
                    Zone("B", ZoneTypes.STREAM),
                    Zone("DIV", ZoneTypes.USE),
                    Zone("SYS-B", ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(
                        id="SYS-A>A",
                        from_zone="SYS-A",
                        to_zone="A",
                        flow_measurements=[FlowMeasurement("GAIN")],
                    ),
                    diversion,
                    InterzoneFlow(
                        id="A>B",
                        from_zone="A",
                        to_zone="B",
                        flow_measurements=[FlowMeasurement("A>B")],
                    ),
                    _residual_gain_loss("SYS-B>B", "SYS-B", "B"),
                ],
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                MeasurementSeries(id="GAIN",    values=[10.0, 10.0]),
                MeasurementSeries(id="DIV",     values=[2.0, 4.0]),
                MeasurementSeries(id="DIV-NF-A",values=[1.0, 2.0]),
                MeasurementSeries(id="DIV-NF-B",values=[2.0, 4.0]),
                MeasurementSeries(id="A>B",     values=[8.0, 6.0]),
            ]),
            txns=[],
        )

        results = solve(solver_input)

        self.assertAlmostEqual(
            _natural_flow_value(results, "A>B", "2000-01-01"),
            8.0,
            places=7,
        )
        self.assertAlmostEqual(
            _natural_flow_value(results, "A>B", "2000-01-02"),
            6.0,
            places=7,
        )

    def test_explicit_zero_overrides_stream_to_stream_default(self):
        """ZERO can suppress the normal calculated stream-to-stream component."""
        routed_flow = _configure_natural_flow(
            InterzoneFlow(
                id="A>B",
                from_zone="A",
                to_zone="B",
                flow_measurements=[FlowMeasurement("A>B")],
            ),
            "ZERO",
        )

        solver_input = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone("SYS-A", ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone("A", ZoneTypes.STREAM),
                    Zone("B", ZoneTypes.STREAM),
                    Zone("SYS-B", ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(
                        id="SYS-A>A",
                        from_zone="SYS-A",
                        to_zone="A",
                        flow_measurements=[FlowMeasurement("GAIN")],
                    ),
                    routed_flow,
                    _residual_gain_loss("SYS-B>B", "SYS-B", "B"),
                ],
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="GAIN",    values=[10.0]),
                MeasurementSeries(id="A>B",     values=[10]),
            ]),
            txns=[],
        )

        results = solve(solver_input)

        self.assertAlmostEqual(
            _natural_flow_value(results, "A>B"),
            0.0,
            places=7,
        )

    def test_negative_specified_value_is_rejected_when_not_bidirectional(self):
        """Bad signed input is rejected rather than silently clamped."""
        diversion = _configure_natural_flow(
            InterzoneFlow(
                id="A>DIV",
                from_zone="A",
                to_zone="DIV",
                flow_measurements=[FlowMeasurement("A>DIV")],
                bidirectional=False,
            ),
            "SPECIFIED",
            measurements=[FlowMeasurement("A>DIV-NF")],
        )

        solver_input = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone("A", ZoneTypes.STREAM),
                    Zone("DIV", ZoneTypes.USE),
                    Zone("SYS-A", ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    diversion,
                    _residual_gain_loss("SYS-A>A", "SYS-A", "A"),
                ],
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="A>DIV",    values=[0.0]),
                MeasurementSeries(id="A>DIV-NF", values=[-1]),
            ]),
            txns=[],
        )

        with self.assertRaisesRegex(
            ValueError,
            r"(?i)(specified natural flow.*A>DIV.*negative|"
            r"A>DIV.*not bidirectional)",
        ):
            solve(solver_input)

    def test_negative_bidirectional_value_routes_in_reverse(self):
        """A negative specified value enters the declared source zone."""
        reverse_flow = _configure_natural_flow(
            InterzoneFlow(
                id="A>B",
                from_zone="A",
                to_zone="B",
                bidirectional=True,
                flow_measurements=[FlowMeasurement("A>B")],
            ),
            "SPECIFIED",
            measurements=[FlowMeasurement("A>B-NF")],
        )

        solver_input = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone("A", ZoneTypes.STREAM),
                    Zone("B", ZoneTypes.STREAM),
                    Zone("C", ZoneTypes.STREAM),
                    Zone("SYS-A", ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone("SYS-B", ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone("SYS-C", ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    reverse_flow,
                    InterzoneFlow(
                        id="A>C",
                        from_zone="A",
                        to_zone="C",
                        flow_measurements=[FlowMeasurement("A>C")],
                    ),
                    _residual_gain_loss("SYS-A>A", "SYS-A", "A"),
                    _residual_gain_loss("SYS-B>B", "SYS-B", "B"),
                    _residual_gain_loss("SYS-C>C", "SYS-C", "C"),
                ],
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="A>B",    values=[0.0]),
                MeasurementSeries(id="A>B-NF", values=[-4]),
                MeasurementSeries(id="A>C",    values=[0.0]),
            ]),
            txns=[],
        )

        results = solve(solver_input)

        self.assertAlmostEqual(
            _natural_flow_value(results, "A>C"),
            4.0,
            places=7,
        )

    def test_specified_natural_flow_may_exceed_measured_flow(self):
        """Natural/reference flow is not capped by the developed measurement."""
        import_flow = _configure_natural_flow(
            InterzoneFlow(
                id="IMPORT>B",
                from_zone="IMPORT",
                to_zone="B",
                flow_measurements=[FlowMeasurement("IMPORT>B")],
            ),
            "SPECIFIED",
            measurements=[FlowMeasurement("IMPORT-NF")],
        )

        solver_input = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone("IMPORT", ZoneTypes.IMPORT),
                    Zone("B", ZoneTypes.STREAM),
                    Zone("C", ZoneTypes.STREAM),
                    Zone("SYS-B", ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone("SYS-C", ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    import_flow,
                    InterzoneFlow(
                        id="B>C",
                        from_zone="B",
                        to_zone="C",
                        flow_measurements=[FlowMeasurement("B>C")],
                    ),
                    _residual_gain_loss("SYS-B>B", "SYS-B", "B"),
                    _residual_gain_loss("SYS-C>C", "SYS-C", "C"),
                ],
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="IMPORT>B",    values=[5.0]),
                MeasurementSeries(id="IMPORT-NF", values=[10]),
                MeasurementSeries(id="B>C",    values=[5.0]),
            ]),
            txns=[],
        )

        results = solve(solver_input)

        self.assertAlmostEqual(
            _natural_flow_value(results, "B>C"),
            10.0,
            places=7,
        )


class SpecifiedNaturalFlowLossCompatibilityTests(unittest.TestCase):
    def _solve_import_with_loss(self, loss_definition):
        import_flow = _configure_natural_flow(
            InterzoneFlow(
                id="IMPORT>B",
                from_zone="IMPORT",
                to_zone="B",
                flow_measurements=[FlowMeasurement("IMPORT>B")],
                loss_to_zone=loss_definition,
            ),
            "SPECIFIED",
            measurements=[FlowMeasurement("IMPORT-NF")],
        )

        solver_input = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone("IMPORT", ZoneTypes.IMPORT),
                    Zone("B", ZoneTypes.STREAM),
                    Zone("C", ZoneTypes.STREAM),
                    Zone("SYS-B", ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone("SYS-C", ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    import_flow,
                    InterzoneFlow(
                        id="B>C",
                        from_zone="B",
                        to_zone="C",
                        flow_measurements=[FlowMeasurement("B>C")],
                    ),
                    _residual_gain_loss("SYS-B>B", "SYS-B", "B"),
                    _residual_gain_loss("SYS-C>C", "SYS-C", "C"),
                ],
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="IMPORT>B",    values=[10.0]),
                MeasurementSeries(id="IMPORT-NF", values=[10]),
                MeasurementSeries(id="B>C",    values=[8.0]),
            ]),
            txns=[],
        )

        return solve(solver_input)

    def test_constant_endpoint_loss_applies_to_specified_natural_flow(self):
        """Specified natural flow uses the shared directional-loss path."""
        results = self._solve_import_with_loss(LossDefinition.linear(0.20))

        self.assertAlmostEqual(
            _natural_flow_value(results, "B>C"),
            8.0,
            places=7,
        )

    def test_piecewise_linear_loss_applies_to_specified_natural_flow(self):
        """Measurement-driven curves work without numeric-fraction arithmetic.

        At an inflow of 10, the curve loses 2, so 8 reaches stream zone B.
        This test does not require allocation-driven breakpoint transitions;
        it verifies the loss-compatible abstraction needed before those are
        added.
        """
        loss_curve = LossDefinition.piecewise_linear(
            [LossCurvePoint(inflow=10.0, loss=2.0)]
        )

        try:
            results = self._solve_import_with_loss(loss_curve)
        except TypeError as exc:
            self.fail(
                "The solver still treats LossDefinition as a numeric fraction: "
                f"{exc}"
            )

        self.assertAlmostEqual(
            _natural_flow_value(results, "B>C"),
            8.0,
            places=7,
        )


if __name__ == "__main__":
    unittest.main()