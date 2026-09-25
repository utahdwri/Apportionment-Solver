"""Shared endpoint transforms keep the per-endpoint numerical behavior."""
from unittest import TestCase

import numpy as np

from ut_water_apportionment.compile.compile import compile
from ut_water_apportionment.compile.kernel import BlockLPError
from tests.test_block_compile import problem, transaction


class SharedLossCodegenTests(TestCase):
    def setUp(self):
        self.plan = compile(problem([transaction('A')]))
        self.namespace = self.plan.executor.__globals__
        self.state = np.zeros(len(self.plan.state_layout.slots))
        layout = self.plan.state_layout
        self.endpoints = [
            (slot.index, f'{flow_id} {endpoint}')
            for endpoint, slots in (('from', layout.loss_from_delivery),
                                    ('to', layout.loss_to_delivery))
            for flow_id, slot in slots.items()
        ]

    def test_one_pair_of_helpers_reads_each_endpoints_current_factor(self):
        source = self.plan.code()
        self.assertEqual(source.count('def _deliver('), 1)
        self.assertEqual(source.count('def _required_inflow('), 1)
        self.assertNotIn('def _deliver_', source)
        self.assertNotIn('def _required_inflow_', source)
        deliver = self.namespace['_deliver']
        required = self.namespace['_required_inflow']
        for day in (1., 2.):
            for number, (index, label) in enumerate(self.endpoints, start=1):
                self.state[index] = 1. / (number + day)
            for number, (index, label) in enumerate(self.endpoints, start=1):
                with self.subTest(day=day, endpoint=label):
                    factor = 1. / (number + day)
                    for amount in (-8., 8.):
                        self.assertEqual(deliver(self.state, index, amount), amount * factor)
                        self.assertEqual(required(self.state, index, amount), amount / factor)

    def test_zero_delivery_and_tolerances_are_unchanged(self):
        deliver = self.namespace['_deliver']
        required = self.namespace['_required_inflow']
        tol, nf_tol = self.namespace['TOL'], self.namespace['NF_TOL']
        index, label = self.endpoints[0]
        for factor in (-tol, 0., tol, 2 * tol, 0.5, 1.):
            with self.subTest(factor=factor):
                self.state[index] = factor
                for amount in (-nf_tol, 0., nf_tol):
                    self.assertEqual(deliver(self.state, index, amount), 0.)
                    self.assertEqual(required(self.state, index, amount), 0.)
                amount = 2 * nf_tol
                self.assertEqual(deliver(self.state, index, amount), amount * max(0., factor))
                if factor <= tol:
                    with self.assertRaises(RuntimeError) as error:
                        required(self.state, index, amount)
                    self.assertEqual(str(error.exception), f'Cannot invert zero-delivery loss for slot {index}')
                else:
                    self.assertEqual(required(self.state, index, amount), amount / factor)

    def test_invalid_factors_keep_endpoint_diagnostics_even_for_zero_amount(self):
        for index, label in self.endpoints:
            for factor in (np.nan, np.inf, -np.inf, -2 * self.namespace['TOL']):
                self.state[index] = factor
                for function in ('_deliver', '_required_inflow'):
                    with self.subTest(endpoint=label, factor=factor, function=function):
                        for amount in (0., 10.):
                            with self.assertRaises(RuntimeError) as error:
                                self.namespace[function](self.state, index, amount)
                            self.assertEqual(str(error.exception), f'Invalid delivery factor for slot {index}')
