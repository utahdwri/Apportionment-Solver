import unittest
from solver.solver import *


class Test(unittest.TestCase):

    def test_get_date_series(self):
        self.assertListEqual( get_date_series('2022-12-30', '2023-01-02'), ['2022-12-30', '2022-12-31', '2023-01-01', '2023-01-02'] )
        self.assertListEqual( get_date_series('2020-02-28', '2020-03-01'), ['2020-02-28', '2020-02-29', '2020-03-01'] )
        self.assertListEqual( get_date_series('2021-02-28', '2021-03-01'), ['2021-02-28', '2021-03-01'] )

class TestScheduleTools(unittest.TestCase):

    def test_get_next_iter(self):
        """Does the get_next_iter method work as expected? 
        (This is an internal function that is complex enough I added some tests)
        """

        solver = ApportionmentSolver(None, None)
        series = {
            "sequential_subseries": [
                {
                    "priority": 0,
                    "varId": "0",
                },
                {
                    "priority": 0,
                    "varId": None,
                    "proportional_subseries":[
                        {
                            "factor": 0.22,
                            "varId": "1"
                        },
                        {
                            "factor": 0.33,
                            "varId": "2", 
                            "sequential_subseries":[
                                {
                                    "priority": 1, 
                                    "varId": "2A"
                                },
                                {
                                    "priority": 2, 
                                    "varId": "2B"
                                },
                            ]
                        },
                        {
                            "factor": 0.44,
                            "varId": "3", 
                            "proportional_subseries":[
                                {
                                    "factor": 3, 
                                    "varId": "3A"
                                },
                                {
                                    "factor": 4, 
                                    "varId": "3B"
                                },
                            ]
                        },
                    ]
                },
                {
                    "priority": 2,
                    "varId": "4",
                },
            ]
        }

        self.assertEqual(solver.get_next_iter(series, maxed_vars=[]), (['0'], [1]) )
        self.assertEqual(solver.get_next_iter(series, maxed_vars=['0']), (['1','2A','3A','3B'], [0.22, 0.33, 0.44*3/7, 0.44*4/7]) )
        self.assertEqual(solver.get_next_iter(series, maxed_vars=['0', '2A']), (['1','2B', '3A', '3B'], [0.22, 0.33, 0.44*3/7, 0.44*4/7]) )
        self.assertEqual(solver.get_next_iter(series, maxed_vars=['0', '2A', '3B']), (['1', '2B', '3A'], [0.22, 0.33, 0.44]) )
        self.assertEqual(solver.get_next_iter(series, maxed_vars=['0', '2A', '2B', '1', '3B']), (['3A'], [0.44]) )
        self.assertEqual(solver.get_next_iter(series, maxed_vars=['0', '2A', '2B', '1', '3B', '3A']), (['4'], [1]) )
        self.assertEqual(solver.get_next_iter(series, maxed_vars=['0', '2A', '2B', '1', '3B', '3A', '4']), ([], []) )