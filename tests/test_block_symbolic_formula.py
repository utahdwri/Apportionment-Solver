"""Contracts for compile-time symbolic scalar projection."""
from unittest import TestCase

import numpy as np

from ut_water_apportionment.compile.compile import try_compile_scalar_formula
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
