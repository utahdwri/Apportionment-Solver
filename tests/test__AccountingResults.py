import unittest
from solver.solver import *


class TestAccountingResults(unittest.TestCase):

    def setUp(self) -> None:
        from test_data import input as input
        self.results = solve(flowlines=input['flowlines'], 
                        nodes=input['nodes'], 
                        paths=input['paths'], 
                        measurements=input['measurements'],
                        zones=input['zones'],
                        day=input['day'],
                        account_starting_balances=input['account_starting_balances'],
                        write_output_files=False)
        
    def test(self):
        simplified_txns = [(i.from_account, i.to_account, i.value) for i in self.results.transactions]

        self.assertIn( ('Reach 2','Use 2-200',2.0), simplified_txns )

# Run the tests
if __name__ == '__main__':
    unittest.main()