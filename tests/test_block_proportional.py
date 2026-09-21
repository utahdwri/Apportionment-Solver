"""Focused contracts for analytical equal-priority water filling."""
from unittest import TestCase

import numpy as np

from ut_water_apportionment.compile.compile import try_compile_proportional_calculation
from ut_water_apportionment.compile.kernel import (
    ProportionalCalculationKernel, compile_lp_kernel,
)
from ut_water_apportionment.compile.lp import (
    BlockLP, Constraint, Proportional, Slot, Variable,
)


class ProportionalCalculationTests(TestCase):
    def test_weighted_water_filling_matches_lp_kernel(self):
        limit_a = Slot(0, 'limit_a')
        limit_b = Slot(1, 'limit_b')
        shared = Slot(2, 'shared')
        allocated_a = Slot(3, 'allocated_a')
        allocated_b = Slot(4, 'allocated_b')
        reference_a = Slot(5, 'reference_a')
        reference_b = Slot(6, 'reference_b')

        model = BlockLP(
            variables={
                'A': Variable(upper=limit_a),
                'B': Variable(upper=limit_b),
            },
            constraints=[Constraint(
                'shared', {'A': 1.0, 'B': 1.0}, upper=shared,
            )],
            rule=Proportional({'A': reference_a, 'B': reference_b}),
            updates={
                'A': {limit_a: -1.0, shared: -1.0, allocated_a: 1.0},
                'B': {limit_b: -1.0, shared: -1.0, allocated_b: 1.0},
            },
        )

        analytical = try_compile_proportional_calculation(model)
        self.assertIsInstance(analytical, ProportionalCalculationKernel)

        # A:B starts at 3:1.  The first common increment gives A=6, B=2;
        # A then blocks at its own limit, so B receives the last 2 cfs.
        analytical_state = np.array([6.0, 6.0, 10.0, 0.0, 0.0, 3.0, 1.0])
        lp_state = analytical_state.copy()

        self.assertEqual(analytical.execute(analytical_state), 0)
        lp_calls = compile_lp_kernel(model).execute(lp_state)
        self.assertGreater(lp_calls, 0)
        np.testing.assert_allclose(analytical_state, lp_state, atol=1e-8)
        self.assertAlmostEqual(analytical_state[allocated_a.index], 6.0)
        self.assertAlmostEqual(analytical_state[allocated_b.index], 4.0)

    def test_unlimited_phase_then_finite_phase_matches_lp_kernel(self):
        limit_a = Slot(0, 'limit_a')
        limit_b = Slot(1, 'limit_b')
        limit_c = Slot(2, 'limit_c')
        allocated_a = Slot(3, 'allocated_a')
        allocated_b = Slot(4, 'allocated_b')
        allocated_c = Slot(5, 'allocated_c')
        reference_a = Slot(6, 'reference_a')
        reference_b = Slot(7, 'reference_b')
        reference_c = Slot(8, 'reference_c')

        model = BlockLP(
            variables={
                'A': Variable(upper=limit_a),
                'B': Variable(upper=limit_b),
                'C': Variable(upper=limit_c),
            },
            constraints=[],
            rule=Proportional({
                'A': reference_a,
                'B': reference_b,
                'C': reference_c,
            }),
            updates={
                'A': {limit_a: -1.0, allocated_a: 1.0},
                'B': {limit_b: -1.0, allocated_b: 1.0},
                'C': {limit_c: -1.0, allocated_c: 1.0},
            },
        )

        analytical_state = np.array([
            2.0, 4.0, 3.0,
            0.0, 0.0, 0.0,
            np.inf, np.inf, 3.0,
        ])
        lp_state = analytical_state.copy()
        analytical = try_compile_proportional_calculation(model)
        self.assertIsInstance(analytical, ProportionalCalculationKernel)
        self.assertEqual(analytical.execute(analytical_state), 0)
        compile_lp_kernel(model).execute(lp_state)
        np.testing.assert_allclose(analytical_state, lp_state, atol=1e-8)
        np.testing.assert_allclose(
            analytical_state[[allocated_a.index, allocated_b.index, allocated_c.index]],
            [2.0, 4.0, 3.0],
        )

    def test_tiny_reference_member_remains_in_proportional_cohort(self):
        limit_a = Slot(0, 'limit_a')
        limit_b = Slot(1, 'limit_b')
        allocated_a = Slot(2, 'allocated_a')
        allocated_b = Slot(3, 'allocated_b')
        reference_a = Slot(4, 'reference_a')
        reference_b = Slot(5, 'reference_b')

        model = BlockLP(
            variables={
                'A': Variable(upper=limit_a),
                'B': Variable(upper=limit_b),
            },
            constraints=[],
            rule=Proportional({'A': reference_a, 'B': reference_b}),
            updates={
                'A': {limit_a: -1.0, allocated_a: 1.0},
                'B': {limit_b: -1.0, allocated_b: 1.0},
            },
        )
        analytical_state = np.array([5.0, 5.0, 0.0, 0.0, 1e-10, 1.0])
        lp_state = analytical_state.copy()
        analytical = try_compile_proportional_calculation(model)
        self.assertIsInstance(analytical, ProportionalCalculationKernel)
        self.assertEqual(analytical.execute(analytical_state), 0)
        compile_lp_kernel(model).execute(lp_state)
        np.testing.assert_allclose(analytical_state, lp_state, atol=1e-8)

    def test_negative_runtime_coefficient_falls_back_to_lp(self):
        limit_a = Slot(0, 'limit_a')
        limit_b = Slot(1, 'limit_b')
        capacity = Slot(2, 'capacity')
        coefficient_b = Slot(3, 'coefficient_b')
        allocated_a = Slot(4, 'allocated_a')
        allocated_b = Slot(5, 'allocated_b')

        model = BlockLP(
            variables={
                'A': Variable(upper=limit_a),
                'B': Variable(upper=limit_b),
            },
            constraints=[Constraint(
                'signed', {'A': 1.0, 'B': coefficient_b}, upper=capacity,
            )],
            rule=Proportional({'A': 1.0, 'B': 1.0}),
            updates={
                'A': {limit_a: -1.0, capacity: -1.0, allocated_a: 1.0},
                'B': {limit_b: -1.0, capacity: 1.0, allocated_b: 1.0},
            },
        )
        analytical = try_compile_proportional_calculation(model)
        self.assertIsInstance(analytical, ProportionalCalculationKernel)

        analytical_state = np.array([2.0, 2.0, 5.0, -1.0, 0.0, 0.0])
        lp_state = analytical_state.copy()
        analytical_calls = analytical.execute(analytical_state)
        lp_calls = compile_lp_kernel(model).execute(lp_state)
        self.assertEqual(analytical_calls, lp_calls)
        self.assertGreater(analytical_calls, 0)
        np.testing.assert_allclose(analytical_state, lp_state, atol=1e-8)

    def test_witness_and_lower_bound_models_fall_through(self):
        witness = BlockLP(
            variables={'A': Variable(upper=10.0), 'W': Variable(upper=10.0)},
            constraints=[Constraint('row', {'A': 1.0, 'W': 1.0}, upper=10.0)],
            rule=Proportional({'A': 10.0}),
            updates={'A': {}},
        )
        lower_row = BlockLP(
            variables={'A': Variable(upper=10.0), 'B': Variable(upper=10.0)},
            constraints=[Constraint(
                'row', {'A': 1.0, 'B': 1.0}, lower=1.0, upper=10.0,
            )],
            rule=Proportional({'A': 10.0, 'B': 10.0}),
            updates={'A': {}, 'B': {}},
        )
        negative_constant = BlockLP(
            variables={'A': Variable(upper=10.0), 'B': Variable(upper=10.0)},
            constraints=[Constraint(
                'row', {'A': 1.0, 'B': -1.0}, upper=10.0,
            )],
            rule=Proportional({'A': 10.0, 'B': 10.0}),
            updates={'A': {}, 'B': {}},
        )
        self.assertIsNone(try_compile_proportional_calculation(witness))
        self.assertIsNone(try_compile_proportional_calculation(lower_row))
        self.assertIsNone(try_compile_proportional_calculation(negative_constant))
