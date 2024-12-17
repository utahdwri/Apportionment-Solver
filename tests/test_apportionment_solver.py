import unittest
from solver.apportionment_solver import ApportionmentSolver_v2

class ApportionmentTestCase(unittest.TestCase):
    """Extend with stuff I want for multiple tests."""

    def assertTransactionEqual(self, results, transaction_id, expected_value:float):
        """Given a result dictionary, check if the given transaction practically 
        equals the given expected value. """

        computed_value = results['TRXN_'+str(transaction_id)]
        decimal_places = 4
        self.assertAlmostEqual(computed_value, expected_value, decimal_places)


class TrivialProblems(ApportionmentTestCase):

    def test_simple_apportionments(self):
        """Does the measured diversion get apportioned correctly to its parts
        when the measured diversion is LESS than the sum of allowed rights?
        (I.e. the junior rights should not recieve a full apportionment.)
        """

        system = ApportionmentSolver_v2()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER', 'USER', 12)
        system.add_transaction(id=1, priority=1, limit= 3, path=['RIVER','USER'])
        system.add_transaction(id=2, priority=2, limit= 6, path=['RIVER','USER'])
        system.add_transaction(id=3, priority=3, limit=12, path=['RIVER','USER'])
        system.add_transaction(id=4, priority=4, limit= 4, path=['RIVER','USER'])
        results = system.solve()

        self.assertTransactionEqual(results, transaction_id=1, expected_value=3)
        self.assertTransactionEqual(results, transaction_id=2, expected_value=6)
        self.assertTransactionEqual(results, transaction_id=3, expected_value=3)
        self.assertTransactionEqual(results, transaction_id=4, expected_value=0)


    def test_simple_apportionments_2(self):
        """Does the measured diversion get apportioned correctly to its parts
        when the measured diversion is MORE than the sum of allowed rights?
        """

        system = ApportionmentSolver_v2()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER', 'USER', 100)
        system.add_transaction(id=1, priority=1, limit= 3, path=['RIVER','USER'])
        system.add_transaction(id=2, priority=2, limit= 6, path=['RIVER','USER'])
        system.add_transaction(id=3, priority=3, limit=12, path=['RIVER','USER'])
        system.add_transaction(id=4, priority=4, limit= 4, path=['RIVER','USER'])
        results = system.solve()

        self.assertTransactionEqual(results, transaction_id=1, expected_value=3)
        self.assertTransactionEqual(results, transaction_id=2, expected_value=6)
        self.assertTransactionEqual(results, transaction_id=3, expected_value=12)
        self.assertTransactionEqual(results, transaction_id=4, expected_value=4)


    def test_equal_priority_apportionments(self):
        """"""
        system = ApportionmentSolver_v2()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER', 'A', 10)
        system.add_reach_diversion('RIVER', 'B', 5)
        system.add_reach_diversion('RIVER', 'C', 6)
        system.add_transaction(id=1, priority=1, limit=1, path=['RIVER','A'])
        system.add_transaction(id=2, priority=1, limit=6, path=['RIVER','B'])
        system.add_transaction(id=3, priority=1, limit=6, path=['RIVER','C'])
        system.add_transaction(id=4, priority=1, limit=6, path=['RIVER','C'])
        results = system.solve()

        self.assertTransactionEqual(results, transaction_id=1, expected_value=1) #limited by water right
        self.assertTransactionEqual(results, transaction_id=2, expected_value=5) #limited by measured diversion
        self.assertTransactionEqual(results, transaction_id=3, expected_value=3) #limited by proportion
        self.assertTransactionEqual(results, transaction_id=4, expected_value=3) #limited by proportion


    def test_simple_reservoir(self):
        """Check if things are still good even if there is a reservoir."""

        system = ApportionmentSolver_v2()
        system.add_reach('RIVER')
        system.add_reach_reservoir('RIVER', 'STORAGE', 0)
        system.add_reach_diversion('RIVER', 'A', 10)
        system.add_reach_diversion('RIVER', 'B', 10)
        system.add_transaction(id=1, priority=1, limit=2, path=['RIVER','A'])
        system.add_transaction(id=2, priority=1, limit=4, path=['RIVER','B'])
        system.add_transaction(id=3, priority=2, limit=50, path=['RIVER','STORAGE'])
        system.add_transaction(id=4, priority=3, limit=None, path=['STORAGE','RIVER','A'])
        system.add_transaction(id=5, priority=3, limit=None, path=['STORAGE','RIVER','B'])
        results = system.solve()

        self.assertTransactionEqual(results, transaction_id=4, expected_value=8)
        self.assertTransactionEqual(results, transaction_id=5, expected_value=6)


    def test_simple_reservoir_with_downstream_gage(self):
        """Check if things are still good even if there is a reservoir that has
        a downstream measurement that constrains apporitonments to delieveries."""

        system = ApportionmentSolver_v2()
        system.add_reach('UPSTREAM')
        system.add_reach('DOWNSTREAM')
        system.add_reach_connection('UPSTREAM', 'DOWNSTREAM', 2)
        system.add_reach_reservoir('UPSTREAM', 'STORAGE', 0)
        system.add_reach_diversion('DOWNSTREAM', 'A', 10)
        system.add_reach_diversion('DOWNSTREAM', 'B', 10)
        system.add_transaction(id=1, priority=1, limit=2, path=['DOWNSTREAM','A'])
        system.add_transaction(id=2, priority=1, limit=4, path=['DOWNSTREAM','B'])
        system.add_transaction(id=3, priority=2, limit=50, path=['UPSTREAM','STORAGE'])
        system.add_transaction(id=4, priority=3, limit=None, path=['STORAGE','UPSTREAM','DOWNSTREAM','A'])
        system.add_transaction(id=5, priority=3, limit=None, path=['STORAGE','UPSTREAM','DOWNSTREAM','B'])
        results = system.solve()

        self.assertTransactionEqual(results, transaction_id=4, expected_value=1)
        self.assertTransactionEqual(results, transaction_id=5, expected_value=1)



class Reservoirs(ApportionmentTestCase):
    
    def reservoir_problem_input(self, stor_chg=None, stor_loss=None, Q1_2=None, Q2_200=None, Q3_4=None, Q4_400=None, Q5_500=None, Q5_6=None):
        """    
        1---#-->2------> \~~~3~~~/ ---#-->4----->5---#-->6
                #         \_____/         #      #
                |                         |      |
                v                         v      v
                200                       400    500

        """
        system = ApportionmentSolver_v2()
        system.add_reach('REACH-A')
        system.add_reach('REACH-B')
        system.add_reach('REACH-C')
        system.add_reach('REACH-D')
        system.add_reach_connection('REACH-A', 'REACH-B', Q1_2)
        system.add_reach_connection('REACH-B', 'REACH-C', Q3_4)
        system.add_reach_connection('REACH-C', 'REACH-D', Q5_6)
        system.add_reach_reservoir('REACH-B', 'STOR', stor_chg, stor_loss)
        system.add_reach_diversion('REACH-B', 'DIV-1', Q2_200)
        system.add_reach_diversion('REACH-C', 'DIV-2', Q4_400)
        system.add_reach_diversion('REACH-C', 'DIV-3', Q5_500)

        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'])
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'])
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'])
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'])
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'])
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'])

        return system
    
        #{
        #    "WR@2": {"wrnum":"02-1", "priority":1, "cfs_limit":   2, "from_nodes":["2"], "to_nodes":["200"], "forward_flowlines": ["2-200"            ], "backward_flowlines":[] },
        #    "WR@4": {"wrnum":"02-2", "priority":2, "cfs_limit":   4, "from_nodes":["4"], "to_nodes":["400"], "forward_flowlines": ["4-400"            ], "backward_flowlines":[] },
        #    "WR@5": {"wrnum":"02-3", "priority":3, "cfs_limit":   6, "from_nodes":["5"], "to_nodes":["500"], "forward_flowlines": ["5-500"            ], "backward_flowlines":[] },
        #    "WR@3": {"wrnum":"02-4", "priority":4, "cfs_limit":  20, "from_nodes":["3"], "to_nodes":["3"  ], "forward_flowlines": [                   ], "backward_flowlines":[], "to_account":"ReservoirA"},
        #    "SD@5": {"wrnum":"" , "priority":9900, "cfs_limit":None, "from_nodes":["3"], "to_nodes":["500"], "forward_flowlines": ["3-4","4-5","5-500"], "backward_flowlines":[], "from_account":"ReservoirA"  },
        #    "SD@4": {"wrnum":"" , "priority":9901, "cfs_limit":None, "from_nodes":["3"], "to_nodes":["400"], "forward_flowlines": ["3-4","4-400"      ], "backward_flowlines":[], "from_account":"ReservoirA"  },
        #}
        
    def assertApportionmentsEqual(self, results, expected):

        aliases = {'WR@2': 1, 'WR@4': 2, 'WR@5': 3, 'WR@3':4, 'SD@5':5, 'SD@4':6}

        for key in expected:
            trxn_id = key
            if key in aliases:
                trxn_id = aliases[key]
            self.assertTransactionEqual(results, trxn_id, expected[key])


    def test_trivial(self):
        """This is a prety trivial example, just to check the most simple case... should probably make some more interesting tests...
        """
        system = self.reservoir_problem_input(stor_chg=0, stor_loss=0, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=4, Q5_500=4, Q5_6=1)
        computed = system.solve()
        
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':0, 'SD@5':0, 'SD@4':0}
        self.assertApportionmentsEqual(computed, expected)

    def test_storage_diversions_with_no_deliveries(self):
        """Test storage diversions with no deliveries.
        """
        system = self.reservoir_problem_input(stor_chg=2, stor_loss=0, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=4, Q5_500=4, Q5_6=1)
        computed = system.solve()

        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':2, 'SD@5':0, 'SD@4':0} 
        self.assertApportionmentsEqual(computed, expected)

    def test_storage_deliveries_with_no_diversions(self):
        """Test storage deliveries with no diversions.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=6, Q5_500=4, Q5_6=1)
        computed = system.solve()
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':0, 'SD@5':0, 'SD@4':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_equal_priority_deliveries(self):
        """Test storage deliveries that have equal priority.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=6, Q5_500=4, Q5_6=1)
        
        # input['paths']['SD@4']['priority'] = input['paths']['SD@5']['priority']
        system.vars['TRXN_'+str(6)].priority = system.vars['TRXN_'+str(5)].priority

        computed = system.solve()
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':0, 'SD@5':0, 'SD@4':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_storage_deliveries_and_diversions_and_losses(self):
        """Test storage deliveries with diversions into storage at the same time, with some evaporative losses specified as well.
        """
        system = self.reservoir_problem_input(stor_chg=-1, stor_loss=1, Q1_2=5, Q2_200=2, Q3_4=7, Q4_400=6, Q5_500=4, Q5_6=1)
        computed = system.solve()
        expected = {'WR@2': 2, 'WR@4': 4, 'WR@5': 4, 'WR@3':2, 'SD@5':0, 'SD@4':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_equal_priority_apportionmnets(self):
        """Test equal priority apportionments with storage deliveries on top.
        """
        system = self.reservoir_problem_input(stor_chg=-4, stor_loss=1, Q1_2=2, Q2_200=1, Q3_4=4, Q4_400=2+2, Q5_500=3+1, Q5_6=1)
        #input['paths']['WR@2']['priority'] = 1 #2 cfs is limit
        #input['paths']['WR@4']['priority'] = 1 #4 cfs is limit
        #input['paths']['WR@5']['priority'] = 1 #6 cfs is limit
        system.vars['TRXN_'+str(1)].priority = 1
        system.vars['TRXN_'+str(2)].priority = 1
        system.vars['TRXN_'+str(3)].priority = 1

        computed = system.solve()
        expected = {'WR@2': 1, 'WR@4': 2, 'WR@5': 3, 'WR@3':0, 'SD@5':1, 'SD@4':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_equal_priority_apportionmnets2(self):
        """Test equal priority apportionments when the storage right also has an equal priority.

        For this case there is 4 cfs of gain above the reservoir (all of which is below the first gage).
        There is another 8 cfs of gain below the reservoir.

        """
        system = self.reservoir_problem_input(stor_chg=-4, stor_loss=1, Q1_2=0, Q2_200=1, Q3_4=6, Q4_400=7, Q5_500=7, Q5_6=0)
        #input['paths']['WR@2']['priority'] = 1 # 2 cfs is limit
        #input['paths']['WR@3']['priority'] = 1 #20 cfs is limit
        #input['paths']['WR@4']['priority'] = 1 # 4 cfs is limit
        #input['paths']['WR@5']['priority'] = 1 # 6 cfs is limit
        system.vars['TRXN_'+str(1)].priority = 1
        system.vars['TRXN_'+str(2)].priority = 1
        system.vars['TRXN_'+str(3)].priority = 1
        system.vars['TRXN_'+str(4)].priority = 1

        computed = system.solve()
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
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q1_2=0, Q2_200=0, Q3_4=2, Q4_400=6, Q5_500=8, Q5_6=0)
        # Change WR@2 so it delivers water downstream.
        #input['paths']['WR@2'] = {"wrnum":"02-1", "priority":1, "cfs_limit": 2, "from_nodes":["2"], "to_nodes":["500"], "forward_flowlines": ["2-3", "3-4", "4-400" ], "backward_flowlines":[] }
        system.vars['TRXN_'+str(1)].ub = 0
        system.add_transaction(id=101, priority=1, limit=2, path=['REACH-B', 'REACH-C', 'DIV-2'])

        computed = system.solve()
        expected = {101: 0, 'WR@4': 4, 'WR@5': 6, 'WR@3':0, 'SD@4':0, 'SD@5':2} 
        self.assertApportionmentsEqual(computed, expected)

    def test_change_water_that_is_available_at_htf_source(self):
        """Move water downstream when it is NOT available at the origional source but is available at the here-after source.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q1_2=2, Q2_200=0, Q3_4=4, Q4_400=6, Q5_500=8, Q5_6=0)
        
        # Change WR@2 so it delivers water downstream.
        #input['paths']['WR@2'] = {"wrnum":"02-1", "priority":1, "cfs_limit": 2, "from_nodes":["2"], "to_nodes":["500"], "forward_flowlines": ["2-3", "3-4", "4-400" ], "backward_flowlines":[] }
        system.vars['TRXN_'+str(1)].ub = 0
        system.add_transaction(id=101, priority=1, limit=2, path=['REACH-B', 'REACH-C', 'DIV-2'])

        computed = system.solve()
        expected = {101: 2, 'WR@4': 4, 'WR@5': 6, 'WR@3':0, 'SD@4':0, 'SD@5':2} 
        self.assertApportionmentsEqual(computed, expected)
        ## Add a possible storage delivery to the diversion at node 2. 
        #input['paths']['SD@2'] = {"wrnum":"" , "priority":9903, "cfs_limit":None, "from_nodes":["3"], "to_nodes":["2"], "forward_flowlines": ["2-200"], "backward_flowlines":["2-3"], "from_account":"ReservoirA"  }

    def test_spill_to_natural_flow(self):
        """What if the reservoir releases water that is not picked up?
        """
        system = self.reservoir_problem_input(stor_chg=-5, stor_loss=0, Q1_2=0, Q2_200=2, Q3_4=5, Q4_400=0, Q5_500=0, Q5_6=5)
        computed = system.solve()
        expected = {'WR@2': 2, 'WR@4': 0, 'WR@5': 0, 'WR@3':0, 'SD@5':0, 'SD@4':0} 
        self.assertApportionmentsEqual(computed, expected)

    def test_storage_diversion_exceeds_storage_right(self):
        """In practice, I doubt this is very important, but I still want the program to support this case.
        """
        system = self.reservoir_problem_input(stor_chg=25, stor_loss=0, Q1_2=0, Q2_200=0, Q3_4=0, Q4_400=0, Q5_500=0, Q5_6=0)
        computed = system.solve()
        expected = {'WR@2': 0, 'WR@4': 0, 'WR@5': 0, 'WR@3':20, 'SD@5':0, 'SD@4':0} 
        self.assertApportionmentsEqual(computed, expected)



    def test_presentation_example(self):
        """
        """
        system = self.reservoir_problem_input(stor_chg=-10, stor_loss=0, Q1_2=0, Q2_200=0, Q3_4=20, Q4_400=15, Q5_500=10, Q5_6=5)
        #input['paths']['WR@4']['cfs_limit'] = 5
        #input['paths']['WR@5']['cfs_limit'] = 2
        #input['paths']['WR@3']['cfs_limit'] = 100
        system.vars['TRXN_'+str(2)].ub = 5
        system.vars['TRXN_'+str(3)].ub = 2
        system.vars['TRXN_'+str(4)].ub = 100

        computed = system.solve()
        expected = {'WR@2': 0, 'WR@3':8, 'WR@4': 5, 'WR@5': 2, 'SD@4':10, 'SD@5':8} 
        self.assertApportionmentsEqual(computed, expected)



class Changes(ApportionmentTestCase):

    def assertApportionmentsEqual(self, results, expected):
        for key in expected:
            trxn_id = key
            self.assertTransactionEqual(results, trxn_id, expected[key])

    def reservoir_plus_problem_input(self,
                stor_chg=None, stor_loss=None, 
                Q_AC=None, Q_BC=None, Q_CD=None, Q_DE=None, Q_EF=None, 
                Q_DIV1=None, Q_DIV2=None, Q_DIV3=None, Q_DIV4=None):
        """    
              DIV-1
                ^
       REACH-A  |
         ---------->#---
                        |
       REACH-B          v  REACH-C     REACH-D  REACH-E   REACH-F
         ---------->#--> \~~~~~~~/ -->#------->#-------->#--->
                #         \_____/         #        #        
                |                         |        |        
                v                         v        v        
              DIV-2         STOR        DIV-3    DIV-4      

        """

        system = ApportionmentSolver_v2()
        system.add_reach('REACH-A')
        system.add_reach('REACH-B')
        system.add_reach('REACH-C')
        system.add_reach('REACH-D')
        system.add_reach('REACH-E')
        system.add_reach('REACH-F')
        system.add_reach_connection('REACH-A', 'REACH-C', Q_AC)
        system.add_reach_connection('REACH-B', 'REACH-C', Q_BC)
        system.add_reach_connection('REACH-C', 'REACH-D', Q_CD)
        system.add_reach_connection('REACH-D', 'REACH-E', Q_DE)
        system.add_reach_connection('REACH-E', 'REACH-F', Q_EF)
        system.add_reach_diversion('REACH-A', 'DIV-1', Q_DIV1)
        system.add_reach_diversion('REACH-B', 'DIV-2', Q_DIV2)
        system.add_reach_reservoir('REACH-C', 'STOR', stor_chg, stor_loss)
        system.add_reach_diversion('REACH-D', 'DIV-3', Q_DIV3)
        system.add_reach_diversion('REACH-E', 'DIV-4', Q_DIV4)

        return system


    def alt_reservoir_problem_input(self,
                    stor_chg=None, stor_loss=None, 
                    Q_AB=None, Q_BC=None, Q_CD=None,
                    Q_DIV1=None, Q_DIV2=None):
        """
                
           REACH-A        REACH-B      REACH-C  REACH-D
         ---------->#--- \~~~~~~~/ -->#------>#-------->
                #         \_____/         #        
                |                         |        
                v                         v        
              DIV-1        STOR         DIV-2    

        """

        system = ApportionmentSolver_v2()
        system.add_reach('REACH-A')
        system.add_reach('REACH-B')
        system.add_reach('REACH-C')
        system.add_reach('REACH-D')
        system.add_reach_connection('REACH-A', 'REACH-B', Q_AB)
        system.add_reach_connection('REACH-B', 'REACH-C', Q_BC)
        system.add_reach_connection('REACH-C', 'REACH-D', Q_CD)
        system.add_reach_diversion('REACH-A', 'DIV-1', Q_DIV1)
        system.add_reach_reservoir('REACH-B', 'STOR', stor_chg, stor_loss)
        system.add_reach_diversion('REACH-C', 'DIV-2', Q_DIV2)

        return system



    def test_1(self):
        """ First test some simple example paramters. 

              DIV-1
                ^2
       REACH-A  |   0
         ---------->#---
                        |
       REACH-B      0   v  REACH-C     REACH-D  REACH-E   REACH-F
         ---------->#--> \~~~~~~~/ -->#------->#-------->#--->
                #         \_____/         #4       #2
                |                         |        |        
                v2                        v        v        
              DIV-2         STOR        DIV-3    DIV-4      

        """
        system = self.reservoir_plus_problem_input(stor_chg=0, stor_loss=0, 
                               Q_AC=0, Q_BC=0, Q_CD=2, Q_DE=0, Q_EF=0, 
                               Q_DIV1=2, Q_DIV2=2, Q_DIV3=4, Q_DIV4=2)
        system.add_transaction(id=1, priority=1870, limit=   5, path=['REACH-A','DIV-1'])
        system.add_transaction(id=2, priority=1885, limit=   5, path=['REACH-B','DIV-2'])
        system.add_transaction(id=3, priority=1900, limit=   2, path=['REACH-E','DIV-4'])
        system.add_transaction(id=4, priority=1910, limit=   5, path=['REACH-D','DIV-3'])
        system.add_transaction(id=5, priority=1950, limit=  50, path=['REACH-C','STOR'])
        system.add_transaction(id=6, priority=9900, limit=None, path=['STOR','REACH-C','REACH-D','DIV-3'])
        system.add_transaction(id=7, priority=9901, limit=None, path=['STOR','REACH-C','REACH-D','REACH-E','DIV-4'])
        computed = system.solve()


        expected = {
            1: 2, # all of DIV-1
            2: 2, # all of DIV-2
            5: 0, # there was no change in storage, so DIV-STOR = deliveries
            4: 4, # 
            3: 2, 
            6: 0, 
            7: 0 
        } 
        self.assertApportionmentsEqual(computed, expected)

    def test_2(self):
        """ Same as above, but adding a downstream-moving change, from div@2 -> div@7. """

        # Increased the measured flows because of the delivery.
        system = self.reservoir_plus_problem_input(stor_chg=0, stor_loss=0, 
                               Q_AC=0+2, Q_BC=0, Q_CD=2+2, Q_DE=0+2, Q_EF=0, 
                               Q_DIV1=2-2, Q_DIV2=2, Q_DIV3=4, Q_DIV4=2+2)
        system.add_handoff('REACH-A', 'a123')

        system.add_transaction(id= 1, priority=1870, limit=   5, path=['REACH-A','a123']) # HA transaction
        system.add_transaction(id= 2, priority=1885, limit=   5, path=['REACH-B','DIV-2'])
        system.add_transaction(id= 3, priority=1900, limit=   2, path=['REACH-E','DIV-4'])
        system.add_transaction(id= 4, priority=1910, limit=   5, path=['REACH-D','DIV-3'])
        system.add_transaction(id= 5, priority=1950, limit=  50, path=['REACH-C','STOR'])
        system.add_transaction(id= 6, priority=9900, limit=None, path=['STOR','REACH-C','REACH-D','DIV-3'])
        system.add_transaction(id= 7, priority=9901, limit=None, path=['STOR','REACH-C','REACH-D','REACH-E','DIV-4'])
        system.add_transaction(id=11, priority=9901, limit=None, path=['a123','REACH-A','REACH-C','REACH-D','REACH-E','DIV-4']) # HA transaction
        

        # Solve and check.
        computed = system.solve()
        expected = {
            1: 2, # same as above
            2: 2, 
            5: 0, 
            4: 4, 
            3: 2, 
            6: 0, 
            7: 0, 
            11:2
        } 
        self.assertApportionmentsEqual(computed, expected)

    
    def test_3(self):
        """
        
        """
        system = self.alt_reservoir_problem_input(stor_chg=0, stor_loss=0, 
                               Q_AB=2, Q_BC=2, Q_CD=0, Q_DIV1=0, Q_DIV2=4)
        system.add_handoff('REACH-A', 'REACH-A-a123')

        system.add_transaction(id=1, priority=1870, limit=  10, path=['REACH-A', 'REACH-A-a123']) # HTF
        system.add_transaction(id=2, priority=1885, limit=   2, path=['REACH-C', 'DIV-2'])
        system.add_transaction(id=3, priority=1950, limit=  50, path=['REACH-B', 'STOR'])
        system.add_transaction(id=4, priority=9900, limit=None, path=['STOR', 'REACH-B', 'REACH-C', 'DIV-2'])
        system.add_transaction(id=10, priority=2015, limit=  10, path=['REACH-A-a123', 'REACH-A', 'REACH-B', 'REACH-C', 'DIV-2']) # HA change
   
        # Solve and check.
        computed = system.solve()
        expected = {
            1: 2, # same as above
            2: 2, 
            4: 0, 
            10:2
        } 
        self.assertApportionmentsEqual(computed, expected)



    def test_change_application_accounting_question(self):
        """This is based on an example problem in this google doc:
        https://docs.google.com/document/d/1TBD5BMyy6aycwFimGeNxlE-ozBxTOfrLR0EpvBQttV0/edit#heading=h.908q10f6uovo
        

        REACH-A     REACH-B         REACH-C     REACH-D
        ----\_/-->#------------>#------------>#--------->6
                      |      |      |      |      
            STOR    DIV-1  DIV-2  DIV-3  DIV-4   
                     1900   1920   1940   1930 
                     chg    shares shares 1950 chg (on 1900)


        """

        system = ApportionmentSolver_v2()
        system.add_reach('REACH-A')
        system.add_reach('REACH-B')
        system.add_reach('REACH-C')
        system.add_reach_connection('REACH-A', 'REACH-B', 5)
        system.add_reach_connection('REACH-B', 'REACH-C', 5)
        system.add_reach_reservoir('REACH-A', 'STOR', -5, 0)
        system.add_handoff('REACH-B', 'REACH-B-CHG')
        system.add_reach_diversion('REACH-B', 'DIV-2', 5)
        system.add_reach_diversion('REACH-C', 'DIV-3', 5)
        system.add_reach_diversion('REACH-C', 'DIV-4', 5)

        system.add_transaction(id=1, priority=1900, limit=10, path=['REACH-B', 'REACH-B-CHG'])
        system.add_transaction(id=2, priority=1920, limit=10, path=['REACH-B', 'DIV-2'])
        system.add_transaction(id=3, priority=1930, limit=10, path=['REACH-C', 'DIV-4'])
        system.add_transaction(id=4, priority=1940, limit=10, path=['REACH-C', 'DIV-3'])
        system.add_transaction(id=5, priority=1950, limit=10, path=['REACH-B-CHG', 'REACH-B', 'REACH-C', 'DIV-4'])
        system.add_transaction(id=6, priority=9998, limit=10, path=['STOR', 'REACH-A', 'REACH-B', 'DIV-2'])
        system.add_transaction(id=7, priority=9999, limit=10, path=['STOR', 'REACH-A', 'REACH-B', 'REACH-C', 'DIV-3'])
        # "C_1900" : {"wrnum":"02-1", "priority":1900, "cfs_limit":10, "from_nodes":["2"], "to_nodes":["2"], "forward_flowlines": [], "backward_flowlines":[], "to_change":"a1"},
        # "A_1920" : {"wrnum":"02-2", "priority":1920, "cfs_limit":10, "from_nodes":["3"], "to_nodes":["A"], "forward_flowlines": ["3-A"], "backward_flowlines":[] },
        # "C_1930" : {"wrnum":"02-3", "priority":1930, "cfs_limit":10, "from_nodes":["5"], "to_nodes":["C"], "forward_flowlines": ["5-C"], "backward_flowlines":[] },
        # "B_1940" : {"wrnum":"02-4", "priority":1940, "cfs_limit":10, "from_nodes":["4"], "to_nodes":["B"], "forward_flowlines": ["4-B"], "backward_flowlines":[] },
        # "C_1950" : {"wrnum":"02-5", "priority":1950, "cfs_limit":10, "from_nodes":["2"], "to_nodes":["C"], "forward_flowlines": ["2-3","3-4","4-5","5-C"], "backward_flowlines":[] , "from_change":"a1"},
        # "A_stor" : {"wrnum":""    , "priority":9998, "cfs_limit":10, "from_nodes":["1"], "to_nodes":["A"], "forward_flowlines": ["1-2","2-3","3-A"], "backward_flowlines":[] },
        # "B_stor" : {"wrnum":""    , "priority":9999, "cfs_limit":10, "from_nodes":["1"], "to_nodes":["B"], "forward_flowlines": ["1-2","2-3","3-4","4-B"], "backward_flowlines":[] },
            


        # Solve and check.
        computed = system.solve()
        expected = {
            1: 0,
            2: 5,
            3: 5,
            4: 0,
            5: 0,
            6: 0,
            7: 5,
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


