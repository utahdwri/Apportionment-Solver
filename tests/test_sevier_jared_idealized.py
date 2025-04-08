import unittest
from solver.apportionment_solver import ApportionmentSolver

'''

***
4/14/2023 Problem:

TRXN_241   =    7.5000*** NOT EQUAL TO EXPECTED VALUE OF    0.0000  (+7.5) 25   - remaining div is zero (because the diversion is satisfied by the lower reach portion of 731)
TRXN_731   =   29.4700*** NOT EQUAL TO EXPECTED VALUE OF   36.9700  (-7.5) 100A
TRXN_732   =   25.6700*** NOT EQUAL TO EXPECTED VALUE OF   18.1700  (+7.5) 100B 
TRXN_312   =  147.2380*** NOT EQUAL TO EXPECTED VALUE OF  154.7380  (-7.5) 999  - what remains is apportioned to Piute Resv.

The General Solver considers T241 to be senior to the Primary rights.

Jared's WCAT apportions water to Primary rights from the lower reach first, 
then to T241, and then again to the Primary rights from the upper reach.

To duplicate this, I'd have to split the primary rights into portions that divert 
from the lower reach gains, and then portions that divert from the upper reach gains.

***
4/14/2023 Problem:

Similar to 4/14 issue, but now involving transactions going to SV/Piute where the Primary-lower supplies the entire diversion.
'''

class UpperSevier(unittest.TestCase):


    def get_value(self, name, date):
        import pandas as pd
        if not hasattr(self, '_df'):
            self._df = pd.read_csv("tests/Upper Sevier (Draft5)_2023.csv", skiprows=1)
        
        df = self._df

        value = df.loc[df['Unnamed: 0'] == date][name].values[0]
        return float(value)
    
    


    def get_limit(self, date, dates, vals):
        mm_dd = date[-5:]
        for i in range(1,len(dates)):
            if mm_dd >= dates[i-1] and mm_dd < dates[i]:
                return vals[i-1]
        return 0
    


    def build_and_test_combined(self, date:str, test_message=''):
        """       

        """

        system = ApportionmentSolver()

        # ------------------------------------------------------------------------------------------
        # NETWORK:
        # ------------------------------------------------------------------------------------------

        
        system.add_reach('OtterCreekReach', storage_chg=0)
        system.add_reach('EastForkReach', storage_chg= self.get_value('DC', date)-self.get_value('DV', date))
        system.add_reach('SouthForkReach', storage_chg=0)
        system.add_reach('PiuteReach', storage_chg=0) # (self.get_value('DG', date)-self.get_value('DF', date))
        system.add_reach('ThreeCreeks', storage_chg=0)
        system.add_reach('LowerReach', storage_chg=0, 
                        # expected_gain=self.get_value('AL', date)-self.get_value('N', date)) # note: Jared does not count 'imports' in his divertable flow calculation
        )
        system.add_reach('A')

        # Otter Creek Reach
        system.add_reach_reservoir('OtterCreekReach', 'OtterCreekResv', 
                                   storage_chg=self.get_value('AA', date), # - (self.get_value('DC', date)-self.get_value('DV', date)), #I'm assigning the storage correction to the Resv instead of to the reach.
                                   storage_loss=self.get_value('AB', date) )
        system.add_connection('OtterCreekReach', 'EastForkReach', flow=self.get_value('E', date))

        # East Fork
        system.add_reach_diversion('EastForkReach', 'KingstonDiv', 
                                   flow=self.get_value('F1', date) )
        system.add_connection('EastForkReach', 'OtterCreekResv', flow=self.get_value('A', date))

        #
        system.add_connection('EastForkReach', 'PiuteReach', 
                                    flow=self.get_value('G', date) )
        system.add_connection('SouthForkReach', 'PiuteReach', 
                                    flow=self.get_value('H', date) )
        # Piute
        system.add_reach_diversion('PiuteReach', 'KingstonPipe', self.get_value('F2', date))
        system.add_reach_diversion('PiuteReach', 'Zabriskie', self.get_value('F3', date) )
        system.add_reach_diversion('PiuteReach', 'Allen', self.get_value('F4', date))
        system.add_reach_diversion('PiuteReach', 'KingstonMain', self.get_value('F5', date))
        system.add_reach_diversion('PiuteReach', 'KingstonGleave', self.get_value('F6', date))
        # Compound flume & South Fork
        system.add_reach_reservoir('PiuteReach', 'PiuteResv', 
                                   storage_chg=self.get_value('AD', date)   - (self.get_value('DG', date)-self.get_value('DF', date)), #I'm assigning the storage correction to the Resv instead of to the reach.
                                   storage_loss=self.get_value('AE', date))
        system.add_reach_diversion('PiuteReach', 'ConveyanceLosses', self.get_value('AC', date))

        #
        system.add_connection('PiuteReach', 'LowerReach', self.get_value('M', date))
        system.add_connection('ThreeCreeks', 'LowerReach', self.get_value('N', date))

        # Lower Reach (Piute to Vermillion)
        system.add_reach_diversion('LowerReach', 'Mills', self.get_value('O', date))
        system.add_reach_diversion('LowerReach', 'SBend', self.get_value('P', date))
        system.add_reach_diversion('LowerReach', 'Loss-1', 0)
        #system.add_reach_diversion('LowerReach', 'SValley', self.get_value('R', date))
        system.add_reach_diversion('LowerReach', 'SValley', self.get_value('AJ', date) )
        system.add_reach_diversion('LowerReach', 'Piute&Losses', self.get_value('S', date)*(1/self.get_value('AF', date)) )
        #system.add_reach_diversion('LowerReach', 'PiuteLosses', self.get_value('S', date)*(1/self.get_value('AF', date)-1) )
        system.add_reach_diversion('LowerReach', 'Joseph', self.get_value('Q', date))
        system.add_reach_diversion('LowerReach', 'Monroe', self.get_value('T', date))
        system.add_reach_diversion('LowerReach', 'Brooklyn', self.get_value('U', date))
        system.add_reach_diversion('LowerReach', 'Elsinore', self.get_value('V', date))
        system.add_reach_diversion('LowerReach', 'Richfield', self.get_value('W', date))
        system.add_reach_diversion('LowerReach', 'Annabella', self.get_value('X', date))
        system.add_reach_diversion('LowerReach', 'Vermillion', self.get_value('Y', date))
        system.add_reach_diversion('LowerReach', 'Loss-2', 0)
        system.add_connection('LowerReach', 'A', self.get_value('Z', date))


        # ------------------------------------------------------------------------------------------
        # TRANSACTIONS:
        # ------------------------------------------------------------------------------------------
        limit40 = self.get_limit(date, ['04-01','04-16','05-01','10-01'],[30, 31.25, 1.25, 0])
        limit73 = self.get_limit(date, ['04-01','05-01','10-01'], [55.14, 5.14, 0])
        limit80 = self.get_limit(date, ['05-01','10-01'], [68, 0])
        limit180 = self.get_limit(date, ['04-01','06-01','10-01'], [3, 1.66, 0])
        perc190 =  self.get_limit(date, ['04-01','06-01','10-01'], [1/3, 2/3, 0])
        limit230 = self.get_limit(date, ['04-01','10-01'], [1.5, 0])



        # Taylor Fish Pond Springs
        # This 4cfs was essentially moved from near Marysvale. Why is it first 
        # in priority? Is it from the stream or the gain?
        system.add_transaction(id=10, priority=3, upper_limit= 4.0, path=['LowerReach_GAINS', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('BM', date))

        # Three Creeks Release (from a tributary reservoir). 
        # Why is this a high priority transaction as opposed to a storage delivery?
        # How can we keep the unused portion from being sent back to natural flow?
        system.add_transaction(id=20, priority=4, upper_limit=None, path=['ThreeCreeks', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('BL', date))
        

        # Flowing Wells
        system.add_transaction(id=30, priority=5, upper_limit= 1.5, path=['LowerReach_GAINS', 'LowerReach', 'Brooklyn'], 
                               expected_value=self.get_value('BN', date))

        # 1st Priority
        system.add_transaction(id=60, priority=10, upper_limit=limit40, path=['LowerReach', 'SBend'], 
                               expected_value= self.get_value('BO', date) # lower reach
                                             + self.get_value('DJ', date) # Piute reach
                                             + self.get_value('EF', date) # Upper reach
                               )

        # Primary
        system.add_transaction(id=70, priority=100, upper_limit= 1.23, path=None, child_series_name='Mills-Primary')
        system.add_transaction(id=701, series_name='Mills-Primary', priority=1, upper_limit=None, path=['LowerReach', 'Mills'], 
                               expected_value= self.get_value('BQ', date)
                                             + self.get_value('EP1', date) )
        system.add_transaction(id=702, series_name='Mills-Primary', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EP2', date)
                               )
        system.add_transaction(id=71, priority=100, upper_limit=10.9, path=None, child_series_name='SBend-Primary')
        system.add_transaction(id=711, series_name='SBend-Primary', priority=1, upper_limit=None, path=['LowerReach', 'SBend'], 
                               expected_value= self.get_value('BR', date)
                                             + self.get_value('EQ1', date) )
        system.add_transaction(id=712, series_name='SBend-Primary', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EQ2', date)
                               )
        system.add_transaction(id=72, priority=100, upper_limit=2.56+25.9, path=None, child_series_name='Joseph-Primary')
        system.add_transaction(id=721, series_name='Joseph-Primary', priority=1, upper_limit=None, path=['LowerReach', 'Joseph'], 
                               expected_value= self.get_value('BT', date)
                                             + self.get_value('ER1', date) )
        system.add_transaction(id=722, series_name='Joseph-Primary', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('ER2', date)
                               )
        system.add_transaction(id=73, priority=100, upper_limit=limit73, path=None, child_series_name='SValley-Primary')
        system.add_transaction(id=731, series_name='SValley-Primary', priority=1, upper_limit=None, path=['LowerReach', 'SValley'], 
                               expected_value= self.get_value('BU', date)
                                             + self.get_value('ES1', date) )
        system.add_transaction(id=732, series_name='SValley-Primary', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('ES2', date)
                               )
        system.add_transaction(id=74, priority=100, upper_limit=47.9, path=None, child_series_name='Monroe-Primary')
        system.add_transaction(id=741, series_name='Monroe-Primary', priority=1, upper_limit=None, path=['LowerReach', 'Monroe'], 
                               expected_value= self.get_value('BV', date)
                                             + self.get_value('ET1', date) )
        system.add_transaction(id=742, series_name='Monroe-Primary', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('ET2', date)
                               )
        system.add_transaction(id=75, priority=100, upper_limit=29.77, path=None, child_series_name='Brooklyn-Primary')
        system.add_transaction(id=751, series_name='Brooklyn-Primary', priority=1, upper_limit=None, path=['LowerReach', 'Brooklyn'], 
                               expected_value= self.get_value('BW', date)
                                             + self.get_value('EU1', date) )
        system.add_transaction(id=752, series_name='Brooklyn-Primary', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EU2', date)
                               )
        system.add_transaction(id=76, priority=100, upper_limit=19.92, path=None, child_series_name='Elsinore-Primary')
        system.add_transaction(id=761, series_name='Elsinore-Primary', priority=1, upper_limit=None, path=['LowerReach', 'Elsinore'], 
                               expected_value= self.get_value('BX', date)
                                             + self.get_value('EV1', date) )
        system.add_transaction(id=762, series_name='Elsinore-Primary', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EV2', date)
                               )
        system.add_transaction(id=77, priority=100, upper_limit=85.9, path=None, child_series_name='Richfield-Primary')
        system.add_transaction(id=771, series_name='Richfield-Primary', priority=1, upper_limit=None, path=['LowerReach', 'Richfield'], 
                               expected_value= self.get_value('BY', date)
                                             + self.get_value('EW1', date) )
        system.add_transaction(id=772, series_name='Richfield-Primary', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EW2', date)
                               )
        system.add_transaction(id=78, priority=100, upper_limit=30.4, path=None, child_series_name='Annabella-Primary')
        system.add_transaction(id=781, series_name='Annabella-Primary', priority=1, upper_limit=None, path=['LowerReach', 'Annabella'], 
                               expected_value= self.get_value('BZ', date)
                                             + self.get_value('EX1', date) )
        system.add_transaction(id=782, series_name='Annabella-Primary', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EX2', date)
                               )
        system.add_transaction(id=79, priority=100, upper_limit=37.8, path=['LowerReach', 'Vermillion'], 
                               expected_value= self.get_value('CA', date)
                                             + self.get_value('EY', date)
                               )

        # Second Class
        system.add_transaction(id=80, priority=102, upper_limit=limit80, path=None, child_series_name='SValley-2nd')
        system.add_transaction(id=801, series_name='SValley-2nd', priority=1, upper_limit=None, path=['LowerReach', 'SValley'], 
                               expected_value= self.get_value('CC', date)
                                             + self.get_value('FB1', date))
        system.add_transaction(id=802, series_name='SValley-2nd', priority=2, upper_limit=None, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('FB2', date)
                               )

        # Third Class
        system.add_transaction(id=90, priority=103, upper_limit=11.5, path=['LowerReach', 'SValley'], 
                               expected_value= self.get_value('CD', date)
                                             + self.get_value('FC', date)
                               )

        # New Storage Zone A 
        system.add_transaction(id=100, priority=997, upper_limit=None, path=['LowerReach', 'A'], 
                               expected_value= self.get_value('CE', date)
                                             + self.get_value('FD', date)
                               )
        
        # Piute High Water Apportionment
        # 1/7 - moved to the last priority to get 2023-04-22 to work.
        system.add_transaction(id=110, priority=998, upper_limit=None, path=['LowerReach', 'Piute&Losses'], 
                               expected_value= self.get_value('CF', date)
                                             + self.get_value('FE1', date)
                               )





        # Otter Creek Guarantee
        # This is a (decreed) storage delivery, so does it need to have such a high priority?
        # This delivery is actually to the Piute Diversion (in the lower reach)
        system.add_transaction(id=120, priority=1, upper_limit=0.92, 
            path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'LowerReach', 'Piute&Losses'], 
                               expected_value=self.get_value('EO', date))
        
        # Trxns 13, 14 not included because they are used for reach storage,
        #   and I'm having those values be user-specified.


        # Price Spring (Piute Subreach)	61-2069 to Piute Storage
        # Note: Since the right is for a spring, it's not entitled to any water 
        #       in the river, just the gain (from the spring, but I guess limiting
        #       it to the reach gain is good enough). The spring is shown on the hydro, 
        #       close to 61-105. I suppose this right is given this preferential
        #       position in the priority ordering because it is an import of sorts.
        system.add_transaction(id=150, priority=2, upper_limit=1.78, path=['PiuteReach_GAINS', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DM', date))

        # trxn 16 already counted.

        # Primary Barnson Spring (Piute Subreach) 61-2070
        # This needs to be pro-rated along with a 22 cfs right -- both are rights
        # to the gains in a specified reach, and the decree says what that quantity
        # should be.
        system.add_transaction(id=170, priority=20, upper_limit=12, path=['PiuteReach_GAINS', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DL', date))
        
        # Piute Storage from East Fork - 61-2068
        system.add_transaction(id=180, priority=21, upper_limit=limit180, path=['EastForkReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DI', date))

        # % Feeder Canal to Otter Creek Storage
        # Why the 33%? Is this an approximation of what was actually delivered to 
        # the reservoir or is it a limit on the water right?
        # TODO - update the logic according to what the 33% means...
        system.add_transaction(id=190, priority=22, upper_limit=perc190*self.get_value('A', date), path=['EastForkReach', 'OtterCreekResv'], 
                               expected_value=self.get_value('DK', date))

        # Otter Creek "Gain"
        system.add_transaction(id=200, priority=23, upper_limit=None, path=['OtterCreekReach', 'OtterCreekResv'], 
                               lower_limit=None,
                               expected_value=self.get_value('DU', date)
                               )

        # Trxns 21, 22 not included because they are used for reach storage,
        #   and I'm having those values be user-specified.

        # 61-858
        system.add_transaction(id=230, priority=24, upper_limit=limit230, path=['SouthForkReach', 'PiuteReach', 'LowerReach', 'Piute&Losses'], 
                               expected_value=self.get_value('CX', date))
        
        # 61-2065 Mitchell Slough (SF Subreach)
        system.add_transaction(id=241, priority=25, upper_limit=7.5, path=['SouthForkReach', 'PiuteReach', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('CW', date))
        
        # 61-2065 South Fork (SF Subreach)
        system.add_transaction(id=242, priority=25, upper_limit=0.84, path=['SouthForkReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('CV', date))
        
        # Otter Creek Guarantee, 61-2103 et al
        system.add_transaction(id=250, priority=27, upper_limit=13, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'LowerReach', 'SBend'], 
                               expected_value=self.get_value('EL', date))

        # Trxns 26 is combined here with a previous entry


        # Piute Storage, 63-3015
        system.add_transaction(id=312, priority=999, upper_limit=None, path=['PiuteReach', 'PiuteResv'], 
                        expected_value= self.get_value('FE2', date)
                        )
        
        # Exchange from Piute back up to Otter Creek
        system.add_transaction(id=10001, priority=10001, upper_limit=None, 
                               path=['PiuteResv', 'PiuteReach', 'EastForkReach', 'OtterCreekReach', 'OtterCreekResv'] )

        # Otter Creek storage deliveries - Implied by formula for 'OCR' in the WCAT model.
        # OCR = PREV(OCR)+GA/c-EO-EL+DK+DU-F-AB-AC
        # [Otter Creek Reservoir Storage] = PREV(OCR) + [Otter Creek Transfers IN] - [OCG to Piute Diversion] - [OCG to South Bend Diversion] + [Feeder Canal Apportionment] - [Kingston Total Diversion] - [Evap] - [Conveyance Loss]
        #
        # Otter Creek storage deliveries to Piute:
        system.add_transaction(id=10002, priority=10002, upper_limit=None, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'LowerReach', 'Piute&Losses'] ) 
        system.add_transaction(id=10003, priority=10003, upper_limit=None, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'LowerReach', 'SBend'] ) 
        # Otter Creek storage deliveries to Kingston diversions:
        system.add_transaction(id=10004, priority=10004, upper_limit=None, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'KingstonDiv'] ) 
        system.add_transaction(id=10005, priority=10004, upper_limit=None, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'KingstonPipe'] )
        system.add_transaction(id=10006, priority=10004, upper_limit=None, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'Zabriskie'] )
        system.add_transaction(id=10007, priority=10004, upper_limit=None, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'Allen'] )
        system.add_transaction(id=10008, priority=10004, upper_limit=None, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'KingstonMain'] )
        system.add_transaction(id=10009, priority=10004, upper_limit=None, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'KingstonGleave'] )



        # Allow circular East Fork -> Otter Creek Resv -> East Fork flow. This is critical to getting it to work.
        system.add_transaction(id=10011, priority=10011, upper_limit=None, 
                               path=['EastForkReach', 'OtterCreekResv', 'OtterCreekReach', 'EastForkReach'] )
        

        

        system.solve()
        system.assert_variables_equal_expected(message=test_message)





    def test_combined_20230401(self):
        self.build_and_test_combined(date='2023-04-01')
    def test_combined_20230402(self):
        self.build_and_test_combined(date='2023-04-02')
    def test_combined_20230403(self):
        self.build_and_test_combined(date='2023-04-03')
    def test_combined_20230404(self):
        self.build_and_test_combined(date='2023-04-04')
    def test_combined_20230405(self):
        self.build_and_test_combined(date='2023-04-05')
    def test_combined_20230406(self):
        self.build_and_test_combined(date='2023-04-06')
    def test_combined_20230407(self):
        self.build_and_test_combined(date='2023-04-07')
    def test_combined_20230408(self):
        self.build_and_test_combined(date='2023-04-08')
    def test_combined_20230409(self):
        self.build_and_test_combined(date='2023-04-09')
    def test_combined_20230410(self):
        self.build_and_test_combined(date='2023-04-10')
    def test_combined_20230411(self):
        self.build_and_test_combined(date='2023-04-11')
    def test_combined_20230412(self):
        self.build_and_test_combined(date='2023-04-12')
    def test_combined_20230413(self):
        self.build_and_test_combined(date='2023-04-13')
    def test_combined_20230414(self):
        self.build_and_test_combined(date='2023-04-14')
    def test_combined_20230415(self):
        self.build_and_test_combined(date='2023-04-15')
    def test_combined_20230416(self):
        self.build_and_test_combined(date='2023-04-16')
    def test_combined_20230417(self):
        self.build_and_test_combined(date='2023-04-17')
    def test_combined_20230418(self):
        self.build_and_test_combined(date='2023-04-18')
    def test_combined_20230419(self):
        self.build_and_test_combined(date='2023-04-19')
    def test_combined_20230420(self):
        self.build_and_test_combined(date='2023-04-20')
    def test_combined_20230421(self):
        self.build_and_test_combined(date='2023-04-21')
    def test_combined_20230422(self):
        self.build_and_test_combined(date='2023-04-22')
    def test_combined_20230423(self):
        self.build_and_test_combined(date='2023-04-23')
    def test_combined_20230424(self):
        self.build_and_test_combined(date='2023-04-24')
    def test_combined_20230425(self):
        self.build_and_test_combined(date='2023-04-25')
    def test_combined_20230426(self):
        self.build_and_test_combined(date='2023-04-26')
    def test_combined_20230427(self):
        self.build_and_test_combined(date='2023-04-27')
    def test_combined_20230428(self):
        self.build_and_test_combined(date='2023-04-28')
    def test_combined_20230429(self):
        self.build_and_test_combined(date='2023-04-29')
    def test_combined_20230430(self):
        self.build_and_test_combined(date='2023-04-30')
        
    
    def test_combined_202304(self):
        from datetime import date, timedelta
        for d in (date(2023,4,1) + timedelta(n) for n in range(30)):
            yyyy_mm_dd = d.isoformat()
            self.build_and_test_combined(date=yyyy_mm_dd, test_message=yyyy_mm_dd + ': ')




    def test_combined_202305(self):
        from datetime import date, timedelta
        for d in (date(2023,5,1) + timedelta(n) for n in range(31)):
            yyyy_mm_dd = d.isoformat()
            self.build_and_test_combined(date=yyyy_mm_dd, test_message=yyyy_mm_dd + ': ')

    def test_combined_202306(self):
        from datetime import date, timedelta
        for d in (date(2023,6,1) + timedelta(n) for n in range(30)):
            yyyy_mm_dd = d.isoformat()
            self.build_and_test_combined(date=yyyy_mm_dd, test_message=yyyy_mm_dd + ': ')
    