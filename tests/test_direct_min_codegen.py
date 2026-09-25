"""The direct MIN path relies on validated inputs and preserved capacities."""
import ast
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase

import numpy as np

from ut_water_apportionment.compile.codegen import generate_plan_source
from ut_water_apportionment.compile.compile import (
    CompiledOperation, CounterflowCompletion, compile,
)
from ut_water_apportionment.compile.kernel import (
    BlockLPError, DirectCalculationKernel, compile_lp_kernel,
)
from ut_water_apportionment.compile.lp import BlockLP, Constraint, Maximize, Slot, Variable
from tests.test_block_compile import problem, transaction


def generated(operations, slots, capacities, counterflow=()):
    layout = SimpleNamespace(
        slots={slot.name: slot for slot in slots},
        natural_flow={slot.name: slot for slot in capacities},
        spill_credits=list(counterflow),
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


def dynamic_model():
    coefficient = Slot(0, 'coefficient', sign=1)
    negative = Slot(1, 'negative coefficient', sign=-1, source_index=0, source_factor=-1.0)
    capacity, limit, allocated = [Slot(i, name) for i, name in enumerate(
        ('capacity', 'limit', 'allocated'), start=2)]
    one = Slot(5, 'one', sign=1, constant_value=1.0)
    slots = [coefficient, negative, capacity, limit, allocated, one]
    model = BlockLP({'A': Variable(upper=limit)}, [
        Constraint('shared capacity', {'A': coefficient}, upper=capacity),
    ], Maximize({'A': one}), {'A': {allocated: one, limit: -1.0, capacity: negative}})
    return model, slots, [capacity, limit]


class DirectMinCodegenTests(TestCase):
    def test_real_direct_blocks_are_min_then_checked_inline_updates(self):
        plan = compile(problem([transaction('A', 1, 3), transaction('B', 2, 9)], (10, 5, 0)))
        blocks = [node for node in ast.parse(plan.code()).body
                  if isinstance(node, ast.FunctionDef) and node.name.endswith('_direct') and node.name.startswith('_block_')]
        self.assertEqual(len(blocks), 2)
        for block in blocks:
            self.assertIsInstance(block.body[0], ast.Assign)
            self.assertEqual(block.body[0].value.func.id, 'min')
            self.assertEqual(block.body[1].value.func.id, 'checked_nonnegative_increment')
            self.assertTrue(all(isinstance(node, ast.AugAssign) for node in block.body[2:-1]))
            self.assertIsInstance(block.body[-1], ast.Return)
        self.assertNotIn('_lower', plan.code())
        self.assertEqual([row.value for row in plan.solve().apportionments
                          if row.txn_id in ('A', 'B')], [3., 7., 3., 2., 0., 0.])

    def test_dynamic_zero_and_positive_coefficients_preserve_capacities(self):
        model, slots, capacities = dynamic_model()
        kernel = DirectCalculationKernel(model)
        source, ns = generated([kernel, kernel], slots, capacities)
        self.assertNotIn('General scalar interval', source)
        self.assertIn('state[S_CAPACITY] -= (state[S_COEFFICIENT]) * amount', source)
        self.assertIn('state[S_ALLOCATED] += amount', source)
        rng = np.random.default_rng(731)
        for coefficient in (0.0, 0.001, 0.5, 1.0, 2.0, 100.0):
            for capacity, limit in rng.uniform(0, 100, size=(10, 2)):
                actual = np.array([coefficient, -coefficient, capacity, limit, 0., 1.])
                expected = actual.copy()
                ns['execute'](actual)
                kernel.execute(expected)
                kernel.execute(expected)
                np.testing.assert_allclose(actual, expected, atol=1e-10)
                self.assertTrue(all(actual[slot.index] >= -1e-10 for slot in capacities))

    def test_tiny_positive_coefficients_are_not_treated_as_zero(self):
        for dynamic in (False, True):
            with self.subTest(dynamic=dynamic):
                model, slots, capacities = dynamic_model()
                if not dynamic:
                    model.constraints = [Constraint('tiny', {'A': 1e-20}, upper=slots[2])]
                    model.updates['A'][slots[2]] = -1e-20
                _, ns = generated([DirectCalculationKernel(model)], slots, capacities)
                state = np.array([1e-20, -1e-20, 1., 1e25, 0., 1.])
                ns['execute'](state)
                self.assertAlmostEqual(state[4] / 1e20, 1.)
                self.assertAlmostEqual(state[2], 0.)

    def test_invalid_daily_values_fail_before_any_commit(self):
        model, slots, capacities = dynamic_model()
        _, ns = generated([DirectCalculationKernel(model)], slots, capacities)
        for index, value in ((0, -1e-12), (0, np.nan), (0, np.inf),
                             (2, -1.), (2, np.nan), (2, np.inf), (2, -np.inf),
                             (3, -1.), (3, np.nan)):
            with self.subTest(index=index, value=value):
                state = np.array([2., -2., 8., 10., 0., 1.])
                state[index] = value
                original = state.copy()
                with self.assertRaises((ns['SolverError'], BlockLPError)):
                    ns['execute'](state)
                np.testing.assert_array_equal(state, original)
        state = np.array([2., -2., 8., np.inf, 0., 1.])
        ns['execute'](state)
        self.assertEqual(state[4], 4.)

    def test_later_block_input_is_validated_before_first_allocation(self):
        first, second, allocated = slots = [Slot(i, name) for i, name in enumerate(
            ('first capacity', 'second capacity', 'allocated'))]
        operations = [DirectCalculationKernel(BlockLP(
            {'A': Variable(upper=capacity)}, [], Maximize({'A': 1.}),
            {'A': {allocated: 1., capacity: -1.}},
        )) for capacity in (first, second)]
        _, ns = generated(operations, slots, [first, second])
        state = np.array([5., np.nan, 0.])
        with self.assertRaises((ns['SolverError'], BlockLPError)):
            ns['execute'](state)
        self.assertEqual(state[0], 5.)
        self.assertEqual(state[2], 0.)

    def test_counterflow_completion_is_part_of_the_same_block(self):
        model, slots, capacities = dynamic_model()
        kernel = DirectCalculationKernel(model)
        source, _ = generated([kernel], slots, capacities, counterflow=[kernel])
        self.assertIn('def _block_0(state):', source)
        self.assertIn('def _block_0_direct(state):', source)
        self.assertIn('def _block_0_counterflow(state):', source)
        self.assertNotIn('def _replay_block_', source)
        self.assertNotIn('REPLAY block', source)


    def test_signed_residual_can_become_feasible_after_earlier_blocks(self):
        residual, allocated = slots = [Slot(0, 'signed residual'), Slot(1, 'allocated')]
        credit = DirectCalculationKernel(BlockLP(
            {'A': Variable(upper=7.)}, [], Maximize({'A': 1.}), {'A': {residual: 1.}}))
        debit = DirectCalculationKernel(BlockLP({'B': Variable(upper=10.)}, [
            Constraint('net flow', {'B': 1.}, upper=residual),
        ], Maximize({'B': 1.}), {'B': {residual: -1., allocated: 1.}}))
        source, ns = generated([credit, debit], slots, [])
        self.assertNotIn('General scalar interval', source)
        state = np.array([-5., 0.])
        ns['execute'](state)
        np.testing.assert_array_equal(state, [0., 2.])
        # A preceding credit that does not restore feasibility still fails at
        # the debit, before it commits any invalid allocation.
        state = np.array([-8., 0.])
        with self.assertRaises((ns['SolverError'], BlockLPError)):
            ns['execute'](state)
        self.assertEqual(state[1], 0.)

    def test_omitting_zero_row_requires_a_joint_capacity_bound(self):
        capacity, allocated = slots = [Slot(0, 'capacity'), Slot(1, 'allocated')]
        # Each individual bound holds, but committing both consumes twice the
        # available capacity. The later zero row must retain its feasibility check.
        writer = BlockLP({'A': Variable(upper=capacity), 'B': Variable(upper=capacity)},
                        [], Maximize({'A': 1., 'B': 1.}),
                        {'A': {capacity: -1.}, 'B': {capacity: -1.}})
        reader = DirectCalculationKernel(BlockLP({'C': Variable(upper=1.)}, [
            Constraint('zero row', {'C': 0.}, upper=capacity),
        ], Maximize({'C': 1.}), {'C': {allocated: 1.}}))
        source, ns = generated([compile_lp_kernel(writer), reader], slots, [capacity])
        self.assertIn("General scalar interval for 'C'", source)
        state = np.array([1., 0.])
        with self.assertRaises(BlockLPError):
            ns['execute'](state)
        self.assertEqual(state[1], 0.)

    def test_negative_witness_and_replay_writer_prevent_zero_row_elision(self):
        capacity, allocated = slots = [Slot(0, 'capacity'), Slot(1, 'allocated')]
        writer = BlockLP({'A': Variable(upper=3.), 'W': Variable(upper=3.)}, [
            Constraint('counterflow', {'A': 1., 'W': -1.}, upper=capacity),
        ], Maximize({'A': 1.}), {'A': {capacity: -1.}})
        reader = DirectCalculationKernel(BlockLP({'C': Variable(upper=1.)}, [
            Constraint('zero row', {}, upper=capacity),
        ], Maximize({'C': 1.}), {'C': {allocated: 1.}}))
        source, _ = generated([reader], slots, [capacity], counterflow=[compile_lp_kernel(writer)])
        self.assertIn("General scalar interval for 'C'", source)
        _, ns = generated([compile_lp_kernel(writer), reader], slots, [capacity])
        with self.assertRaises(BlockLPError):
            ns['execute'](np.array([1., 0.]))

    def test_nonfinite_constant_rows_use_checked_general_interval(self):
        allocated = Slot(0, 'allocated')
        for bound in (np.nan, np.inf, -np.inf):
            with self.subTest(bound=bound):
                model = BlockLP({'A': Variable(upper=1.)}, [
                    Constraint('invalid', {'A': 1.}, upper=bound),
                ], Maximize({'A': 1.}), {'A': {allocated: 1.}})
                source, ns = generated([DirectCalculationKernel(model)], [allocated], [])
                self.assertIn('General scalar interval', source)
                with self.assertRaises(BlockLPError):
                    ns['execute'](np.zeros(1))

    def test_normalized_negative_row_and_constant_zero_row(self):
        allocated = Slot(0, 'allocated')
        model = BlockLP({'A': Variable(upper=10.)}, [
            Constraint('normalized upper bound', {'A': -2.}, lower=-8.),
            Constraint('redundant zero row', {'A': 0.}, upper=0.),
        ], Maximize({'A': 1.}), {'A': {allocated: 1.}})
        source, ns = generated([DirectCalculationKernel(model)], [allocated], [])
        self.assertNotIn('General scalar interval', source)
        state = np.zeros(1)
        ns['execute'](state)
        self.assertEqual(state[0], 4.)

    def test_increment_check_clamps_only_roundoff_and_rejects_unbounded(self):
        allocated = Slot(0, 'allocated')
        model = BlockLP({'A': Variable()}, [], Maximize({'A': 1.}), {'A': {allocated: 1.}})
        _, ns = generated([DirectCalculationKernel(model)], [allocated], [])
        check = ns['checked_nonnegative_increment']
        self.assertEqual(check(-1e-10), 0.)
        for value in (-1e-3, np.nan, np.inf, -np.inf):
            with self.subTest(value=value), self.assertRaises(ns['SolverError']):
                check(value)
        with self.assertRaises(ns['SolverError']):
            ns['execute'](np.zeros(1))
