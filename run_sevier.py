'''
import unittest 

from somefile import get_simplified_graph, solve_apportionments

user_extent = 'Extent'

graph = get_simplified_graph(user_extent)
'''


import pandas as pd
from solver.solver import solve_period
from solver.query_database import query

zone_input = [
    {
        "name": "Piute to Vermillion Reach",
        "description": "Below Piute Reservoir to Gage Below Vermillion Dam",
        "type": "inflow",
        "in_measurements": ["3846"],
        "out_measurements": ["836",  "731","732","733","736","737","738","739","740","741"],
        "accounts": [],
    },
    {"name": "South Bend Diversion", "type": "outflow", "in_measurements": ["731"], "out_measurements": [], "accounts": []},
    {"name": "SVC Diversion", "type": "outflow", "in_measurements": ["732"], "out_measurements": [], "accounts": []},
    {"name": "Joseph Diversion", "type": "outflow", "in_measurements": ["733"], "out_measurements": [], "accounts": []},
    {"name": "Monroe Diversion", "type": "outflow", "in_measurements": ["736"], "out_measurements": [], "accounts": []},
    {"name": "Elsinore Diversion", "type": "outflow", "in_measurements": ["737"], "out_measurements": [], "accounts": []},
    {"name": "Brookln Diversion", "type": "outflow", "in_measurements": ["738"], "out_measurements": [], "accounts": []},
    {"name": "Richfield Diversion", "type": "outflow", "in_measurements": ["739"], "out_measurements": [], "accounts": []},
    {"name": "Annabella Diversion", "type": "outflow", "in_measurements": ["740"], "out_measurements": [], "accounts": []},
    {"name": "Vermillion Diversion", "type": "outflow", "in_measurements": ["741"], "out_measurements": [], "accounts": []},
]
beg_date = '2023-06-08'
end_date = '2023-06-18'


FROM_DB = True

if FROM_DB:
    flowlines, nodes, paths, all_measurements = query(
        system_id=51,                      # 'Sevier River'
        beg_date=beg_date,                         # 2023-06-01
        end_date=end_date,
        downstream_boundary_nodes=['9956'],   #right below node 9903 Vermillion diversion node
        upstream_boundary_nodes=[],
        zones=zone_input
        )
else:
    import json
    # Open up the json file containing the latest queried data.
    with open('output/query_data.json', 'r') as f:
        json_data = json.load(f)
        flowlines = json_data['flowlines']
        nodes = json_data['nodes']
        paths = json_data['paths']
        all_measurements = json_data['measurements']


# Show what we found in the database:
print('''From DB:
- {} Flowlines
- {} Nodes
- {} Paths
- {} Measurements
'''.format(len(flowlines), len(nodes), len(paths), len(all_measurements)))

delpaths = ['2021', '2022', '2023', '2024', '2025', '2026', '2027', '2028', '2029', '2030', '2031', '2001', '1411', '3891', '3890']
for x in delpaths:
    if x in paths:
        del paths[x]

#
transactions = solve_period(flowlines=flowlines, nodes=nodes, paths=paths, measurements=all_measurements, 
                first_day=beg_date, last_day=end_date,  
                write_output_files=True, zones=zone_input,
                log_file='output/sevier.log')


#print(results.transactions)
#print(results.to_df().to_string())

df = pd.DataFrame.from_dict(transactions)
print(df.to_string())