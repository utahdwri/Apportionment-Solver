import unittest
from solver.solver import *
from solver.wr_network import MeasurementManager, Path

class TestMeasurementManager(unittest.TestCase):

    def setUp(self) -> None:
        measurement_manager = MeasurementManager({
            '0':{"flowlineId":"1-2",   "timeseries":[{"date":"2023-04-09", "value":5}]},
            '1':{"flowlineId":"2-200", "timeseries":[{"date":"2023-04-09", "value":2}]},
            '2':{"flowlineId":"3-4",   "timeseries":[{"date":"2023-04-09", "value":7}]},
            '3':{"flowlineId":"4-400", "timeseries":[{"date":"2023-04-09", "value":4}]},
            '4':{"flowlineId":"5-500", "timeseries":[{"date":"2023-04-09", "value":4}]},
            '5':{"flowlineId":"5-6",   "timeseries":[{"date":"2023-04-09", "value":1}]},
            '6':{"nodeId":"3", "type":"measured_storage_change", "timeseries":[{"date":"2023-04-09", "value":0 }]},
            '7':{"nodeId":"3", "type":"measured_storage_loss"  , "timeseries":[{"date":"2023-04-09", "value":0 }]}
        })
        self.measurement_manager = measurement_manager 

    def test_allows_retrieval_by_nodeId(self):
        actual_value = self.measurement_manager.get(nodeId='3', type='measured_storage_change', date='2023-04-09')
        self.assertEqual(actual_value, 0)

    def test_allows_retrieval_by_flowlineId(self):
        actual_value = self.measurement_manager.get(flowlineId='5-6', date='2023-04-09')
        self.assertEqual(actual_value, 1)

    def test_raises_exception_when_duplicate_measurements(self):
        #Or should it keep only the top-most measurement?
    
        mm = MeasurementManager({
            '5':{"flowlineId":"5-6",   "timeseries":[{"date":"2023-04-09", "value":1 }], "dist_from_top": 10},
            '5b':{"flowlineId":"5-6",  "timeseries":[{"date":"2023-04-09", "value":1.5 }], "dist_from_top": 50},
            '5c':{"flowlineId":"5-6",  "timeseries":[{"date":"2023-04-09", "value":2 }], "dist_from_top": 2},
        })

        # If we include the if_multiple flag, it should return the top measurement.
        upstream_value = mm.get(flowlineId="5-6", if_multiple='PICK_UPSTREAM_MEASUREMENT', date='2023-04-09')
        self.assertEqual(upstream_value, 2)

        # But without the if_multiple flag, we should get an exception.
        with self.assertRaises(Exception):
            mm.get(flowlineId="5-6", date='2023-04-09')

    def test_add_flowline_measurement_for_net_storage(self):
        mm = self.measurement_manager
        mm.add_flowline_measurement_for_net_storage(flowlineId='fakeId', nodeId='3')
        self.assertEqual(mm.get(flowlineId='fakeId', date='2023-04-09'), 0)


class TestCFSLimitUtils(unittest.TestCase):
    def test_parse_plain_number(self):
        # If the cfs_input is just a number, the value getter should return that number
        value_getter = Path.parse_cfs(cfs_input="3.1415")
        self.assertEqual(value_getter('2012-03-12'), 3.1415)
        self.assertEqual(value_getter('2012-02-31'), 3.1415)
        self.assertEqual(value_getter('9999-02-31'), 3.1415)
        self.assertEqual(value_getter(None), 3.1415)
    
    def test_parse_seasonal_period_numbers(self):
        # If the cfs input is valid json, the value getter should multiply the base 
        # number by a seasonal factor that varies based on specified periods begining 
        # on specified month-days.
        value_getter = Path.parse_cfs('''{"base":50, "season":[["0401",1], ["0601",0.5], ["1001",0]]}''')
        self.assertEqual(value_getter('2012-03-12'), 0)
        self.assertEqual(value_getter('2012-04-01'), 50)
        self.assertEqual(value_getter('2012-05-31'), 50)
        self.assertEqual(value_getter('2012-06-01'), 25)
        self.assertEqual(value_getter('2012-10-01'), 0)

    def test_silly_seasonal_input(self):
        # It should still work even if the seasonal periods are not properly sorted.
        value_getter = Path.parse_cfs('''{"base":50, "season":[["0601",0.5], ["1001",0],["0401",1]]}''')
        self.assertEqual(value_getter('2012-04-12'), 50)

    def test_parse_with_daily_values(self):
        # How should it work if we need to retrieve a daily value from dvrtDB or someplace similar?
        value_getter = Path.parse_cfs('''{"base":2, "season":[["0401",1],["0601",0.5],["1001",0]], "ts":{"2023-03-31":10,"2023-04-01":11,"2023-04-02":12}}''')
        self.assertEqual(value_getter('2023-03-31'), 0)
        self.assertEqual(value_getter('2023-04-01'), 22) # = 2 * 1 * 11
        self.assertEqual(value_getter('2023-04-02'), 24) # = 2 * 1 * 12


class TestSubsets(unittest.TestCase):

    def setUp(self) -> None:
        # some example input:

        '''

       {0}-*--→1-----→2-----→3-----→4-----→{5}-*---→6
               ‖      ‖      ↑      |       ↑       |
               *      ‖      |      |       |       |
               ‖      ‖      |      |       |       |
               ↓      ↓      |      ↓       |       ↓
              #A#----→B====→#C#     D------→E-----→#F#
                    ↱ |            ↱ ↰
                  /   |           /   \
                /     ↓          |     |
              (G)     H         (I)   (J)

        Nodes A, C, D are 'use/output nodes'. 
        Nodes 0 and 5 are 'storage nodes'.

        '''

        self.example_data = {
            "flowlines": {
                "0-1": { "from_node": "0", "to_node": "1", "is_natural":True },
                "1-2": { "from_node": "1", "to_node": "2", "is_natural":True },
                "2-3": { "from_node": "2", "to_node": "3", "is_natural":True },
                "3-4": { "from_node": "3", "to_node": "4", "is_natural":True },
                "4-5": { "from_node": "4", "to_node": "5", "is_natural":True },
                "5-6": { "from_node": "5", "to_node": "6", "is_natural":True },
                "1-A": { "from_node": "1", "to_node": "A", "is_natural":False },
                "2-B": { "from_node": "2", "to_node": "B", "is_natural":False },
                "C-3": { "from_node": "C", "to_node": "3", "is_natural":False },
                "4-D": { "from_node": "4", "to_node": "D", "is_natural":False },
                "E-5": { "from_node": "E", "to_node": "5", "is_natural":False },
                "6-F": { "from_node": "6", "to_node": "F", "is_natural":False },
                "A-B": { "from_node": "A", "to_node": "B", "is_natural":False },
                "B-C": { "from_node": "B", "to_node": "C", "is_natural":False },
                "D-E": { "from_node": "D", "to_node": "E", "is_natural":False },
                "E-F": { "from_node": "E", "to_node": "F", "is_natural":False },
                "G-B": { "from_node": "G", "to_node": "B", "is_natural":False },
                "B-H": { "from_node": "B", "to_node": "H", "is_natural":False },
                "I-D": { "from_node": "I", "to_node": "D", "is_natural":False },
                "J-D": { "from_node": "J", "to_node": "D", "is_natural":False },
            },
            "nodes": {
                "0":{"type":"",    "storage":True,"source":True           }, # storage
                "1":{"type":"",                   "source":True           }, # source
                "2":{"type":"",                   "source":True           }, # source
                "3":{"type":"",                   "source":True           }, # source
                "4":{"type":"",                   "source":True           }, # source
                "5":{"type":"",    "storage":True,"source":True           }, # storage"
                "6":{"type":"",                   "source":True           }, # source"
                "A":{"type":"",                                 "use":True}, # use"
                "B":{"type":"",                                           }, # 
                "C":{"type":"",                                 "use":True}, # use"
                "D":{"type":"",                                           }, # use"
                "E":{"type":"",                                           }, #
                "F":{"type":"",                                 "use":True}, #
                "G":{"type":"",    }, #
                "H":{"type":"",    }, #
                "I":{"type":"",    }, #
                "J":{"type":"",    }, #
            },
            "paths": {
                "0" : {"wrnum":"02-1", "priority":1, "cfs_limit":1, "from_nodes":["1"], "to_nodes":["A"], "forward_flowlines": ["1-A"], "backward_flowlines":[] },
                "1" : {"wrnum":"02-2", "priority":2, "cfs_limit":1, "from_nodes":["2"], "to_nodes":["C"], "forward_flowlines": ["2-B", "B-C"], "backward_flowlines":[] },
            },
            "measurements": {
                "0": {"flowlineId":"0-1",   "timeseries":[{"date":"2023-06-01", "value":1}]},
                "1": {"flowlineId":"5-6",   "timeseries":[{"date":"2023-06-01", "value":1}]},
                "2": {"nodeId":"0", "type":"measured_storage_change", "timeseries":[{"date":"2023-06-01", "value":0}] },
                "3": {"nodeId":"0", "type":"measured_storage_loss"  , "timeseries":[{"date":"2023-06-01", "value":0}] },
                "4": {"nodeId":"5", "type":"measured_storage_change", "timeseries":[{"date":"2023-06-01", "value":0}] },
                "5": {"nodeId":"5", "type":"measured_storage_loss"  , "timeseries":[{"date":"2023-06-01", "value":0}] },
            },
        }

        self.example_problem = ApportionmentProblem()
        self.example_problem.load_objects(self.example_data['flowlines'], self.example_data['nodes'], self.example_data['paths'], self.example_data['measurements'])


    def get_subset_containing_nodeId(self, zones, nodeId):
        """Helper function that retrieves the zone from the given dict of 
        zones that contains the given nodeId.
        """
        for id, zone in zones.items():
            if nodeId in zone.interior_nodes:
                return zone
        raise ValueError("Found no zone containing NodeId of {}. Did find {}.".format(
            nodeId, zone.interior_nodes))


    def init_problemA(self, paths=None, zones=None):
        """Initialize very simple problem input for subsequent testing.

        1 -*-> 2 -*-> 3
              / \
             *   * 
             |   |
             v   v
             2a  2b
        """

        # Set default values.

        if paths is None:
            paths = {
                "0" : {"wrnum":"02-1", "priority":1, "cfs_limit":1, "from_nodes":["2"], "to_nodes":["2a"], "forward_flowlines": ["2-2a"], "backward_flowlines":[] },
                "1" : {"wrnum":"02-2", "priority":1, "cfs_limit":1, "from_nodes":["2"], "to_nodes":["2b"], "forward_flowlines": ["2-2b"], "backward_flowlines":[] },
            }

        if zones is None:
            zones = [
                {
                    "id": "0",
                    "name": "Upstream Reach",
                    "type": "inflow",
                    "in_measurements": [],
                    "out_measurements": ["0"],
                    "accounts": [],
                },
                {
                    "id": "1",
                    "name": "Main Reach",
                    "type": "inflow",
                    "in_measurements": ["0"],
                    "out_measurements": ["1", "2"],
                    "accounts": ["1"],
                },
                {
                    "id": "2",
                    "name": "Use 2-2a",
                    "type": "outflow",
                    "in_measurements": ["2"],
                    "out_measurements": [],
                    "accounts": ["2"],
                },
            ]

        input = {
            "flowlines": {
                "1-2": { "from_node": "1", "to_node": "2", "is_natural":True },
                "2-3": { "from_node": "2", "to_node": "3", "is_natural":True },
                "2-2a": { "from_node": "2", "to_node": "2a", "is_natural":False },
                "2-2b": { "from_node": "2", "to_node": "2b", "is_natural":False },
            },
            "paths": paths,
            "nodes": {
                "1":{},
                "2":{},
                "3":{},
                "2a":{},
                "2b":{},
            },
            "measurements": {
                "0": {"flowlineId":"1-2",   "timeseries":[{"date":"2023-06-01", "value":5}]},
                "1": {"flowlineId":"2-3",   "timeseries":[{"date":"2023-06-01", "value":3}]},
                "2": {"flowlineId":"2-2a",  "timeseries":[{"date":"2023-06-01", "value":3}]},
            },
            "zones": zones
        }
        # Set things up...
        problem = ApportionmentProblem()
        problem.load_objects(input['flowlines'], input['nodes'], input['paths'], input['measurements'])
        problem.init_zones(input['zones'])

        return problem

    def init_problemB(self):
        """Initialize problem input slightly different from problem-A 
        for subsequent testing.



        INPUT W/ FORK         DESIRED SUBSET/OUTPUT
        1
        |                                 | 
        |                                 v  Inflow from another reach 
        *                              ______                            
        |      -*----2c               |      | ---> diversion (wr + unauth) 
        2__2b/                   ---> |      |                          
        |    \                 NF gain|______| ---> diversion (wr + unauth) 
        |      -*----2d        or loss    |                      
        *                                 v (Outflow to another reach)
        |                                                                  
        v
        3                                                                  

        """

        input = {
            "flowlines": {
                "1-2": { "from_node": "1", "to_node": "2", "is_natural":True },
                "2-3": { "from_node": "2", "to_node": "3", "is_natural":True },

                "2-2b": { "from_node": "2", "to_node": "2b", "is_natural":False },
                "2b-2c": { "from_node": "2b", "to_node": "2c", "is_natural":False },
                "2b-2d": { "from_node": "2b", "to_node": "2d", "is_natural":False },
            },
            "paths": {
                "WR-c" : {"wrnum":"02-1", "priority":1, "cfs_limit":   2, "from_nodes":["2"], "to_nodes":["2c"], "forward_flowlines": ["2-2b", "2b-2c"], "backward_flowlines":[] },
                "WR-d" : {"wrnum":"02-2", "priority":2, "cfs_limit":   4, "from_nodes":["2"], "to_nodes":["2d"], "forward_flowlines": ["2-2b", "2b-2d"], "backward_flowlines":[] },
            },
            "nodes": {
                "1":{},
                "2":{},
                "3":{},
                "2b":{},
                "2c":{},
                "2d":{},
            },
            "measurements": {
                "0": {"flowlineId":"1-2",   "timeseries":[{"date":"2023-06-01", "value":5}]},
                "1": {"flowlineId":"2-3",   "timeseries":[{"date":"2023-06-01", "value":3}]},
                "2": {"flowlineId":"2b-2c", "timeseries":[{"date":"2023-06-01", "value":4}]},
                "3": {"flowlineId":"2b-2d", "timeseries":[{"date":"2023-06-01", "value":4}]},
            },
            "zones":[
                {
                    "id":"0",
                    "name": "Main Reach",
                    "type": "inflow",
                    "in_measurements": ["0"],
                    "out_measurements": ["1", "2", "3"],
                    "accounts":[],
                },
                {
                    "id":"1",
                    "name": "Use At 2c",
                    "type": "outflow",
                    "in_measurements": ["2"],
                    "out_measurements": [],
                    "accounts":[],
                },
                {
                    "id":"2",
                    "name": "Use At 2d",
                    "type": "outflow",
                    "in_measurements": ["3"],
                    "out_measurements": [],
                    "accounts":[],
                }
            ]
        }

        # Set things up...
        problem = ApportionmentProblem()
        problem.load_objects(input['flowlines'], input['nodes'], input['paths'], input['measurements'])
        problem.init_zones(input['zones'])

        return problem



    # --- START OF TESTS ---

    def test_zones(self):
        """Given problem input with zones & accounts provided, the output 
        accounting should be expressed in terms of those zones & accounts."""
        problem = self.init_problemA()
        
        # Find the zone that contains node #2, and test if it is as expected.
        zone = self.get_subset_containing_nodeId(problem.zones, "2")
        found_cnt = len(zone.to_zones)
        self.assertEqual(found_cnt, 2, "There should be 2 outflows"
                         " from the zone around node #2 (one to node 3, another"
                         " to node 2a, none to node 2b because it is not"
                         " measured), but we found {}.\n"
                         "Here is the zone info: {}"
                         .format(found_cnt, zone))
  

    def test_fork_diversion(self):
        """Given a network that has two diversions forking from a single 
        diversion with measurements on the two prongs, the simplified zones 
        should show both diversions as originating from the source (rather 
        than from one diversion comming from the other diversion)."""
        problem = self.init_problemB()

        # Find the zone that contains node #2...
        # Then test it, specifically test if it has the 2 expected outflows.
        self.assertSetEqual(set(problem.zones["0"].to_zones), 
                            set(["-1", "1", "2"]) )


    def test_zone_with_handoff(self):
        """Given input involving a change, the zone containing the 'handoff 
        node' should include a reference to the handoff in its 'handoff'
        dict attribute."""

        problem = self.init_problemA(paths={
            "0" : {"wrnum":"02-1", "priority":1, "cfs_limit":1, "from_nodes":["2"], "to_nodes":["2a"], "forward_flowlines": ["2-2a"], "backward_flowlines":[] },
            "1-htf" : {"wrnum":"02-2", "priority":1, "cfs_limit":1, "from_nodes":["2"], "to_nodes":["2"], "forward_flowlines": [], "backward_flowlines":[], "to_change":"a123", },
            "1-ha" : {"wrnum":"02-2", "priority":10, "cfs_limit":1, "from_nodes":["2"], "to_nodes":["2a"], "forward_flowlines": ["2-2a"], "backward_flowlines":[], "from_change":"a123", },
        })

        self.assertEqual(len(problem.zones["1"].change_handoffs), 1)



# Run the tests
if __name__ == '__main__':
    pass
    #unittest.main()