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
            'interzonal_flows':[
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
            'interzonal_flows':[
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
        var_values, errors_cnt, vars = solver.solve()
        
        print('VALUES: ')
        print(var_values)



    def test_duchesne(self):
        accounting_network = {
            "zones": [

                {"name": "UpperDuchesneReach", "type": "stream"},
                {"name": "MidDuchesneReach", "type": "stream"},
                {"name": "LowerDuchesneReach", "type": "stream"},
                {"name": "StrawberryReach", "type": "stream"},

                {"name": "Duchesne Tunnel", "type": "use"},
                {"name": "Strawberry Aqueduct Collection System", "type": "use"},
                {"name": "Rhodes Canal", "type": "use"},
                {"name": "Farm Creek Canal", "type": "use"},
                {"name": "New Tabby", "type": "use"},
                {"name": "Jasper Pike", "type": "use"},
                {"name": "Hicken Ditch", "type": "use"},
                {"name": "WPPBB Pipeline", "type": "use"},

                {"name": "Jones Ditch", "type": "use"},
                {"name": "Stillwater", "type": "storage", "storage_contents_id":3344},
                {"name": "CUP2", "type": "use"},
                {"name": "Shankes Pipe", "type": "use"},
                {"name": "Ivie Pipe", "type": "use"},
                {"name": "Knights Canal", "type": "use"},
                {"name": "Pioneer Canal", "type": "use"},
                {"name": "Orchard Mesa Canal", "type": "use"},

                {"name": "Syar Tunnel", "type": "use"},
                {"name": "Starvation", "type": "storage", "storage_contents_id":481},
                 
                {"name": "Rocky Point Canal", "type": "use"},
                {"name": "Duchesne Feeder Canal", "type": "use"},
                {"name": "Grey Mountain", "type": "use"},
                {"name": "Myton Townsite Canal", "type": "use"},
                {"name": "Ouray School Canal", "type": "use"},
                {"name": "Leland Canal", "type": "use"},

                {"name": "Stillwater Evaporation", "type": "use"},
                {"name": "Starvation Evaporation", "type": "use"},

            ],
            "interzonal_flows":[

                # UpperDuchesneReach
                {
                    'name':'>Duchesne Tunnel', 
                    'from_zone': 'UpperDuchesneReach', 
                    'to_zone':'Duchesne Tunnel', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':530}]}
                },
                {
                    'name':'>Strawberry Aqueduct Collection System', 
                    'from_zone': 'UpperDuchesneReach', 
                    'to_zone':'Strawberry Aqueduct Collection System', 
                    'uhd_mapping': {
                        'positive_flows': [{'measurement_id':2990}, {'measurement_id':3062}, {'measurement_id':2872}, {'measurement_id':3065}],
                        'negative_flows': [{'measurement_id':8481}]
                    }
                },
                {
                    'name':'>Rhodes Canal',
                    'from_zone': 'UpperDuchesneReach', 
                    'to_zone':'Rhodes Canal', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':509}, {'measurement_id':1541}]}
                },
                {
                    'name':'>Farm Creek Canal', 
                    'from_zone': 'UpperDuchesneReach', 
                    'to_zone':'Farm Creek Canal', 'uhd_mapping': {
                        'positive_flows': [{'measurement_id':510}],
                        'negative_flows': [{'measurement_id':9835}]
                    }
                },
                {
                    'name':'>New Tabby', 
                    'from_zone': 'UpperDuchesneReach', 
                    'to_zone':'New Tabby', 
                    'uhd_mapping': {
                        'positive_flows': [{'measurement_id':8489}], # previously was using 511, but that is not linked to UHD
                        'negative_flows': [{'measurement_id':8490}]
                    }
                },
                {
                    'name':'>Jasper Pike', 
                    'from_zone': 'UpperDuchesneReach', 
                    'to_zone':'Jasper Pike', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':512}]}
                },
                {
                    'name':'>Hicken Ditch', 
                    'from_zone': 'UpperDuchesneReach', 
                    'to_zone':'Hicken Ditch', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':1551}]}
                },
                {
                    'name':'>WPPBB Pipeline', 
                    'from_zone': 'UpperDuchesneReach', 
                    'to_zone':'WPPBB Pipeline', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':9450}]}
                },
                {
                    'name':'UpperDuchesneReach>MidDuchesneReach', 
                    'from_zone': 'UpperDuchesneReach', 
                    'to_zone':'MidDuchesneReach', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':10725}]}
                }, # Duchesne River near Tabiona

                # MidDuchesneReach
                {
                    'name':'>Jones Ditch', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'Jones Ditch', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':1556}]}
                },
                {
                    'name':'>Stillwater', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'Stillwater', 
                    'uhd_mapping': {'positive_flows': []}
                },
                #{ # TODO
                #    'name':'>Stillwater Evaporation', 
                #    'from_zone': 'Stillwater', 
                #    'to_zone':'Stillwater Evaporation', 
                #    'uhd_mapping': {'positive_flows': [{'measurement_id':8492}]}
                #},
                {
                    'name':'>CUP2', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'CUP2', 
                    'uhd_mapping': {
                        'positive_flows': [{'measurement_id':3346},{'measurement_id':3072}]
                    }
                },
                {
                    'name':'>Shankes Pipe', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'Shankes Pipe', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':3034}]}
                },
                {
                    'name':'>Ivie Pipe', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'Ivie Pipe', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':9577}]}
                },
                {
                    'name':'>Knights Canal', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'Knights Canal', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':3025}]}
                },
                {
                    'name':'>Pioneer Canal', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'Pioneer Canal', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':513}]}
                },
                {
                    'name':'>Orchard Mesa Canal', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'Orchard Mesa Canal', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':8494}]}
                },
                {
                    'name':'MidDuchesneReach>LowerDuchesneReach', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'LowerDuchesneReach', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':3026}]}
                }, # CUP Knights Diversion Bypass

                # StrawberryReach
                {
                    'name':'>Syar Tunnel', 
                    'from_zone': 'StrawberryReach', 
                    'to_zone':'Syar Tunnel', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':10554}]}
                },
                {
                    'name':'>Starvation', 
                    'from_zone': 'StrawberryReach', 
                    'to_zone':'Starvation', 
                    'uhd_mapping': {'positive_flows': []}
                },
                #{ # TODO - put evaporation back in!
                #    'name':'>Starvation Evaporation', 
                #    'from_zone': 'Starvation', 
                #    'to_zone':'Starvation Evaporation', 
                #    'uhd_mapping': {'positive_flows': [{'measurement_id':8495}]}
                #},
                {
                    'name':'StrawberryReach>LowerDuchesneReach', 
                    'from_zone': 'StrawberryReach', 
                    'to_zone':'LowerDuchesneReach', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':792}]}
                }, # Starvation Res Release

                # Add diversion into Starvation via Knight Starvation Pipeline.
                {
                    'name':'MidDuchesneReach>Starvation', 
                    'from_zone': 'MidDuchesneReach', 
                    'to_zone':'Starvation', 
                    'uhd_mapping': {
                        'positive_flows': [{'measurement_id':9484}, {'measurement_id':9483}]
                    }
                },


                # LowerDuchesneReach
                {
                    'name':'>Rocky Point Canal', 
                    'from_zone': 'LowerDuchesneReach', 
                    'to_zone':'Rocky Point Canal', 
                    'uhd_mapping': {
                        'positive_flows': [{'measurement_id':2501}], # previously used 515 (calculated value)
                        'negative_flows': [] # TODO - need to add the overflow measurement.
                    }
                },
                {
                    'name':'>Duchesne Feeder Canal', 
                    'from_zone': 'LowerDuchesneReach', 
                    'to_zone':'Duchesne Feeder Canal', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':516}]}
                },
                {
                    'name':'>Grey Mountain', 
                    'from_zone': 'LowerDuchesneReach', 
                    'to_zone':'Grey Mountain', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':525}]}
                },
                {
                    'name':'>Myton Townsite Canal', 
                    'from_zone': 'LowerDuchesneReach', 
                    'to_zone':'Myton Townsite Canal', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':528}]}
                },
                {
                    'name':'>Ouray School Canal', 
                    'from_zone': 'LowerDuchesneReach', 
                    'to_zone':'Ouray School Canal', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':529}]}
                },
                {
                    'name':'>Leland Canal', 
                    'from_zone': 'LowerDuchesneReach', 
                    'to_zone':'Leland Canal', 
                    'uhd_mapping': {'positive_flows': [{'measurement_id':555}]}
                }

            ],
            'beg_date': '2024-01-01',
            'end_date': '2024-10-31'
        }
        solver = GeneralSolver()
        solver.build_problem(accounting_network)
        var_values, errors_cnt, vars = solver.solve()
        
        print('VALUES: ')
        print(var_values)
        solver.save_to_db(vars, var_values)


