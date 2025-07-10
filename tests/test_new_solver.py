import unittest
import time
from new_solver.main import (
    AccountingNetwork,
    AccountingNetworkZone,
    AccountingNetworkFlow,
    ZoneTypes
)


class TestNewSolver(unittest.TestCase):

    def test_natural_flow(self):
        system = AccountingNetwork()
        system.add_reach('REACH A')
        system.add_reach('REACH B')
        system.add_reach('REACH C')
        system.connect_zones('A>B', 'REACH A', 'REACH B')
        system.connect_zones('B>C', 'REACH B', 'REACH C')
        system.add_reach_diversion('A>1', 'REACH A', 'USER 1')
        system.add_reach_diversion('A>2', 'REACH A', 'USER 2')
        system.add_reach_diversion('B>3', 'REACH B', 'USER 3')

        system.solve_apportionments(
            flows={
                'A>B': 2,
                'B>C': 3,
                'A>1':10,
                'A>2':12,
                'B>3': 3
            }, 
            storage_changes={}
        )

        self.assertEqual(system.get_natural_flow('A>B'), 24)
        self.assertEqual(system.get_natural_flow('B>C'), 28)