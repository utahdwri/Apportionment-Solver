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
                        log_file='output/dev2.txt', 
                        write_output_files=write_output_files)


input = {
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

# Solve and check.
computed = run_solver(**input)