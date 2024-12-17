from solver.apportionment_solver import ApportionmentSolver_v2

'''system = ApportionmentSolver_v2()
system.add_reach('RIVER')
system.add_reach_diversion('RIVER', 'USER-1', 12.3)
system.add_reach_diversion('RIVER', 'USER-2',  3.8)
system.add_transaction(id=123, priority=1, limit=10.2 , path=['RIVER','USER-1'])
system.add_transaction(id=124, priority=2, limit= 0.82, path=['RIVER','USER-2'])
system.add_transaction(id=125, priority=3, limit= 1.1 , path=['RIVER','USER-2'])'''


"""
REACH-A       REACH-B            REACH-C         REACH-D
1---#-->2------> \~~~3~~~/ ---#-->4----->5---#-->6
        #         \_____/         #      #
        |                         |      |
        v                         v      v
       DIV-1       STOR         DIV-2   DIV-3

"""

stor_chg=-4
stor_loss=1
Q1_2=2
Q2_200=1
Q3_4=4
Q4_400=2+2
Q5_500=3+1
Q5_6=1


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
    

results = system.solve()

print(system)

print(results)


