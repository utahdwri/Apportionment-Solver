'''
Some example problem input for the tests.
'''

input = {
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
        "WR@2" : {"wrnum":"02-1", "priority":1, "cfs_limit":   2, "from_nodes":["2"], "to_nodes":["200"], "forward_flowlines": ["2-200"            ], "backward_flowlines":[], "from_account":"10002", "to_account":"1000201", },
        "WR@4" : {"wrnum":"02-2", "priority":2, "cfs_limit":   4, "from_nodes":["4"], "to_nodes":["400"], "forward_flowlines": ["4-400"            ], "backward_flowlines":[], "from_account":"10003", "to_account":"1000301", },
        "WR@5a": {"wrnum":"02-30","priority":3, "cfs_limit":   1, "from_nodes":["5"], "to_nodes":["500"], "forward_flowlines": ["5-500"            ], "backward_flowlines":[], "from_account":"10003", "to_account":"1000302", },
        "WR@5b": {"wrnum":"02-31","priority":3, "cfs_limit":   2, "from_nodes":["5"], "to_nodes":["500"], "forward_flowlines": ["5-500"            ], "backward_flowlines":[], "from_account":"10003", "to_account":"1000302", },
        "WR@5c": {"wrnum":"02-32","priority":3, "cfs_limit":   3, "from_nodes":["5"], "to_nodes":["500"], "forward_flowlines": ["5-500"            ], "backward_flowlines":[], "from_account":"10003", "to_account":"1000302", },
        "WR@3" : {"wrnum":"02-4", "priority":4, "cfs_limit":  20, "from_nodes":["3"], "to_nodes":["3"  ], "forward_flowlines": [                   ], "backward_flowlines":[], "from_account":"10002", "to_account":"1000205", },
        "SD@5" : {"wrnum":"" , "priority":9900, "cfs_limit":None, "from_nodes":["3"], "to_nodes":["500"], "forward_flowlines": ["3-4","4-5","5-500"], "backward_flowlines":[], "from_account":"1000205", "to_account":"1000302", },
        "SD@4" : {"wrnum":"" , "priority":9901, "cfs_limit":None, "from_nodes":["3"], "to_nodes":["400"], "forward_flowlines": ["3-4","4-400"      ], "backward_flowlines":[], "from_account":"1000205", "to_account":"1000301", },
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
        "0": {"flowlineId":"1-2",   "timeseries":[{"date":"2023-06-01", "value":5}]},
        "1": {"flowlineId":"2-200", "timeseries":[{"date":"2023-06-01", "value":2}]},
        "2": {"flowlineId":"3-4",   "timeseries":[{"date":"2023-06-01", "value":7}]},
        "3": {"flowlineId":"4-400", "timeseries":[{"date":"2023-06-01", "value":4}]},
        "4": {"flowlineId":"5-500", "timeseries":[{"date":"2023-06-01", "value":4}]},
        "5": {"flowlineId":"5-6",   "timeseries":[{"date":"2023-06-01", "value":1}]},
        "N3": {"nodeId":"3", "type":"measured_storage_change", "timeseries":[{"date":"2023-06-01", "value":0}] },
        "E3": {"nodeId":"3", "type":"measured_storage_loss"  , "timeseries":[{"date":"2023-06-01", "value":0}] }
    },
    "zones": [
        {
            "id": "1",
            "name": "Reach 1",
            "type": "inflow",
            "in_measurements": [],
            "out_measurements": ["0"],
            "accounts":["10001"],
        },
        {
            "id": "2",
            "name": "Reach 2",
            "type": "inflow",
            "in_measurements": ["0"],
            "out_measurements": ["2", "1", "N3"],
            "accounts":["10002"],
        },
        {
            "id": "3",
            "name": "Reach 3",
            "type": "inflow",
            "in_measurements": ["2"],
            "out_measurements": ["5", "3", "4"],
            "accounts":["10003"],
        },
        {
            "id": "4",
            "name": "Reach 4",
            "type": "inflow",
            "in_measurements": ["5"],
            "out_measurements": [],
            "accounts":["10004"],
        },
        {
            "id": "5",
            "name": "Use 2-200",
            "type": "outflow",
            "in_measurements": ["1"],
            "out_measurements": [],
            "accounts":["1000201"],
        },
        {
            "id": "6",
            "name": "Use 4-400",
            "type": "outflow",
            "in_measurements": ["3"],
            "out_measurements": [],
            "accounts":["1000301"],
        },
        {
            "id": "7",
            "name": "Use 5-500",
            "type": "outflow",
            "in_measurements": ["4"],
            "out_measurements": [],
            "accounts":["1000302"],
        },
        {
            "id": "8",
            "name": "Reservoir Storage",
            "type": "storage",
            "in_measurements": ["N3"],
            "out_measurements": ["E3"],
            "accounts":["1000205"],
        },
    ],
    "day": "2023-06-01",
    "account_starting_balances": {
        "1000205": 100,
    }
}

"""


Expected output:
 
                    3'
                    ^
                    | dS=0, E=0
                    |
1---*---->2------->\3/---*---->4--------->5-----*---->6
   5 cfs  |             7 cfs  |          |    1 cfs
          |                    |          |
          * 2 cfs              * 4 cfs    * 4 cfs
          v                    v          v
         200                  400        500

Reach 1: Gain of 5 cfs
Reach 2: Gain of 4 cfs
Reach 3: Gain of 2 cfs
Reach 4: Loss of 1 cfs

WR@2  Apportionment: 2 cfs (3 remaining in Reach 1)     
WR@4  Apportionment: 4 cfs (Reach 1,2,3 has 5 remaining)   
WR@5a Apportionment: 5/9*1/6 =     
WR@5b Apportionment: 5/9*2/6  
WR@5c Apportionment: 5/9*3/6  
WR@3  Apportionment: 0 cfs    
SD@5  Delivery     : 
SD@4  Delivery     : 
         

"""