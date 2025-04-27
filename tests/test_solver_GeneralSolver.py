import unittest
import time
from solver.solver import GeneralSolver


class SolveSystem(unittest.TestCase):
    """Tests for the GeneralApportionmentSolver class"""

    def test_1(self):

        accounting_network = {
            'zones': [
                {'name':'A', 'type':'stream'},
                {'name':'B', 'type':'stream'},
                {'name':'C', 'type':'use'}
            ],
            'connections':[
                {'name':'con-1', 'from_zone': 'A', 'to_zone': 'B', 'uhd_mapping': {'positive_flows': [{'measurement_id':2778}]}},
                {'name':'con-2', 'from_zone': 'B', 'to_zone': 'C', 'uhd_mapping': {'positive_flows': [{'measurement_id':2779}]}}
            ],
            'beg_date': '2024-06-01',
            'end_date': '2024-06-30'
        }

        solver = GeneralSolver()
        solver.build_problem(accounting_network)
        var_values, errors_cnt, vars = solver.solve()
        

        print('??\n\n')

        solver.save_to_db(vars, var_values)


    def test_sevier(self):
        accounting_network = {
            'zones': [
                {"name": "Piute to Vermillion Reach", "type": "stream"},
                {"name": "Upstream", "type": "stream"},
                {"name": "Below Vermillion", "type": "stream"},
                {"name": "South Bend Diversion", "type": "use"},
                {"name": "SVC Diversion", "type": "use"},
                {"name": "Joseph Diversion", "type": "use"},
                {"name": "Monroe Diversion", "type": "use"},
                {"name": "Elsinore Diversion", "type": "use"},
                {"name": "Brookln Diversion", "type": "use"},
                {"name": "Richfield Diversion", "type": "use"},
                {"name": "Annabella Diversion", "type": "use"},
                {"name": "Vermillion Diversion", "type": "use"},
            ],
            'connections':[
                {'name':'con-1', 'from_zone': 'Upstream', 'to_zone': 'Piute to Vermillion Reach', 'uhd_mapping': {'positive_flows': [{'measurement_id':3846}]}},
                {'name':'con-2', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': 'South Bend Diversion', 'uhd_mapping': {'positive_flows': [{'measurement_id':731}]}},
                {'name':'con-3', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': 'Below Vermillion', 'uhd_mapping': {'positive_flows': [{'measurement_id':836}]}},
                
                {'name':'con-4', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': "SVC Diversion", 'uhd_mapping': {'positive_flows': [{'measurement_id':732}]}},
                {'name':'con-5', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': "Joseph Diversion", 'uhd_mapping': {'positive_flows': [{'measurement_id':733}]}},
                {'name':'con-6', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': "Monroe Diversion", 'uhd_mapping': {'positive_flows': [{'measurement_id':736}]}},
                {'name':'con-7', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': "Elsinore Diversion", 'uhd_mapping': {'positive_flows': [{'measurement_id':737}]}},
                {'name':'con-8', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': "Brookln Diversion", 'uhd_mapping': {'positive_flows': [{'measurement_id':738}]}},
                {'name':'con-9', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': "Richfield Diversion", 'uhd_mapping': {'positive_flows': [{'measurement_id':739}]}},
                {'name':'con-10', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': "Annabella Diversion", 'uhd_mapping': {'positive_flows': [{'measurement_id':740}]}},
                {'name':'con-11', 'from_zone': 'Piute to Vermillion Reach', 'to_zone': "Vermillion Diversion", 'uhd_mapping': {'positive_flows': [{'measurement_id':741}]}}
            ],
            'beg_date': '2023-06-08',
            'end_date': '2023-06-18'
        }
        solver = GeneralSolver()
        solver.build_problem(accounting_network)
        var_values, errors_cnt = solver.solve()
        
        print('VALUES: ')
        print(var_values)