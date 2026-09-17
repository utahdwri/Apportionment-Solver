import unittest
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
    V2CompilationOptions,
    compile_solver_input_v2,
)
from ut_water_apportionment.loss_models import LossDefinition, LossInterval
from ut_water_apportionment.compiled_test.model import (
    CompilerModel,
    ParamExpr,
    ParametricConstraint,
    ParametricVariable,
    SymbolicExpr,
)
from ut_water_apportionment.compiled_test.transforms import (
    remove_redundant_constraint_sides,
)
from ut_water_apportionment.compiled_test.runtime import (
    V2CompilationSession,
    V2LPSolver,
)
from ut_water_apportionment.compiled_test.execution_ir import (
    ConstraintResidual,
    ResidualState,
)
from ut_water_apportionment.compiled_test.program import (
    DirectScalarProgram,
    EqualPriorityProgram,
    ReducedLPProgram,
)


def _simple_input() -> SolverInput:
    return SolverInput(
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
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
            PathTrxn(
                id="TRXN_2",
                priority=2,
                upper_limit=6,
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
            PathTrxn(
                id="TRXN_3",
                priority=3,
                upper_limit=12,
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
            PathTrxn(
                id="TRXN_4",
                priority=4,
                upper_limit=4,
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
        ],
    )



def _equal_priority_branch_input(*, tiny_first: bool = False) -> SolverInput:
    """Two equal-priority transactions with different physical bottlenecks."""

    return SolverInput(
        beg_date="2000-01-01",
        end_date="2000-01-01",
        accounting_graph=AccountingGraph(
            zones=[
                Zone(id="R", type=ZoneTypes.STREAM),
                Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                Zone(id="U1", type=ZoneTypes.USE),
                Zone(id="U2", type=ZoneTypes.USE),
            ],
            interzone_flows=[
                InterzoneFlow(
                    id="R>U1",
                    from_zone="R",
                    to_zone="U1",
                    flow_measurements=[FlowMeasurement(measurement_id="M1")],
                ),
                InterzoneFlow(
                    id="R>U2",
                    from_zone="R",
                    to_zone="U2",
                    flow_measurements=[FlowMeasurement(measurement_id="M2")],
                ),
                InterzoneFlow(
                    id="SYS>R",
                    from_zone="SYS",
                    to_zone="R",
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                    bidirectional=True,
                ),
            ],
        ),
        measurements=MeasurementCollection(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            series=[
                MeasurementSeries(id="M1", values=[1 if tiny_first else 2]),
                MeasurementSeries(id="M2", values=[8]),
            ],
        ),
        txns=[
            PathTrxn(
                id="A",
                priority=1,
                upper_limit=1e-9 if tiny_first else 10,
                path=[TrxnPathItem(flow_id="R>U1")],
            ),
            PathTrxn(
                id="B",
                priority=1,
                upper_limit=10,
                path=[TrxnPathItem(flow_id="R>U2")],
            ),
        ],
    )

def _reservoir_input() -> SolverInput:
    return SolverInput(
        beg_date="2000-01-01",
        end_date="2000-01-03",
        accounting_graph=AccountingGraph(
            zones=[
                Zone(id="RIVER", type=ZoneTypes.STREAM),
                Zone(id="STO", type=ZoneTypes.STORAGE, storage_meas_ids=["STO"]),
                Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                Zone(id="USER", type=ZoneTypes.USE),
            ],
            interzone_flows=[
                InterzoneFlow(
                    id="RIVER>STO",
                    from_zone="RIVER",
                    to_zone="STO",
                    bidirectional=True,
                    flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                ),
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
            beg_date="1999-12-31",
            end_date="2000-01-03",
            series=[
                MeasurementSeries(id="1", values=[12, 12, 3, 10]),
                MeasurementSeries(id="STO", values=[1200, 1200, 1200, 1200]),
            ],
        ),
        txns=[
            PathTrxn(
                id="TRXN_1",
                priority=1,
                upper_limit=3,
                path=[TrxnPathItem(flow_id="RIVER>USER")],
            ),
            PathTrxn(
                id="TRXN_2",
                priority=2,
                upper_limit=6,
                path=[
                    TrxnPathItem(flow_id="RIVER>STO", factor=-1),
                    TrxnPathItem(flow_id="RIVER>USER"),
                ],
            ),
        ],
    )


def _result_map(result):
    return {
        (row.date, row.txn_id, row.interzone_flow_id): row.value
        for row in result.apportionments
    }


class CompiledV2Tests(unittest.TestCase):
    def test_runtime_guards_render_actual_algebra_and_have_frozen_alternates(self):
        session = V2CompilationSession()
        engine = V2LPSolver(session=session)
        engine.add_variable("X", lb=0, ub=10)
        engine.add_constraint("C", lb=0, ub=None)
        engine.set_coefficient("C", "X", 1)
        engine._v2_dynamic_bound_sides.add(("constraint", "C", "lower"))

        session.begin_preparation()
        session.prepare_objective(
            engine, variable_names=["X"], maximization=True
        )
        session.finish_preparation()

        text = session.formulas()
        report = session.report()
        self.assertIn(
            "REQUIRE 0 >= constraint[C].remaining_lower",
            text,
        )
        self.assertNotIn("side remains redundant", text)
        self.assertGreater(report["guarded_programs"], 0)
        self.assertEqual(
            report["guarded_programs"],
            report["guarded_programs_with_fallback"],
        )
        self.assertEqual(
            report["guarded_programs"],
            report["prepared_unguarded_variants"],
        )

    def test_runtime_guard_selects_frozen_alternate_when_predicate_fails(self):
        session = V2CompilationSession()
        engine = V2LPSolver(session=session)
        engine.add_variable("X", lb=0, ub=10)
        engine.add_constraint("C", lb=0, ub=None)
        engine.set_coefficient("C", "X", 1)
        engine._v2_dynamic_bound_sides.add(("constraint", "C", "lower"))

        session.begin_preparation()
        session.prepare_objective(
            engine, variable_names=["X"], maximization=True
        )
        session.finish_preparation()

        normal = session.resolve(
            engine,
            variable_names=["X"],
            maximization=True,
            weights=None,
        )
        self.assertTrue(normal.model.guards)

        # Make the guarded redundancy false while keeping the conservative
        # problem feasible.  The already-frozen alternate must be selected.
        engine.update_constraint_lb("C", lb=5)
        session.reset_execution_stats()
        alternate = session.resolve(
            engine,
            variable_names=["X"],
            maximization=True,
            weights=None,
        )
        self.assertFalse(alternate.model.guards)
        self.assertAlmostEqual(alternate.execute(engine).requested_values["X"], 10)
        report = session.report()
        self.assertEqual(report["execution_guarded_variant_hits"], 0)
        self.assertGreater(report["execution_guard_fallback_hits"], 0)
        self.assertEqual(report["execution_cache_misses"], 0)

    def test_redundant_side_can_be_structurally_proven_without_runtime_guard(self):
        parameter = "constraint[C].remaining_lower"
        model = CompilerModel(
            variables={
                "X": ParametricVariable(
                    "X",
                    ParamExpr.constant_value(0.0),
                    ParamExpr.constant_value(1.0),
                )
            },
            constraints={
                "C": ParametricConstraint(
                    "C",
                    lower=ParamExpr.slot(parameter),
                    upper=None,
                    coefficients={"X": ParamExpr.constant_value(1.0)},
                )
            },
            reconstruction={"X": SymbolicExpr.variable("X")},
            source_variable_count=1,
            parameter_defaults={parameter: -1.0},
            parameter_domains={parameter: (-10.0, -0.5)},
        )

        removed = remove_redundant_constraint_sides(model, protected=set())

        self.assertEqual(removed, 1)
        self.assertIsNone(model.constraints["C"].lower)
        self.assertEqual(model.guards, [])
        self.assertEqual(len(model.structural_proofs), 1)
        proof = model.structural_proofs[0]
        self.assertEqual(proof.text(), f"0 >= {parameter}")
        self.assertTrue(proof.structurally_proven(model.parameter_domains))

    def test_parameter_expression_supports_products_quotients_and_sign_domains(self):
        first = ParamExpr.slot("loss_remaining_1")
        second = ParamExpr.slot("loss_remaining_2")
        expression = first.multiplied(second).scaled(0.95)
        ratio = expression.divided(first)

        parameters = {
            "loss_remaining_1": 0.9,
            "loss_remaining_2": 0.8,
        }
        self.assertAlmostEqual(expression.evaluate(parameters), 0.684)
        self.assertAlmostEqual(ratio.evaluate(parameters), 0.76)
        self.assertEqual(
            expression.sign(
                {
                    "loss_remaining_1": (0.0, 1.0),
                    "loss_remaining_2": (0.0, 1.0),
                },
                parameters,
            ),
            1,
        )
        self.assertIn("loss_remaining_1", expression.text())
        self.assertIn("loss_remaining_2", expression.text())

    def test_simple_v2_solves_without_whole_day_fallback(self):
        plan = compile_solver_input_v2(_simple_input())
        actual = plan.solve()
        values = _result_map(actual)

        self.assertAlmostEqual(values[("2000-01-01", "TRXN_1", "RIVER>USER")], 3.0)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_2", "RIVER>USER")], 6.0)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_3", "RIVER>USER")], 3.0)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_4", "RIVER>USER")], 0.0)
        report = plan.report()
        self.assertEqual(report["whole_day_lp_fallbacks"], 0)
        self.assertGreater(report["program_count"], 0)


    def test_reporting_slacks_are_derived_from_leftover_measurement(self):
        from ut_water_apportionment.apportioner import Apportioner

        plan = compile_solver_input_v2(_simple_input())
        measurements = MeasurementCollection(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            series=[MeasurementSeries(id="1", values=[30])],
        )

        # These legacy methods used LP objectives to obtain spill/slack/path
        # values.  V2 must derive reporting slacks and final path values without
        # either routine.
        with patch.object(
            Apportioner,
            "solve_for_nonpath_vars",
            side_effect=AssertionError("legacy final slack solve was called"),
        ), patch.object(
            Apportioner,
            "calculate_spills",
            side_effect=AssertionError("legacy spill solve was called"),
        ):
            result = plan.solve(measurements=measurements)

        values = _result_map(result)
        allocated = sum(
            values[("2000-01-01", f"TRXN_{index}", "RIVER>USER")]
            for index in range(1, 5)
        )
        leftover = 30.0 - allocated
        self.assertAlmostEqual(leftover, 5.0)
        self.assertAlmostEqual(
            values[(
                "2000-01-01",
                "SLACK_RIVER_TO_USER_RIVER>USER",
                "RIVER>USER",
            )],
            leftover,
        )
        text = plan.formulas()
        self.assertIn(
            "SLACK_RIVER_TO_USER_RIVER>USER = "
            "MAX(constraint[MEAS_RIVER>USER].remaining, 0)",
            text,
        )
        self.assertIn(
            "residual[SYS>RIVER] = constraint[MEAS_SYS>RIVER].remaining",
            text,
        )

    def test_v2_day_routine_uses_explicit_execution_ir_and_residual_updates(self):
        from ut_water_apportionment.apportioner import Apportioner

        plan = compile_solver_input_v2(_simple_input())
        text = plan.formulas()

        self.assertIn("EXECUTABLE DAY ROUTINE", text)
        self.assertIn("PASS 1 — PRIORITY ALLOCATION", text)
        assignment = "TRXN_1 = variable[TRXN_1].current + MIN("
        self.assertIn(assignment, text)
        update = "constraint[MEAS_RIVER>USER].remaining -= TRXN_1.increment"
        self.assertIn(update, text)
        self.assertLess(text.index(assignment), text.index(update))
        self.assertLess(
            text.index(update),
            text.index("TRXN_2 = variable[TRXN_2].current + MIN("),
        )
        self.assertIn("PASS 2 — RERUN PRIORITY ALLOCATION", text)

        # V2 owns the day/priority progression now; the legacy Apportioner
        # schedule walker must not be needed by the compiled execution path.
        with patch.object(
            Apportioner,
            "calculate_apportionments",
            side_effect=AssertionError("legacy schedule walker was called"),
        ):
            result = plan.solve()

        values = _result_map(result)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_1", "RIVER>USER")], 3.0)
        report = plan.report()
        self.assertTrue(report["explicit_execution_ir"])
        self.assertGreater(report["execution_residual_commits"], 0)
        self.assertGreater(report["execution_residual_row_updates"], 0)


    def test_equal_priority_uses_frozen_logical_transaction_kernel(self):
        plan = compile_solver_input_v2(_equal_priority_branch_input())
        programs = [
            program
            for program in plan._session.programs
            if isinstance(program, EqualPriorityProgram)
        ]
        self.assertTrue(programs)
        program = next(program for program in programs if not program.model.guards)

        self.assertEqual(set(program.member_ids), {"A", "B"})
        self.assertIn("A", program.model.variables)
        self.assertIn("B", program.model.variables)
        self.assertFalse(any("___" in name for name in program.model.variables))
        self.assertTrue(program.model.uses_residual_state)

        # A's own measured flow blocks at 2 cfs. B must then continue alone to
        # 8 cfs. Both the common-increment solve and the blocked-member
        # classification should use the frozen logical cohort model, not the
        # production path-leg auxiliary LP.
        with patch.object(
            V2LPSolver,
            "solve_auxiliary_objective",
            side_effect=AssertionError("production auxiliary LP was used"),
        ):
            result = plan.solve()

        values = _result_map(result)
        self.assertAlmostEqual(values[("2000-01-01", "A", "R>U1")], 2.0)
        self.assertAlmostEqual(values[("2000-01-01", "B", "R>U2")], 8.0)

        report = plan.report()
        self.assertGreater(report["equal_priority_compiled_kernels"], 0)
        self.assertGreater(
            report["execution_equal_priority_common_increment_solves"], 1
        )
        self.assertGreater(
            report["equal_priority_direct_common_increment_formulas"], 0
        )
        self.assertGreater(
            report["execution_equal_priority_direct_common_increment_formulas"], 0
        )
        self.assertGreater(
            report["execution_equal_priority_classification_solves"], 0
        )
        self.assertEqual(report["execution_equal_priority_cache_misses"], 0)
        self.assertEqual(report["auxiliary_kernel_solves"], 0)

        text = plan.formulas()
        self.assertIn("FROZEN LOGICAL EQUAL-PRIORITY KERNEL", text)
        self.assertIn("dTRXN_i >= proportion_i * g", text)
        self.assertNotIn("combined_A", text)
        self.assertNotIn("combined_B", text)

    def test_equal_priority_runtime_uses_direct_frozen_regime_index(self):
        plan = compile_solver_input_v2(_equal_priority_branch_input())

        # Runtime already knows the structural regime selected by the day IR.
        # It must not fingerprint the full production LP again for each cohort
        # operation merely to rediscover the same frozen program.
        with patch.object(
            V2CompilationSession,
            "_equal_priority_structure_signature",
            side_effect=AssertionError("runtime production-LP fingerprint was used"),
        ):
            result = plan.solve()

        values = _result_map(result)
        self.assertAlmostEqual(values[("2000-01-01", "A", "R>U1")], 2.0)
        self.assertAlmostEqual(values[("2000-01-01", "B", "R>U2")], 8.0)

    def test_final_path_outputs_use_compile_time_reconstruction(self):
        plan = compile_solver_input_v2(_equal_priority_branch_input())

        # Path collapse/reconstruction is a compiler transform. Once the plan
        # is frozen, daily execution must not rebuild a CompilerModel from the
        # production LP just to report non-anchor path values.
        with patch.object(
            CompilerModel,
            "from_engine",
            side_effect=AssertionError("daily path reconstruction rebuilt compiler IR"),
        ):
            result = plan.solve()

        values = _result_map(result)
        self.assertAlmostEqual(values[("2000-01-01", "A", "R>U1")], 2.0)
        self.assertAlmostEqual(values[("2000-01-01", "B", "R>U2")], 8.0)

    def test_tiny_equal_priority_member_stays_on_logical_cohort_ir(self):
        plan = compile_solver_input_v2(
            _equal_priority_branch_input(tiny_first=True)
        )

        with patch.object(
            V2LPSolver,
            "solve_auxiliary_objective",
            side_effect=AssertionError("production auxiliary LP was used"),
        ):
            result = plan.solve()

        values = _result_map(result)
        self.assertAlmostEqual(values[("2000-01-01", "A", "R>U1")], 1e-9)
        self.assertAlmostEqual(values[("2000-01-01", "B", "R>U2")], 8.0)
        report = plan.report()
        self.assertGreater(report["execution_equal_priority_scalar_solves"], 0)
        self.assertEqual(report["equal_priority_scalar_auxiliary"], 0)
        self.assertEqual(report["auxiliary_kernel_solves"], 0)

    def test_simple_v2_exposes_spreadsheet_style_remaining_constraints(self):
        problem = _simple_input()
        plan = compile_solver_input_v2(problem)
        plan.solve()
        text = plan.formulas()

        self.assertIn("V2P1: DIRECT MAXIMIZE TRXN_1", text)
        self.assertIn("constraint[MEAS_RIVER>USER].remaining", text)
        self.assertIn("constraint[NF_ZONE_RIVER].remaining_upper", text)
        self.assertIn("variable[TRXN_1].remaining_upper", text)
        self.assertNotIn("V2P1: DIRECT MAXIMIZE TRXN_1___", text)
        self.assertIn(
            "compiled directly from its logical residual column",
            text,
        )
        self.assertIn(
            "residual lower side stays redundant",
            text,
        )
        self.assertNotIn(
            "constraint[MEAS_RIVER>USER].remaining - variable[TRXN_2].remaining_lower",
            text,
        )

    def test_unique_priorities_compile_directly_from_sparse_residual_columns(self):
        plan = compile_solver_input_v2(_simple_input())
        report = plan.report()

        self.assertEqual(report["program_count"], 4)
        self.assertEqual(report["direct_programs"], 4)
        self.assertEqual(report["reduced_lp_kernels"], 0)
        self.assertEqual(report["early_direct_sequential_programs"], 4)
        self.assertEqual(report["sequential_direct_contexts"], 1)
        self.assertEqual(report["guarded_programs"], 0)
        self.assertEqual(report["prepared_unguarded_variants"], 0)

        for program in plan._session.programs:
            self.assertEqual(program.active_variable_count, 1)
            self.assertEqual(len(program.model.variables), 1)
            self.assertFalse(program.model.guards)

        text = plan.formulas()
        self.assertIn("variable[TRXN_1].remaining_upper", text)
        self.assertIn("constraint[MEAS_RIVER>USER].remaining", text)
        self.assertIn("constraint[NF_ZONE_RIVER].remaining_upper", text)
        self.assertNotIn("REDUCED_LP_KERNEL", text)

    def test_frozen_direct_runtime_uses_indexed_parameter_and_residual_layouts(self):
        plan = compile_solver_input_v2(_simple_input())
        direct = [
            program
            for program in plan._session.programs
            if isinstance(program, DirectScalarProgram)
        ]
        self.assertEqual(len(direct), 4)

        # Named ParamExpr/slot dictionaries are compiler/debug IR only. The
        # daily direct path must stay on the lowered integer-indexed arrays.
        for program in direct:
            self.assertTrue(program.parameter_slot_names)
            self.assertEqual(
                set(program.parameter_slot_names),
                set(program.model._all_slots()),
            )
            indexed_text = " ".join(
                expression.python_text()
                for _name, lower, upper, coefficient in program._indexed_constraints
                for expression in (lower, upper, coefficient)
                if expression is not None
            )
            indexed_text += " " + (
                "" if program._indexed_upper is None
                else program._indexed_upper.python_text()
            )
            self.assertIn("p[", indexed_text)

        effects = next(iter(plan._execution_program.effects_by_regime.values()))
        self.assertTrue(
            all(
                effect.constraint_index is not None
                for transaction_effects in effects.values()
                for effect in transaction_effects
            )
        )

        with patch.object(
            CompilerModel,
            "runtime_parameters",
            side_effect=AssertionError(
                "daily direct execution fell back to named parameter dictionaries"
            ),
        ):
            result = plan.solve()
        self.assertTrue(result.apportionments)

        report = plan.report()
        self.assertEqual(report["indexed_direct_programs"], 4)
        self.assertEqual(report["indexed_parameter_layouts"], 4)
        self.assertEqual(report["indexed_residual_layouts"], 1)
        self.assertGreater(report["indexed_residual_slots_total"], 0)

    def test_plan_code_is_executable_python_over_indexed_residual_ir(self):
        plan = compile_solver_input_v2(_simple_input())
        code = plan.code()

        self.assertIn("def execute_regime_0(apportioner, schedule, runtime):", code)
        self.assertIn("upper_0 = min(", code)
        self.assertIn("r_upper[", code)
        self.assertIn("engine.update_variable_bounds", code)
        self.assertNotIn("COMPILED_MAX", code)

        # The simple four-priority system is entirely generated arithmetic; it
        # must not invoke the interpreted scalar backend at runtime.
        with patch.object(
            V2LPSolver,
            "maximize_and_update_variable",
            side_effect=AssertionError("generated Python fell back to scalar interpreter"),
        ):
            result = plan.solve()
        self.assertTrue(result.apportionments)
        report = plan.report()
        self.assertTrue(report["generated_python"])
        self.assertEqual(report["generated_python_direct_assignments"], 4)
        self.assertEqual(report["execution_generated_direct_assignments"], 4)
        self.assertEqual(report["execution_generated_python_days"], 1)

    def test_generated_python_matches_execution_ir_interpreter(self):
        problem = _simple_input()
        generated = compile_solver_input_v2(problem)
        interpreted = compile_solver_input_v2(
            problem,
            options=V2CompilationOptions(enable_generated_python=False),
        )

        generated_values = _result_map(generated.solve())
        interpreted_values = _result_map(interpreted.solve())
        self.assertEqual(set(generated_values), set(interpreted_values))
        for key, expected in interpreted_values.items():
            self.assertAlmostEqual(generated_values[key], expected, places=10)
        self.assertTrue(generated.report()["generated_python"])
        self.assertFalse(interpreted.report()["generated_python"])

    def test_generated_python_calls_prebuilt_kernel_for_coupled_case(self):
        problem = _reservoir_input()
        generated = compile_solver_input_v2(problem)
        interpreted = compile_solver_input_v2(
            problem,
            options=V2CompilationOptions(enable_generated_python=False),
        )
        self.assertIn("prebuilt frozen kernel", generated.code())
        self.assertIn("runtime.execute_assignment", generated.code())

        generated_values = _result_map(generated.solve())
        interpreted_values = _result_map(interpreted.solve())
        self.assertEqual(set(generated_values), set(interpreted_values))
        for key, expected in interpreted_values.items():
            self.assertAlmostEqual(generated_values[key], expected, places=8)
        self.assertGreater(generated.report()["generated_python_kernel_calls"], 0)

    def test_reduced_kernel_runtime_also_uses_indexed_parameters(self):
        plan = compile_solver_input_v2(_reservoir_input())
        self.assertTrue(
            any(isinstance(program, ReducedLPProgram) for program in plan._session.programs)
        )
        with patch.object(
            CompilerModel,
            "runtime_parameters",
            side_effect=AssertionError(
                "reduced frozen execution fell back to named parameter dictionaries"
            ),
        ):
            result = plan.solve()
        self.assertTrue(result.apportionments)
        self.assertGreater(plan.report()["indexed_reduced_lp_programs"], 0)

    def test_reduced_kernels_drop_committed_seniors_from_final_residual_ir(self):
        # Keep explicit coverage of the generic reduced-LP fallback path even
        # though ordinary sequential systems now compile earlier to direct MIN
        # programs.
        plan = compile_solver_input_v2(
            _simple_input(),
            options=V2CompilationOptions(enable_early_direct_sequential=False),
        )

        kernels = {
            program.requested[0]: program
            for program in plan._session.programs
            if isinstance(program, ReducedLPProgram) and not program.model.guards
        }

        first = kernels["TRXN_1___RIVER>USER"]
        second = kernels["TRXN_2___RIVER>USER"]
        third = kernels["TRXN_3___RIVER>USER"]

        self.assertEqual(first.active_variable_count, 4)
        self.assertEqual(second.active_variable_count, 3)
        self.assertEqual(third.active_variable_count, 2)
        self.assertNotIn("TRXN_1", second.model.variables)
        self.assertNotIn("TRXN_1", third.model.variables)
        self.assertNotIn("TRXN_2", third.model.variables)
        self.assertTrue(first.model.uses_residual_state)
        self.assertTrue(second.model.uses_residual_state)
        self.assertTrue(third.model.uses_residual_state)

    def test_sequential_conservative_kernels_use_compiled_scalar_projection(self):
        # This is the exact generic fallback retained for structures that fail
        # the early direct-sequential proof.
        plan = compile_solver_input_v2(
            _simple_input(),
            options=V2CompilationOptions(enable_early_direct_sequential=False),
        )

        conservative = [
            program
            for program in plan._session.programs
            if isinstance(program, ReducedLPProgram) and not program.model.guards
        ]
        self.assertTrue(conservative)
        self.assertTrue(all(program._projected_scalar is not None for program in conservative))

        result = plan.solve()
        values = _result_map(result)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_1", "RIVER>USER")], 3.0)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_2", "RIVER>USER")], 6.0)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_3", "RIVER>USER")], 3.0)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_4", "RIVER>USER")], 0.0)

        # Conservative kernels retain the exact reduced LP as a fallback, but
        # their normal scalar path is now precompiled to one variable instead of
        # rescanning all junior transaction columns at runtime.
        self.assertTrue(
            all(
                set(program._projected_scalar.model.variables) == {program._quick_scalar_name}
                for program in conservative
            )
        )

    def test_reduced_kernel_reads_rhs_from_explicit_residual_state(self):
        session = V2CompilationSession()
        engine = V2LPSolver(session=session)
        engine.add_variable("X", lb=0, ub=10)
        engine.add_variable("Y", lb=0, ub=10)
        engine.add_constraint("C", lb=10, ub=10)
        engine.set_coefficient("C", "X", 1)
        engine.set_coefficient("C", "Y", 1)

        session.begin_preparation()
        session.prepare_objective(
            engine,
            variable_names=["X"],
            maximization=True,
            residual_transaction_names={"X", "Y"},
        )
        session.finish_preparation()

        program = session.resolve(
            engine,
            variable_names=["X"],
            maximization=True,
            weights=None,
        )
        self.assertIsInstance(program, ReducedLPProgram)

        # The mutable production LP still says X + Y == 10.  The frozen
        # kernel must instead solve the final transformed IR against the
        # execution residual, which says only 4 remains.
        engine.v2_residual_state = ResidualState(
            constraints={"C": ConstraintResidual(lower=4, upper=4, equality=True)}
        )

        # The reduced kernel may still read dynamic variable bounds and matrix
        # coefficient parameters from the engine, but it must not go back to
        # the production constraint row for its RHS.
        engine.get_constraint_bounds = lambda name: self.fail(
            f"reduced residual kernel read production row {name}"
        )
        result = program.execute(engine)
        self.assertAlmostEqual(result.requested_values["X"], 4.0)

    def test_directional_ambiguity_keeps_only_needed_senior_recourse(self):
        plan = compile_solver_input_v2(_reservoir_input())
        kernels = [
            program
            for program in plan._session.programs
            if isinstance(program, ReducedLPProgram)
            and program.requested[0] == "TRXN_2___RIVER>STO"
        ]

        guarded = next(program for program in kernels if program.model.guards)
        conservative = next(
            program for program in kernels if not program.model.guards
        )

        # TRXN_2 participates in the storage/counterflow tie-break.  The
        # conservative final IR must retain senior TRXN_1 as a zero-based
        # recourse increment because the tie-break can change the feasible
        # region.  Guarded presolve is still free to prove it unnecessary.
        self.assertIn("TRXN_1", conservative.model.variables)
        self.assertNotIn("TRXN_1", guarded.model.variables)
        self.assertTrue(conservative.model.uses_residual_state)
        self.assertTrue(guarded.model.uses_residual_state)

    def test_v2_collapses_path_continuity_before_residual_kernel(self):
        problem = _reservoir_input()
        plan = compile_solver_input_v2(problem)
        plan.solve()
        text = plan.formulas()

        self.assertIn("MAXIMIZE TRXN_2", text)
        self.assertIn(
            "TRXN_2 collapsed 2 LP path-leg variables into one logical transaction variable",
            text,
        )
        self.assertNotIn("zero-RHS equality CONT_TRXN_2_0", text)


    def test_v2_logical_transaction_preserves_nonunit_continuity_scale(self):
        problem = _reservoir_input()
        problem.txns[1].path[0].loss_after = 0.10

        plan = compile_solver_input_v2(problem)
        plan.solve()
        text = plan.formulas()

        self.assertIn(
            "TRXN_2___RIVER>STO = 1*TRXN_2; "
            "TRXN_2___RIVER>USER = 0.9*TRXN_2",
            text,
        )
        self.assertNotIn("MAXIMIZE TRXN_2___", text)

    def test_reservoir_v2_uses_compiler_transformations(self):
        plan = compile_solver_input_v2(_reservoir_input())
        plan.solve()
        self.assertEqual(plan.report()["whole_day_lp_fallbacks"], 0)
        self.assertGreater(plan.report()["logical_transactions_collapsed"], 0)
        self.assertGreater(plan.report()["continuity_rows_removed"], 0)

    def test_v2_is_prepared_and_frozen_before_public_solve(self):
        plan = compile_solver_input_v2(_simple_input())
        report = plan.report()

        self.assertTrue(report["frozen"])
        self.assertFalse(report["runtime_compilation"])
        self.assertGreater(report["program_count"], 0)
        self.assertIn("STRUCTURAL FROZEN IR", plan.formulas())

        program_count = report["program_count"]
        plan.solve()
        report = plan.report()
        self.assertEqual(report["program_count"], program_count)
        self.assertGreater(report["execution_cache_hits"], 0)
        self.assertEqual(report["execution_cache_misses"], 0)

    def test_frozen_v2_reuses_parameterized_bounds_for_new_measurements(self):
        problem = _simple_input()
        plan = compile_solver_input_v2(problem)
        program_count = plan.report()["program_count"]

        for flow in (8, 12, 20):
            with self.subTest(flow=flow):
                measurements = MeasurementCollection(
                    beg_date="2000-01-01",
                    end_date="2000-01-01",
                    series=[MeasurementSeries(id="1", values=[flow])],
                )
                actual = plan.solve(measurements=measurements)
                values = _result_map(actual)
                expected = {
                    8: (3.0, 5.0, 0.0, 0.0),
                    12: (3.0, 6.0, 3.0, 0.0),
                    20: (3.0, 6.0, 11.0, 0.0),
                }[flow]
                for index, expected_value in enumerate(expected, start=1):
                    self.assertAlmostEqual(
                        values[("2000-01-01", f"TRXN_{index}", "RIVER>USER")],
                        expected_value,
                    )
                self.assertEqual(plan.report()["program_count"], program_count)
                self.assertEqual(plan.report()["execution_cache_misses"], 0)

    def test_reservoir_days_reuse_frozen_parameterized_programs(self):
        plan = compile_solver_input_v2(_reservoir_input())
        program_count = plan.report()["program_count"]
        self.assertLess(program_count, 20)

        plan.solve()
        report = plan.report()
        self.assertEqual(report["program_count"], program_count)
        self.assertEqual(report["execution_cache_misses"], 0)
        self.assertGreater(report["execution_cache_hits"], 0)

    def test_structural_compilation_is_independent_of_measurement_values(self):
        first = _simple_input()
        second = _simple_input()
        second.measurements = MeasurementCollection(
            beg_date="2000-01-01",
            end_date="2000-01-01",
            series=[MeasurementSeries(id="1", values=[987.654])],
        )
        second.__post_init__()

        first_plan = compile_solver_input_v2(first)
        second_plan = compile_solver_input_v2(second)

        self.assertEqual(first_plan.formulas(), second_plan.formulas())
        self.assertEqual(
            first_plan.report()["program_count"],
            second_plan.report()["program_count"],
        )
        self.assertTrue(first_plan.report()["structural_preparation"])
        self.assertFalse(first_plan.report()["date_traced_preparation"])

    def test_constant_coefficient_year_uses_one_structural_regime(self):
        problem = _simple_input()
        problem.end_date = "2000-12-31"
        problem.measurements = MeasurementCollection(
            beg_date="2000-01-01",
            end_date="2000-12-31",
            series=[MeasurementSeries(id="1", values=[12] * 366)],
        )
        problem.__post_init__()

        plan = compile_solver_input_v2(problem)
        report = plan.report()

        self.assertEqual(report["structural_regimes"], 1)
        self.assertEqual(report["structural_duplicate_regimes"], 0)

    def test_time_varying_fractional_loss_uses_one_parameterized_structure(self):
        timed_loss = LossDefinition.time_varying_piecewise_linear(
            intervals=[
                LossInterval(
                    beg_date="2000-01-03",
                    end_date="2000-01-03",
                    loss=LossDefinition.linear(0.10),
                )
            ],
            default=LossDefinition.linear(0.0),
        )
        problem = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-04",
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="A", type=ZoneTypes.STREAM),
                    Zone(id="B", type=ZoneTypes.STREAM),
                    Zone(id="DIV", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(
                        id="A>B",
                        from_zone="A",
                        to_zone="B",
                        flow_measurements=[FlowMeasurement(measurement_id="A>B")],
                        loss_to_zone=timed_loss,
                    ),
                    InterzoneFlow(
                        id="B>DIV",
                        from_zone="B",
                        to_zone="DIV",
                        flow_measurements=[FlowMeasurement(measurement_id="B>DIV")],
                    ),
                    InterzoneFlow(
                        id="SYS>A",
                        from_zone="SYS",
                        to_zone="A",
                        flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                        bidirectional=True,
                    ),
                    InterzoneFlow(
                        id="SYS>B",
                        from_zone="SYS",
                        to_zone="B",
                        flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
                        bidirectional=True,
                    ),
                ],
            ),
            measurements=MeasurementCollection(
                beg_date="2000-01-01",
                end_date="2000-01-04",
                series=[
                    MeasurementSeries(id="A>B", values=[10, 10, 10, 10]),
                    MeasurementSeries(id="B>DIV", values=[10, 10, 9, 10]),
                ],
            ),
            txns=[
                PathTrxn(
                    id="TRXN_1",
                    priority=1,
                    upper_limit=10,
                    path=[
                        TrxnPathItem(flow_id="A>B"),
                        TrxnPathItem(flow_id="B>DIV"),
                    ],
                )
            ],
        )

        plan = compile_solver_input_v2(problem)
        report = plan.report()

        self.assertEqual(report["structural_regimes"], 1)
        self.assertEqual(report["structural_duplicate_regimes"], 0)
        text = plan.formulas()
        self.assertIn("coefficient[CONT_TRXN_1_0,TRXN_1___A>B]", text)
        with patch.object(
            CompilerModel,
            "runtime_parameters",
            side_effect=AssertionError(
                "time-varying loss execution used named parameter dictionaries"
            ),
        ):
            result = plan.solve()
        final_report = plan.report()
        self.assertEqual(final_report["execution_cache_misses"], 0)
        self.assertGreater(final_report["indexed_residual_parameter_layouts"], 0)
        self.assertGreater(final_report["indexed_residual_parameter_slots_total"], 0)
        indexed_effects = next(iter(plan._execution_program.effects_by_regime.values()))
        self.assertTrue(
            any(
                effect.indexed_coefficient is not None
                for transaction_effects in indexed_effects.values()
                for effect in transaction_effects
                if effect.coefficient.slots()
            )
        )
        values = _result_map(result)
        self.assertAlmostEqual(values[("2000-01-01", "TRXN_1", "B>DIV")], 10.0)
        self.assertAlmostEqual(values[("2000-01-03", "TRXN_1", "B>DIV")], 9.0)
        self.assertAlmostEqual(values[("2000-01-04", "TRXN_1", "B>DIV")], 10.0)


if __name__ == "__main__":
    unittest.main()
