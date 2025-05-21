import unittest
from solver.apportionment_solver import ApportionmentSolver


class A_SingleReachProblems(unittest.TestCase):

    def test_simple_apportionments(self):
        """Does the measured diversion get apportioned correctly to its parts
        when the measured diversion is LESS than the sum of allowed rights?
        (I.e. the junior rights should not recieve a full apportionment.)
        """

        system = ApportionmentSolver()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER>USER', 'RIVER', 'USER', 12)
        system.add_transaction(id=1, priority=1, upper_limit= 3, apath=[{'flow_name':'RIVER>USER', 'factor':1}], expected_value=3)
        system.add_transaction(id=2, priority=2, upper_limit= 6, apath=[{'flow_name':'RIVER>USER', 'factor':1}], expected_value=6)
        system.add_transaction(id=3, priority=3, upper_limit=12, apath=[{'flow_name':'RIVER>USER', 'factor':1}], expected_value=3)
        system.add_transaction(id=4, priority=4, upper_limit= 4, apath=[{'flow_name':'RIVER>USER', 'factor':1}], expected_value=0)
        
        system.solve()
        system.assert_variables_equal_expected()


    def test_simple_apportionments_2(self):
        """Does the measured diversion get apportioned correctly to its parts
        when the measured diversion is MORE than the sum of allowed rights?
        """

        system = ApportionmentSolver()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER>USER', 'RIVER', 'USER', 100)
        system.add_transaction(id=1, priority=1, upper_limit= 3, apath=[{'flow_name':'RIVER>USER', 'factor':1}], expected_value=3)
        system.add_transaction(id=2, priority=2, upper_limit= 6, apath=[{'flow_name':'RIVER>USER', 'factor':1}], expected_value=6)
        system.add_transaction(id=3, priority=3, upper_limit=12, apath=[{'flow_name':'RIVER>USER', 'factor':1}], expected_value=12)
        system.add_transaction(id=4, priority=4, upper_limit= 4, apath=[{'flow_name':'RIVER>USER', 'factor':1}], expected_value=4)

        system.solve()
        system.assert_variables_equal_expected()


    def test_equal_priority_apportionments(self):
        """"""
        system = ApportionmentSolver()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER>A', 'RIVER', 'A', 10)
        system.add_reach_diversion('RIVER>B', 'RIVER', 'B', 5)
        system.add_reach_diversion('RIVER>C', 'RIVER', 'C', 6)
        system.add_transaction(id=1, priority=1, upper_limit=1, apath=[{'flow_name':'RIVER>A', 'factor':1}], expected_value=1) #limited by water right
        system.add_transaction(id=2, priority=1, upper_limit=6, apath=[{'flow_name':'RIVER>B', 'factor':1}], expected_value=5) #limited by measured diversion
        system.add_transaction(id=3, priority=1, upper_limit=6, apath=[{'flow_name':'RIVER>C', 'factor':1}], expected_value=3) #limited by proportion
        system.add_transaction(id=4, priority=1, upper_limit=6, apath=[{'flow_name':'RIVER>C', 'factor':1}], expected_value=3) #limited by proportion

        system.solve()
        system.assert_variables_equal_expected()

    def test_equal_priority_apportionments_no_diversions(self):
        """
        2/5/2025 - The program was not handeling equal-priority apportionments 
        correctly when all upper-limits are zero. It was 
        """
        system = ApportionmentSolver()
        system.add_reach('RIVER')
        system.add_reach_diversion('RIVER>A', 'RIVER', 'A', 0)
        system.add_reach_diversion('RIVER>B', 'RIVER', 'B', 0)
        system.add_reach_diversion('RIVER>C', 'RIVER', 'C', 0)
        system.add_transaction(id=1, priority=1, upper_limit=0, apath=[{'flow_name':'RIVER>A', 'factor':1}], expected_value=0) #limited by water right
        system.add_transaction(id=2, priority=1, upper_limit=0, apath=[{'flow_name':'RIVER>B', 'factor':1}], expected_value=0) #limited by measured diversion
        system.add_transaction(id=3, priority=1, upper_limit=0, apath=[{'flow_name':'RIVER>C', 'factor':1}], expected_value=0) #limited by proportion
        system.add_transaction(id=4, priority=1, upper_limit=0, apath=[{'flow_name':'RIVER>C', 'factor':1}], expected_value=0) #limited by proportion

        system.solve()
        system.assert_variables_equal_expected()

    def test_reach_with_negative_storage_change(self):
        """"""
        system = ApportionmentSolver()
        system.add_reach('RIVER', storage_chg=-10, # This means extra water was released into the reach (from bank storage or similar)
                         expected_gain=0)
        system.add_reach_diversion('RIVER>A', 'RIVER', 'A', 10)

        system.solve()
        system.assert_variables_equal_expected()

    def test_reach_with_positive_storage_change(self):
        """"""
        system = ApportionmentSolver()
        system.add_reach('RIVER', storage_chg=2, # This means the reach soaked up some of the water 
                         expected_gain=12)
        system.add_reach_diversion('RIVER>A', 'RIVER', 'A', 10)

        system.solve()
        system.assert_variables_equal_expected()

class B_Reservoirs(unittest.TestCase):
    
    def reservoir_problem_input(self, stor_chg=0, stor_loss=0, Q_AB=0, 
                                Q_DIV1=0, Q_BC=0, Q_DIV2=0, Q_DIV3=0, 
                                Q_CD=0):
        """    
        1---#-->2------> \\~~~3~~~/ ---#-->4----->5---#-->6
                #         \\_____/         #      #
                |                          |      |
                v                          v      v
                200                        400    500

        """
        system = ApportionmentSolver()
        system.add_reach('REACH-A')
        system.add_reach('REACH-B')
        system.add_reach('REACH-C')
        system.add_reach('REACH-D')
        system.add_connection('A>B', 'REACH-A', 'REACH-B', Q_AB)
        system.add_connection('B>C', 'REACH-B', 'REACH-C', Q_BC)
        system.add_connection('C>D', 'REACH-C', 'REACH-D', Q_CD)
        system.add_reach_reservoir('B>STOR', 'REACH-B', 'STOR', stor_chg, stor_loss)
        system.add_reach_diversion('B>1', 'REACH-B', 'DIV-1', Q_DIV1)
        system.add_reach_diversion('C>2', 'REACH-C', 'DIV-2', Q_DIV2)
        system.add_reach_diversion('C>3', 'REACH-C', 'DIV-3', Q_DIV3)

        return system
    



    def test_simple_reservoir(self):
        """Check if things still work if there is a reservoir."""

        system = ApportionmentSolver()
        system.add_reach('RIVER')
        system.add_reach_reservoir('RIVER>STORAGE', 'RIVER', 'STORAGE', 0)
        system.add_reach_diversion('RIVER>A', 'RIVER', 'A', 10)
        system.add_reach_diversion('RIVER>B', 'RIVER', 'B', 10)
        system.add_transaction(id=1, priority=1, upper_limit=2, apath=[{'flow_name':'RIVER>A', 'factor':1}])
        system.add_transaction(id=2, priority=1, upper_limit=4, apath=[{'flow_name':'RIVER>B', 'factor':1}])
        system.add_transaction(id=3, priority=2, upper_limit=50, apath=[{'flow_name':'RIVER>STORAGE', 'factor':1}])
        system.add_transaction(id=4, priority=3, upper_limit=None, apath=[{'flow_name':'RIVER>STORAGE', 'factor':-1}, {'flow_name':'RIVER>A', 'factor':1}], expected_value=8)
        system.add_transaction(id=5, priority=3, upper_limit=None, apath=[{'flow_name':'RIVER>STORAGE', 'factor':-1}, {'flow_name':'RIVER>B', 'factor':1}], expected_value=6)

        system.solve()
        system.assert_variables_equal_expected()

    def test_simple_reservoir_with_downstream_gage(self):
        """Check if things are still good even if there is a reservoir that has
        a downstream measurement that constrains apporitonments to delieveries."""

        system = ApportionmentSolver()
        system.add_reach('UPSTREAM')
        system.add_reach('DOWNSTREAM')
        system.add_connection('UPSTREAM>DOWNSTREAM', 'UPSTREAM', 'DOWNSTREAM', 2)
        system.add_reach_reservoir('UPSTREAM>STORAGE', 'UPSTREAM', 'STORAGE', 0)
        system.add_reach_diversion('DOWNSTREAM>A', 'DOWNSTREAM', 'A', 10)
        system.add_reach_diversion('DOWNSTREAM>B', 'DOWNSTREAM', 'B', 10)
        system.add_transaction(id=1, priority=1, upper_limit=2, apath=[{'flow_name':'DOWNSTREAM>A', 'factor':1}])
        system.add_transaction(id=2, priority=1, upper_limit=4, apath=[{'flow_name':'DOWNSTREAM>B', 'factor':1}])
        system.add_transaction(id=3, priority=2, upper_limit=50, apath=[{'flow_name':'UPSTREAM>STORAGE', 'factor':1}])
        system.add_transaction(id=4, priority=3, upper_limit=None, apath=[{'flow_name':'UPSTREAM>STORAGE', 'factor':-1}, {'flow_name':'UPSTREAM>DOWNSTREAM', 'factor':1}, {'flow_name':'DOWNSTREAM>A', 'factor':1}], expected_value=1)
        system.add_transaction(id=5, priority=3, upper_limit=None, apath=[{'flow_name':'UPSTREAM>STORAGE', 'factor':-1}, {'flow_name':'UPSTREAM>DOWNSTREAM', 'factor':1}, {'flow_name':'DOWNSTREAM>B', 'factor':1}], expected_value=1)

        system.solve()
        system.assert_variables_equal_expected()


    def test_trivial(self):
        """This is a prety trivial example, just to check the most simple case... should probably make some more interesting tests...
        """
        system = self.reservoir_problem_input(stor_chg=0, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=4, Q_DIV3=4, Q_CD=1)
        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1', 'factor':1}], expected_value=2)
        system.add_transaction(id=2, priority=   2, upper_limit=   4, apath=[{'flow_name':'C>2', 'factor':1}], expected_value=4)
        system.add_transaction(id=3, priority=   3, upper_limit=   6, apath=[{'flow_name':'C>3', 'factor':1}], expected_value=4)
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=0)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=0)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()

    def test_storage_diversions_with_no_deliveries(self):
        """Test storage diversions with no deliveries.
        """
        system = self.reservoir_problem_input(stor_chg=2, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=4, Q_DIV3=4, Q_CD=1)
        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1', 'factor':1}], expected_value=2)
        system.add_transaction(id=2, priority=   2, upper_limit=   4, apath=[{'flow_name':'C>2', 'factor':1}], expected_value=4)
        system.add_transaction(id=3, priority=   3, upper_limit=   6, apath=[{'flow_name':'C>3', 'factor':1}], expected_value=4)
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=2)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=0)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()

    def test_storage_deliveries_with_no_diversions(self):
        """Test storage deliveries with no diversions.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=6, Q_DIV3=4, Q_CD=1)
        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1'   , 'factor':1}], expected_value=2)
        system.add_transaction(id=2, priority=   2, upper_limit=   4, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=4)
        system.add_transaction(id=3, priority=   3, upper_limit=   6, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=4)
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=0)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=0)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=2)

        system.solve()
        system.assert_variables_equal_expected()
        
    def test_equal_priority_deliveries(self):
        """Test storage deliveries that have equal priority.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=6, Q_DIV3=4, Q_CD=1)

        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1'   , 'factor':1}], expected_value=2)
        system.add_transaction(id=2, priority=   2, upper_limit=   4, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=4)
        system.add_transaction(id=3, priority=   3, upper_limit=   6, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=4)
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=0)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=0)
        system.add_transaction(id=6, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=2) # changed priority
        
        system.solve()
        system.assert_variables_equal_expected()
        
    def test_storage_deliveries_and_diversions_and_losses(self):
        """Test storage deliveries with diversions into storage at the same time, with some evaporative losses specified as well.
        """
        system = self.reservoir_problem_input(stor_chg=-1, stor_loss=1, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=6, Q_DIV3=4, Q_CD=1)

        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1'   , 'factor':1}], expected_value=2)
        system.add_transaction(id=2, priority=   2, upper_limit=   4, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=4)
        system.add_transaction(id=3, priority=   3, upper_limit=   6, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=4)
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=2)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=0)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=2)

        system.solve()
        system.assert_variables_equal_expected()

    def test_equal_priority_apportionmnets(self):
        """Test equal priority apportionments with storage deliveries on top.
        """
        system = self.reservoir_problem_input(stor_chg=-4, stor_loss=1, Q_AB=2, Q_DIV1=1, Q_BC=4, Q_DIV2=2+2, Q_DIV3=3+1, Q_CD=1)

        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1'   , 'factor':1}], expected_value=1)
        system.add_transaction(id=2, priority=   1, upper_limit=   4, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=2) # changed priority
        system.add_transaction(id=3, priority=   1, upper_limit=   6, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=3) # changed priority
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=0)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=1)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=2)

        system.solve()
        system.assert_variables_equal_expected()

    def test_equal_priority_apportionmnets2(self):
        """Test equal priority apportionments when the storage right also has an equal priority.

        For this case there is 4 cfs of gain above the reservoir (all of which is below the first gage).
        There is another 8 cfs of gain below the reservoir.

        """
        system = self.reservoir_problem_input(stor_chg=-4, stor_loss=1, Q_AB=0, Q_DIV1=1, Q_BC=6, Q_DIV2=7, Q_DIV3=7, Q_CD=0)

        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1'   , 'factor':1}], expected_value=1)
        system.add_transaction(id=2, priority=   1, upper_limit=   4, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=8/10*4)
        system.add_transaction(id=3, priority=   1, upper_limit=   6, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=8/10*6)
        system.add_transaction(id=4, priority=   1, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=3)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=7 - 8/10*6)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=7 - 8/10*4)

        system.solve()
        system.assert_variables_equal_expected()

    def test_change_water_that_is_not_available_at_htf_source(self):
        """Move water downstream when it is NOT available at the origional source but is available at the here-after source.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=0, Q_DIV1=0, Q_BC=2, Q_DIV2=6, Q_DIV3=8, Q_CD=0)

        #system.add_transaction(id=1, priority=   1, limit=   2, path=['REACH-B','DIV-1'])
        system.add_transaction(id=2, priority=   2, upper_limit=   4, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=4)
        system.add_transaction(id=3, priority=   3, upper_limit=   6, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=6)
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=0)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=2)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=0)
        # Change 1 so it delivers water downstream.
        system.add_transaction(id=101, priority=1, upper_limit=2, apath=[{'flow_name':'B>C', 'factor':1},{'flow_name':'C>2', 'factor':1}], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()

    def test_change_water_that_is_available_at_htf_source(self):
        """Move water downstream when it is NOT available at the origional source but is available at the here-after source.
        """
        system = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=2, Q_DIV1=0, Q_BC=4, Q_DIV2=6, Q_DIV3=8, Q_CD=0)

        system.add_transaction(id=101, priority=1, upper_limit=2, apath=[{'flow_name':'B>C', 'factor':1},{'flow_name':'C>2', 'factor':1}], expected_value=2)# Change 1 so it delivers water downstream.
        system.add_transaction(id=2, priority=   2, upper_limit=   4, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=4)
        system.add_transaction(id=3, priority=   3, upper_limit=   6, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=6)
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=0)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=2)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=0)
        
        system.solve()
        system.assert_variables_equal_expected()
        
    def test_spill_to_natural_flow(self):
        """What if the reservoir releases water that is not picked up?
        """
        system = self.reservoir_problem_input(stor_chg=-5, stor_loss=0, Q_AB=0, Q_DIV1=2, Q_BC=5, Q_DIV2=0, Q_DIV3=0, Q_CD=5)

        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1'   , 'factor':1}], expected_value=2)
        system.add_transaction(id=2, priority=   2, upper_limit=   4, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=0)
        system.add_transaction(id=3, priority=   3, upper_limit=   6, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=0)
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=0)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=0)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()

    def test_storage_diversion_exceeds_storage_right(self):
        """In practice, I doubt this is very important, but I still want the program to support this case.
        """
        system = self.reservoir_problem_input(stor_chg=25, stor_loss=0, Q_AB=0, Q_DIV1=0, Q_BC=0, Q_DIV2=0, Q_DIV3=0, Q_CD=0)

        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1'   , 'factor':1}], expected_value=0)
        system.add_transaction(id=2, priority=   2, upper_limit=   4, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=0)
        system.add_transaction(id=3, priority=   3, upper_limit=   6, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=0)
        system.add_transaction(id=4, priority=   4, upper_limit=  20, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=20)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=0)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()


    def test_presentation_example(self):
        """
        """
        system = self.reservoir_problem_input(stor_chg=-10, stor_loss=0, Q_AB=0, Q_DIV1=0, Q_BC=20, Q_DIV2=15, Q_DIV3=10, Q_CD=5)

        system.add_transaction(id=1, priority=   1, upper_limit=   2, apath=[{'flow_name':'B>1'   , 'factor':1}], expected_value=0)
        system.add_transaction(id=2, priority=   2, upper_limit=   5, apath=[{'flow_name':'C>2'   , 'factor':1}], expected_value=5)
        system.add_transaction(id=3, priority=   3, upper_limit=   2, apath=[{'flow_name':'C>3'   , 'factor':1}], expected_value=2)
        system.add_transaction(id=4, priority=   4, upper_limit= 100, apath=[{'flow_name':'B>STOR', 'factor':1}], expected_value=8)
        system.add_transaction(id=5, priority=9900, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>3', 'factor':1}], expected_value=8)
        system.add_transaction(id=6, priority=9901, upper_limit=None, apath=[{'flow_name':'B>STOR', 'factor':-1}, {'flow_name':'B>C', 'factor':1}, {'flow_name':'C>2', 'factor':1}], expected_value=10)

        system.solve()
        system.assert_variables_equal_expected()

    def test_water_rights_cant_steal_storage_water(self):
        """Water in a storage reservoir should not be "splilled" to augment 
        the natural source and therefor allow a direct flow right to recieve 
        a larger apportionment."""

        system = ApportionmentSolver()
        system.add_reach('S')
        system.add_reach_reservoir('S>R', 'S', 'R', storage_chg=-5, storage_loss=0)
        system.add_reach_diversion('S>A', 'S', 'A', flow=5)

        system.add_transaction(id=1, priority=1900, upper_limit=5, apath=[{'flow_name':'S>A', 'factor':1}], expected_value=0)
        system.add_transaction(id=2, priority=1950, upper_limit=100, apath=[{'flow_name':'S>R', 'factor':1}], expected_value=0)
        system.add_transaction(id=3, priority=9999, upper_limit=None, apath=[{'flow_name':'S>R', 'factor':-1}, {'flow_name':'S>A', 'factor':1}], expected_value=5)

        system.solve()
        system.assert_variables_equal_expected()

        '''
        This result: {TRXN-1: 5, TRXN-2: 0, TRXN-3: 0} 
        would indicate that when the apportionment to trxn #1 is maximized 
        storage is being pulled out of the reservoir to create divertable flow.
        This should not happen. 
        '''

    def xtest_water_rights_cant_steal_storage_water_v2(self):
        """This is very similar to the previous test, but what if natural flow 
        water is avaialable? """

        system = ApportionmentSolver()
        system.add_reach('S', expected_gain=5)
        system.add_reach('OUTFLOW')
        system.add_reach_reservoir('S>R', 'S', 'R', storage_chg=-5, storage_loss=0)
        system.add_reach_diversion('S>A', 'S', 'A', flow=5)
        system.add_connection('S>OUT', 'S', 'OUTFLOW', flow=5)

        system.add_transaction(id=1, priority=1900, upper_limit=5, apath=[{'flow_name':'S>A', 'factor':1}], expected_value=5)
        system.add_transaction(id=2, priority=1950, upper_limit=100, apath=[{'flow_name':'S>R', 'factor':1}], expected_value=0)
        system.add_transaction(id=3, priority=9999, upper_limit=None, apath=[{'flow_name':'S>R', 'factor':-1}, {'flow_name':'S>A', 'factor':1}], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()


class B_Imports(unittest.TestCase):
    def test_1(self):
        """ """

        system = ApportionmentSolver()
        system.add_reach('S')
        system.add_reach_import('IMP>S', 'S', 'IMP', flow=5)
        system.add_reach_diversion('S>DIV', 'S', 'DIV', flow=5)

        system.add_transaction(id=1, priority=10, upper_limit=5, apath=[{'flow_name':'IMP>S', 'factor':1}, {'flow_name':'S>DIV', 'factor':1}], expected_value=5)
        system.add_transaction(id=2, priority=2, upper_limit=5, apath=[{'flow_name':'S>DIV', 'factor':1}], expected_value=0)

        system.solve()
        system.assert_variables_equal_expected()


    def test_unmeasured_import(self):
        """Imports really should be measured. But if they are not, then we
        would expect that the import transaction should be less than the 
        observed gain in the reach. """

        system = ApportionmentSolver()
        system.add_reach('UPPER-REACH')
        system.add_reach('LOWER-REACH')
        system.add_connection('Upper>Lower', 'UPPER-REACH', 'LOWER-REACH', 5)
        system.add_reach_diversion('Lower>A', 'LOWER-REACH', 'A', 8)
        system.add_transaction(id=1, priority=1, upper_limit=10, apath=[{'flow_name':'GAINS_TO:LOWER-REACH', 'factor':1},{'flow_name':'Lower>A', 'factor':1}],
                               expected_value=3) # This should be limited to the gains in the lower reach
        system.add_transaction(id=2, priority=2, upper_limit=10, apath=[{'flow_name':'Lower>A', 'factor':1}],
                               expected_value=5) # This should be the remaining diversion

        system.solve()
        system.assert_variables_equal_expected()




class D_PrioritySeries(unittest.TestCase):

    def test_1(self):
        """
        """

        system = ApportionmentSolver()
        system.add_reach('RIVER')
        system.add_reach_diversion('Diversion', 'RIVER', 'USER', 12)
        system.add_transaction(id=1, priority=1, upper_limit= 7, apath=None, expected_value=7, child_series_name='sub-series')
        system.add_transaction(id=2, series_name='sub-series', priority=2, upper_limit= 6, apath=[{'flow_name':'Diversion', 'factor':1}], expected_value=6)
        system.add_transaction(id=3, series_name='sub-series', priority=3, upper_limit=12, apath=[{'flow_name':'Diversion', 'factor':1}], expected_value=1)
        system.add_transaction(id=4, series_name='sub-series', priority=4, upper_limit= 4, apath=[{'flow_name':'Diversion', 'factor':1}], expected_value=0)
        
        system.solve()
        system.assert_variables_equal_expected()

    def test_2(self):
        """
        This is a case that should be solved in two iterations:

        1st: TRXN-2 gets 0.5 proportion, resulting in 3.75, 
             TRXN-5 gets 0.3 proportion, resulting in 2.25
             TRXN-6 gets 0.2 proportion, resulting in 1.5

        2nd: TRXN-3 should be increased with 0.5 proportion from zero to 1.25
             TRXN-6 should be increased with 0.5 proportion from 1.5 to 2.75

        This test checks if during the 2nd iteration TRXN-6 is properly increased 
        from its starting, non-zero value.

        """

        system = ApportionmentSolver()
        system.add_reach('RIVER')
        system.add_reach_diversion('DiversionA', 'RIVER', 'A', 6)
        system.add_reach_diversion('DiversionB', 'RIVER', 'B', 4)
        system.add_transaction(id=1, priority=1, upper_limit= 10, apath=None, child_series_name='sub-series-1')
        system.add_transaction(id=2,   series_name='sub-series-1', priority=1, upper_limit= 4, apath=[{'flow_name':'DiversionA', 'factor':1}], expected_value=3.75)
        system.add_transaction(id=3,   series_name='sub-series-1', priority=2, upper_limit= 6, apath=[{'flow_name':'DiversionB', 'factor':1}], expected_value=1.25)
        system.add_transaction(id=4, priority=1, upper_limit= 10, apath=None, child_series_name='sub-series-2')
        system.add_transaction(id=5,   series_name='sub-series-2', priority=1, upper_limit= 6, apath=[{'flow_name':'DiversionA', 'factor':1}], expected_value=2.25)
        system.add_transaction(id=6,   series_name='sub-series-2', priority=1, upper_limit= 4, apath=[{'flow_name':'DiversionB', 'factor':1}], expected_value=2.75)
        
        system.solve()
        system.assert_variables_equal_expected()

    def test_3(self):
        """
        """

        system = ApportionmentSolver()
        system.add_reach('UPPER')
        system.add_reach('LOWER')
        system.add_connection('UPPER>LOWER', 'UPPER', 'LOWER', flow=2)
        system.add_reach_reservoir('UPPER>RESV', 'UPPER', 'RESV', 10)
        system.add_reach_diversion('>A', 'LOWER', 'A', 2)
        system.add_reach_diversion('>B', 'LOWER', 'B', 8)
        system.add_reach_diversion('>C', 'LOWER', 'C', 5)

        system.add_transaction(id=2, priority=1, upper_limit= 4, apath=None, expected_value=4, child_series_name='series-A')
        system.add_transaction(id=20, series_name='series-A', priority=10, upper_limit=None, apath=[{'flow_name':'>A', 'factor':1}], expected_value=2)
        system.add_transaction(id=21, series_name='series-A', priority=11, upper_limit=None, apath=[{'flow_name':'UPPER>LOWER', 'factor':-1}, {'flow_name':'UPPER>RESV', 'factor':1}], expected_value=2)

        system.add_transaction(id=3, priority=1, upper_limit= 8, apath=None, expected_value=8, child_series_name='series-B')
        system.add_transaction(id=30, series_name='series-B', priority=10, upper_limit=None, apath=[{'flow_name':'>B', 'factor':1}], expected_value=8)
        system.add_transaction(id=31, series_name='series-B', priority=11, upper_limit=None, apath=[{'flow_name':'UPPER>LOWER', 'factor':-1}, {'flow_name':'UPPER>RESV', 'factor':1}], expected_value=0)

        system.add_transaction(id=4, priority=1, upper_limit= 9, apath=None, expected_value=9, child_series_name='series-C')
        system.add_transaction(id=40, series_name='series-C', priority=10, upper_limit=None, apath=[{'flow_name':'>C', 'factor':1}], expected_value=5)
        system.add_transaction(id=41, series_name='series-C', priority=11, upper_limit=None, apath=[{'flow_name':'UPPER>LOWER', 'factor':-1}, {'flow_name':'UPPER>RESV', 'factor':1}], expected_value=4)
        
        system.solve()
        system.assert_variables_equal_expected()

    def test_4(self):
        """
        Same as previous, but now with a much smaller limit on diversions into the reservoir.
        """

        system = ApportionmentSolver()
        system.add_reach('UPPER')
        system.add_reach('LOWER')
        system.add_connection('UPPER>LOWER', 'UPPER', 'LOWER', flow=2)
        system.add_reach_reservoir('UPPER>RESV', 'UPPER', 'RESV', 3)
        system.add_reach_diversion('>A', 'LOWER', 'A', 2)
        system.add_reach_diversion('>B', 'LOWER', 'B', 8)
        system.add_reach_diversion('>C', 'LOWER', 'C', 5)

        system.add_transaction(id=2, priority=1, upper_limit= 4, apath=None, child_series_name='series-A')
        system.add_transaction(id=20, series_name='series-A', priority=10, upper_limit=None, apath=[{'flow_name':'>A', 'factor':1}], expected_value=2)
        system.add_transaction(id=21, series_name='series-A', priority=11, upper_limit=None, apath=[{'flow_name':'UPPER>LOWER', 'factor':-1}, {'flow_name':'UPPER>RESV', 'factor':1}], expected_value=0.22222 + 2.777777 * 2/6.5)

        system.add_transaction(id=3, priority=1, upper_limit= 8, apath=None, child_series_name='series-B')
        system.add_transaction(id=30, series_name='series-B', priority=10, upper_limit=None, apath=[{'flow_name':'>B', 'factor':1}], expected_value=8)
        system.add_transaction(id=31, series_name='series-B', priority=11, upper_limit=None, apath=[{'flow_name':'UPPER>LOWER', 'factor':-1}, {'flow_name':'UPPER>RESV', 'factor':1}], expected_value=0)

        system.add_transaction(id=4, priority=1, upper_limit= 9, apath=None, child_series_name='series-C')
        system.add_transaction(id=40, series_name='series-C', priority=10, upper_limit=None, apath=[{'flow_name':'>C', 'factor':1}], expected_value=5)
        system.add_transaction(id=41, series_name='series-C', priority=11, upper_limit=None, apath=[{'flow_name':'UPPER>LOWER', 'factor':-1}, {'flow_name':'UPPER>RESV', 'factor':1}], expected_value=0 + 2.777777 * 4.5/6.5)
        
        system.solve()
        system.assert_variables_equal_expected()


    def test_Leahs_3_reach_problem(self):

        system = ApportionmentSolver()
        system.add_reach('R1')
        system.add_reach('R2')
        system.add_reach('R3')
        system.add_reach('Downstream')
        system.add_reach_import('Imports>R1', 'R1', 'Imports', 40)
        system.add_connection('R1>R2', 'R1', 'R2', flow=10)
        system.add_connection('R2>R3', 'R2', 'R3', flow=30)
        system.add_connection('R3>', 'R3', 'Downstream', flow=5)
        system.add_reach_diversion('R1>A', 'R1', 'A', 50)
        system.add_reach_diversion('R2>B', 'R2', 'B', 30)
        system.add_reach_diversion('R3>C', 'R3', 'C', 20)

        system.add_transaction(id=1, priority=1, upper_limit=30, apath=[{'flow_name':'R1>A', 'factor':1}], expected_value=20)
        system.add_transaction(id=2, priority=1, upper_limit=20, apath=[{'flow_name':'R2>B', 'factor':1}], expected_value=20)
        system.add_transaction(id=3, priority=1, upper_limit=40, apath=[{'flow_name':'R3>C', 'factor':1}], expected_value=20)
        system.add_transaction(id=11, priority=2, upper_limit=None, apath=[{'flow_name':'Imports>R1', 'factor':1}, {'flow_name':'R1>A', 'factor':1}])
        system.add_transaction(id=12, priority=3, upper_limit=None, apath=[{'flow_name':'Imports>R1', 'factor':1}, {'flow_name':'R1>R2', 'factor':1}, {'flow_name':'R2>B', 'factor':1}])
        system.add_transaction(id=13, priority=4, upper_limit=None, apath=[{'flow_name':'Imports>R1', 'factor':1}, {'flow_name':'R1>R2', 'factor':1}, {'flow_name':'R2>R3', 'factor':1}, {'flow_name':'R3>C', 'factor':1}])

        system.solve()
        system.assert_variables_equal_expected()

    def test_Leahs_3_reach_problem2(self):

        system = ApportionmentSolver()
        system.add_reach('R1')
        system.add_reach('R2')
        system.add_reach('R3')
        system.add_reach('Downstream')
        system.add_reach_import('Imports>R1', 'R1', 'Imports', 100)
        system.add_connection('R1>R2', 'R1', 'R2', flow=10)
        system.add_connection('R2>R3', 'R2', 'R3', flow=30)
        system.add_connection('R3>', 'R3', 'Downstream', flow=5)
        system.add_reach_diversion('R1>A', 'R1', 'A', 50)
        system.add_reach_diversion('R2>B', 'R2', 'B', 30)
        system.add_reach_diversion('R3>C', 'R3', 'C', 20)

        system.add_transaction(id=1, priority=1, upper_limit=30, apath=[{'flow_name':'R1>A', 'factor':1}], expected_value=0)
        system.add_transaction(id=2, priority=1, upper_limit=20, apath=[{'flow_name':'R2>B', 'factor':1}], expected_value=20)
        system.add_transaction(id=3, priority=1, upper_limit=40, apath=[{'flow_name':'R3>C', 'factor':1}], expected_value=20)
        system.add_transaction(id=11, priority=2, upper_limit=None, apath=[{'flow_name':'Imports>R1', 'factor':1}, {'flow_name':'R1>A', 'factor':1}])
        system.add_transaction(id=12, priority=3, upper_limit=None, apath=[{'flow_name':'Imports>R1', 'factor':1}, {'flow_name':'R1>R2', 'factor':1}, {'flow_name':'R2>B', 'factor':1}])
        system.add_transaction(id=13, priority=4, upper_limit=None, apath=[{'flow_name':'Imports>R1', 'factor':1}, {'flow_name':'R1>R2', 'factor':1}, {'flow_name':'R2>R3', 'factor':1}, {'flow_name':'R3>C', 'factor':1}])


        system.solve()
        system.assert_variables_equal_expected()


class E_SharedTrxnLimits(unittest.TestCase):

    def test_1(self):
        """ Can trxn-2 share the limit of trxn-1? I.e., be limited to it's remaining right?
        """

        system = ApportionmentSolver()
        system.add_reach('RIVER')
        system.add_reach_diversion('>A', 'RIVER', 'A', 10)
        system.add_reach_diversion('>B', 'RIVER', 'B', 10)
        system.add_transaction(id=1, priority=1, upper_limit= 15, apath=[{'flow_name':'>A', 'factor':1}], expected_value=10)
        system.add_transaction(id=2, priority=2, upper_limit=None, apath=[{'flow_name':'>B', 'factor':1}], limited_by_id=1, expected_value=5)
        
        system.solve()
        system.assert_variables_equal_expected()

class F_TransactionsThatMayBeNegative(unittest.TestCase):

    def test_1(self):
        """
        """

        system = ApportionmentSolver()
        system.add_reach('RIVER')
        system.add_reach_reservoir('River>A', 'RIVER', 'RESV-A', 10)
        system.add_reach_reservoir('River>B', 'RIVER', 'RESV-B', -10)
        system.add_transaction(id=1, priority=1, upper_limit= 15, apath=[{'flow_name':'River>A', 'factor':-1}, {'flow_name':'River>B', 'factor':1}], lower_limit=-15, expected_value=-10)
        
        system.solve()
        system.assert_variables_equal_expected()


class DocumentationExamples(unittest.TestCase):

    def test_summary_example(self):
        """
        """

        system = ApportionmentSolver()
        system.add_reach('REACH-A')
        system.add_reach('REACH-B')
        
        system.add_connection('A>B', 'REACH-A', 'REACH-B', flow=90)
        system.add_reach_reservoir('A>STOR', 'REACH-A', 'STOR', storage_chg=-35, storage_loss=5)
        system.add_reach_diversion('B>1', 'REACH-B', 'DIV-1', flow=50)
        system.add_reach_diversion('B>2', 'REACH-B', 'DIV-2', flow=50)

        system.add_transaction(id=1, priority=1880, upper_limit=  40, apath=[{'flow_name':'B>1', 'factor':1}], expected_value=40)
        system.add_transaction(id=2, priority=1890, upper_limit=  20, apath=[{'flow_name':'B>1', 'factor':1}], expected_value=10)
        system.add_transaction(id=3, priority=1890, upper_limit=  40, apath=[{'flow_name':'B>2', 'factor':1}], expected_value=20)
        system.add_transaction(id=4, priority=1950, upper_limit= 100, apath=[{'flow_name':'A>STOR', 'factor':1}], expected_value=0)
        system.add_transaction(id=5, priority=9901, upper_limit=None, apath=[{'flow_name':'A>STOR', 'factor':-1},{'flow_name':'A>B', 'factor':1},{'flow_name':'B>2', 'factor':1}], expected_value=30)

        system.solve()
        system.assert_variables_equal_expected()