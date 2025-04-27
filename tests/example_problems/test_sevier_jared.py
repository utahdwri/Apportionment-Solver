import unittest
from solver.apportionment_solver import ApportionmentSolver


class UpperSevier(unittest.TestCase):


    def get_value(self, name, date):
        import pandas as pd
        if not hasattr(self, '_df'):
            self._df = pd.read_csv("tests/example_problems/UPS-Jared_2023.csv", skiprows=1)
        
        df = self._df

        value = df.loc[df['Unnamed: 0'] == date][name].values[0]
        return float(value)
    
    


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


        dS_east_fork = self.get_value('DC', date)-self.get_value('DV', date)
        dS_piute     = self.get_value('DG', date)-self.get_value('DF', date)
        
        system.add_reach('OtterCreekReach', storage_chg=0)
        system.add_reach('EastForkReach', storage_chg=dS_east_fork)
        system.add_reach('SouthForkReach', storage_chg=0)
        system.add_reach('PiuteReach', storage_chg=0) # (self.get_value('DG', date)-self.get_value('DF', date))
        system.add_reach('ThreeCreeks', storage_chg=0)
        system.add_reach('LowerReach', storage_chg=0, 
                        # expected_gain=self.get_value('AL', date)-self.get_value('N', date)) # note: Jared does not count 'imports' in his divertable flow calculation
        )
        system.add_reach('A')

        # Otter Creek Reach
        system.add_reach_reservoir(None, 'OtterCreekReach', 'OtterCreekResv', 
                                   storage_chg=self.get_value('AA', date), # - (self.get_value('DC', date)-self.get_value('DV', date)), #I'm assigning the storage correction to the Resv instead of to the reach.
                                   storage_loss=self.get_value('AB', date) )
        system.add_connection(None, 'OtterCreekReach', 'EastForkReach', flow=self.get_value('E', date))

        # East Fork
        system.add_reach_diversion(None, 'EastForkReach', 'KingstonDiv', 
                                   flow=self.get_value('F1', date) )
        system.add_connection(None, 'EastForkReach', 'OtterCreekResv', flow=self.get_value('A', date))

        #
        system.add_connection(None, 'EastForkReach', 'PiuteReach', 
                                    flow=self.get_value('G', date) )
        system.add_connection(None, 'SouthForkReach', 'PiuteReach', 
                                    flow=self.get_value('H', date) )
        # Piute
        system.add_reach_diversion(None, 'PiuteReach', 'KingstonPipe', self.get_value('F2', date))
        system.add_reach_diversion(None, 'PiuteReach', 'Zabriskie', self.get_value('F3', date) )
        system.add_reach_diversion(None, 'PiuteReach', 'Allen', self.get_value('F4', date))
        system.add_reach_diversion(None, 'PiuteReach', 'KingstonMain', self.get_value('F5', date))
        system.add_reach_diversion(None, 'PiuteReach', 'KingstonGleave', self.get_value('F6', date))
        # Compound flume & South Fork
        system.add_reach_reservoir(None, 'PiuteReach', 'PiuteResv', 
                                   storage_chg=self.get_value('AD', date) - dS_piute, #I'm assigning the storage correction to the Resv instead of to the reach.
                                   storage_loss=self.get_value('AE', date))
        system.add_reach_diversion(None, 'PiuteReach', 'ConveyanceLosses', self.get_value('AC', date))

        #
        system.add_connection(None, 'PiuteReach', 'LowerReach', self.get_value('M', date))
        system.add_connection(None, 'ThreeCreeks', 'LowerReach', self.get_value('N', date))

        # Lower Reach (Piute to Vermillion)
        system.add_reach_diversion(None, 'LowerReach', 'Mills', self.get_value('O', date))
        system.add_reach_diversion(None, 'LowerReach', 'SBend', self.get_value('P', date))
        system.add_reach_diversion(None, 'LowerReach', 'Loss-1', 0)
        #system.add_reach_diversion(None, 'LowerReach', 'SValley', self.get_value('R', date))
        system.add_reach_diversion(None, 'LowerReach', 'SValley', self.get_value('AJ', date) )
        system.add_reach_diversion(None, 'LowerReach', 'Piute&Losses', self.get_value('S', date)*(1/self.get_value('AF', date)) )
        #system.add_reach_diversion(None, 'LowerReach', 'PiuteLosses', self.get_value('S', date)*(1/self.get_value('AF', date)-1) )
        system.add_reach_diversion(None, 'LowerReach', 'Joseph', self.get_value('Q', date))
        system.add_reach_diversion(None, 'LowerReach', 'Monroe', self.get_value('T', date))
        system.add_reach_diversion(None, 'LowerReach', 'Brooklyn', self.get_value('U', date))
        system.add_reach_diversion(None, 'LowerReach', 'Elsinore', self.get_value('V', date))
        system.add_reach_diversion(None, 'LowerReach', 'Richfield', self.get_value('W', date))
        system.add_reach_diversion(None, 'LowerReach', 'Annabella', self.get_value('X', date))
        system.add_reach_diversion(None, 'LowerReach', 'Vermillion', self.get_value('Y', date))
        system.add_reach_diversion(None, 'LowerReach', 'Loss-2', 0)
        system.add_connection(None, 'LowerReach', 'A', self.get_value('Z', date))


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
        system.add_transaction(id=10, priority=1, upper_limit= 4.0, npath=['LowerReach_GAINS', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('BM', date))

        # Three Creeks Release (from a tributary reservoir). 
        # Why is this a high priority transaction as opposed to a storage delivery?
        # How can we keep the unused portion from being sent back to natural flow?
        system.add_transaction(id=20, priority=2, upper_limit=None, npath=['ThreeCreeks', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('BL', date))
        

        # Flowing Wells
        system.add_transaction(id=30, priority=3, upper_limit= 1.5, npath=['LowerReach_GAINS', 'LowerReach', 'Brooklyn'], 
                               expected_value=self.get_value('BN', date))

        # 1st Priority
        system.add_transaction(id=60, priority=6, upper_limit=limit40, npath=['LowerReach_GAINS', 'LowerReach', 'SBend'], 
                               expected_value= self.get_value('BO', date) # lower reach
                               )

        # Primary from Lower Reach Gains
        system.add_transaction(id=70, priority=7, upper_limit= 1.23    , npath=['LowerReach_GAINS', 'LowerReach', 'Mills'], expected_value= self.get_value('BQ', date))
        system.add_transaction(id=71, priority=7, upper_limit=10.9     , npath=['LowerReach_GAINS', 'LowerReach', 'SBend'], expected_value= self.get_value('BR', date) )
        system.add_transaction(id=72, priority=7, upper_limit=2.56+25.9, npath=['LowerReach_GAINS', 'LowerReach', 'Joseph'], expected_value= self.get_value('BT', date) )
        system.add_transaction(id=73, priority=7, upper_limit=limit73  , npath=['LowerReach_GAINS', 'LowerReach', 'SValley'], expected_value= self.get_value('BU', date) )
        system.add_transaction(id=74, priority=7, upper_limit=47.9     , npath=['LowerReach_GAINS', 'LowerReach', 'Monroe'], expected_value= self.get_value('BV', date) )
        system.add_transaction(id=75, priority=7, upper_limit=29.77    , npath=['LowerReach_GAINS', 'LowerReach', 'Brooklyn'], expected_value= self.get_value('BW', date))
        system.add_transaction(id=76, priority=7, upper_limit=19.92    , npath=['LowerReach_GAINS', 'LowerReach', 'Elsinore'], expected_value= self.get_value('BX', date) )
        system.add_transaction(id=77, priority=7, upper_limit=85.9     , npath=['LowerReach_GAINS', 'LowerReach', 'Richfield'], expected_value= self.get_value('BY', date))
        system.add_transaction(id=78, priority=7, upper_limit=30.4     , npath=['LowerReach_GAINS', 'LowerReach', 'Annabella'], expected_value= self.get_value('BZ', date) )
        system.add_transaction(id=79, priority=7, upper_limit=37.8     , npath=['LowerReach_GAINS', 'LowerReach', 'Vermillion'], expected_value= self.get_value('CA', date))
        
        # Second Class
        system.add_transaction(id=80, priority=8, upper_limit=limit80, npath=['LowerReach_GAINS', 'LowerReach', 'SValley'], expected_value= self.get_value('CC', date))

        # Third Class
        system.add_transaction(id=90, priority=9, upper_limit=11.5, npath=['LowerReach_GAINS', 'LowerReach', 'SValley'], expected_value= self.get_value('CD', date))

        # New Storage Zone A 
        system.add_transaction(id=100, priority=10, upper_limit=None, npath=['LowerReach_GAINS', 'LowerReach', 'A'], expected_value= self.get_value('CE', date))
        
        # Piute High Water Apportionment
        system.add_transaction(id=110, priority=11, upper_limit=None, npath=['LowerReach_GAINS', 'LowerReach', 'Piute&Losses'], expected_value= self.get_value('CF', date))


        # ---

        
        # Otter Creek Guarantee
        system.add_transaction(id=120, priority=12, upper_limit=0.92, 
            npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'LowerReach', 'Piute&Losses'], 
                               expected_value=self.get_value('EO', date))
        
        # Trxns 13, 14 not included because they are used for reach storage,
        #   and I'm having those values be user-specified.


        # Price Spring (Piute Subreach)	61-2069 to Piute Storage
        system.add_transaction(id=150, priority=15, upper_limit=1.78, npath=['PiuteReach_GAINS', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DM', date))

        # 1st Priority
        system.add_transaction(id=160, priority=16, upper_limit=limit40, npath=['PiuteReach', 'LowerReach', 'SBend'], 
                               expected_value= self.get_value('DJ', date) # Piute reach
                                             + self.get_value('EF', date) # Upper reach
                               )


        # Primary Barnson Spring (Piute Subreach) 61-2070
        # This needs to be pro-rated along with a 22 cfs right -- both are rights
        # to the gains in a specified reach, and the decree says what that quantity
        # should be.
        system.add_transaction(id=170, priority=17, upper_limit=12, npath=['PiuteReach_GAINS', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DL', date))
        
        # Piute Storage from East Fork - 61-2068
        system.add_transaction(id=180, priority=18, upper_limit=limit180, npath=['EastForkReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DI', date))

        # % Feeder Canal to Otter Creek Storage
        # Why the 33%? Is this an approximation of what was actually delivered to 
        # the reservoir or is it a limit on the water right?
        # TODO - update the logic according to what the 33% means...
        system.add_transaction(id=190, priority=19, upper_limit=perc190*self.get_value('A', date), npath=['EastForkReach', 'OtterCreekResv'], 
                               expected_value=self.get_value('DK', date))

        # Otter Creek "Gain"
        system.add_transaction(id=200, priority=20, upper_limit=None, npath=['OtterCreekReach', 'OtterCreekResv'], 
                               lower_limit=None,
                               expected_value=self.get_value('DU', date)
                               )

        # Trxns 21, 22 not included because they are used for reach storage,
        #   and I'm having those values be user-specified.

        # 61-858
        system.add_transaction(id=230, priority=23, upper_limit=limit230, npath=['SouthForkReach', 'PiuteReach', 'LowerReach', 'Piute&Losses'], 
                               expected_value=self.get_value('CX', date))
        
        # 61-2065 Mitchell Slough (SF Subreach)
        system.add_transaction(id=241, priority=24, upper_limit=7.5, npath=['SouthForkReach', 'PiuteReach', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('CW', date))
        
        # 61-2065 South Fork (SF Subreach)
        system.add_transaction(id=242, priority=24, upper_limit=0.84, npath=['SouthForkReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('CV', date))
        
        # Otter Creek Guarantee, 61-2103 et al
        system.add_transaction(id=250, priority=25, upper_limit=13, npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'LowerReach', 'SBend'], 
                               expected_value=self.get_value('EL', date))


        # 26 - 1st Priority

        # Primary from Upper Reach
        system.add_transaction(id=700, priority=27, upper_limit= 1.23    , limited_by_id=70, npath=None, child_series_name='Mills-Primary')
        system.add_transaction(id=701, series_name='Mills-Primary', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'Mills'], 
                               expected_value= self.get_value('EP1', date) )
        system.add_transaction(id=702, series_name='Mills-Primary', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EP2', date)
                               )
        system.add_transaction(id=710, priority=27, upper_limit=10.9     , limited_by_id=71, npath=None, child_series_name='SBend-Primary')
        system.add_transaction(id=711, series_name='SBend-Primary', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'SBend'], 
                               expected_value= self.get_value('EQ1', date) )
        system.add_transaction(id=712, series_name='SBend-Primary', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EQ2', date)
                               )
        system.add_transaction(id=720, priority=27, upper_limit=2.56+25.9, limited_by_id=72, npath=None, child_series_name='Joseph-Primary')
        system.add_transaction(id=721, series_name='Joseph-Primary', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'Joseph'], 
                               expected_value= self.get_value('ER1', date) )
        system.add_transaction(id=722, series_name='Joseph-Primary', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('ER2', date)
                               )
        system.add_transaction(id=730, priority=27, upper_limit=limit73  , limited_by_id=73, npath=None, child_series_name='SValley-Primary')
        system.add_transaction(id=731, series_name='SValley-Primary', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'SValley'], 
                               expected_value= self.get_value('ES1', date) )
        system.add_transaction(id=732, series_name='SValley-Primary', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('ES2', date)
                               )
        system.add_transaction(id=740, priority=27, upper_limit=47.9     , limited_by_id=74, npath=None, child_series_name='Monroe-Primary')
        system.add_transaction(id=741, series_name='Monroe-Primary', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'Monroe'], 
                               expected_value= self.get_value('ET1', date) )
        system.add_transaction(id=742, series_name='Monroe-Primary', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('ET2', date)
                               )
        system.add_transaction(id=750, priority=27, upper_limit=29.77    , limited_by_id=75, npath=None, child_series_name='Brooklyn-Primary')
        system.add_transaction(id=751, series_name='Brooklyn-Primary', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'Brooklyn'], 
                               expected_value= self.get_value('EU1', date) )
        system.add_transaction(id=752, series_name='Brooklyn-Primary', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EU2', date)
                               )
        system.add_transaction(id=760, priority=27, upper_limit=19.92    , limited_by_id=76, npath=None, child_series_name='Elsinore-Primary')
        system.add_transaction(id=761, series_name='Elsinore-Primary', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'Elsinore'], 
                               expected_value= self.get_value('EV1', date) )
        system.add_transaction(id=762, series_name='Elsinore-Primary', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EV2', date)
                               )
        system.add_transaction(id=770, priority=27, upper_limit=85.9     , limited_by_id=77, npath=None, child_series_name='Richfield-Primary')
        system.add_transaction(id=771, series_name='Richfield-Primary', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'Richfield'], 
                               expected_value= self.get_value('EW1', date) )
        system.add_transaction(id=772, series_name='Richfield-Primary', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EW2', date)
                               )
        system.add_transaction(id=780, priority=27, upper_limit=30.4     , limited_by_id=78, npath=None, child_series_name='Annabella-Primary')
        system.add_transaction(id=781, series_name='Annabella-Primary', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'Annabella'], 
                               expected_value= self.get_value('EX1', date) )
        system.add_transaction(id=782, series_name='Annabella-Primary', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EX2', date)
                               )
        system.add_transaction(id=791, priority=27, upper_limit=37.8     , limited_by_id=79, npath=['PiuteReach', 'LowerReach', 'Vermillion'], 
                               expected_value= self.get_value('EY', date)
                               )





        # Second Class
        system.add_transaction(id=800, priority=28, upper_limit=None, limited_by_id=80, npath=None, child_series_name='SValley-2nd')
        system.add_transaction(id=801, series_name='SValley-2nd', priority=1, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'SValley'], 
                               expected_value= self.get_value('FB1', date))
        system.add_transaction(id=802, series_name='SValley-2nd', priority=2, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('FB2', date)
                               )

        # Third Class
        system.add_transaction(id=290, priority=29, upper_limit=None, limited_by_id=90, npath=['PiuteReach', 'LowerReach', 'SValley'], 
                               expected_value= self.get_value('FC', date)
                               )

        # New Storage Zone A 
        system.add_transaction(id=300, priority=30, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'A'], 
                               expected_value= self.get_value('FD', date)
                               )
        
        # Piute High Water Apportionment
        system.add_transaction(id=311, priority=31, upper_limit=None, npath=['PiuteReach', 'LowerReach', 'Piute&Losses'], 
                               expected_value= self.get_value('FE1', date)
                               )
        system.add_transaction(id=312, priority=31, upper_limit=None, npath=['PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('FE2', date)
                               )


        
        # Exchange from Piute back up to Otter Creek
        system.add_transaction(id=10001, priority=10001, upper_limit=None, 
                               npath=['PiuteResv', 'PiuteReach', 'EastForkReach', 'OtterCreekReach', 'OtterCreekResv'] )

        # Otter Creek storage deliveries - Implied by formula for 'OCR' in the WCAT model.
        # OCR = PREV(OCR)+GA/c-EO-EL+DK+DU-F-AB-AC
        # [Otter Creek Reservoir Storage] = PREV(OCR) + [Otter Creek Transfers IN] - [OCG to Piute Diversion] - [OCG to South Bend Diversion] + [Feeder Canal Apportionment] - [Kingston Total Diversion] - [Evap] - [Conveyance Loss]
        #
        # Otter Creek storage deliveries to Piute:
        system.add_transaction(id=10002, priority=10002, upper_limit=None, npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'LowerReach', 'Piute&Losses'] ) 
        system.add_transaction(id=10003, priority=10003, upper_limit=None, npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'LowerReach', 'SBend'] ) 
        # Otter Creek storage deliveries to Kingston diversions:
        system.add_transaction(id=10004, priority=10004, upper_limit=None, npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'KingstonDiv'] ) 
        system.add_transaction(id=10005, priority=10004, upper_limit=None, npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'KingstonPipe'] )
        system.add_transaction(id=10006, priority=10004, upper_limit=None, npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'Zabriskie'] )
        system.add_transaction(id=10007, priority=10004, upper_limit=None, npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'Allen'] )
        system.add_transaction(id=10008, priority=10004, upper_limit=None, npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'KingstonMain'] )
        system.add_transaction(id=10009, priority=10004, upper_limit=None, npath=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'KingstonGleave'] )



        # Allow circular East Fork -> Otter Creek Resv -> East Fork flow. This is critical to getting it to work.
        system.add_transaction(id=10011, priority=10011, upper_limit=None, 
                               npath=['EastForkReach', 'OtterCreekResv', 'OtterCreekReach', 'EastForkReach'] )
        

        return system

    def build_and_test(self,date:str, test_message=''):
        system = self.build(date, test_message)
        system.solve()
        system.assert_variables_equal_expected(message=test_message)



    def test_combined_20230401(self):
        self.build_and_test(date='2023-04-01')
    def test_combined_20230402(self):
        self.build_and_test(date='2023-04-02')
    def test_combined_20230403(self):
        self.build_and_test(date='2023-04-03')
    def test_combined_20230404(self):
        self.build_and_test(date='2023-04-04')
    def test_combined_20230405(self):
        self.build_and_test(date='2023-04-05')
    def test_combined_20230406(self):
        self.build_and_test(date='2023-04-06')
    def test_combined_20230407(self):
        self.build_and_test(date='2023-04-07')
    def test_combined_20230408(self):
        self.build_and_test(date='2023-04-08')
    def test_combined_20230409(self):
        self.build_and_test(date='2023-04-09')
    def test_combined_20230410(self):
        self.build_and_test(date='2023-04-10')
    def test_combined_20230411(self):
        self.build_and_test(date='2023-04-11')
    def test_combined_20230412(self):
        self.build_and_test(date='2023-04-12')
    def test_combined_20230413(self):
        self.build_and_test(date='2023-04-13')
    def test_combined_20230414(self):
        self.build_and_test(date='2023-04-14')
    def test_combined_20230415(self):
        self.build_and_test(date='2023-04-15')
    def test_combined_20230416(self):
        self.build_and_test(date='2023-04-16')
    def test_combined_20230417(self):
        self.build_and_test(date='2023-04-17')
    def test_combined_20230418(self):
        self.build_and_test(date='2023-04-18')
    def test_combined_20230419(self):
        self.build_and_test(date='2023-04-19')
    def test_combined_20230420(self):
        self.build_and_test(date='2023-04-20')
    def test_combined_20230421(self):
        self.build_and_test(date='2023-04-21')
    def test_combined_20230422(self):
        self.build_and_test(date='2023-04-22')
    def test_combined_20230423(self):
        self.build_and_test(date='2023-04-23')
    def test_combined_20230424(self):
        self.build_and_test(date='2023-04-24')
    def test_combined_20230425(self):
        self.build_and_test(date='2023-04-25')
    def test_combined_20230426(self):
        self.build_and_test(date='2023-04-26')
    def test_combined_20230427(self):
        self.build_and_test(date='2023-04-27')
    def test_combined_20230428(self):
        self.build_and_test(date='2023-04-28')
    def test_combined_20230429(self):
        self.build_and_test(date='2023-04-29')
    def test_combined_20230430(self):
        self.build_and_test(date='2023-04-30')
        
    
    def test_combined_202304(self):
        from datetime import date, timedelta
        for d in (date(2023,4,1) + timedelta(n) for n in range(30)):
            yyyy_mm_dd = d.isoformat()
            self.build_and_test(date=yyyy_mm_dd, test_message=yyyy_mm_dd + ': ')




    def test_combined_202305(self):
        from datetime import date, timedelta
        for d in (date(2023,5,1) + timedelta(n) for n in range(31)):
            yyyy_mm_dd = d.isoformat()
            self.build_and_test(date=yyyy_mm_dd, test_message=yyyy_mm_dd + ': ')

    def test_combined_202306(self):
        from datetime import date, timedelta
        for d in (date(2023,6,1) + timedelta(n) for n in range(30)):
            yyyy_mm_dd = d.isoformat()
            self.build_and_test(date=yyyy_mm_dd, test_message=yyyy_mm_dd + ': ')
    
    
    def test_sankey(self):
        import json
        from datetime import date, timedelta
        graph = {}
        var_values = {"dates":[], "variables":{}, "arcs":{}}

        for d in (date(2023,4,1) + timedelta(n) for n in range(165)):
            # Run 
            yyyy_mm_dd = d.isoformat()
            system = self.build(date=yyyy_mm_dd)
            system.solve()

            # Extract the data.
            graph, this_var_values = system.to_sankey_data(use_expected_values=True)

            # Merge this day's data with the previous data.
            var_values['dates'].append(yyyy_mm_dd)
            for v in this_var_values:
                if v not in var_values['variables']:
                    var_values['variables'][v] = []
                var_values['variables'][v].append( this_var_values[v][0] )

        with open('sankey.js', 'w') as f:
            f.write('let graph_data = ' + json.dumps(graph, indent=2))
            f.write('\n\n')
            f.write('let daily_values = ' + json.dumps(var_values, indent=2))
        #print(json.dumps(graph))
        #print(json.dumps(var_values))