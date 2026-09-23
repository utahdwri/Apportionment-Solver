"""Behavioral checks for shared code in the emitted daily program."""
from types import SimpleNamespace
from unittest import TestCase

import numpy as np

from ut_water_apportionment.compile.codegen import generate_plan_source
from ut_water_apportionment.compile.compile import (
    CompiledOperation, CounterflowCompletion, compile, try_compile_scalar_formula,
)
from ut_water_apportionment.compile.formula import add_expr, scalar_expr, ZERO_EXPR
from ut_water_apportionment.compile.kernel import (
    DirectCalculationKernel, ProportionalCalculationKernel, compile_lp_kernel,
)
from ut_water_apportionment.compile.lp import (
    BlockLP, Constraint, Maximize, Proportional, Slot, Variable,
)
from tests.test_block_compile import problem, transaction


def generated(operations, slots, counterflow=()):
    layout = SimpleNamespace(
        slots={slot.name: slot for slot in slots},
        spill_credits=[],
        limits={},
        measurement_available={},
        measurement_forward_remaining={},
        measurement_reverse_remaining={},
    )
    secondary = list(counterflow) + [None] * (len(operations) - len(counterflow))
    compiled = [
        CompiledOperation(
            operation,
            None if secondary[index] is None else CounterflowCompletion(secondary[index]),
        )
        for index, operation in enumerate(operations)
    ]
    program = generate_plan_source(compiled, layout)
    namespace = dict(program.namespace)
    exec(program.source, namespace)
    return program.source, namespace


class CompactCodegenTests(TestCase):
    def test_direct_rows_keep_signed_zero_and_infeasible_cases(self):
        coefficient, capacity, allocated = [Slot(i, name) for i, name in enumerate(
            ('coefficient', 'capacity', 'allocated'))]
        for coefficient_value, lower, upper, objective, expected in (
            (2.0, None, capacity, 1.0, 4.0),
            (-2.0, capacity, None, 1.0, 4.0),
            (2.0, 4.0, capacity, -1.0, 2.0),
            (0.0, None, capacity, 1.0, 10.0),
        ):
            with self.subTest(coefficient=coefficient_value, objective=objective):
                model = BlockLP({'A': Variable(upper=10.0)}, [
                    Constraint('row', {'A': coefficient}, lower=lower, upper=upper),
                ], Maximize({'A': objective}), {'A': {allocated: 1.0}})
                _, ns = generated([DirectCalculationKernel(model)], [coefficient, capacity, allocated])
                state = np.array([coefficient_value, -8.0 if coefficient_value < 0 else 8.0, 0.0])
                ns['_block_0_direct'](state)
                self.assertEqual(state[2], expected)
                state[:] = [0.0, -8.0, 0.0]
                if upper is capacity:
                    with self.assertRaises(ns['SolverError']):
                        ns['_block_0_direct'](state)

    def test_commit_preserves_snapshot_when_a_coefficient_is_updated(self):
        coefficient, allocated = Slot(0, 'coefficient'), Slot(1, 'allocated')
        model = BlockLP({'A': Variable(upper=2.0)}, [], Maximize({'A': 1.0}),
                        {'A': {coefficient: 1.0, allocated: coefficient}})
        kernel = DirectCalculationKernel(model)
        _, ns = generated([kernel], [coefficient, allocated])
        actual = np.array([3.0, 0.0])
        expected = actual.copy()
        ns['_block_0_direct'](actual)
        kernel.execute(expected)
        np.testing.assert_array_equal(actual, expected)
        np.testing.assert_array_equal(actual, [5.0, 6.0])

    def test_shared_proportional_loop_matches_lp_in_all_phases(self):
        slots = [Slot(i, name) for i, name in enumerate(('a', 'b', 'shared', 'out_a', 'out_b'))]
        a, b, capacity, out_a, out_b = slots
        for references in ({'A': 3.0, 'B': 1.0}, {'A': np.inf, 'B': 1.0}, {'A': 1e-10, 'B': 1.0}):
            with self.subTest(references=references):
                model = BlockLP({'A': Variable(upper=a), 'B': Variable(upper=b)},
                    [Constraint('shared', {'A': 1.0, 'B': 1.0}, upper=capacity)],
                    Proportional(references), {
                        'A': {a: -1.0, capacity: -1.0, out_a: 1.0},
                        'B': {b: -1.0, capacity: -1.0, out_b: 1.0},
                    })
                kernel = ProportionalCalculationKernel(model)
                source, ns = generated([kernel, kernel], slots, counterflow=[kernel])
                self.assertEqual(source.count('def _allocate_proportionally('), 1)
                self.assertEqual(source.count('_commit(state, increments):'), 1)
                blocker_source = source.split('def _block_0_direct_blocked_members', 1)[1].split('def _block_0_direct_commit', 1)[0]
                self.assertNotIn('_common_increment(', blocker_source)
                self.assertIn('_coefficient', blocker_source)
                actual = np.array([6.0, 8.0, 10.0, 0.0, 0.0])
                expected = actual.copy()
                ns['_block_0_direct'](actual)
                compile_lp_kernel(model).execute(expected)
                np.testing.assert_allclose(actual, expected, atol=1e-8)

    def test_projected_lower_and_equality_rows_still_match_lp(self):
        a, b, w, reserved, capacity, out_a, out_b = slots = [
            Slot(i, name) for i, name in enumerate(('a', 'b', 'w', 'reserved', 'capacity', 'out_a', 'out_b'))]
        model = BlockLP(
            {'A': Variable(upper=a), 'B': Variable(upper=b), 'W': Variable(upper=w)},
            [Constraint('reservation', {'A': 1.0, 'B': 1.0, 'W': -1.0}, lower=reserved, upper=reserved),
             Constraint('minimum witness', {'W': 1.0}, lower=1.0),
             Constraint('shared', {'A': 1.0, 'B': 1.0}, upper=capacity)],
            Proportional({'A': 3.0, 'B': 1.0}), {
                'A': {a: -1.0, capacity: -1.0, out_a: 1.0},
                'B': {b: -1.0, capacity: -1.0, out_b: 1.0},
            })
        kernel = try_compile_scalar_formula(model)
        self.assertIsNotNone(kernel)
        self.assertNotIn('_lower', kernel.formula_source)
        _, ns = generated([kernel], slots)
        actual = np.array([8., 8., 6., 2., 8., 0., 0.])
        expected = actual.copy()
        ns['_block_0_direct'](actual)
        compile_lp_kernel(model).execute(expected)
        np.testing.assert_allclose(actual, expected, atol=1e-8)

    def test_alias_labels_do_not_prevent_symbolic_cancellation(self):
        positive = Slot(0, 'positive coefficient', sign=1)
        negative = Slot(1, 'negative coefficient', sign=-1, source_index=0, source_factor=-1.0)
        self.assertEqual(add_expr(scalar_expr(positive), scalar_expr(negative)), ZERO_EXPR)

    def test_generated_counterflow_formula_keeps_its_lp_guard_fallback(self):
        coefficient, allocated = Slot(0, 'counterflow', sign=-1), Slot(1, 'allocated')
        model = BlockLP(
            {'A': Variable(upper=10.0), 'W': Variable(upper=4.0)},
            [Constraint('net', {'A': 1.0, 'W': coefficient}, upper=3.0)],
            Maximize({'A': 1.0}), {'A': {allocated: 1.0}})
        kernel = try_compile_scalar_formula(model)
        _, ns = generated([kernel], [coefficient, allocated])
        for value in (-2.0, -1.0, 0.0, 0.5):
            with self.subTest(coefficient=value):
                actual = np.array([value, 0.0])
                expected = actual.copy()
                calls = ns['_block_0_direct'](actual)
                compile_lp_kernel(model).execute(expected)
                np.testing.assert_allclose(actual, expected, atol=1e-8)
                self.assertEqual(calls, 1 if value > 0 else 0)
