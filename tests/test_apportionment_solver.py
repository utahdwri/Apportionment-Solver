import unittest
from solver.apportionment_solver import ApportionmentSolver_v2


class A_SingleReachProblems(unittest.TestCase):

    def test_simple_apportionments(self):
        """Does the measured diversion get apportioned correctly to its parts
        when the measured diversion is LESS than the sum of allowed rights?
        (I.e. the junior rights should not recieve a full apportionment.)
        """

        system = ApportionmentSolver_v2()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER', 'USER', 12)
        system.add_transaction(id=1, priority=1, limit= 3, path=['RIVER','USER'], expected_value=3)
        system.add_transaction(id=2, priority=2, limit= 6, path=['RIVER','USER'], expected_value=6)
        system.add_transaction(id=3, priority=3, limit=12, path=['RIVER','USER'], expected_value=3)
        system.add_transaction(id=4, priority=4, limit= 4, path=['RIVER','USER'], expected_value=0)
        
        system.solve()
        system.assert_variables_equal_expected()


    def test_simple_apportionments_2(self):
        """Does the measured diversion get apportioned correctly to its parts
        when the measured diversion is MORE than the sum of allowed rights?
        """

        system = ApportionmentSolver_v2()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER', 'USER', 100)
        system.add_transaction(id=1, priority=1, limit= 3, path=['RIVER','USER'], expected_value=3)
        system.add_transaction(id=2, priority=2, limit= 6, path=['RIVER','USER'], expected_value=6)
        system.add_transaction(id=3, priority=3, limit=12, path=['RIVER','USER'], expected_value=12)
        system.add_transaction(id=4, priority=4, limit= 4, path=['RIVER','USER'], expected_value=4)

        system.solve()
        system.assert_variables_equal_expected()


    def test_equal_priority_apportionments(self):
        """"""
        system = ApportionmentSolver_v2()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER', 'A', 10)
        system.add_reach_diversion('RIVER', 'B', 5)
        system.add_reach_diversion('RIVER', 'C', 6)
        system.add_transaction(id=1, priority=1, limit=1, path=['RIVER','A'], expected_value=1) #limited by water right
        system.add_transaction(id=2, priority=1, limit=6, path=['RIVER','B'], expected_value=5) #limited by measured diversion
        system.add_transaction(id=3, priority=1, limit=6, path=['RIVER','C'], expected_value=3) #limited by proportion
        system.add_transaction(id=4, priority=1, limit=6, path=['RIVER','C'], expected_value=3) #limited by proportion

        system.solve()
        system.assert_variables_equal_expected()

    def test_reach_with_negative_storage_change(self):
        """"""
        system = ApportionmentSolver_v2()
        system.add_reach('RIVER', storage_chg=-10, # This means the reach has some extra bank sotrage (or similar) that was released
                         expected_gain=0)
        system.add_reach_diversion('RIVER', 'A', 10)

        system.solve()
        system.assert_variables_equal_expected()

    def test_reach_with_positive_storage_change(self):
        """"""
        system = ApportionmentSolver_v2()
        system.add_reach('RIVER', storage_chg=2, # This means the reach soaked up some of the water 
                         expected_gain=12)
        system.add_reach_diversion('RIVER', 'A', 10)

        system.solve()
        system.assert_variables_equal_expected()

class B_Reservoirs(unittest.TestCase):
    
    def reservoir_problem_input(self, stor_chg=None, stor_loss=None, Q_AB=None, 
                                Q_DIV1=None, Q_BC=None, Q_DIV2=None, Q_DIV3=None, 
                                Q_CD=None):
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
        system.add_connection('REACH-A', 'REACH-B', Q_AB)
        system.add_connection('REACH-B', 'REACH-C', Q_BC)
        system.add_connection('REACH-C', 'REACH-D', Q_CD)
        system.add_reach_reservoir('REACH-B', 'STOR', stor_chg, stor_loss)
        system.add_reach_diversion('REACH-B', 'DIV-1', Q_DIV1)
        system.add_reach_diversion('REACH-C', 'DIV-2', Q_DIV2)
        system.add_reach_diversion('REACH-C', 'DIV-3', Q_DIV3)

        return system
    



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
        system.add_transaction(id=4, priority=3, limit=None, path=['STORAGE','RIVER','A'], expected_value=8)
        system.add_transaction(id=5, priority=3, limit=None, path=['STORAGE','RIVER','B'], expected_value=6)

        system.solve()
        system.assert_variables_equal_expected()

    def test_simple_reservoir_with_downstream_gage(self):
        """Check if things are still good even if there is a reservoir that has
        a downstream measurement that constrains apporitonments to delieveries."""

        system = ApportionmentSolver_v2()
        system.add_reach('UPSTREAM')
        system.add_reach('DOWNSTREAM')
        system.add_connection('UPSTREAM', 'DOWNSTREAM', 2)
        system.add_reach_reservoir('UPSTREAM', 'STORAGE', 0)
        system.add_reach_diversion('DOWNSTREAM', 'A', 10)
        system.add_reach_diversion('DOWNSTREAM', 'B', 10)
        system.add_transaction(id=1, priority=1, limit=2, path=['DOWNSTREAM','A'])
        system.add_transaction(id=2, priority=1, limit=4, path=['DOWNSTREAM','B'])
        system.add_transaction(id=3, priority=2, limit=50, path=['UPSTREAM','STORAGE'])
        system.add_transaction(id=4, priority=3, limit=None, path=['STORAGE','UPSTREAM','DOWNSTREAM','A'], expected_value=1)
        system.add_transaction(id=5, priority=3, limit=None, path=['STORAGE','UPSTREAM','DOWNSTREAM','B'], expected_value=1)

        system.solve()
        system.assert_variables_equal_expected()


    def test_trivial(self):
        """This is a prety trivial example, just to check the most simple case... should probably make some more interesting tests...
        """
        system = self.reservoir_problem_input(stor_chg=0, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=4, Q_DIV3=4, Q_CD=1)
        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=2)
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'], expected_value=4)
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'], expected_value=4)
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=0)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=0)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()

    def test_storage_diversions_with_no_deliveries(self):
        """Test storage diversions with no deliveries.
        """
        system = self.reservoir_problem_input(stor_chg=2, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=4, Q_DIV3=4, Q_CD=1)
        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=2)
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'], expected_value=4)
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'], expected_value=4)
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=2)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=0)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()

    def test_storage_deliveries_with_no_diversions(self):
        """Test storage deliveries with no diversions.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=6, Q_DIV3=4, Q_CD=1)
        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=2)
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'], expected_value=4)
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'], expected_value=4)
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=0)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=0)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=2)

        system.solve()
        system.assert_variables_equal_expected()
        
    def test_equal_priority_deliveries(self):
        """Test storage deliveries that have equal priority.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=6, Q_DIV3=4, Q_CD=1)

        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=2)
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'], expected_value=4)
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'], expected_value=4)
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=0)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=0)
        system.add_transaction(id=6, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=2) # changed priority
        
        system.solve()
        system.assert_variables_equal_expected()
        
    def test_storage_deliveries_and_diversions_and_losses(self):
        """Test storage deliveries with diversions into storage at the same time, with some evaporative losses specified as well.
        """
        system = self.reservoir_problem_input(stor_chg=-1, stor_loss=1, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=6, Q_DIV3=4, Q_CD=1)

        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=2)
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'], expected_value=4)
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'], expected_value=4)
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=2)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=0)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=2)

        system.solve()
        system.assert_variables_equal_expected()

    def test_equal_priority_apportionmnets(self):
        """Test equal priority apportionments with storage deliveries on top.
        """
        system = self.reservoir_problem_input(stor_chg=-4, stor_loss=1, Q_AB=2, Q_DIV1=1, Q_BC=4, Q_DIV2=2+2, Q_DIV3=3+1, Q_CD=1)

        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=1)
        system.add_transaction(id=2, priority=   1, limit=   4, path=['REACH-C','DIV-2'], expected_value=2) # changed priority
        system.add_transaction(id=3, priority=   1, limit=   6, path=['REACH-C','DIV-3'], expected_value=3) # changed priority
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=0)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=1)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=2)

        system.solve()
        system.assert_variables_equal_expected()

    def test_equal_priority_apportionmnets2(self):
        """Test equal priority apportionments when the storage right also has an equal priority.

        For this case there is 4 cfs of gain above the reservoir (all of which is below the first gage).
        There is another 8 cfs of gain below the reservoir.

        """
        system = self.reservoir_problem_input(stor_chg=-4, stor_loss=1, Q_AB=0, Q_DIV1=1, Q_BC=6, Q_DIV2=7, Q_DIV3=7, Q_CD=0)

        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=1)
        system.add_transaction(id=2, priority=   1, limit=   4, path=['REACH-C','DIV-2'], expected_value=8/10*4)
        system.add_transaction(id=3, priority=   1, limit=   6, path=['REACH-C','DIV-3'], expected_value=8/10*6)
        system.add_transaction(id=4, priority=   1, limit=  20, path=['REACH-B','STOR'], expected_value=3)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=7 - 8/10*6)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=7 - 8/10*4)

        system.solve()
        system.assert_variables_equal_expected()

    def test_change_water_that_is_not_available_at_htf_source(self):
        """Move water downstream when it is NOT available at the origional source but is available at the here-after source.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=0, Q_DIV1=0, Q_BC=2, Q_DIV2=6, Q_DIV3=8, Q_CD=0)

        #system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'])
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'], expected_value=4)
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'], expected_value=6)
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=0)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=2)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=0)
        # Change 1 so it delivers water downstream.
        system.add_transaction(id=101, priority=1, limit=2, path=['REACH-B', 'REACH-C', 'DIV-2'], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()

    def test_change_water_that_is_available_at_htf_source(self):
        """Move water downstream when it is NOT available at the origional source but is available at the here-after source.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=2, Q_DIV1=0, Q_BC=4, Q_DIV2=6, Q_DIV3=8, Q_CD=0)

        system.add_transaction(id=101, priority=1, limit=2, path=['REACH-B', 'REACH-C', 'DIV-2'], expected_value=2)# Change 1 so it delivers water downstream.
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'], expected_value=4)
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'], expected_value=6)
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=0)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=2)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=0)
        
        system.solve()
        system.assert_variables_equal_expected()
        
    def test_spill_to_natural_flow(self):
        """What if the reservoir releases water that is not picked up?
        """
        system = self.reservoir_problem_input(stor_chg=-5, stor_loss=0, Q_AB=0, Q_DIV1=2, Q_BC=5, Q_DIV2=0, Q_DIV3=0, Q_CD=5)

        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=2)
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'], expected_value=0)
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'], expected_value=0)
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=0)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=0)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()

    def test_storage_diversion_exceeds_storage_right(self):
        """In practice, I doubt this is very important, but I still want the program to support this case.
        """
        system = self.reservoir_problem_input(stor_chg=25, stor_loss=0, Q_AB=0, Q_DIV1=0, Q_BC=0, Q_DIV2=0, Q_DIV3=0, Q_CD=0)

        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=0)
        system.add_transaction(id=2, priority=   2, limit=   4, path=['REACH-C','DIV-2'], expected_value=0)
        system.add_transaction(id=3, priority=   3, limit=   6, path=['REACH-C','DIV-3'], expected_value=0)
        system.add_transaction(id=4, priority=   4, limit=  20, path=['REACH-B','STOR'], expected_value=20)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=0)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()


    def test_presentation_example(self):
        """
        """
        system = self.reservoir_problem_input(stor_chg=-10, stor_loss=0, Q_AB=0, Q_DIV1=0, Q_BC=20, Q_DIV2=15, Q_DIV3=10, Q_CD=5)

        system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'], expected_value=0)
        system.add_transaction(id=2, priority=   2, limit=   5, path=['REACH-C','DIV-2'], expected_value=5)
        system.add_transaction(id=3, priority=   3, limit=   2, path=['REACH-C','DIV-3'], expected_value=2)
        system.add_transaction(id=4, priority=   4, limit= 100, path=['REACH-B','STOR'], expected_value=8)
        system.add_transaction(id=5, priority=9900, limit=None, path=['STOR','REACH-B','REACH-C','DIV-3'], expected_value=8)
        system.add_transaction(id=6, priority=9901, limit=None, path=['STOR','REACH-B','REACH-C','DIV-2'], expected_value=10)

        system.solve()
        system.assert_variables_equal_expected()

    def test_water_rights_cant_steal_storage_water(self):
        """Water in a storage reservoir should not be "splilled" to augment 
        the natural source and therefor allow a direct flow right to recieve 
        a larger apportionment."""

        system = ApportionmentSolver_v2()
        system.add_reach('S')
        system.add_reach_reservoir('S', 'R', storage_chg=-5, storage_loss=0)
        system.add_reach_diversion('S', 'A', flow=5)

        system.add_transaction(id=1, priority=1900, limit=5, path=['S', 'A'], expected_value=0)
        system.add_transaction(id=2, priority=1950, limit=100, path=['S', 'R'], expected_value=0)
        system.add_transaction(id=3, priority=9999, limit=None, path=['R', 'S', 'A'], expected_value=5)

        system.solve()
        system.assert_variables_equal_expected()

        '''
        This result: {TRXN-1: 5, TRXN-2: 0, TRXN-3: 0} 
        would indicate that when the apportionment to trxn #1 is maximized 
        sorage is being pulled out of the reservoir to create divertable flow.
        This should not happen. 
        '''
        
class B_Imports(unittest.TestCase):
    def test_1(self):
        """ """

        system = ApportionmentSolver_v2()
        system.add_reach('S')
        system.add_reach_import('S', 'IMP', flow=5)
        system.add_reach_diversion('S', 'DIV', flow=5)

        system.add_transaction(id=1, priority=10, limit=5, path=['IMP', 'S', 'DIV'], expected_value=5)
        system.add_transaction(id=2, priority=2, limit=5, path=['S', 'DIV'], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()


    def test_unmeasured_import(self):
        """Imports really should be measured. But if they are not, then we
        would expect that the import transaction should be less than the 
        observed gain in the reach. """

        system = ApportionmentSolver_v2()
        system.add_reach('UPPER-REACH')
        system.add_reach('LOWER-REACH')
        system.add_connection('UPPER-REACH', 'LOWER-REACH', 5)
        system.add_reach_diversion('LOWER-REACH', 'A', 8)
        system.add_transaction(id=1, priority=1, limit=10, path=['LOWER-REACH_GAINS','LOWER-REACH','A'],
                               expected_value=3) # This should be limited to the gains in the lower reach
        system.add_transaction(id=2, priority=2, limit=10, path=['LOWER-REACH','A'],
                               expected_value=5) # This should be the remaining diversion

        system.solve()
        system.assert_variables_equal_expected()


class C_Changes(unittest.TestCase):

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
        system.add_connection('REACH-A', 'REACH-C', Q_AC)
        system.add_connection('REACH-B', 'REACH-C', Q_BC)
        system.add_connection('REACH-C', 'REACH-D', Q_CD)
        system.add_connection('REACH-D', 'REACH-E', Q_DE)
        system.add_connection('REACH-E', 'REACH-F', Q_EF)
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
        system.add_connection('REACH-A', 'REACH-B', Q_AB)
        system.add_connection('REACH-B', 'REACH-C', Q_BC)
        system.add_connection('REACH-C', 'REACH-D', Q_CD)
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
        system.add_transaction(id=1, priority=1870, limit=   5, path=['REACH-A','DIV-1'], expected_value=2)
        system.add_transaction(id=2, priority=1885, limit=   5, path=['REACH-B','DIV-2'], expected_value=2)
        system.add_transaction(id=3, priority=1900, limit=   2, path=['REACH-E','DIV-4'])
        system.add_transaction(id=4, priority=1910, limit=   5, path=['REACH-D','DIV-3'], expected_value=4)
        system.add_transaction(id=5, priority=1950, limit=  50, path=['REACH-C','STOR'], expected_value=0)
        system.add_transaction(id=6, priority=9900, limit=None, path=['STOR','REACH-C','REACH-D','DIV-3'], expected_value=0)
        system.add_transaction(id=7, priority=9901, limit=None, path=['STOR','REACH-C','REACH-D','REACH-E','DIV-4'], expected_value=0)
        
        system.solve()
        system.assert_variables_equal_expected()

    def test_2(self):
        """ Same as above, but adding a downstream-moving change, from div@2 -> div@7. """

        # Increased the measured flows because of the delivery.
        system = self.reservoir_plus_problem_input(stor_chg=0, stor_loss=0, 
                               Q_AC=0+2, Q_BC=0, Q_CD=2+2, Q_DE=0+2, Q_EF=0, 
                               Q_DIV1=2-2, Q_DIV2=2, Q_DIV3=4, Q_DIV4=2+2)
        system.add_handoff('REACH-A', 'a123')

        system.add_transaction(id= 1, priority=1870, limit=   5, path=['REACH-A','a123'], expected_value=2) # HA transaction
        system.add_transaction(id= 2, priority=1885, limit=   5, path=['REACH-B','DIV-2'], expected_value=2)
        system.add_transaction(id= 3, priority=1900, limit=   2, path=['REACH-E','DIV-4'], expected_value=2)
        system.add_transaction(id= 4, priority=1910, limit=   5, path=['REACH-D','DIV-3'], expected_value=4)
        system.add_transaction(id= 5, priority=1950, limit=  50, path=['REACH-C','STOR'], expected_value=0)
        system.add_transaction(id= 6, priority=9900, limit=None, path=['STOR','REACH-C','REACH-D','DIV-3'], expected_value=0)
        system.add_transaction(id= 7, priority=9901, limit=None, path=['STOR','REACH-C','REACH-D','REACH-E','DIV-4'], expected_value=0)
        system.add_transaction(id=11, priority=9901, limit=None, path=['a123','REACH-A','REACH-C','REACH-D','REACH-E','DIV-4'], expected_value=2) # HA transaction
        
        system.solve()
        system.assert_variables_equal_expected()

    
    def test_3(self):
        """
        
        """
        system = self.alt_reservoir_problem_input(stor_chg=0, stor_loss=0, 
                               Q_AB=2, Q_BC=2, Q_CD=0, Q_DIV1=0, Q_DIV2=4)
        system.add_handoff('REACH-A', 'REACH-A-a123')

        system.add_transaction(id=1, priority=1870, limit=  10, path=['REACH-A', 'REACH-A-a123'], expected_value=2) # HTF
        system.add_transaction(id=2, priority=1885, limit=   2, path=['REACH-C', 'DIV-2'], expected_value=2)
        system.add_transaction(id=3, priority=1950, limit=  50, path=['REACH-B', 'STOR'])
        system.add_transaction(id=4, priority=9900, limit=None, path=['STOR', 'REACH-B', 'REACH-C', 'DIV-2'], expected_value=0)
        system.add_transaction(id=10, priority=2015, limit=  10, path=['REACH-A-a123', 'REACH-A', 'REACH-B', 'REACH-C', 'DIV-2'], expected_value=2) # HA change
   
        system.solve()
        system.assert_variables_equal_expected()


    def test_change_that_depends_on_delivery(self):
        """I'm not sure what the correct answer is for this one."""

        system = ApportionmentSolver_v2()
        system.add_reach('A')
        system.add_reach_reservoir('A', 'R', storage_chg=0, storage_loss=0)
        system.add_reach('B')
        system.add_connection('A', 'B', 0)
        system.add_reach_diversion('B', 'DIV', flow=1)
        system.add_handoff('B', 'CHG')

        system.add_transaction(id=1, priority=1910, limit=5, path=['B', 'CHG'], expected_value=1)
        system.add_transaction(id=2, priority=1930, limit=50, path=['A', 'R'])
        system.add_transaction(id=3, priority=1990, limit=5, path=['CHG', 'B', 'A', 'R'], expected_value=1)
        system.add_transaction(id=4, priority=9999, limit=None, path=['R', 'A', 'B', 'DIV'], expected_value=1)

        system.solve()
        system.assert_variables_equal_expected()
        
        raise NotImplementedError('What is the answer for this one?')


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
        system.add_connection('REACH-A', 'REACH-B', 5)
        system.add_connection('REACH-B', 'REACH-C', 5)
        system.add_reach_reservoir('REACH-A', 'STOR', -5, 0)
        system.add_handoff('REACH-B', 'REACH-B-CHG')
        system.add_reach_diversion('REACH-B', 'DIV-2', 5)
        system.add_reach_diversion('REACH-C', 'DIV-3', 5)
        system.add_reach_diversion('REACH-C', 'DIV-4', 5)

        system.add_transaction(id=1, priority=1900, limit=10, path=['REACH-B', 'REACH-B-CHG'], expected_value=0)
        system.add_transaction(id=2, priority=1920, limit=10, path=['REACH-B', 'DIV-2'], expected_value=5)
        system.add_transaction(id=3, priority=1930, limit=10, path=['REACH-C', 'DIV-4'], expected_value=5)
        system.add_transaction(id=4, priority=1940, limit=10, path=['REACH-C', 'DIV-3'], expected_value=0)
        system.add_transaction(id=5, priority=1950, limit=10, path=['REACH-B-CHG', 'REACH-B', 'REACH-C', 'DIV-4'], expected_value=0)
        system.add_transaction(id=6, priority=9998, limit=10, path=['STOR', 'REACH-A', 'REACH-B', 'DIV-2'], expected_value=0)
        system.add_transaction(id=7, priority=9999, limit=10, path=['STOR', 'REACH-A', 'REACH-B', 'REACH-C', 'DIV-3'], expected_value=5)
        
        system.solve()
        system.assert_variables_equal_expected()


