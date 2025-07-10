import unittest
import time
from solver.apportionment_solver import ApportionmentSolver

def non_neg(v):
    if v < 0:
        return 0
    return v

class Duchesne(unittest.TestCase):


    def get_value(self, name, date, storage_change=False, convert_from_acft=False):
        import pandas as pd
        from datetime import datetime, timedelta
        import math

        if not hasattr(self, '_df'):
            self._df = pd.read_csv("tests/example_problems/Duchesne-Input.csv", skiprows=0)
        
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

        system = ApportionmentSolver()

        # ------------------------------------------------------------------------------------------
        # NETWORK:
        # ------------------------------------------------------------------------------------------
        
        system.add_reach('UpperDuchesneReach', storage_chg=0)
        system.add_reach('MidDuchesneReach', storage_chg=0)
        system.add_reach('LowerDuchesneReach', storage_chg=0)
        system.add_reach('StrawberryReach', storage_chg=0)


        # UpperDuchesneReach
        system.add_reach_diversion('>Duchesne Tunnel', 'UpperDuchesneReach', 'Duchesne Tunnel', non_neg(self.get_value('530', date)))
        system.add_reach_diversion('>Strawberry Aqueduct Collection System', 'UpperDuchesneReach', 'Strawberry Aqueduct Collection System', non_neg( 
              self.get_value('2990', date) # Hades
            - self.get_value('8481', date) # Hades Return
            + self.get_value('3062', date) # Win
            + self.get_value('2872', date) # Rhodes
            + self.get_value('3065', date) # Vat
        ))
        system.add_reach_diversion('>Rhodes Canal', 'UpperDuchesneReach', 'Rhodes Canal', non_neg(
              self.get_value('509', date) 
            + self.get_value('1541', date)
        ))
        #TODO#system.add_reach_diversion(None, 'UpperDuchesneReach', 'Warm Springs #1', non_neg(self.get_value(2513, date))) # not linked to wrn
        #TODO#system.add_reach_diversion(None, 'UpperDuchesneReach', 'Warm Springs #2', non_neg(self.get_value(9354, date))) # not linked to wrn # monthly data
        #TODO#system.add_reach_diversion(None, 'UpperDuchesneReach', 'Turnbow Ditch', non_neg(self.get_value(, date))) # not linked to wrn # monthly data
        #TODO#system.add_reach_diversion(None, 'UpperDuchesneReach', 'Defa Ditch', non_neg(self.get_value(, date))) # not linked to wrn # monthly data
        system.add_reach_diversion('>Farm Creek Canal', 'UpperDuchesneReach', 'Farm Creek Canal', non_neg(self.get_value('510', date) - self.get_value('9835', date)))
        #TODO#system.add_reach_diversion(None, 'UpperDuchesneReach', 'Little Farm Creek Canal', non_neg(self.get_value(, date))) # not linked to wrn # monthly data
        system.add_reach_diversion('>New Tabby', 'UpperDuchesneReach', 'New Tabby', non_neg(self.get_value('511', date) - self.get_value('8490', date)))
        system.add_reach_diversion('>Jasper Pike', 'UpperDuchesneReach', 'Jasper Pike', non_neg(self.get_value('512', date)) )
        system.add_reach_diversion('>Hicken Ditch', 'UpperDuchesneReach', 'Hicken Ditch', non_neg(self.get_value('1551', date)) )
        system.add_reach_diversion('>WPPBB Pipeline', 'UpperDuchesneReach', 'WPPBB Pipeline', non_neg(self.get_value('9450', date)) )
        system.add_connection('UpperDuchesneReach>MidDuchesneReach', 'UpperDuchesneReach', 'MidDuchesneReach', flow=non_neg(self.get_value('10725', date))) # Duchesne River near Tabiona

        # MidDuchesneReach
        system.add_reach_diversion('>Jones Ditch', 'MidDuchesneReach', 'Jones Ditch', flow=non_neg(self.get_value('1556', date)) )
        system.add_reach_reservoir('>Stillwater', 'MidDuchesneReach', 'Stillwater', 
            storage_chg=self.get_value('3344', date, storage_change=True, convert_from_acft=True),
            storage_loss=self.get_value('8492', date)
        )
        system.add_reach_diversion('>CUP2', 'MidDuchesneReach', 'CUP2', (
              self.get_value('3346', date) # Stillwater Tunnel Diversion
            + self.get_value('3072', date) # Docs Diversion
        ))
        system.add_reach_diversion('>Shankes Pipe', 'MidDuchesneReach', 'Shankes Pipe', flow=non_neg(self.get_value('3034', date) ))
        system.add_reach_diversion('>Ivie Pipe', 'MidDuchesneReach', 'Ivie Pipe', flow=non_neg(self.get_value('9577', date) ))
        system.add_reach_diversion('>Knights Canal', 'MidDuchesneReach', 'Knights Canal', flow=non_neg(self.get_value('3025', date) ))
        system.add_reach_diversion('>Pioneer Canal', 'MidDuchesneReach', 'Pioneer Canal', flow=non_neg(self.get_value('513', date) ))
        system.add_reach_diversion('>Orchard Mesa Canal', 'MidDuchesneReach', 'Orchard Mesa Canal', flow=non_neg(self.get_value('8494', date) ))
        system.add_connection('MidDuchesneReach>LowerDuchesneReach', 'MidDuchesneReach', 'LowerDuchesneReach', flow=non_neg(self.get_value('3026', date))) # CUP Knights Diversion Bypass

        # StrawberryReach
        system.add_reach_diversion('>Syar Tunnel', 'StrawberryReach', 'Syar Tunnel', flow=non_neg(self.get_value('10554', date)))
        # TODO: Currant Creek Resv
        # TODO: JRRT #1
        # TODO: JRRT #2
        # TODO: JRRT #3
        # TODO: Little Red Creek Canal
        # TODO: Hoover Ditch
        # TODO: Murdock #3
        # TODO: Murdock #2
        # TODO: Murdock #1
        # TODO: Red Creek Resv
        # TODO: Hays Diversion
        # TODO: Reimann Diversion #1
        system.add_reach_reservoir('StrawberryReach>Starvation', 'StrawberryReach', 'Starvation', 
            storage_chg=self.get_value('481', date, storage_change=True, convert_from_acft=True),
            storage_loss=self.get_value('8495', date)
        )
        system.add_connection('StrawberryReach>LowerDuchesneReach', 'StrawberryReach', 'LowerDuchesneReach', flow=non_neg(self.get_value('792', date))) # Starvation Res Release

        # Add diversion into Starvation via Knight Starvation Pipeline.
        system.add_connection('MidDuchesneReach>Starvation', 'MidDuchesneReach', 'Starvation', flow=non_neg(self.get_value('9484', date) + self.get_value('9483', date)))


        # LowerDuchesneReach
        system.add_reach_diversion('>Rocky Point Canal', 'LowerDuchesneReach', 'Rocky Point Canal', flow=non_neg(self.get_value('515', date))) # NOTE - this is a calculated stn
        system.add_reach_diversion('>Duchesne Feeder Canal', 'LowerDuchesneReach', 'Duchesne Feeder Canal', flow=non_neg(self.get_value('516', date)))
        # TODO - are Midview Lateral, Moon Lake Canal, Pahcease Canal part of the calcs -- or counted with Duchesne Feeder?
        system.add_reach_diversion('>Grey Mountain', 'LowerDuchesneReach', 'Grey Mountain', flow=non_neg(self.get_value('525', date)))
        # TODO - does flow to Pleasant Valley Canal need to be broken out from Grey Mountain, or can they be grouped together?
        system.add_reach_diversion('>Myton Townsite Canal', 'LowerDuchesneReach', 'Myton Townsite Canal', flow=non_neg(self.get_value('528', date)))
        system.add_reach_diversion('>Ouray School Canal', 'LowerDuchesneReach', 'Ouray School Canal', flow=non_neg(self.get_value('529', date)))
        system.add_reach_diversion('>Leland Canal', 'LowerDuchesneReach', 'Leland Canal', flow=non_neg(self.get_value('555', date)))
        # TODO - what is OPIC Duchesne Ditch? Is it linked to wrn? Does it have daily data?


        # ------------------------------------------------------------------------------------------
        # TRANSACTIONS:
        # ------------------------------------------------------------------------------------------
        mar01_nov15 = self.get_limit(date, ['03-01','11-16'], [1, 0])
        may01_oct15 = self.get_limit(date, ['05-01','10-16'], [1, 0])
        zone1 = self.get_limit(date, ['05-01','05-16','06-16','07-01','07-16','08-16','09-16', '10-16'], [0.01983, 0.03306, 0.03051, 0.02834, 0.02479, 0.02333, 0.01322, 0])
        zone2 = self.get_limit(date, ['04-20','05-01','05-16','06-01','06-16','07-01','07-16','08-16','09-01','09-16','10-01','10-16'], [0.013687, 0.01983, 0.02333,0.03306,0.03051,0.02834,0.02479,0.02333, 0.01983, 0.01322, 0.00991, 0])
        zone3 = self.get_limit(date, ['04-01','05-01','05-11','05-21','06-01','06-21','07-11','08-16','09-01','09-16','10-01','10-16'], [0.0124, 0.01587, 0.02204, 0.02479, 0.02834, 0.02645, 0.02479, 0.02333, 0.01983, 0.01322, 0.00793, 0])

        # Duchesne Tunnel
        system.add_transaction(id=1003, priority=19360625, upper_limit= 550*mar01_nov15, apath=[{'connection_name':'>Duchesne Tunnel', 'factor':1}])

        # Strawberry Aqueduct Collection System
        system.add_transaction(id=2003, priority=19641119, upper_limit= 690, apath=[{'factor':1,'connection_name':'>Strawberry Aqueduct Collection System'}]) #43-3822 - Note: I made up the flow limit of 690

        # Rhodes Canal
        system.add_transaction(id=3000, priority=90000000, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':-1,'connection_name':'UpperDuchesneReach>MidDuchesneReach'}, {'factor':1,'connection_name':'>Rhodes Canal'}])
        system.add_transaction(id=3011, priority=19060622, upper_limit=217.8*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-155
        system.add_transaction(id=3021, priority=19061023, upper_limit=42.9*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-152
        system.add_transaction(id=3031, priority=19080512, upper_limit=149.6*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-1214
        system.add_transaction(id=3041, priority=19080515, upper_limit=64.2*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-153
        system.add_transaction(id=3051, priority=19100629, upper_limit=74.6*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-224
        system.add_transaction(id=3062, priority=19120611, upper_limit=66*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-219
        system.add_transaction(id=3071, priority=19131025, upper_limit=24.07*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-156
        system.add_transaction(id=3082, priority=19140113, upper_limit=38.2*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-225
        system.add_transaction(id=3091, priority=19140120, upper_limit=35*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-154
        system.add_transaction(id=3101, priority=19150915, upper_limit=50.85*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-147
        system.add_transaction(id=3111, priority=19160420, upper_limit=1.5*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-11190
        system.add_transaction(id=3121, priority=19160420, upper_limit=1.47*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-9860
        system.add_transaction(id=3132, priority=19161129, upper_limit=9.57*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-223
        system.add_transaction(id=3141, priority=19190131, upper_limit=100.9*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-151
        system.add_transaction(id=3151, priority=19220525, upper_limit=127.7*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-150
        system.add_transaction(id=3163, priority=19260609, upper_limit=64.2*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-1715
        system.add_transaction(id=3173, priority=19280507, upper_limit=67.73*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-148
        system.add_transaction(id=3183, priority=19320923, upper_limit=8.9*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-242
        system.add_transaction(id=3193, priority=19360407, upper_limit=18.89*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-226
        system.add_transaction(id=3203, priority=19360529, upper_limit=61.41*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-149
        system.add_transaction(id=3213, priority=19641119, upper_limit=0.835*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-12098
        system.add_transaction(id=3223, priority=19641119, upper_limit=0.835*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-12099
        system.add_transaction(id=3233, priority=19641119, upper_limit=20*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-282
        system.add_transaction(id=3243, priority=19641119, upper_limit=15*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-312
        system.add_transaction(id=3253, priority=19641119, upper_limit=15*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-313
        system.add_transaction(id=3263, priority=19641119, upper_limit=5.165*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-351
        system.add_transaction(id=3273, priority=19641119, upper_limit=5.165*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-353
        system.add_transaction(id=3283, priority=19641119, upper_limit=5*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-3792
        system.add_transaction(id=3293, priority=19641119, upper_limit=20*zone1, apath=[{'factor':1,'connection_name':'>Rhodes Canal'}]) #43-3801
        
        # Farm Creek Canal
        system.add_transaction(id=4000, priority=90000001, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':-1,'connection_name':'UpperDuchesneReach>MidDuchesneReach'}, {'factor':1,'connection_name':'>Farm Creek Canal'}]) #
        system.add_transaction(id=4011, priority=19050729, upper_limit=136.4*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-168
        system.add_transaction(id=4021, priority=19051019, upper_limit=118.33*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-160
        system.add_transaction(id=4031, priority=19060228, upper_limit=94.9*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-162
        system.add_transaction(id=4041, priority=19060521, upper_limit=522.7*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-158
        system.add_transaction(id=4051, priority=19060904, upper_limit=53*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-159
        system.add_transaction(id=4061, priority=19070709, upper_limit=128.4*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-163
        system.add_transaction(id=4071, priority=19081114, upper_limit=103.2*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-171
        system.add_transaction(id=4081, priority=19101022, upper_limit=34.3*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-157
        system.add_transaction(id=4091, priority=19110405, upper_limit=120.7*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-164
        system.add_transaction(id=4101, priority=19120725, upper_limit=63.33*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-165
        system.add_transaction(id=4111, priority=19170426, upper_limit=179.5*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-166
        system.add_transaction(id=4121, priority=19180523, upper_limit=9*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-167
        system.add_transaction(id=4133, priority=19520701, upper_limit=59.44*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-10474
        system.add_transaction(id=4143, priority=19520701, upper_limit=100.26*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-424
        system.add_transaction(id=4153, priority=19641119, upper_limit=60*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-7364
        system.add_transaction(id=4163, priority=19641119, upper_limit=48.9*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-288
        system.add_transaction(id=4173, priority=19641119, upper_limit=50*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-290
        system.add_transaction(id=4183, priority=19641119, upper_limit=21.3*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-317
        system.add_transaction(id=4193, priority=19641119, upper_limit=25*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-318
        system.add_transaction(id=4203, priority=19641119, upper_limit=2*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-334
        system.add_transaction(id=4213, priority=19641119, upper_limit=60*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-3795
        system.add_transaction(id=4223, priority=19840816, upper_limit=34.9*zone1, apath=[{'factor':1,'connection_name':'>Farm Creek Canal'}]) #43-422

        # New Tabby
        system.add_transaction(id=5000, priority=90000002, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':-1,'connection_name':'UpperDuchesneReach>MidDuchesneReach'}, {'factor':1,'connection_name':'>New Tabby'}]) #
        system.add_transaction(id=5012, priority=19000000, upper_limit=94.4*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-2401
        system.add_transaction(id=5022, priority=19000000, upper_limit=16*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-2402
        system.add_transaction(id=5032, priority=19000000, upper_limit=35.05*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-2403
        system.add_transaction(id=5041, priority=19050729, upper_limit=4.0*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-10987
        system.add_transaction(id=5051, priority=19050729, upper_limit=247.98*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-307
        system.add_transaction(id=5061, priority=19060228, upper_limit=50*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-173
        system.add_transaction(id=5071, priority=19110811, upper_limit=284.7*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-301
        system.add_transaction(id=5081, priority=19130308, upper_limit=144.4*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-302
        system.add_transaction(id=5091, priority=19150607, upper_limit=36.6*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-303
        system.add_transaction(id=5102, priority=19170426, upper_limit=72.25*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-304
        system.add_transaction(id=5112, priority=19170830, upper_limit=9.2*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-1236
        system.add_transaction(id=5121, priority=19190519, upper_limit=16.89*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-172
        system.add_transaction(id=5133, priority=19391213, upper_limit=75*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-306
        system.add_transaction(id=5143, priority=19570821, upper_limit=40*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-305
        system.add_transaction(id=5153, priority=19641119, upper_limit=12.1*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-3791
        system.add_transaction(id=5163, priority=19641119, upper_limit=12.1*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-6961
        system.add_transaction(id=5173, priority=19641119, upper_limit=20.47*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-7365
        system.add_transaction(id=5183, priority=19641119, upper_limit=36*zone1, apath=[{'factor':1,'connection_name':'>New Tabby'}]) #43-291

        # Jasper Pike
        system.add_transaction(id=6011, priority=19050729, upper_limit=1190.51*zone1, apath=[{'factor':1,'connection_name':'>Jasper Pike'}]) #43-169
        system.add_transaction(id=6022, priority=19210906, upper_limit=.55*zone1, apath=[{'factor':1,'connection_name':'>Jasper Pike'}]) #43-170

        # Hicken Ditch
        system.add_transaction(id=7000, priority=90000003, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':-1,'connection_name':'UpperDuchesneReach>MidDuchesneReach'}, {'factor':1,'connection_name':'>Hicken Ditch'}]) #
        system.add_transaction(id=7012, priority=19050911, upper_limit=1.2*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-10992
        system.add_transaction(id=7021, priority=19050911, upper_limit=78.8*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-362
        system.add_transaction(id=7031, priority=19100914, upper_limit=33.68*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-363
        system.add_transaction(id=7041, priority=19100914, upper_limit=6*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-10512
        system.add_transaction(id=7051, priority=19100914, upper_limit=84.53*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-365
        system.add_transaction(id=7061, priority=19100914, upper_limit=18.6*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-366
        system.add_transaction(id=7071, priority=19100914, upper_limit=91.19*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-367
        system.add_transaction(id=7083, priority=19230206, upper_limit=36.3*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-364
        system.add_transaction(id=7093, priority=19500103, upper_limit=49*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-368
        system.add_transaction(id=7103, priority=19641119, upper_limit=20.75*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-3802
        system.add_transaction(id=7113, priority=19641119, upper_limit=15*zone1, apath=[{'factor':1,'connection_name':'>Hicken Ditch'}]) #43-283

        # WPPBB Pipeline
        system.add_transaction(id=8000, priority=90000004, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':-1,'connection_name':'UpperDuchesneReach>MidDuchesneReach'}, {'factor':1,'connection_name':'>WPPBB Pipeline'}]) #
        system.add_transaction(id=8011, priority=19050916, upper_limit=7.2*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-372
        system.add_transaction(id=8021, priority=19050916, upper_limit=4.55*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-379
        system.add_transaction(id=8031, priority=19050916, upper_limit=23.4*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-582
        system.add_transaction(id=8041, priority=19050916, upper_limit=6*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-585
        system.add_transaction(id=8051, priority=19051120, upper_limit=.03*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-586
        system.add_transaction(id=8061, priority=19051120, upper_limit=.331*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-589
        system.add_transaction(id=8071, priority=19051120, upper_limit=19.4*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-373
        system.add_transaction(id=8081, priority=19051120, upper_limit=22*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-374
        system.add_transaction(id=8091, priority=19111204, upper_limit=6.92*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-13141
        system.add_transaction(id=8101, priority=19111204, upper_limit=85.38*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-383
        system.add_transaction(id=8111, priority=19130326, upper_limit=4.58*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-13142
        system.add_transaction(id=8121, priority=19130326, upper_limit=23.42*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-371
        system.add_transaction(id=8131, priority=19131101, upper_limit=49*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-396
        system.add_transaction(id=8141, priority=19140228, upper_limit=77.5*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-398
        system.add_transaction(id=8151, priority=19140228, upper_limit=4.6*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-13135
        system.add_transaction(id=8161, priority=19140228, upper_limit=9.6*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-401
        system.add_transaction(id=8171, priority=19140312, upper_limit=68.2*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-399
        system.add_transaction(id=8182, priority=19180905, upper_limit=6.1*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-386
        system.add_transaction(id=8191, priority=19180905, upper_limit=26.8*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-387
        system.add_transaction(id=8203, priority=19321125, upper_limit=52*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-370
        system.add_transaction(id=8212, priority=19350513, upper_limit=10.1*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-395
        system.add_transaction(id=8223, priority=19500310, upper_limit=22.07*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-394
        system.add_transaction(id=8233, priority=19500310, upper_limit=44.13*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-400
        system.add_transaction(id=8243, priority=19641119, upper_limit=17*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-1832
        system.add_transaction(id=8253, priority=19641119, upper_limit=16*zone1, apath=[{'factor':1,'connection_name':'>WPPBB Pipeline'}]) #43-3786


        #
        #
        #

        # Jones Ditch
        system.add_transaction(id=11000, priority=90000005, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'>Jones Ditch'}]) #
        system.add_transaction(id=11011, priority=18610000, upper_limit=44.12*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-264
        system.add_transaction(id=11021, priority=19070206, upper_limit=31.6*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-403
        system.add_transaction(id=11031, priority=19070206, upper_limit=41.5*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-404
        system.add_transaction(id=11041, priority=19120819, upper_limit=36.8*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-265
        system.add_transaction(id=11051, priority=19120819, upper_limit=21.5*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-266
        system.add_transaction(id=11062, priority=19290318, upper_limit=26.3*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-267
        system.add_transaction(id=11073, priority=19641119, upper_limit=13*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-3790
        system.add_transaction(id=11083, priority=19641119, upper_limit=13.0*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-3793
        system.add_transaction(id=11093, priority=19641119, upper_limit=8.0*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-3794
        system.add_transaction(id=11103, priority=19641119, upper_limit=14.0*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-3803
        system.add_transaction(id=11113, priority=19641119, upper_limit=10.0*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-3804
        system.add_transaction(id=11123, priority=19641119, upper_limit=7*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-1826
        system.add_transaction(id=11133, priority=19641119, upper_limit=13*zone2, apath=[{'factor':1,'connection_name':'>Jones Ditch'}]) #43-1827

        # Stillwater
        system.add_transaction(id=12113, priority=19641119.1, upper_limit=None, apath=[{'factor':1,'connection_name':'>Stillwater'}])

        # CUP2, (Stillwater Tunnel Diversion, Docs Diversion)
        system.add_transaction(id=13000, priority=90000006, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'>CUP2'}])
        system.add_transaction(id=13113, priority=19641119.2, upper_limit=None, apath=[{'factor':1,'connection_name':'>CUP2'}])

        # Shankes Pipe
        system.add_transaction(id=14000, priority=90000007, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'>Shankes Pipe'}]) #
        system.add_transaction(id=14012, priority=19141214, upper_limit=47.9*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-12431
        system.add_transaction(id=14021, priority=19141214, upper_limit=5*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-308
        system.add_transaction(id=14031, priority=19161104, upper_limit=19*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-377
        system.add_transaction(id=14041, priority=19161104, upper_limit=38.5*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-415
        system.add_transaction(id=14051, priority=19161104, upper_limit=84*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-417
        system.add_transaction(id=14061, priority=19180312, upper_limit=15.8*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-411
        system.add_transaction(id=14071, priority=19180312, upper_limit=15.8*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-413
        system.add_transaction(id=14081, priority=19180312, upper_limit=15.8*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-378
        system.add_transaction(id=14093, priority=19641119, upper_limit=32.0*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-3798
        system.add_transaction(id=14103, priority=19641119, upper_limit=14.0*zone2, apath=[{'factor':1,'connection_name':'>Shankes Pipe'}]) #43-3799

        # Ivie Pipe
        system.add_transaction(id=15013, priority=19370201, upper_limit=22.77*zone2, apath=[{'factor':1,'connection_name':'>Ivie Pipe'}]) #43-257

        # Knights Canal
        system.add_transaction(id=16000, priority=90000008, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'>Knights Canal'}]) #
        system.add_transaction(id=16011, priority=19190404, upper_limit=12*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-258
        system.add_transaction(id=16021, priority=19190906, upper_limit=16.52*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-259
        system.add_transaction(id=16031, priority=19190906, upper_limit=12.24*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-8762
        system.add_transaction(id=16041, priority=19190906, upper_limit=9.5*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-8763
        system.add_transaction(id=16051, priority=19190906, upper_limit=8.85*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-8764
        system.add_transaction(id=16061, priority=19190906, upper_limit=12.69*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-8722
        system.add_transaction(id=16073, priority=19641119, upper_limit=5*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-8575
        system.add_transaction(id=16083, priority=19641119, upper_limit=12*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-1830
        system.add_transaction(id=16093, priority=19681011, upper_limit=7.0*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-3785
        system.add_transaction(id=16103, priority=19681011, upper_limit=1.25*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-8768
        system.add_transaction(id=16113, priority=19681011, upper_limit=2.25*zone2, apath=[{'factor':1,'connection_name':'>Knights Canal'}]) #43-8723

        # Pioneer Canal
        system.add_transaction(id=17000, priority=90000009, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'>Pioneer Canal'}]) #
        system.add_transaction(id=17011, priority=19050918, upper_limit=109.6*zone2, apath=[{'factor':1,'connection_name':'>Pioneer Canal'}]) #43-270
        system.add_transaction(id=17021, priority=19051027, upper_limit=90.1*zone2, apath=[{'factor':1,'connection_name':'>Pioneer Canal'}]) #43-277
        system.add_transaction(id=17031, priority=19060929, upper_limit=684.2*zone2, apath=[{'factor':1,'connection_name':'>Pioneer Canal'}]) #43-276
        system.add_transaction(id=17041, priority=19110218, upper_limit=131.5*zone2, apath=[{'factor':1,'connection_name':'>Pioneer Canal'}]) #43-269
        system.add_transaction(id=17051, priority=19110322, upper_limit=47.5*zone2, apath=[{'factor':1,'connection_name':'>Pioneer Canal'}]) #43-278
        system.add_transaction(id=17061, priority=19110322, upper_limit=44.25*zone2, apath=[{'factor':1,'connection_name':'>Pioneer Canal'}]) #43-280
        system.add_transaction(id=17071, priority=19180903, upper_limit=22.94*zone2, apath=[{'factor':1,'connection_name':'>Pioneer Canal'}]) #43-185
        system.add_transaction(id=17083, priority=19511113, upper_limit=157.7*zone2, apath=[{'factor':1,'connection_name':'>Pioneer Canal'}]) #43-275
        system.add_transaction(id=17093, priority=19641119, upper_limit=100.3*zone2, apath=[{'factor':1,'connection_name':'>Pioneer Canal'}]) #43-1856

        # Starvation (From Knight Diversion)
        system.add_transaction(id=18000, priority=90000010, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'MidDuchesneReach>Starvation'}])
        system.add_transaction(id=18181, priority=19050729, upper_limit=2, apath=[{'factor':1,'connection_name':'MidDuchesneReach>Starvation'}]) #43-11416
        system.add_transaction(id=18191, priority=19050729, upper_limit=5, apath=[{'factor':1,'connection_name':'MidDuchesneReach>Starvation'}]) #43-180
        system.add_transaction(id=18201, priority=19050729, upper_limit=8, apath=[{'factor':1,'connection_name':'MidDuchesneReach>Starvation'}]) #43-203
        system.add_transaction(id=18213, priority=19641119.3, upper_limit=None, apath=[{'factor':1,'connection_name':'MidDuchesneReach>Starvation'}]) #43-3822

        # Orchard Mesa Canal
        system.add_transaction(id=19000, priority=90000011, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #
        system.add_transaction(id=19011, priority=19071118, upper_limit=52.16*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-188
        system.add_transaction(id=19021, priority=19080813, upper_limit=34.7*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-189
        system.add_transaction(id=19031, priority=19080813, upper_limit=68.2*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-2348
        system.add_transaction(id=19041, priority=19080813, upper_limit=1*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-8710
        system.add_transaction(id=19051, priority=19090727, upper_limit=5.4*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-8724
        system.add_transaction(id=19061, priority=19090727, upper_limit=4.4*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-8725
        system.add_transaction(id=19071, priority=19090727, upper_limit=173.44*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-200
        system.add_transaction(id=19081, priority=19090727, upper_limit=27.18*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-186
        system.add_transaction(id=19091, priority=19101022, upper_limit=12.5*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-190
        system.add_transaction(id=19101, priority=19110421, upper_limit=5*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-11417
        system.add_transaction(id=19111, priority=19130111, upper_limit=179.44*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-184
        system.add_transaction(id=19121, priority=19130111, upper_limit=30.31*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-2057
        system.add_transaction(id=19131, priority=19130227, upper_limit=70.15*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-187
        system.add_transaction(id=19141, priority=19130227, upper_limit=70.15*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-260
        system.add_transaction(id=19152, priority=19200127, upper_limit=33.55*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-193
        system.add_transaction(id=19162, priority=19250506, upper_limit=39.4*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-192
        system.add_transaction(id=19173, priority=19250506, upper_limit=1.0*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-10569
        system.add_transaction(id=19183, priority=19500314, upper_limit=24.5*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-11090
        system.add_transaction(id=19193, priority=19561011, upper_limit=21.83*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-191
        system.add_transaction(id=19203, priority=19630718, upper_limit=9.7*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-10568
        system.add_transaction(id=19213, priority=19630718, upper_limit=107.6*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-402
        system.add_transaction(id=19223, priority=19641119, upper_limit=3*zone2, apath=[{'factor':1,'connection_name':'>Orchard Mesa Canal'}]) #43-3781

        #
        #
        #

        # Rocky Point Canal
        system.add_transaction(id=21000, priority=90000012, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'MidDuchesneReach>LowerDuchesneReach'}, {'factor':1,'connection_name':'>Rocky Point Canal'}]) #  
        #system.add_transaction(id=21012, priority=        , upper_limit=*zone1, path=['LowerDuchesneReach', 'Rocky Point Canal']) #43-13009  
        #system.add_transaction(id=21022, priority=        , upper_limit=*zone1, path=['LowerDuchesneReach', 'Rocky Point Canal']) #43-13010  
        system.add_transaction(id=21031, priority=19050729, upper_limit=133.49*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-181  133.49 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21041, priority=19050729, upper_limit=8, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-203  8 × Year Round Diversion
        system.add_transaction(id=21052, priority=19050828, upper_limit=42*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-12865  42 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21061, priority=19050828, upper_limit=125.9*zone1, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-175  125.9 × Duchesne Zone 1 Delivery Schedule
        system.add_transaction(id=21071, priority=19050828, upper_limit=967.2*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-176  967.2 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21082, priority=19050828, upper_limit=10*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-11251  10 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21091, priority=19060409, upper_limit=10*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-1207  10 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21101, priority=19060409, upper_limit=5.89*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-8212  5.89 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21111, priority=19060818, upper_limit=8*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-8489  8 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21121, priority=19060818, upper_limit=13*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-7326  13 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21131, priority=19060818, upper_limit=2.5*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-1208  2.5 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21141, priority=19060818, upper_limit=2.5*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-13071  2.5 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21152, priority=19060818, upper_limit=5*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-1631  5 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21162, priority=19070131, upper_limit=63.03*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-12357  63.03 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21173, priority=19070131, upper_limit=8.46*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-468  8.46 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21181, priority=19070131, upper_limit=1.79*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-477  1.79 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21191, priority=19070131, upper_limit=59.19*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-194  59.19 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21201, priority=19070131, upper_limit=43.88*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-195  43.88 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21212, priority=19070131, upper_limit=15.49*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-9213  15.49 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21221, priority=19071118, upper_limit=31.59*zone2, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-11218  31.59 × Duchesne Zone 2 Delivery Schedule
        system.add_transaction(id=21231, priority=19081130, upper_limit=990.06*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-177  990.06 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21241, priority=19090727, upper_limit=38.8*zone2, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-201  38.8 × Duchesne Zone 2 Delivery Schedule
        system.add_transaction(id=21251, priority=19110421, upper_limit=6*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-8896  6 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21261, priority=19130213, upper_limit=77*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-198  77 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21271, priority=19130414, upper_limit=40.3*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-199  40.3 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21283, priority=19341229, upper_limit=35.83*zone2, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-182  35.83 × Duchesne Zone 2 Delivery Schedule
        system.add_transaction(id=21293, priority=19500314, upper_limit=120*zone2, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-178  120 × Duchesne Zone 2 Delivery Schedule
        system.add_transaction(id=21303, priority=19540819, upper_limit=100*zone2, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-179  100 × Duchesne Zone 2 Delivery Schedule
        system.add_transaction(id=21313, priority=19550324, upper_limit=204.3*zone2, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-183  204.3 × Duchesne Zone 2 Delivery Schedule
        system.add_transaction(id=21323, priority=19641119, upper_limit=42*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-1843  42 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21333, priority=19641119, upper_limit=3*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-1864  3 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21343, priority=19641119, upper_limit=189.015*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-7368  189.015 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21353, priority=19641119, upper_limit=189.015*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-7369  189.015 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=21363, priority=19641119, upper_limit=393*zone3, apath=[{'factor':1,'connection_name':'>Rocky Point Canal'}]) #43-3820  393 × Duchesne Zone 3 Delivery Schedule

        # Duchesne Feeder Canal
        system.add_transaction(id=22000, priority=90000013, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'MidDuchesneReach>LowerDuchesneReach'}, {'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #  
        system.add_transaction(id=22022, priority=18610000, upper_limit=62.2, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-1682  62.2
        system.add_transaction(id=22032, priority=18610000, upper_limit=5.81, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-1685  5.81
        system.add_transaction(id=22042, priority=18610000, upper_limit=3.3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-1686  3.3
        system.add_transaction(id=22052, priority=18610000, upper_limit=3.3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-1686  3.3
        system.add_transaction(id=22061, priority=19050710, upper_limit=23*zone3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-13611  23 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=22071, priority=19050710, upper_limit=1078.25*zone3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-443  1078.25 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=22081, priority=19050828, upper_limit=470*zone3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-436  470 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=22092, priority=19050828, upper_limit=911.93*zone3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-434  911.93 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=22101, priority=19150520, upper_limit=130.19*zone3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-435  130.19 × Duchesne Zone 3 Delivery Schedule
        #system.add_transaction(id=22112, priority=19180622, upper_limit=*zone3, path=['LowerDuchesneReach', 'Duchesne Feeder Canal']) #43-1239  
        #system.add_transaction(id=22122, priority=19180622, upper_limit=*zone3, path=['LowerDuchesneReach', 'Duchesne Feeder Canal']) #43-1239  
        system.add_transaction(id=22132, priority=19180622, upper_limit=175, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-1239  175
        system.add_transaction(id=22141, priority=19210906, upper_limit=2.4*may01_oct15, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-444  2.4 × May01 through Oct15
        #system.add_transaction(id=22152, priority=19220803, upper_limit=*zone3, path=['LowerDuchesneReach', 'Duchesne Feeder Canal']) #43-1967  
        system.add_transaction(id=22162, priority=19220803, upper_limit=0, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-1967  0
        #system.add_transaction(id=22172, priority=19220803, upper_limit=*zone3, path=['LowerDuchesneReach', 'Duchesne Feeder Canal']) #43-1967  
        system.add_transaction(id=22183, priority=19641119, upper_limit=105*zone3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-1845  105 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=22193, priority=19641119, upper_limit=69*zone3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-1846  69 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=22203, priority=19641119, upper_limit=62.1*zone3, apath=[{'factor':1,'connection_name':'>Duchesne Feeder Canal'}]) #43-1847  62.1 × Duchesne Zone 3 Delivery Schedule

        # Grey Mountain
        system.add_transaction(id=23000, priority=90000014, upper_limit=None, apath=[{'factor':-1,'connection_name':'>Stillwater'}, {'factor':1,'connection_name':'MidDuchesneReach>LowerDuchesneReach'}, {'factor':1,'connection_name':'>Grey Mountain'}]) #  
        system.add_transaction(id=23011, priority=19050710, upper_limit=1208.85*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-1204  1208.85 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23021, priority=19050710, upper_limit=3982.57*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-459  3982.57 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23031, priority=19050828, upper_limit=67.3*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-255  67.3 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23042, priority=19050828, upper_limit=122.25*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-1664  122.25 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23052, priority=19050828, upper_limit=25*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-1672  25 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23061, priority=19131229, upper_limit=55.8*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-2359  55.8 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23072, priority=19131229, upper_limit=4.5*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-1667  4.5 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23082, priority=19131229, upper_limit=43.8*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-1669  43.8 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23092, priority=19131229, upper_limit=129.1*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-1224  129.1 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23102, priority=19150429, upper_limit=248.63*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-1228  248.63 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23111, priority=19210906, upper_limit=8.38*may01_oct15, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-460  8.38 × May01 through Oct15
        system.add_transaction(id=23123, priority=19641119, upper_limit=9.0*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-3787  9.0 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=23133, priority=19641119, upper_limit=11.0*zone3, apath=[{'factor':1,'connection_name':'>Grey Mountain'}]) #43-3789  11.0 × Duchesne Zone 3 Delivery Schedule

        # Myton Townsite Canal
        system.add_transaction(id=24001, priority=19050619, upper_limit=3562.1*zone3, apath=[{'factor':1,'connection_name':'>Myton Townsite Canal'}]) #43-452  3562.1 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=24011, priority=19050703, upper_limit=1378.24*zone3, apath=[{'factor':1,'connection_name':'>Myton Townsite Canal'}]) #43-453  1378.24 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=24021, priority=19050703, upper_limit=12*may01_oct15, apath=[{'factor':1,'connection_name':'>Myton Townsite Canal'}]) #43-1201  12 × May01 through Oct15
        system.add_transaction(id=24032, priority=19050710, upper_limit=18.78*zone3, apath=[{'factor':1,'connection_name':'>Myton Townsite Canal'}]) #43-1204  18.78 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=24041, priority=19050710, upper_limit=40.88*zone3, apath=[{'factor':1,'connection_name':'>Myton Townsite Canal'}]) #43-459  40.88 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=24052, priority=19130327, upper_limit=111.12*zone3, apath=[{'factor':1,'connection_name':'>Myton Townsite Canal'}]) #43-1681  111.12 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=24062, priority=19130727, upper_limit=83.91*zone3, apath=[{'factor':1,'connection_name':'>Myton Townsite Canal'}]) #43-1680  83.91 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=24071, priority=19210906, upper_limit=.88*may01_oct15, apath=[{'factor':1,'connection_name':'>Myton Townsite Canal'}]) #43-454  .88 × May01 through Oct15

        # Ouray School Canal
        system.add_transaction(id=25001, priority=19050703, upper_limit=4*may01_oct15, apath=[{'factor':1,'connection_name':'>Ouray School Canal'}]) #43-1202  4 × May01 through Oct15
        system.add_transaction(id=25011, priority=19050703, upper_limit=3.75*may01_oct15, apath=[{'factor':1,'connection_name':'>Ouray School Canal'}]) #43-1203  3.75 × May01 through Oct15
        system.add_transaction(id=25021, priority=19050703, upper_limit=3795.78*zone3, apath=[{'factor':1,'connection_name':'>Ouray School Canal'}]) #43-482  3795.78 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=25031, priority=19210906, upper_limit=5.47*may01_oct15, apath=[{'factor':1,'connection_name':'>Ouray School Canal'}]) #43-483  5.47 × May01 through Oct15
        system.add_transaction(id=25043, priority=19721128, upper_limit=80*zone3, apath=[{'factor':1,'connection_name':'>Ouray School Canal'}]) #43-7294  80 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=25053, priority=19721128, upper_limit=19*zone3, apath=[{'factor':1,'connection_name':'>Ouray School Canal'}]) #43-12189  19 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=25063, priority=19721128, upper_limit=1*zone3, apath=[{'factor':1,'connection_name':'>Ouray School Canal'}]) #43-12841  1 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=25073, priority=19961203, upper_limit=35.0*zone3, apath=[{'factor':1,'connection_name':'>Ouray School Canal'}]) #43-10892  35.0 × Duchesne Zone 3 Delivery Schedule

        # Leland Canal
        system.add_transaction(id=26001, priority=19050703, upper_limit=842.2*zone3, apath=[{'factor':1,'connection_name':'>Leland Canal'}]) #43-480  842.2 × Duchesne Zone 3 Delivery Schedule
        system.add_transaction(id=26011, priority=19160124, upper_limit=100*zone3, apath=[{'factor':1,'connection_name':'>Leland Canal'}]) #43-485  100 × Duchesne Zone 3 Delivery Schedule
        

        # Connect the reservoirs...
        system.add_transaction(id=88000, priority=99000001, upper_limit=None, lower_limit=None, apath=[
            {'factor':-1,'connection_name':'>Stillwater'}, 
            {'factor': 1,'connection_name':'MidDuchesneReach>LowerDuchesneReach'}, 
            {'factor':-1,'connection_name':'StrawberryReach>LowerDuchesneReach'},
            {'factor': 1,'connection_name':'StrawberryReach>Starvation'}]) #  



        return system

    
    def test_sankey(self):
        import json
        from datetime import date, timedelta
        graph = {}
        var_values = {"dates":[], "variables":{}, "arcs":{}}
        errors_cnt = 0

        #for d in (date(2004,10,1) + timedelta(n) for n in range(20*365)):
        for d in (date(2023,1,1) + timedelta(n) for n in range(365)):
        #for d in (date(2012,9,16) + timedelta(n) for n in range(1)):
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
