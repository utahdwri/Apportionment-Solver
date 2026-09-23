"""Contracts for compile-time symbolic scalar projection."""
from unittest import TestCase

import numpy as np

from ut_water_apportionment.compile.compile import (
    try_compile_parent_group_formula, try_compile_scalar_formula,
)
from ut_water_apportionment.compile.kernel import ScalarFormulaKernel, compile_lp_kernel
from ut_water_apportionment.compile.lp import (
    BlockLP, Constraint, Maximize, Proportional, Slot, Variable,
)


class SymbolicScalarFormulaTests(TestCase):
    def test_counterflow_witness_is_projected_before_runtime(self):
        limit_a = Slot(0, 'limit_a')
        limit_w = Slot(1, 'limit_w')
        capacity = Slot(2, 'capacity')
        coefficient_w = Slot(3, 'coefficient_w', -1)
        allocated = Slot(4, 'allocated')

        model = BlockLP(
            variables={'A': Variable(upper=limit_a), 'W': Variable(upper=limit_w)},
            constraints=[Constraint(
                'net', {'A': 1.0, 'W': coefficient_w}, upper=capacity,
            )],
            rule=Maximize({'A': 1.0}),
            updates={'A': {limit_a: -1.0, allocated: 1.0}},
        )
        formula = try_compile_scalar_formula(model)
        self.assertIsInstance(formula, ScalarFormulaKernel)
        self.assertIn('def maximum(state, factors):', formula.formula_source)
        self.assertFalse(hasattr(formula, '_formula_cache'))

        # Same precompiled Python formula handles changing coefficient values.
        for coefficient in (-1.0, -0.75, -2.0, 0.0):
            formula_state = np.array([10.0, 4.0, 3.0, coefficient, 0.0])
            lp_state = formula_state.copy()
            self.assertEqual(formula.execute(formula_state), 0)
            compile_lp_kernel(model).execute(lp_state)
            np.testing.assert_allclose(formula_state, lp_state, atol=1e-8)

    def test_reservation_equality_matches_lp(self):
        limit_a = Slot(0, 'limit_a')
        limit_b = Slot(1, 'limit_b')
        limit_w = Slot(2, 'limit_w')
        reserved = Slot(3, 'reserved')
        shared = Slot(4, 'shared')
        alloc_a = Slot(5, 'alloc_a')
        alloc_b = Slot(6, 'alloc_b')

        model = BlockLP(
            variables={
                'A': Variable(upper=limit_a),
                'B': Variable(upper=limit_b),
                'W': Variable(upper=limit_w),
            },
            constraints=[
                Constraint(
                    'reservation', {'A': 1.0, 'B': 1.0, 'W': -1.0},
                    lower=reserved, upper=reserved,
                ),
                Constraint('shared', {'A': 1.0, 'B': 1.0}, upper=shared),
            ],
            rule=Proportional({'A': 3.0, 'B': 1.0}),
            updates={
                'A': {limit_a: -1.0, shared: -1.0, alloc_a: 1.0},
                'B': {limit_b: -1.0, shared: -1.0, alloc_b: 1.0},
            },
        )
        formula = try_compile_scalar_formula(model)
        self.assertIsInstance(formula, ScalarFormulaKernel)
        formula_state = np.array([8.0, 8.0, 6.0, 2.0, 8.0, 0.0, 0.0])
        lp_state = formula_state.copy()
        self.assertEqual(formula.execute(formula_state), 0)
        compile_lp_kernel(model).execute(lp_state)
        np.testing.assert_allclose(formula_state, lp_state, atol=1e-8)

    def test_runtime_sign_violation_uses_lp_fallback(self):
        limit_a = Slot(0, 'limit_a')
        limit_w = Slot(1, 'limit_w')
        capacity = Slot(2, 'capacity')
        coefficient_w = Slot(3, 'coefficient_w', -1)
        allocated = Slot(4, 'allocated')
        model = BlockLP(
            variables={'A': Variable(upper=limit_a), 'W': Variable(upper=limit_w)},
            constraints=[Constraint('net', {'A': 1.0, 'W': coefficient_w}, upper=capacity)],
            rule=Maximize({'A': 1.0}),
            updates={'A': {allocated: 1.0}},
        )
        formula = try_compile_scalar_formula(model)
        state = np.array([10.0, 4.0, 3.0, 0.5, 0.0])
        lp_state = state.copy()
        self.assertEqual(formula.execute(state), 1)
        compile_lp_kernel(model).execute(lp_state)
        np.testing.assert_allclose(state, lp_state, atol=1e-8)

    def test_unknown_dynamic_coefficient_sign_is_not_compiled(self):
        coefficient = Slot(0, 'unknown_coefficient')
        allocated = Slot(1, 'allocated')
        model = BlockLP(
            variables={'A': Variable(upper=10.0), 'W': Variable(upper=10.0)},
            constraints=[Constraint('r', {'A': 1.0, 'W': coefficient}, upper=4.0)],
            rule=Maximize({'A': 1.0}),
            updates={'A': {allocated: 1.0}},
        )
        self.assertIsNone(try_compile_scalar_formula(model))


    def test_equivalent_target_columns_are_aggregated_before_variable_budget(self):
        limit_a = Slot(0, 'limit_a')
        limit_b = Slot(1, 'limit_b')
        allocated_a = Slot(2, 'allocated_a')
        allocated_b = Slot(3, 'allocated_b')
        model = BlockLP(
            variables={'A': Variable(upper=limit_a), 'B': Variable(upper=limit_b)},
            constraints=[
                # This redundant lower row deliberately prevents the ordinary
                # easy-floor shortcut so equivalent-column aggregation is the
                # simplification that makes the formula fit the variable budget.
                Constraint('nonnegative_sum', {'A': 1.0, 'B': 1.0}, lower=0.0),
            ],
            rule=Proportional({'A': 1.0, 'B': 1.0}),
            updates={
                'A': {limit_a: -1.0, allocated_a: 1.0},
                'B': {limit_b: -1.0, allocated_b: 1.0},
            },
        )
        formula = try_compile_scalar_formula(model, max_variables=1)
        self.assertIsInstance(formula, ScalarFormulaKernel)

        formula_state = np.array([10.0, 10.0, 0.0, 0.0])
        lp_state = formula_state.copy()
        self.assertEqual(formula.execute(formula_state), 0)
        compile_lp_kernel(model).execute(lp_state)
        np.testing.assert_allclose(formula_state, lp_state, atol=1e-8)

    def test_two_route_parent_feasibility_compiles_to_formula(self):
        remaining_1 = Slot(0, 'remaining_group_1')
        remaining_2 = Slot(1, 'remaining_group_2')
        capacity_a = Slot(2, 'capacity_a')
        capacity_b = Slot(3, 'capacity_b')
        limit_1 = Slot(4, 'limit_1')
        limit_2 = Slot(5, 'limit_2')
        allocated_1 = Slot(6, 'allocated_1')
        allocated_2 = Slot(7, 'allocated_2')

        model = BlockLP(
            variables={
                'P1': Variable(upper=limit_1),
                'P2': Variable(upper=limit_2),
                'A1': Variable(upper=4.0),
                'B1': Variable(upper=6.0),
                'A2': Variable(upper=6.0),
                'B2': Variable(upper=4.0),
            },
            constraints=[
                Constraint(
                    "parent_feasibility['P1']",
                    {'A1': 1.0, 'B1': 1.0, 'P1': -1.0},
                    lower=remaining_1, upper=remaining_1,
                ),
                Constraint(
                    "parent_feasibility['P2']",
                    {'A2': 1.0, 'B2': 1.0, 'P2': -1.0},
                    lower=remaining_2, upper=remaining_2,
                ),
                Constraint('route_a', {'A1': 1.0, 'A2': 1.0}, upper=capacity_a),
                Constraint('route_b', {'B1': 1.0, 'B2': 1.0}, upper=capacity_b),
            ],
            rule=Proportional({'P1': 1.0, 'P2': 1.0}),
            updates={
                'P1': {limit_1: -1.0, remaining_1: 1.0, allocated_1: 1.0},
                'P2': {limit_2: -1.0, remaining_2: 1.0, allocated_2: 1.0},
            },
        )

        formula = try_compile_parent_group_formula(model)
        self.assertIsInstance(formula, ScalarFormulaKernel)
        formula_state = np.array([0.0, 0.0, 6.0, 4.0, 10.0, 10.0, 0.0, 0.0])
        lp_state = formula_state.copy()
        self.assertEqual(formula.execute(formula_state), 0)
        compile_lp_kernel(model).execute(lp_state)
        np.testing.assert_allclose(formula_state, lp_state, atol=1e-8)
        np.testing.assert_allclose(formula_state[6:], [5.0, 5.0], atol=1e-8)

    def test_generated_plan_source_contains_formula_not_projection_cache(self):
        # This is primarily a source-level contract: emitted plans reconstruct
        # the already-generated formula source, not a lazy projector/cache.
        limit_a = Slot(0, 'limit_a')
        limit_w = Slot(1, 'limit_w')
        capacity = Slot(2, 'capacity')
        coefficient_w = Slot(3, 'coefficient_w', -1)
        allocated = Slot(4, 'allocated')
        model = BlockLP(
            variables={'A': Variable(upper=limit_a), 'W': Variable(upper=limit_w)},
            constraints=[Constraint('net', {'A': 1.0, 'W': coefficient_w}, upper=capacity)],
            rule=Maximize({'A': 1.0}),
            updates={'A': {allocated: 1.0}},
        )
        kernel = try_compile_scalar_formula(model)
        self.assertIn('_s3 = float(state[3])', kernel.formula_source)
        self.assertNotIn('cache', kernel.formula_source.lower())
