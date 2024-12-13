# This program runs tests!

import unittest 


def run_solver(**kargs):
    import solver.solver as solver

    # For speedy tests, don't log or write output files.
    write_output_files = False

    # For debugging:
    return solver.solve(flowlines=kargs['flowlines'], 
                        nodes=kargs['nodes'], 
                        paths=kargs['paths'], 
                        measurements=kargs['measurements'],
                        zones=kargs['zones'],
                        day=kargs['day'],
                        account_starting_balances=kargs['account_starting_balances'],
                        log_file=None, 
                        write_output_files=write_output_files)


class TestApportionments(unittest.TestCase):
    """
    This class adds a method to evaluate whether apportionment results match the expected results.
    It is ment to be extended via inheretance to the tests cases.
    """

    def assertApportionmentsEqual(self, results, expected_path_values):

        computed = {}
        for varId in results.variables:
            computed[varId] = results.variables[varId].value

        expected = {}
        for pathId in expected_path_values:
            expected['PATH_'+pathId] = expected_path_values[pathId]

        print("")
        print("computed:", computed)
        print("expected:", expected)

        for key in expected:
            self.assertAlmostEqual(computed[key], expected[key], places=4, msg="Incorrect value for path {}. \n\nComputed = {} \n\nExpected = {}".format(key, computed, expected) )




# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------

# TEST PROBLEM INPUT


def simple_problem_input(Q_IN=10, Q_OUT=0, Q_A=2, Q_B=4, Q_C=3):
    """
        
    1---#-->2------>3------>4---#-->5
            |       |       |  
            |       |       |  
            v       v       v
            6       7       8
    
            02-1    02-2    02-3

    """
    return {
        "flowlines": {
            "1-2": { "from_node": "1", "to_node": "2", "is_natural":True },
            "2-3": { "from_node": "2", "to_node": "3", "is_natural":True },
            "3-4": { "from_node": "3", "to_node": "4", "is_natural":True },
            "4-5": { "from_node": "4", "to_node": "5", "is_natural":True },

            "2-6": { "from_node": "2", "to_node": "6", "is_natural":False },
            "3-7": { "from_node": "3", "to_node": "7", "is_natural":False },
            "4-8": { "from_node": "4", "to_node": "8", "is_natural":False },
        },
        "paths": {
            "#1": {"wrnum":"02-1", "priority":1, "cfs_limit":2, "from_nodes":["2"], "to_nodes":["6"], "forward_flowlines": ["2-6"], "backward_flowlines":[] },
            "#2": {"wrnum":"02-2", "priority":2, "cfs_limit":4, "from_nodes":["3"], "to_nodes":["7"], "forward_flowlines": ["3-7"], "backward_flowlines":[] },
            "#3": {"wrnum":"02-3", "priority":3, "cfs_limit":6, "from_nodes":["4"], "to_nodes":["8"], "forward_flowlines": ["4-8"], "backward_flowlines":[] },
        },
        "nodes": {
            "1":{"handoff":False, "storage":False},
            "2":{"handoff":False, "storage":False},
            "3":{"handoff":False, "storage":False},
            "4":{"handoff":False, "storage":False},
            "5":{"handoff":False, "storage":False},
            "6":{"handoff":False, "storage":False},
            "7":{"handoff":False, "storage":False},
            "8":{"handoff":False, "storage":False},
        },
        "measurements" : {
            "0": {"flowlineId":"1-2", "timeseries":[{"date":"2023-06-01", "value":Q_IN}]},
            "1": {"flowlineId":"4-5", "timeseries":[{"date":"2023-06-01", "value":Q_OUT}]},
            "2": {"flowlineId":"2-6", "timeseries":[{"date":"2023-06-01", "value":Q_A}]},
            "3": {"flowlineId":"3-7", "timeseries":[{"date":"2023-06-01", "value":Q_B}]},
            "4": {"flowlineId":"4-8", "timeseries":[{"date":"2023-06-01", "value":Q_C}]},
        },
        "zones": [
            {
                "name": "Main Reach",
                "type": "inflow",
                "in_measurements": ["0"],
                "out_measurements": ["1"],
                "accounts":[],
            }
        ],
        "day": "2023-06-01",
        "account_starting_balances": {}
    }


def reservoir_problem_input(stor_chg=None, stor_loss=None, Q1_2=None, Q2_200=None, Q3_4=None, Q4_400=None, Q5_500=None, Q5_6=None):
    """    
    1---#-->2------> \~~~3~~~/ ---#-->4----->5---#-->6
            #         \_____/         #      #
            |                         |      |
            v                         v      v
            200                       400    500

    """
    return {
        "flowlines": {
            "1-2": { "from_node": "1", "to_node": "2", "is_natural":True },
            "2-3": { "from_node": "2", "to_node": "3", "is_natural":True },
            "3-4": { "from_node": "3", "to_node": "4", "is_natural":True },
            "4-5": { "from_node": "4", "to_node": "5", "is_natural":True },
            "5-6": { "from_node": "5", "to_node": "6", "is_natural":True },

            "2-200": { "from_node": "2", "to_node": "200", "is_natural":False },
            "4-400": { "from_node": "4", "to_node": "400", "is_natural":False },
            "5-500": { "from_node": "5", "to_node": "500", "is_natural":False },
        },
        "paths": {
            "WR@2": {"wrnum":"02-1", "priority":1, "cfs_limit":   2, "from_nodes":["2"], "to_nodes":["200"], "forward_flowlines": ["2-200"            ], "backward_flowlines":[] },
            "WR@4": {"wrnum":"02-2", "priority":2, "cfs_limit":   4, "from_nodes":["4"], "to_nodes":["400"], "forward_flowlines": ["4-400"            ], "backward_flowlines":[] },
            "WR@5": {"wrnum":"02-3", "priority":3, "cfs_limit":   6, "from_nodes":["5"], "to_nodes":["500"], "forward_flowlines": ["5-500"            ], "backward_flowlines":[] },
            "WR@3": {"wrnum":"02-4", "priority":4, "cfs_limit":  20, "from_nodes":["3"], "to_nodes":["3"  ], "forward_flowlines": [                   ], "backward_flowlines":[], "to_account":"ReservoirA"},
            "SD@5": {"wrnum":"" , "priority":9900, "cfs_limit":None, "from_nodes":["3"], "to_nodes":["500"], "forward_flowlines": ["3-4","4-5","5-500"], "backward_flowlines":[], "from_account":"ReservoirA"  },
            "SD@4": {"wrnum":"" , "priority":9901, "cfs_limit":None, "from_nodes":["3"], "to_nodes":["400"], "forward_flowlines": ["3-4","4-400"      ], "backward_flowlines":[], "from_account":"ReservoirA"  },
        },
        "nodes": {
            "1":{},
            "2":{},
            "3":{"storage":True},
            "4":{},
            "5":{},
            "6":{},
            "200":{},
            "400":{},
            "500":{}
        },
        "measurements": {
            "0": {"flowlineId":"1-2"  , "timeseries":[{"date":"2023-06-01", "value":Q1_2  }]},
            "1": {"flowlineId":"2-200", "timeseries":[{"date":"2023-06-01", "value":Q2_200}]},
            "2": {"flowlineId":"3-4"  , "timeseries":[{"date":"2023-06-01", "value":Q3_4  }]},
            "3": {"flowlineId":"4-400", "timeseries":[{"date":"2023-06-01", "value":Q4_400}]},
            "4": {"flowlineId":"5-500", "timeseries":[{"date":"2023-06-01", "value":Q5_500}]},
            "5": {"flowlineId":"5-6"  , "timeseries":[{"date":"2023-06-01", "value":Q5_6  }]},
            "_31": {"nodeId":"3", "type":"measured_storage_change", "timeseries":[{"date":"2023-06-01", "value":stor_chg }]},
            "_32": {"nodeId":"3", "type":"measured_storage_loss"  , "timeseries":[{"date":"2023-06-01", "value":stor_loss}]}
        },
        "zones": [
            {
                "name": "Reach 1",
                "type": "inflow",
                "in_measurements": [],
                "out_measurements": ["0"],
                "accounts":[],
            },
            {
                "name": "Reach 2",
                "type": "inflow",
                "in_measurements": ["0"],
                "out_measurements": ["2", "1", "_31"],
                "accounts":[],
            },
            {
                "name": "Reach 3",
                "type": "inflow",
                "in_measurements": ["2"],
                "out_measurements": ["5", "3", "4"],
                "accounts":[],
            },
            {
                "name": "Reach 4",
                "type": "inflow",
                "in_measurements": ["5"],
                "out_measurements": [],
                "accounts":[],
            },
            {
                "name": "Use 2-200",
                "type": "outflow",
                "in_measurements": ["1"],
                "out_measurements": [],
                "accounts":[],
            },
            {
                "name": "Use 4-400",
                "type": "outflow",
                "in_measurements": ["3"],
                "out_measurements": [],
                "accounts":[],
            },
            {
                "name": "Use 5-500",
                "type": "outflow",
                "in_measurements": ["4"],
                "out_measurements": [],
                "accounts":[],
            },
            {
                "name": "Reservoir Storage",
                "type": "storage",
                "in_measurements": ["_31"],
                "out_measurements": ["_32"],
                "accounts":[],
            },
        ],
        "day": "2023-06-01",
        "account_starting_balances": {}
    }


def alt_reservoir_problem_input(
                stor_chg=None, stor_loss=None, 
                Q2_3=None, Q3_4=None, Q4_5=None,
                Q2_200=None, Q4_400=None):
    """

    1------>2---#--> \~~~3~~~/ ---#-->4---#--->5
            #         \_____/         #        
            |                         |        
            v                         v        
            200                       400       

    """
    return {
        "flowlines": {
            "1-2": { "from_node": "1", "to_node": "2", "is_natural":True },
            "2-3": { "from_node": "2", "to_node": "3", "is_natural":True },
            "3-4": { "from_node": "3", "to_node": "4", "is_natural":True },
            "4-5": { "from_node": "4", "to_node": "5", "is_natural":True },

            "2-200": { "from_node": "2", "to_node": "200", "is_natural":False },
            "4-400": { "from_node": "4", "to_node": "400", "is_natural":False },
        },
        "nodes": {
            "1":{},
            "2":{},
            "3":{"storage":True},
            "4":{},
            "5":{},
            "200":{},
            "400":{},
        },
        "paths": {
            "WR@2": {"wrnum":"02-120", "priority":1870, "cfs_limit":  10, "from_nodes":["2"], "to_nodes":["200"], "forward_flowlines": ["2-200"            ], "backward_flowlines":[] },
            "WR@4": {"wrnum":"02-240", "priority":1885, "cfs_limit":   2, "from_nodes":["4"], "to_nodes":["400"], "forward_flowlines": ["4-400"            ], "backward_flowlines":[] },
            "WR@3": {"wrnum":"02-550", "priority":1950, "cfs_limit":  50, "from_nodes":["3"], "to_nodes":["3"  ], "forward_flowlines": [                   ], "backward_flowlines":[], "to_account":"ReservoirA"},
            "SD@4": {"wrnum":""      , "priority":9900, "cfs_limit":None, "from_nodes":["3"], "to_nodes":["400"], "forward_flowlines": ["3-4","4-400"      ], "backward_flowlines":[], "from_account":"ReservoirA"  },
            
        },
        "measurements": {
            "0": {"flowlineId":"2-3"  , "timeseries":[{"date":"2023-06-01", "value": Q2_3   }] },
            "1": {"flowlineId":"3-4"  , "timeseries":[{"date":"2023-06-01", "value": Q3_4   }] },
            "2": {"flowlineId":"4-5"  , "timeseries":[{"date":"2023-06-01", "value": Q4_5   }] },
            "5": {"flowlineId":"2-200", "timeseries":[{"date":"2023-06-01", "value": Q2_200 }] },
            "6": {"flowlineId":"4-400", "timeseries":[{"date":"2023-06-01", "value": Q4_400 }] },
            "_31": {"nodeId":"3", "type":"measured_storage_change", "timeseries":[{"date":"2023-06-01", "value": stor_chg }] },
            "_32": {"nodeId":"3", "type":"measured_storage_loss"  , "timeseries":[{"date":"2023-06-01", "value": stor_loss}] }
        },
        "zones": [
            {
                "name": "Upper Reach",
                "type": "inflow",
                "in_measurements": [],
                "out_measurements": ["0", "5"],
                "accounts":[],
            },
            {
                "name": "Mid Reach",
                "type": "inflow",
                "in_measurements": ["0"],
                "out_measurements": ["1", "_31"],
                "accounts":[],
            },
            {
                "name": "Lower Reach",
                "type": "inflow",
                "in_measurements": ["1"],
                "out_measurements": ["2", "6"],
                "accounts":[],
            },
            {
                "name": "Mid Storage",
                "type": "storage",
                "in_measurements": ["_31"],
                "out_measurements": ["_32"],
                "accounts":[],
            },
            {
                "name": "Use At 200",
                "type": "outflow",
                "in_measurements": ["5"],
                "out_measurements": [],
                "accounts":[],
            },
            {
                "name": "Use At 400",
                "type": "outflow",
                "in_measurements": ["6"],
                "out_measurements": [],
                "accounts":[],
            },
        ],
        "day": "2023-06-01",
        "account_starting_balances": {}
    }


def reservoir_plus_problem_input(
                stor_chg=None, stor_loss=None, 
                Q2_5=None, Q4_5=None, Q5_6=None, Q6_7=None, Q7_8=None, 
                Q2_200=None, Q4_400=None, Q6_600=None, Q7_700=None):
    """    
            200
            ^
            |
    1------>2---#--
                    |
                    v
    3------>4---#--> \~~~5~~~/ ---#-->6---#--->7---#--->8
            #         \_____/         #        #        
            |                         |        |        
            v                         v        v        
            400                       600      700      

    """
    return {
        "flowlines": {
            "1-2": { "from_node": "1", "to_node": "2", "is_natural":True },
            "2-5": { "from_node": "2", "to_node": "5", "is_natural":True },
            "3-4": { "from_node": "3", "to_node": "4", "is_natural":True },
            "4-5": { "from_node": "4", "to_node": "5", "is_natural":True },
            "5-6": { "from_node": "5", "to_node": "6", "is_natural":True },
            "6-7": { "from_node": "6", "to_node": "7", "is_natural":True },
            "7-8": { "from_node": "7", "to_node": "8", "is_natural":True },

            "2-200": { "from_node": "2", "to_node": "200", "is_natural":False },
            "4-400": { "from_node": "4", "to_node": "400", "is_natural":False },
            "6-600": { "from_node": "6", "to_node": "600", "is_natural":False },
            "7-700": { "from_node": "7", "to_node": "700", "is_natural":False },
        },
        "nodes": {
            "1":{},
            "2":{},
            "3":{},
            "4":{},
            "5":{"storage":True},
            "6":{},
            "7":{},
            "8":{},
            "200":{},
            "400":{},
            "600":{},
            "700":{},
            "800":{},
        },
        "paths": {
            "WR@2": {"wrnum":"02-120", "priority":1870, "cfs_limit":   5, "from_nodes":["2"], "to_nodes":["200"], "forward_flowlines": ["2-200"            ], "backward_flowlines":[] },
            "WR@4": {"wrnum":"02-240", "priority":1885, "cfs_limit":   5, "from_nodes":["4"], "to_nodes":["400"], "forward_flowlines": ["4-400"            ], "backward_flowlines":[] },
            "WR@7": {"wrnum":"02-370", "priority":1900, "cfs_limit":   2, "from_nodes":["7"], "to_nodes":["700"], "forward_flowlines": ["7-700"            ], "backward_flowlines":[] },
            "WR@6": {"wrnum":"02-460", "priority":1910, "cfs_limit":   5, "from_nodes":["6"], "to_nodes":["600"], "forward_flowlines": ["6-600"            ], "backward_flowlines":[] },
            "WR@5": {"wrnum":"02-550", "priority":1950, "cfs_limit":  50, "from_nodes":["5"], "to_nodes":["5"  ], "forward_flowlines": [                   ], "backward_flowlines":[], "to_account":"ReservoirA"},
            "SD@6": {"wrnum":""      , "priority":9900, "cfs_limit":None, "from_nodes":["5"], "to_nodes":["600"], "forward_flowlines": ["5-6","6-600"      ], "backward_flowlines":[], "from_account":"ReservoirA"  },
            "SD@7": {"wrnum":""      , "priority":9901, "cfs_limit":None, "from_nodes":["5"], "to_nodes":["700"], "forward_flowlines": ["5-6","6-7","7-700"], "backward_flowlines":[], "from_account":"ReservoirA"  },
            
        },
        "measurements": {
            "0": {"flowlineId":"2-5"  , "timeseries":[{"date":"2023-06-01", "value":Q2_5   }] },
            "1": {"flowlineId":"4-5"  , "timeseries":[{"date":"2023-06-01", "value":Q4_5   }] },
            "2": {"flowlineId":"5-6"  , "timeseries":[{"date":"2023-06-01", "value":Q5_6   }] },
            "3": {"flowlineId":"6-7"  , "timeseries":[{"date":"2023-06-01", "value":Q6_7   }] },
            "4": {"flowlineId":"7-8"  , "timeseries":[{"date":"2023-06-01", "value":Q7_8   }] },
            "5": {"flowlineId":"2-200", "timeseries":[{"date":"2023-06-01", "value":Q2_200 }] },
            "6": {"flowlineId":"4-400", "timeseries":[{"date":"2023-06-01", "value":Q4_400 }] },
            "7": {"flowlineId":"6-600", "timeseries":[{"date":"2023-06-01", "value":Q6_600 }] },
            "8": {"flowlineId":"7-700", "timeseries":[{"date":"2023-06-01", "value":Q7_700 }] },
            "_51": {"nodeId":"5", "type":"measured_storage_change", "timeseries":[{"date":"2023-06-01", "value":stor_chg}] },
            "_52": {"nodeId":"5", "type":"measured_storage_loss", "timeseries":[{"date":"2023-06-01", "value":stor_loss}] }
        },
        "zones": [
            {
                "id": "1",
                "name": "North Branch",
                "type": "inflow",
                "in_measurements": [],
                "out_measurements": ["0", "5"],
                "accounts":[],
            },
            {
                "id": "2",
                "name": "South Branch",
                "type": "inflow",
                "in_measurements": [],
                "out_measurements": ["1", "6"],
                "accounts":[],
            },
            {
                "id": "3",
                "name": "Mid Reach",
                "type": "inflow",
                "in_measurements": ["0", "1"],
                "out_measurements": ["2", "_51"],
                "accounts":[],
            },
            {
                "id": "4",
                "name": "Lower Reach A",
                "type": "inflow",
                "in_measurements": ["2"],
                "out_measurements": ["3", "7"],
                "accounts":[],
            },
            {
                "id": "5",
                "name": "Lower Reach B",
                "type": "inflow",
                "in_measurements": ["3"],
                "out_measurements": ["4", "8"],
                "accounts":[],
            },
            {
                "id": "6",
                "name": "Mid Storage",
                "type": "storage",
                "in_measurements": ["_51"],
                "out_measurements": ["_52"],
                "accounts":[],
            },
            {
                "id": "7",
                "name": "Use At 200",
                "type": "outflow",
                "in_measurements": ["5"],
                "out_measurements": [],
                "accounts":[],
            },
            {
                "id": "8",
                "name": "Use At 400",
                "type": "outflow",
                "in_measurements": ["6"],
                "out_measurements": [],
                "accounts":[],
            },
            {
                "id": "9",
                "name": "Use At 600",
                "type": "outflow",
                "in_measurements": ["7"],
                "out_measurements": [],
                "accounts":[],
            },
            {
                "id": "10",
                "name": "Use At 700",
                "type": "outflow",
                "in_measurements": ["8"],
                "out_measurements": [],
                "accounts":[],
            },
        ],
        "day": "2023-06-01",
        "account_starting_balances": {}
    }


def problem_input_with_fork_diversion():
    """
    1----->2----->3
          / \
         /   \
        |     |
        *     |
        |     |
        2a    2b

    """
    return {
        "flowlines": {
            "1-2": { "from_node": "1", "to_node": "2", "is_natural":True },
            "2-3": { "from_node": "2", "to_node": "3", "is_natural":True },
            "2-2a": { "from_node": "2", "to_node": "2a", "is_natural":False },
            "2-2b": { "from_node": "2", "to_node": "2b", "is_natural":False },
        },
        "paths": {
            "0" : {"wrnum":"02-1", "priority":1, "cfs_limit":1, "from_nodes":["2"], "to_nodes":["2a"], "forward_flowlines": ["2-2a"], "backward_flowlines":[] },
        },
        "nodes": {
            "1":{},
            "2":{},
            "3":{},
            "2a":{},
            "2b":{},
        },
        "measurements": {
            "2": {"flowlineId":"2-2a",  "timeseries":[{"date":"2023-06-01", "value":3}]},
        },
        "zones":[ # The reach zone is missing so we can do this test.
            {
                "id": "2",
                "name": "Use 2-2a",
                "type": "outflow",
                "in_measurements": ["2"],
                "out_measurements": [],
                "accounts": ["2"],
            }
        ],
        "day":"2023-06-01",
        "account_starting_balances":{}
    }


def change_application_accounting_question_input():
    """
        \1/---#-->2----->3--#-->4----->5--#-->6
                  |      |      |      |      
                 aband.  A      B      C      
                 1900   1920   1940   1930 
                 chg    shares shares 1950 chg (on 1900)
    """
    return {
        "flowlines": {
            "1-2": { "from_node": "1", "to_node": "2", "is_natural":True },
            "2-3": { "from_node": "2", "to_node": "3", "is_natural":True },
            "3-4": { "from_node": "3", "to_node": "4", "is_natural":True },
            "4-5": { "from_node": "4", "to_node": "5", "is_natural":True },
            "5-6": { "from_node": "5", "to_node": "6", "is_natural":True },
            "3-A": { "from_node": "3", "to_node": "A", "is_natural":False },
            "4-B": { "from_node": "4", "to_node": "B", "is_natural":False },
            "5-C": { "from_node": "5", "to_node": "C", "is_natural":False },
        },
        "nodes": {
            "1":{"storage":True},
            "2":{},
            "3":{},
            "4":{},
            "5":{},
            "6":{},
            "A":{},
            "B":{},
            "C":{},
        },
        "paths": {
            "C_1900" : {"wrnum":"02-1", "priority":1900, "cfs_limit":10, "from_nodes":["2"], "to_nodes":["2"], "forward_flowlines": [], "backward_flowlines":[], "to_change":"a1"},
            "A_1920" : {"wrnum":"02-2", "priority":1920, "cfs_limit":10, "from_nodes":["3"], "to_nodes":["A"], "forward_flowlines": ["3-A"], "backward_flowlines":[] },
            "C_1930" : {"wrnum":"02-3", "priority":1930, "cfs_limit":10, "from_nodes":["5"], "to_nodes":["C"], "forward_flowlines": ["5-C"], "backward_flowlines":[] },
            "B_1940" : {"wrnum":"02-4", "priority":1940, "cfs_limit":10, "from_nodes":["4"], "to_nodes":["B"], "forward_flowlines": ["4-B"], "backward_flowlines":[] },
            "C_1950" : {"wrnum":"02-5", "priority":1950, "cfs_limit":10, "from_nodes":["2"], "to_nodes":["C"], "forward_flowlines": ["2-3","3-4","4-5","5-C"], "backward_flowlines":[] , "from_change":"a1"},
            "A_stor" : {"wrnum":""    , "priority":9998, "cfs_limit":10, "from_nodes":["1"], "to_nodes":["A"], "forward_flowlines": ["1-2","2-3","3-A"], "backward_flowlines":[] },
            "B_stor" : {"wrnum":""    , "priority":9999, "cfs_limit":10, "from_nodes":["1"], "to_nodes":["B"], "forward_flowlines": ["1-2","2-3","3-4","4-B"], "backward_flowlines":[] },
        },
        "measurements": {
            "1": {"flowlineId":"1-2",  "timeseries":[{"date":"2023-06-01", "value":5}]},
            "2": {"flowlineId":"3-4",  "timeseries":[{"date":"2023-06-01", "value":5}]},
            "3": {"flowlineId":"5-6",  "timeseries":[{"date":"2023-06-01", "value":0}]},
            "4": {"flowlineId":"3-A",  "timeseries":[{"date":"2023-06-01", "value":5}]},
            "5": {"flowlineId":"4-B",  "timeseries":[{"date":"2023-06-01", "value":5}]},
            "6": {"flowlineId":"5-C",  "timeseries":[{"date":"2023-06-01", "value":5}]},
            "7": {"nodeId":"1", "type":"measured_storage_change", "timeseries":[{"date":"2023-06-01", "value":-5 }]},
            "8": {"nodeId":"1", "type":"measured_storage_loss"  , "timeseries":[{"date":"2023-06-01", "value": 0}]}
        },
        "zones":[
            {"id": "Upper", "name": "Upper Reach", "type": "inflow" , "in_measurements": ["1"], "out_measurements": ["2"], "accounts": [] },
            {"id": "Lower", "name": "Lower Reach", "type": "inflow" , "in_measurements": ["2"], "out_measurements": ["3"], "accounts": [] },
            {"id": "Use-A", "name": "Use-A"      , "type": "outflow", "in_measurements": ["4"], "out_measurements": [], "accounts": [] },
            {"id": "Use-B", "name": "Use-B"      , "type": "outflow", "in_measurements": ["5"], "out_measurements": [], "accounts": [] },
            {"id": "Use-C", "name": "Use-C"      , "type": "outflow", "in_measurements": ["6"], "out_measurements": [], "accounts": [] },
            {"id": "Sto-1", "name": "Storage"    , "type": "storage", "in_measurements": ["7"], "out_measurements": ["8"], "accounts": [] },
        ],
        "day":"2023-06-01",
        "account_starting_balances":{}
    }


# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------
# -----------------------------------------------------------------------------



class BasicStuff(TestApportionments):

    def setUp(self) -> None:
        from test_data import input as input

        self.results = run_solver(**input)
        self.wr2_details = self.results.variables['PATH_WR@2'].details[0]
        return super().setUp()

    def test_single_apportionment_limited_by(self):
        # This apportionmnet should be limited by both the water right and diversion measurment.
        self.assertIn("REM WR:WR@2", self.wr2_details.limited_by)
    
    def test_shared_apportionment_limited_by(self):
        # 
        details = self.results.variables['PATH_WR@5a'].details
        last_step_info = details[len(details)-1]
        self.assertIn("REM MEAS@5-500", last_step_info.limited_by)
        

    def test_result_step_number(self):
        # The step value should be 1 because this is the most senior right.
        self.assertEqual(1, self.wr2_details.step)

    def test_result_pre_post_values(self):
        # The path apportionment should step up from zero to two.
        self.assertAlmostEqual(0.0, self.wr2_details.values['PATH_WR@2'][0])
        self.assertAlmostEqual(2.0, self.wr2_details.values['PATH_WR@2'][1])

    def test_reach_gains_or_losses(self):
        # The output should include the reach gains/losses and the reach-to-reach flows.
        wr2_from_account = self.results.variables['PATH_WR@2'].from_zone.id

        wr2_reach = self.results.variables['GAIN_REACH_'+wr2_from_account]

        self.assertEqual(-5.0 + 2.0 + 7.0, wr2_reach.value)


class ReachAnalysis(TestApportionments):

    def get_input(self):
        """
            
        1---#-->2------>3------>4---#-->5
                |       |       |  
                |       |       |  
                v       v       v
                6       7       8
        
                02-1    02-2    02-3

        """
        return simple_problem_input()



    def test_reach_gain(self):
        """If we measure everything, we might see a net gain in the reach. Is 
        this working? Or perhaps does the program not allow a net gain, and 
        instead constrains the most-junior allocation to something less than 
        what was measured?
        """
        input = self.get_input()
        computed = run_solver(**input)
        expected = {'#1': 2, '#2': 4, '#3': 3} 
        self.assertApportionmentsEqual(computed, expected)

    def test_reach_loss(self):
        """Similar to above but with a net loss in the reach.
        """
        input = self.get_input()
        computed = run_solver(**input)
        expected = {'#1': 2, '#2': 4, '#3': 3} 
        self.assertApportionmentsEqual(computed, expected)

    def test_multiple_lines_between_reaches(self):
        """What if the 1-2 flowline is actually 3 flowlines with 3 measurements 
        that sum to the same value? The answer should be the same, right?
        """

        input = self.get_input()

        # solve using the default input ...
        input['measurements'] = {
            "0": {"flowlineId":"1-2", "timeseries":[{"date":"2023-06-01", "value":10}]},
            "1": {"flowlineId":"4-5", "timeseries":[{"date":"2023-06-01", "value":0}]},
            "2": {"flowlineId":"2-6", "timeseries":[{"date":"2023-06-01", "value":2}]},
            "3": {"flowlineId":"3-7", "timeseries":[{"date":"2023-06-01", "value":4}]},
            "4": {"flowlineId":"4-8", "timeseries":[{"date":"2023-06-01", "value":3}]},
        }
        expected_details = run_solver(**input)
        expected = {varId.replace('PATH_',''):expected_details.variables[varId].value 
        for varId in expected_details.variables if varId in ('PATH_#1', 'PATH_#2', 'PATH_#3')}

        
        # solve again after splitting the line and measurements...
        meas_value = input['measurements']["0"]["timeseries"][0]['value']
        input['measurements']["0"]["timeseries"][0]['value'] = 0.2*meas_value
        input['measurements']["0b"] = {"flowlineId":"1-2b", "timeseries":[{"date":"2023-06-01", "value":0.3*meas_value}]}
        input['measurements']["0c"] = {"flowlineId":"1-2c", "timeseries":[{"date":"2023-06-01", "value":0.5*meas_value}]}

        input['flowlines']["1-2b"] = { "from_node": "1", "to_node": "2", "is_natural":False }
        input['flowlines']["1-2c"] = { "from_node": "1", "to_node": "2", "is_natural":False }
        computed = run_solver(**input)

        # and compare.
        self.assertApportionmentsEqual(computed, expected)


class Reservoirs(TestApportionments):
    def get_input(**kargs):
        return reservoir_problem_input(**kargs)
        
    def test_trivial(self):
        """This is a prety trivial example, just to check the most simple case... should probably make some more interesting tests...
        """
        input = reservoir_problem_input(stor_chg=0, stor_loss=0, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=4, Q5_500=4, Q5_6=1)
        computed = run_solver(**input)
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':0, 'SD@5':0, 'SD@4':0}
        self.assertApportionmentsEqual(computed, expected)

    def test_storage_diversions_with_no_deliveries(self):
        """Test storage diversions with no deliveries.
        """
        input = reservoir_problem_input(stor_chg=2, stor_loss=0, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=4, Q5_500=4, Q5_6=1)
        computed = run_solver(**input)
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':2, 'SD@5':0, 'SD@4':0} 
        self.assertApportionmentsEqual(computed, expected)

    def test_storage_deliveries_with_no_diversions(self):
        """Test storage deliveries with no diversions.
        """
        input = reservoir_problem_input(stor_chg=-2, stor_loss=0, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=6, Q5_500=4, Q5_6=1)
        computed = run_solver(**input)
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':0, 'SD@5':0, 'SD@4':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_equal_priority_deliveries(self):
        """Test storage deliveries that have equal priority.
        """
        input = reservoir_problem_input(stor_chg=-2, stor_loss=0, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=6, Q5_500=4, Q5_6=1)
        input['paths']['SD@4']['priority'] = input['paths']['SD@5']['priority']
        computed = run_solver(**input)
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':0, 'SD@5':0, 'SD@4':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_storage_deliveries_and_diversions_and_losses(self):
        """Test storage deliveries with diversions into storage at the same time, with some evaporative losses specified as well.
        """
        input = reservoir_problem_input(stor_chg=-1, stor_loss=1, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=6, Q5_500=4, Q5_6=1)
        computed = run_solver(**input)
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':2, 'SD@5':0, 'SD@4':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_equal_priority_apportionmnets(self):
        """Test equal priority apportionments with storage deliveries on top.
        """
        input = reservoir_problem_input(stor_chg=-4, stor_loss=1, Q1_2=2, Q2_200=1, Q3_4=4, Q4_400=2+2, Q5_500=3+1, Q5_6=1)
        input['paths']['WR@2']['priority'] = 1 #2 cfs is limit
        input['paths']['WR@4']['priority'] = 1 #4 cfs is limit
        input['paths']['WR@5']['priority'] = 1 #6 cfs is limit
        computed = run_solver(**input)
        expected = {'WR@2': 1, 'WR@4': 2, 'WR@5': 3, 'WR@3':0, 'SD@5':1, 'SD@4':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_equal_priority_apportionmnets2(self):
        """Test equal priority apportionments when the storage right also has an equal priority.

        For this case there is 4 cfs of gain above the reservoir (all of which is below the first gage).
        There is another 8 cfs of gain below the reservoir.

        """
        input = reservoir_problem_input(stor_chg=-4, stor_loss=1, Q1_2=0, Q2_200=1, Q3_4=6, Q4_400=7, Q5_500=7, Q5_6=0)
        input['paths']['WR@2']['priority'] = 1 # 2 cfs is limit
        input['paths']['WR@3']['priority'] = 1 #20 cfs is limit
        input['paths']['WR@4']['priority'] = 1 # 4 cfs is limit
        input['paths']['WR@5']['priority'] = 1 # 6 cfs is limit
        computed = run_solver(**input)
        expected = {
            'WR@2': 1, 
            'WR@3': 3,  # there is only this much remaining NF above the reservoir
            'WR@4': 8/10*4,
            'WR@5': 8/10*6, 
            'SD@4': 7 - 8/10*4,
            'SD@5': 7 - 8/10*6, 
        } 
        self.assertApportionmentsEqual(computed, expected)

    def test_change_water_that_is_not_available_at_htf_source(self):
        """Move water downstream when it is NOT available at the origional source but is available at the here-after source.
        """
        input = reservoir_problem_input(stor_chg=-2, stor_loss=0, Q1_2=0, Q2_200=0, Q3_4=2, Q4_400=6, Q5_500=8, Q5_6=0)
        # Change WR@2 so it delivers water downstream.
        input['paths']['WR@2'] = {"wrnum":"02-1", "priority":1, "cfs_limit": 2, "from_nodes":["2"], "to_nodes":["500"], "forward_flowlines": ["2-3", "3-4", "4-400" ], "backward_flowlines":[] }
        computed = run_solver(**input)
        expected = {'WR@2': 0, 'WR@4': 4, 'WR@5': 6, 'WR@3':0, 'SD@4':0, 'SD@5':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_change_water_that_is_available_at_htf_source(self):
        """Move water downstream when it is NOT available at the origional source but is available at the here-after source.
        """
        input = reservoir_problem_input(stor_chg=-2, stor_loss=0, Q1_2=2, Q2_200=0, Q3_4=4, Q4_400=6, Q5_500=8, Q5_6=0)
        # Change WR@2 so it delivers water downstream.
        input['paths']['WR@2'] = {"wrnum":"02-1", "priority":1, "cfs_limit": 2, "from_nodes":["2"], "to_nodes":["500"], "forward_flowlines": ["2-3", "3-4", "4-400" ], "backward_flowlines":[] }
        computed = run_solver(**input)
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 6, 'WR@3':0, 'SD@4':0, 'SD@5':2} 
        self.assertApportionmentsEqual(computed, expected)
        ## Add a possible storage delivery to the diversion at node 2. 
        #input['paths']['SD@2'] = {"wrnum":"" , "priority":9903, "cfs_limit":None, "from_nodes":["3"], "to_nodes":["2"], "forward_flowlines": ["2-200"], "backward_flowlines":["2-3"], "from_account":"ReservoirA"  }

    def test_spill_to_natural_flow(self):
        """What if the reservoir releases water that is not picked up?
        """
        input = reservoir_problem_input(stor_chg=-5, stor_loss=0, Q1_2=0, Q2_200=2, Q3_4=5, Q4_400=0, Q5_500=0, Q5_6=5)
        computed = run_solver(**input)
        expected = {'WR@2': 2, 'WR@4': 0, 'WR@5': 0, 'WR@3':0, 'SD@5':0, 'SD@4':0} 
        self.assertApportionmentsEqual(computed, expected)

    def test_storage_diversion_exceeds_storage_right(self):
        """In practice, I doubt this is very important, but I still want the program to support this case.
        """
        input = reservoir_problem_input(stor_chg=25, stor_loss=0, Q1_2=0, Q2_200=0, Q3_4=0, Q4_400=0, Q5_500=0, Q5_6=0)
        computed = run_solver(**input)
        expected = {'WR@2': 0, 'WR@4': 0, 'WR@5': 0, 'WR@3':20, 'SD@5':0, 'SD@4':0} 
        self.assertApportionmentsEqual(computed, expected)



    def test_presentation_example(self):
        """
        """
        input = reservoir_problem_input(stor_chg=-10, stor_loss=0, Q1_2=0, Q2_200=0, Q3_4=20, Q4_400=15, Q5_500=10, Q5_6=5)
        input['paths']['WR@4']['cfs_limit'] = 5
        input['paths']['WR@5']['cfs_limit'] = 2
        input['paths']['WR@3']['cfs_limit'] = 100
        computed = run_solver(**input)
        expected = {'WR@2': 0, 'WR@3':8, 'WR@4': 5, 'WR@5': 2, 'SD@4':10, 'SD@5':8} 
        self.assertApportionmentsEqual(computed, expected)


class Changes(TestApportionments):




    def test_1(self):
        """ First test some simple example paramters.

        | Reach     NF
        | --------  --
        | Node-2     2
        | Node-4     2
        | Node-5     2
        | Node-6     2
        | Node-7     2

            200
            ^
            |2  0
    1------>2---#--
                    |
                    v             2       0        0
    3------>4---#--> \~~~5~~~/ ---#-->6---#--->7---#--->8
            #2  0     \_____/         #4       #2       
            |            0            |        |        
            v                         v        v        
            400                       600      700     

        """
        input = reservoir_plus_problem_input(stor_chg=0, stor_loss=0, 
                               Q2_5=0, Q4_5=0, Q5_6=2, Q6_7=0, Q7_8=0, 
                               Q2_200=2, Q4_400=2, Q6_600=4, Q7_700=2)
        computed = run_solver(**input)
        expected = {'WR@2': 2, 'WR@4': 2, 'WR@5': 0, 'WR@6':4, 'WR@7':2, 'SD@6':0, 'SD@7':0} 
        self.assertApportionmentsEqual(computed, expected)

    def test_2(self):
        """ Same as above, but adding a downstream-moving change, from div@2 -> div@7.

            200
            ^
            |0  2
    1------>2---#--
                    |
                    v             4       2        0
    3------>4---#--> \~~~5~~~/ ---#-->6---#--->7---#--->8
            #2  0     \_____/         #4       #4       
            |            0            |        |        
            v                         v        v        
            400                       600      700 
        """

        # Increased the measured flows because of the delivery.
        input = reservoir_plus_problem_input(stor_chg=0, stor_loss=0, 
                               Q2_5=0+2, Q4_5=0, Q5_6=2+2, Q6_7=0+2, Q7_8=0, 
                               Q2_200=2-2, Q4_400=2, Q6_600=4, Q7_700=2+2)
        
        # Modify the base right:
        input['paths']['WR@2'] = {"wrnum":"02-120", "priority":1870, "cfs_limit":5, "from_nodes":["2"], "to_nodes":["2"], "forward_flowlines": [], "backward_flowlines":[] }
        input['paths']['WR@2']['to_change'] = 'a123'

        # And add the HA change:
        input['paths']['chg123'] = {"wrnum":"", "priority":2015, "cfs_limit":5, "from_nodes":["2"], "to_nodes":["700"], "forward_flowlines": ["2-5","5-6","6-7","7-700"], "backward_flowlines":[]  }
        input['paths']['chg123']['from_change'] = 'a123'

        

        # Solve and check.
        computed = run_solver(**input)
        expected = {
            'WR@2': 2, # same as above
            'WR@4': 2, 
            'WR@5': 0, 
            'WR@6': 4, 
            'WR@7': 2, 
            'SD@6': 0, 
            'SD@7': 0, 
            'chg123':2
        } 
        self.assertApportionmentsEqual(computed, expected)

    
    def test_3(self):
        """
        
        """
        input = alt_reservoir_problem_input(stor_chg=0, stor_loss=0, 
                               Q2_3=2, Q3_4=2, Q4_5=0,
                               Q2_200=0, Q4_400=4)
        
        # Modify the base right:
        input['paths']['WR@2'] = {"wrnum":"02-120", "priority":1870, "cfs_limit":10, "from_nodes":["2"], "to_nodes":["2"], "forward_flowlines": [], "backward_flowlines":[] }
        input['paths']['WR@2']['to_change'] = 'a123'

        # And add the HA change:
        input['paths']['chg123'] = {"wrnum":"", "priority":2015, "cfs_limit":10, "from_nodes":["2"], "to_nodes":["400"], "forward_flowlines": ["2-3","3-4","4-400"], "backward_flowlines":[]  }
        input['paths']['chg123']['from_change'] = 'a123'

        # Solve and check.
        computed = run_solver(**input)
        expected = {
            'WR@2': 2, # same as above
            'WR@4': 2, 
            'SD@4': 0, 
            'chg123':2
        } 
        self.assertApportionmentsEqual(computed, expected)



    def test_change_application_accounting_question(self):
        """This is based on an example problem in this google doc:
        https://docs.google.com/document/d/1TBD5BMyy6aycwFimGeNxlE-ozBxTOfrLR0EpvBQttV0/edit#heading=h.908q10f6uovo
        
             5cfs          5cfs          0cfs
        \1/---#-->2----->3--#-->4----->5--#-->6
                  |      |      |      |      
                 aband.  A      B      C      
                 1900   1920   1940   1930 
                 chg    shares shares 1950 chg (on 1900)
        """
        input = change_application_accounting_question_input()

        # Solve and check.
        computed = run_solver(**input)
        expected = {
            "C_1900": 0,
            "A_1920": 5,
            "C_1930": 5,
            "B_1940": 0,
            "C_1950": 0,
            "A_stor": 0,
            "B_stor": 5,
        } 
        self.assertApportionmentsEqual(computed, expected)


    def test_change_to_tributary(self):
        """If a change moves a senior right from one tributary to another
        past other existing uses, and if all these uses can utilize storage,
        ...
            0             0
        1---#-->2----->3--#-->4
                |      |
                #6     #6
                v      v
               200    300

                                                  Expected   Alt
                                                  apport.    apport.
                                                  --------  --------
        1900. 5cfs @300. Changed in 2010 to @200 | 5       | 1
        1910. 5cfs @300                          | 5       | 5
        1920. 5cfs @200                          | 1       | 5
        Storage @300                             | 1       | 1
        Storage @200                             | 0       | 0
        
        ========================================================================

        Now suppose there is a measurement between 2 and 3. Does that change things?

            0      0      0
        1---#-->2--#-->3--#-->4
                |      |
                #6     #6
                v      v
               200    300
                                                  Expected   Alt
                                                  apport.    apport.
                                                  --------  --------
        1900. 5cfs @300. Changed in 2010 to @200 | 5       | 1
        1910. 5cfs @300                          | 5       | 5
        1920. 5cfs @200                          | 1       | 5
        Storage @300                             |        | 1
        Storage @200                             |        | 0

        """


class Misc(TestApportionments):


    def test_import_path_from_undefined_zone(self):
        """When the input defines a measured path that moves water from an area 
        outside all defined zones (i.e. an import)
         - the path apportionment must be less than the measurement
         - if the measurement exceeds the path limit, the apportionmnet should 
           be that limit
        
           NOTE - This test was added after discovering that the formulated LP
           problem was infeasible for these cases; since there is no reach-to-reach slack 
           variable in this case, the apportionment was being required to equal 
           the measured value which was impossible due to the path limit.
        """
    
        input = problem_input_with_fork_diversion()
        
        # run the solver and see if it works.
        computed = run_solver(**input)
        expected = {'0': 1} 
        self.assertApportionmentsEqual(computed, expected)


    def test_negative_measurement_values(self):
        """If there are negative flow measurements, what should the program do?

        For now, the answer is to replace them with zero and proceed with the 
        apportionment.
        """

        # Test with negative stream inflow, negative stream outflow, and 1 of 3 
        # diversions being negative.
        input = simple_problem_input(Q_IN=-0.1, Q_OUT=-0.1, Q_A=1, Q_B=1, Q_C=-100)
        
        # run the solver and see if it works.
        computed = run_solver(**input)
        expected = {'#1': 1, '#2':1, '#3':0} 
        self.assertApportionmentsEqual(computed, expected)

    '''
    def test_sequential_series_nested_inside_proportional_series(self):
        """

        -       1880.   2 cfs           [2]
        -       1880.   3 cfs (child1)  [3]
        -       1880.   5 cfs (child2)  [5]

        child1     1.   1 cfs           [1]
        child1     2.   2 cfs           [4/5]
        child1     2.   3 cfs           [6/5]
        child1     3.   5 cfs           [0]
  
        child2     1.   1 cfs           [ 5/6]
        child2     1.   2 cfs           [10/6]
        child2     1.   3 cfs           [15/6]

        """
        input = simple_problem_input(Q_IN=0, Q_OUT=0, Q_A=12, Q_B=13, Q_C=15)
        
        input["paths"] = {
            "1": {"wrnum":"02-1", "priority":1880, "cfs_limit":2,  },
            "2": {"wrnum":"02-2", "priority":1880, "cfs_limit":3, "child_series":"child1" },
            "3": {"wrnum":"02-3", "priority":1880, "cfs_limit":4, "child_series":"child2" },
        
            "21": {"series":"child1", "wrnum":"", "priority":1, "cfs_limit":1, "from_nodes":["3"], "to_nodes":["7"], "forward_flowlines": ["3-7"], "backward_flowlines":[] },
            "22": {"series":"child1", "wrnum":"", "priority":2, "cfs_limit":2, "from_nodes":["3"], "to_nodes":["7"], "forward_flowlines": ["3-7"], "backward_flowlines":[] },
            "23": {"series":"child1", "wrnum":"", "priority":2, "cfs_limit":3, "from_nodes":["3"], "to_nodes":["7"], "forward_flowlines": ["3-7"], "backward_flowlines":[] },
            "24": {"series":"child1", "wrnum":"", "priority":3, "cfs_limit":5, "from_nodes":["3"], "to_nodes":["7"], "forward_flowlines": ["3-7"], "backward_flowlines":[] },

            "31": {"series":"child2", "wrnum":"", "priority":1, "cfs_limit":1, "from_nodes":["4"], "to_nodes":["8"], "forward_flowlines": ["4-8"], "backward_flowlines":[] },
            "32": {"series":"child2", "wrnum":"", "priority":1, "cfs_limit":2, "from_nodes":["4"], "to_nodes":["8"], "forward_flowlines": ["4-8"], "backward_flowlines":[] },
            "33": {"series":"child2", "wrnum":"", "priority":1, "cfs_limit":3, "from_nodes":["4"], "to_nodes":["8"], "forward_flowlines": ["4-8"], "backward_flowlines":[] },
            
        }


        # run the solver and see if it works.
        computed = run_solver(**input)
        expected = {"1":2, "2":3, "3":5,
                    "21":1, "22":4/5, "23":6/5, "24":0,
                    "31":5/6, "32":10/6, "33":15/6 }

        self.assertApportionmentsEqual(computed, expected)
'''


if __name__ == '__main__':
    mycase = Reservoirs()
    mycase.test_trivial()