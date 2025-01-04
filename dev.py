from solver.apportionment_solver import ApportionmentSolver_v2


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

results = system.solve()

print(" ########################## ")

print(system)

print(results)


