"""Focused contracts for analytical one-variable block execution."""
from unittest import TestCase

import numpy as np

from ut_water_apportionment.compile.compile import try_compile_direct_calculation
from ut_water_apportionment.compile.kernel import (
    DirectCalculationKernel, compile_lp_kernel,
)
from ut_water_apportionment.compile.lp import (
    BlockLP, Constraint, Maximize, Proportional, Slot, Variable,
)


class DirectCalculationTests(TestCase):
    def test_dynamic_scalar_block_matches_lp_kernel(self):
        limit = Slot(0, 'limit')
        coefficient = Slot(1, 'coefficient')
        capacity = Slot(2, 'capacity')
        allocated = Slot(3, 'allocated')
        remaining = Slot(4, 'remaining')
        model = BlockLP(
            variables={'A': Variable(lower=0.0, upper=limit)},
            constraints=[Constraint(
                'measurement', {'A': coefficient}, upper=capacity,
            )],
            rule=Maximize({'A': 1.0}),
            updates={'A': {allocated: 1.0, remaining: -1.0}},
        )
        direct = try_compile_direct_calculation(model)
        self.assertIsInstance(direct, DirectCalculationKernel)

        direct_state = np.array([10.0, 2.0, 12.0, 0.0, 10.0])
        lp_state = direct_state.copy()
        self.assertEqual(direct.execute(direct_state), 0)
        self.assertEqual(compile_lp_kernel(model).execute(lp_state), 1)
        np.testing.assert_allclose(direct_state, lp_state)
        self.assertEqual(direct_state[allocated.index], 6.0)
        self.assertEqual(direct_state[remaining.index], 4.0)

    def test_negative_coefficient_lower_side_becomes_upper_bound(self):
        allocated = Slot(0, 'allocated')
        model = BlockLP(
            variables={'A': Variable(lower=0.0, upper=10.0)},
            constraints=[Constraint(
                'reverse', {'A': -2.0}, lower=-8.0,
            )],
            rule=Maximize({'A': 1.0}),
            updates={'A': {allocated: 1.0}},
        )
        direct_state = np.zeros(1)
        lp_state = direct_state.copy()
        direct = try_compile_direct_calculation(model)
        self.assertIsInstance(direct, DirectCalculationKernel)
        direct.execute(direct_state)
        compile_lp_kernel(model).execute(lp_state)
        np.testing.assert_allclose(direct_state, lp_state)
        self.assertEqual(direct_state[allocated.index], 4.0)

    def test_multi_variable_and_proportional_blocks_fall_through(self):
        coupled = BlockLP(
            variables={'A': Variable(), 'B': Variable()},
            constraints=[Constraint('shared', {'A': 1.0, 'B': 1.0}, upper=10.0)],
            rule=Maximize({'A': 1.0}),
        )
        proportional = BlockLP(
            variables={'A': Variable(upper=10.0)},
            constraints=[],
            rule=Proportional({'A': 10.0}),
        )
        self.assertIsNone(try_compile_direct_calculation(coupled))
        self.assertIsNone(try_compile_direct_calculation(proportional))
