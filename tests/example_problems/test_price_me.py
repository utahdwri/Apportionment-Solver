"""

"""

import unittest
import time
from solver.apportionment_solver import ApportionmentSolver

def non_neg(v):
    if v < 0:
        return 0
    return v

class Price(unittest.TestCase):


    def get_value(self, name, date, storage_change=False, convert_from_acft=False):
        import pandas as pd
        from datetime import datetime, timedelta
        import math

        if not hasattr(self, '_df'):
            self._df = pd.read_csv("tests/example_problems/Price-Input.csv", skiprows=0)
        
        df = self._df

        value = float(df.loc[df['RECORD_DATE'] == date][name].values[0])

        if math.isnan(value):
            value = 0 

        if value < 0:
            value = 0

        if convert_from_acft:
            value = value * 0.50416666666

        if storage_change:
            prev_date = datetime.strptime(date, '%Y-%m-%d').date() - timedelta(days=1)
            prev_value = self.get_value(name, str(prev_date), storage_change=False, convert_from_acft=convert_from_acft)
            value = value - prev_value


        return value
    


    def get_limit(self, date, dates, vals):
        mm_dd = date[-5:]
        for i in range(1,len(dates)):
            if mm_dd >= dates[i-1] and mm_dd < dates[i]:
                return vals[i-1]
        return 0
    


    def build(self, date:str, test_message=''):
        """       

        """


        # Electric Lake
        # Huntington Res.
        # Cleveland Res.
        # Rolfson Res
        # Millers Flat Res
        # Huntington Plant Diversion | (2)
        # @ HUNTINGTON CREEK BELOW POWER PLANT
        # Rowley Ditch               | very small
        # Seeley and Collard Ditch   | very small
        # Huntington Pipeline        | 9735
        # Cleveland Canal            | 2994
        # HCIC North Ditch



        system = ApportionmentSolver()
        # ------------------------------------------------------------------------------------------
        # NETWORK:
        # ------------------------------------------------------------------------------------------
        
        system.add_reach('Price River Abv Scofield', storage_chg=0)
        system.add_reach('Price River Blw Scofield', storage_chg=0)
        system.add_reach('Price River Blw Golf Course', storage_chg=0)

        system.add_reach_reservoir('>Scofield', 'Price River Abv Scofield', 'Scofield', 
            storage_chg=self.get_value('9668', date, storage_change=True, convert_from_acft=True),
            storage_loss=0
        )
        system.add_connection('Abv Scofield>Blw Scofield'   , 'Price River Abv Scofield', 'Price River Blw Scofield'   , flow=non_neg(self.get_value('73', date)))

        system.add_reach_diversion('>North Carbon Group'    , 'Price River Blw Scofield', 'North Carbon Group'         ,      non_neg(self.get_value('8961', date)))
        system.add_reach_diversion('>Bryner-Ploutz Ditch'   , 'Price River Blw Scofield', 'Bryner-Ploutz Ditch'        ,      non_neg(self.get_value('33'  , date)))
        system.add_reach_diversion('>Price Canal'           , 'Price River Blw Scofield', 'Price Canal'                ,      non_neg(self.get_value('30'  , date)))
        system.add_reach_diversion('>Carbon Canal'          , 'Price River Blw Scofield', 'Carbon Canal'               ,      non_neg(self.get_value('29'  , date)))
        system.add_connection('Blw Scofield>Blw Golf Course', 'Price River Blw Scofield', 'Price River Blw Golf Course', flow=non_neg(self.get_value('11311', date)))


        # ------------------------------------------------------------------------------------------
        # TRANSACTIONS:
        # ------------------------------------------------------------------------------------------
        mar01_nov30 = self.get_limit(date, ['03-01','12-01'], [1, 0])
        apr01_oct31 = self.get_limit(date, ['04-01','11-01'], [1, 0])
        mar01_nov15 = self.get_limit(date, ['03-01','11-16'], [1, 0])

        # Scofield
        system.add_transaction(id=1001, priority=19060830.1, upper_limit=None, apath=[{'factor':1,'connection_name':'>Scofield'}]) # 91-2
        system.add_transaction(id=1002, priority=19371011  , upper_limit=None, apath=[{'factor':1,'connection_name':'>Scofield'}]) # 91-78

        # North Carbon Group
        system.add_transaction(id=2000, priority=90000000, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Scofield'}, {'factor':1,'connection_name':'Abv Scofield>Blw Scofield'}, {'factor':1,'connection_name':'>North Carbon Group'}])
        system.add_transaction(id=2001, priority=18740000, upper_limit=5.6  *mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-717
        system.add_transaction(id=2002, priority=18740000, upper_limit=2.133*mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-718
        system.add_transaction(id=2003, priority=18760000, upper_limit=0.117*mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-759
        system.add_transaction(id=2004, priority=18780000, upper_limit=0.067*mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-758
        system.add_transaction(id=2005, priority=18840000, upper_limit=0.1  *mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-757
        system.add_transaction(id=2006, priority=18860000, upper_limit=0.1  *mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-756
        system.add_transaction(id=2007, priority=19060830, upper_limit=2.41 *mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-755
        system.add_transaction(id=2008, priority=19060830, upper_limit=4.45 *mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-753
        system.add_transaction(id=2009, priority=19070000, upper_limit=1.491*mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-754
        system.add_transaction(id=2010, priority=19070000, upper_limit=4.88 *mar01_nov30, apath=[{'factor':1,'connection_name':'>North Carbon Group'}]) # 91-752

        # Bryner-Ploutz Ditch
        system.add_transaction(id=3000, priority=90000001, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Scofield'}, {'factor':1,'connection_name':'Abv Scofield>Blw Scofield'}, {'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}])
        system.add_transaction(id=3001, priority=18740000, upper_limit=1.375*mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-646 etc.
        system.add_transaction(id=3002, priority=18760000, upper_limit=0.367*mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-2316 etc.
        system.add_transaction(id=3003, priority=18800000, upper_limit=0.117*mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-1759
        system.add_transaction(id=3004, priority=18820000, upper_limit=0.117*mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-1760
        system.add_transaction(id=3005, priority=18820000, upper_limit=0.25 *mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-2318
        system.add_transaction(id=3006, priority=19060830, upper_limit=1.32 *mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-2503
        system.add_transaction(id=3007, priority=19060830, upper_limit=1.95 *mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-2720
        system.add_transaction(id=3008, priority=19060830, upper_limit=1.95 *mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-2744
        system.add_transaction(id=3009, priority=19070000, upper_limit=0.678*mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-2504
        system.add_transaction(id=3010, priority=19070000, upper_limit=8    *mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-2717
        system.add_transaction(id=3011, priority=19070000, upper_limit=8    *mar01_nov30, apath=[{'factor':1,'connection_name':'>Bryner-Ploutz Ditch'}]) # 91-2721

        # Price Canal
        system.add_transaction(id=4000, priority=90000002, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Scofield'}, {'factor':1,'connection_name':'Abv Scofield>Blw Scofield'}, {'factor':1,'connection_name':'>Price Canal'}])
        system.add_transaction(id=4001, priority=18740000, upper_limit=9.43  *apr01_oct31, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-304
        system.add_transaction(id=4002, priority=18740000, upper_limit=7.674 *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-305
        system.add_transaction(id=4003, priority=18740000, upper_limit=11    *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-306
        system.add_transaction(id=4004, priority=18740000, upper_limit=8.564 *apr01_oct31, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-30
        system.add_transaction(id=4005, priority=18740000, upper_limit=3.91  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-3710
        system.add_transaction(id=4006, priority=18760000, upper_limit=12.857*mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-762
        system.add_transaction(id=4007, priority=19060830, upper_limit=4.45  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1423
        system.add_transaction(id=4008, priority=19060830, upper_limit=3.82  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1425
        system.add_transaction(id=4009, priority=19060830, upper_limit=3.54  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1793
        system.add_transaction(id=4010, priority=19060830, upper_limit=4.37  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1818
        system.add_transaction(id=4011, priority=19060830, upper_limit=4.27  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1821
        system.add_transaction(id=4012, priority=19070000, upper_limit=32.4  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1152
        system.add_transaction(id=4013, priority=19070000, upper_limit=37.8  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1424
        system.add_transaction(id=4014, priority=19070000, upper_limit=30.2  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1794
        system.add_transaction(id=4015, priority=19070000, upper_limit=37    *apr01_oct31, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1819
        system.add_transaction(id=4016, priority=19070000, upper_limit=36.3  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-1822
        system.add_transaction(id=4017, priority=19110209, upper_limit=20    *mar01_nov15, apath=[{'factor':1,'connection_name':'>Price Canal'}]) # 91-17

        # Carbon Canal
        system.add_transaction(id=5000, priority=90000003, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Scofield'}, {'factor':1,'connection_name':'Abv Scofield>Blw Scofield'}, {'factor':1,'connection_name':'>Carbon Canal'}])
        system.add_transaction(id=5001, priority=18740000, upper_limit=7.98  *apr01_oct31, apath=[{'factor':1,'connection_name':'>Carbon Canal'}]) # 91-366
        system.add_transaction(id=5002, priority=18740000, upper_limit=0.85  *mar01_nov30, apath=[{'factor':1,'connection_name':'>Carbon Canal'}]) # 91-764
        system.add_transaction(id=5003, priority=18760000, upper_limit=0.333 *mar01_nov30, apath=[{'factor':1,'connection_name':'>Carbon Canal'}]) # 91-765
        system.add_transaction(id=5004, priority=19060830, upper_limit=103.12*apr01_oct31, apath=[{'factor':1,'connection_name':'>Carbon Canal'}]) # 91-3
        system.add_transaction(id=5005, priority=19060830, upper_limit=48    *mar01_nov30, apath=[{'factor':1,'connection_name':'>Carbon Canal'}]) # 91-3396
        system.add_transaction(id=5006, priority=19070000, upper_limit=259   *mar01_nov30, apath=[{'factor':1,'connection_name':'>Carbon Canal'}]) # 91-3397


        return system

    
    def test_sankey(self):
        import json
        from datetime import date, timedelta
        graph = {}
        var_values = {"dates":[], "variables":{}, "arcs":{}}
        errors_cnt = 0

        for d in (date(2014,10,1) + timedelta(n) for n in range(366*10)):
            try:
                # Run 
                yyyy_mm_dd = d.isoformat()
                print('Running for:', yyyy_mm_dd)
                system = self.build(date=yyyy_mm_dd)
                system.solve()

                # Extract the data.
                graph, this_var_values = system.to_sankey_data(use_expected_values=False)

                # Merge this day's data with the previous data.
                var_values['dates'].append(yyyy_mm_dd)
                for v in this_var_values:
                    if v not in var_values['variables']:
                        var_values['variables'][v] = []
                    var_values['variables'][v].append( this_var_values[v][0] )
            except Exception as e:
                print(e)
                print(d)
                errors_cnt += 1
                time.sleep(5)
        with open('sankey.js', 'w') as f:
            f.write('let graph_data = ' + json.dumps(graph, indent=2))
            f.write('\n\n')
            f.write('let daily_values = ' + json.dumps(var_values, indent=2))
        #print(json.dumps(graph))
        #print(json.dumps(var_values))
        print('errors_cnt: ', errors_cnt)



