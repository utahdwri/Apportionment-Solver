import unittest
from solver.apportionment_solver import ApportionmentSolver_v2


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
    

    def build_and_test_lower_reach(self, date:str):
        """       

        """

        system = ApportionmentSolver_v2()

        system.add_reach('C')
        system.add_reach('ThreeCreeks')
        system.add_reach('B', expected_gain=self.get_value('AL', date)-self.get_value('N', date)) # note: Jared does not count 'imports' in his divertable flow calculation
        system.add_reach('A')

        system.add_connection('ThreeCreeks', 'B', self.get_value('N', date))

        system.add_connection('C', 'B', self.get_value('M', date))
        system.add_reach_diversion('B', 'Mills', self.get_value('O', date))
        system.add_reach_diversion('B', 'SBend', self.get_value('P', date))
        system.add_reach_diversion('B', 'Loss-1', 0)
        #system.add_reach_diversion('B', 'SValley', self.get_value('R', date))
        system.add_reach_diversion('B', 'SValley', self.get_value('AJ', date) )
        system.add_reach_diversion('B', 'Piute&Losses', self.get_value('S', date)*(1/self.get_value('AF', date)) )
        #system.add_reach_diversion('B', 'PiuteLosses', self.get_value('S', date)*(1/self.get_value('AF', date)-1) )
        system.add_reach_diversion('B', 'Joseph', self.get_value('Q', date))
        system.add_reach_diversion('B', 'Monroe', self.get_value('T', date))
        system.add_reach_diversion('B', 'Brooklyn', self.get_value('U', date))
        system.add_reach_diversion('B', 'Elsinore', self.get_value('V', date))
        system.add_reach_diversion('B', 'Richfield', self.get_value('W', date))
        system.add_reach_diversion('B', 'Annabella', self.get_value('X', date))
        system.add_reach_diversion('B', 'Vermillion', self.get_value('Y', date))
        system.add_reach_diversion('B', 'Loss-2', 0)
        system.add_connection('B', 'A', self.get_value('Z', date))

        # Taylor Fish Pond Springs
        # This 4cfs was essentially moved from near Marysvale. Why is it first 
        # in priority?
        system.add_transaction(id=10, priority=101, limit= 4.0, path=['B', 'SValley'], expected_value=self.get_value('BM', date))

        # Three Creeks Release (from a tributary reservoir). 
        # Why is this a high priority transaction as opposed to a storage delivery?
        # How can we keep the unused portion from being sent back to natural flow?
        system.add_transaction(id=20, priority=102, limit=None, path=['ThreeCreeks', 'B', 'SValley'], expected_value=self.get_value('BL', date))
        system.add_transaction(id=21, priority=102.1, limit=None, path=['ThreeCreeks', 'B', 'A'] )

        # Flowing Wells
        system.add_transaction(id=30, priority=103, limit= 1.5, path=['B', 'Brooklyn'], expected_value=self.get_value('BN', date))

        # 1st Priority
        limit = self.get_limit(date, ['04-01','04-16','05-01','10-01'],[30, 31.25, 1.25, 0])
        system.add_transaction(id=40, priority=104, limit=limit, path=['B', 'SBend'], expected_value=self.get_value('BO', date))

        # Primary
        system.add_transaction(id=70, priority=107, limit= 1.2, path=['B', 'Mills'], expected_value=self.get_value('BQ', date))
        system.add_transaction(id=71, priority=107, limit=10.9, path=['B', 'SBend'], expected_value=self.get_value('BR', date))
        system.add_transaction(id=72, priority=107, limit=28.5, path=['B', 'Joseph'], expected_value=self.get_value('BT', date))
        limit = self.get_limit(date, ['04-01','05-01','10-01'], [55.14, 5.14, 0])
        system.add_transaction(id=73, priority=107, limit=limit, path=['B', 'SValley'], expected_value=self.get_value('BU', date))
        system.add_transaction(id=74, priority=107, limit=47.9, path=['B', 'Monroe'], expected_value=self.get_value('BV', date))
        system.add_transaction(id=75, priority=107, limit=29.8, path=['B', 'Brooklyn'], expected_value=self.get_value('BW', date))
        system.add_transaction(id=76, priority=107, limit=19.9, path=['B', 'Elsinore'], expected_value=self.get_value('BX', date))
        system.add_transaction(id=77, priority=107, limit=85.9, path=['B', 'Richfield'], expected_value=self.get_value('BY', date))
        system.add_transaction(id=78, priority=107, limit=30.4, path=['B', 'Annabella'], expected_value=self.get_value('BZ', date))
        system.add_transaction(id=79, priority=107, limit=37.8, path=['B', 'Vermillion'], expected_value=self.get_value('CA', date))

        # Second Class
        limit = self.get_limit(date, ['05-01','10-01'], [68, 0])
        system.add_transaction(id=80, priority=108, limit=limit, path=['B', 'SValley'], expected_value=self.get_value('CC', date))

        # Third Class
        system.add_transaction(id=90, priority=109, limit=11.5, path=['B', 'SValley'], expected_value=self.get_value('CD', date))

        # New Storage Zone A 
        system.add_transaction(id=100, priority=110, limit=None, path=['B', 'A'], expected_value=self.get_value('CE', date))
        
        # Piute High Water Apportionment
        system.add_transaction(id=110, priority=111, limit=None, path=['B', 'Piute&Losses'], expected_value=self.get_value('CF', date))
        
        '''
        # Storage
        system.add_transaction(id=200, priority=9999, limit=None, path=['C', 'B', 'Piute&Losses'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'Mills'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'SBend'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'SValley'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'Piute&Losses'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'Joseph'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'Monroe'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'Brooklyn'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'Elsinore'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'Richfield'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'Annabella'] )
        system.add_transaction(id=201, priority=9999, limit=None, path=['C', 'B', 'Vermillion'] )
        '''


        system.solve()
        system.assert_variables_equal_expected()

   

    def build_and_test_combined(self, date:str, test_message=''):
        """       

        """

        system = ApportionmentSolver_v2()

        # ------------------------------------------------------------------------------------------
        # NETWORK:
        # ------------------------------------------------------------------------------------------

        
        system.add_reach('OtterCreekReach', storage_chg=0)
        system.add_reach('EastForkReach', storage_chg=-(self.get_value('DC', date)-self.get_value('DV', date)))
        system.add_reach('SouthForkReach', storage_chg=0)
        system.add_reach('PiuteReach', storage_chg=-(self.get_value('DG', date)-self.get_value('DF', date)))
        system.add_reach('ThreeCreeks', storage_chg=0)
        system.add_reach('LowerReach', storage_chg=0, 
                        # expected_gain=self.get_value('AL', date)-self.get_value('N', date)) # note: Jared does not count 'imports' in his divertable flow calculation
        )
        system.add_reach('A')

        # Otter Creek Reach
        system.add_reach_reservoir('OtterCreekReach', 'OtterCreekResv', 
                                   storage_chg=self.get_value('AA', date),
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
                                   storage_chg=self.get_value('AD', date),
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
        system.add_transaction(id=10, priority=101, limit= 4.0, path=['LowerReach_GAINS', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('BM', date))

        # Three Creeks Release (from a tributary reservoir). 
        # Why is this a high priority transaction as opposed to a storage delivery?
        # How can we keep the unused portion from being sent back to natural flow?
        system.add_transaction(id=20, priority=102, limit=None, path=['ThreeCreeks', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('BL', date))
        

        # Flowing Wells
        system.add_transaction(id=30, priority=103, limit= 1.5, path=['LowerReach_GAINS', 'LowerReach', 'Brooklyn'], 
                               expected_value=self.get_value('BN', date))

        # 1st Priority
        system.add_transaction(id=40, priority=104, limit=limit40, path=['LowerReach', 'SBend'], 
                               expected_value= self.get_value('BO', date) # lower reach
                                             + self.get_value('DJ', date) # Piute reach
                                             + self.get_value('EF', date) # Upper reach
                               )

        # Primary
        primary_priority = 9107
        pstore_priority = 9107
        system.add_transaction(id=70, priority=primary_priority, limit= 1.23, path=['LowerReach', 'Mills'], 
                               expected_value= self.get_value('BQ', date)
                                             + self.get_value('EP1', date) )
        system.add_transaction(id=71, priority=primary_priority, limit=10.9, path=['LowerReach', 'SBend'], 
                               expected_value= self.get_value('BR', date)
                                             + self.get_value('EQ1', date) )
        system.add_transaction(id=72, priority=primary_priority, limit=2.56+25.9, path=['LowerReach', 'Joseph'], 
                               expected_value= self.get_value('BT', date)
                                             + self.get_value('ER1', date) )
        system.add_transaction(id=73, priority=primary_priority, limit=limit73, path=['LowerReach', 'SValley'], 
                               expected_value= self.get_value('BU', date)
                                             + self.get_value('ES1', date) )
        system.add_transaction(id=74, priority=primary_priority, limit=47.9, path=['LowerReach', 'Monroe'], 
                               expected_value= self.get_value('BV', date)
                                             + self.get_value('ET1', date) )
        system.add_transaction(id=75, priority=primary_priority, limit=29.77, path=['LowerReach', 'Brooklyn'], 
                               expected_value= self.get_value('BW', date)
                                             + self.get_value('EU1', date) )
        system.add_transaction(id=76, priority=primary_priority, limit=19.92, path=['LowerReach', 'Elsinore'], 
                               expected_value= self.get_value('BX', date)
                                             + self.get_value('EV1', date) )
        system.add_transaction(id=77, priority=primary_priority, limit=85.9, path=['LowerReach', 'Richfield'], 
                               expected_value= self.get_value('BY', date)
                                             + self.get_value('EW1', date) )
        system.add_transaction(id=78, priority=primary_priority, limit=30.4, path=['LowerReach', 'Annabella'], 
                               expected_value= self.get_value('BZ', date)
                                             + self.get_value('EX1', date) )
        system.add_transaction(id=79, priority=primary_priority, limit=37.8, path=['LowerReach', 'Vermillion'], 
                               expected_value= self.get_value('CA', date)
                                             + self.get_value('EY', date)
                               )

        # Second Class
        system.add_transaction(id=80, priority=primary_priority+1, limit=limit80, path=['LowerReach', 'SValley'], 
                               expected_value= self.get_value('CC', date)
                                             + self.get_value('FB1', date))

        # Third Class
        system.add_transaction(id=90, priority=primary_priority+2, limit=11.5, path=['LowerReach', 'SValley'], 
                               expected_value= self.get_value('CD', date)
                                             + self.get_value('FC', date)
                               )



        # New Storage Zone A 
        system.add_transaction(id=100, priority=9110, limit=None, path=['LowerReach', 'A'], 
                               expected_value= self.get_value('CE', date)
                                             + self.get_value('FD', date)
                               )
        
        # Piute High Water Apportionment
        # 1/7 - moved to the last priority to get 2023-04-22 to work.
        system.add_transaction(id=110, priority=9111, limit=None, path=['LowerReach', 'Piute&Losses'], 
                               expected_value= self.get_value('CF', date)
                                             + self.get_value('FE1', date)
                               
                               )





        # Otter Creek Guarantee
        # This is a (decreed) storage delivery, so does it need to have such a high priority?
        # This delivery is actually to the Piute Diversion (in the lower reach)
        system.add_transaction(id=120, priority=112, limit=0.92, 
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
        system.add_transaction(id=150, priority=115, limit=1.78, path=['PiuteReach_GAINS', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DM', date))

        # 1st Priority (Piute Subreach)	63-3018 to South Bend Diversion
        #       Seems like this right should be senior to all of the proportional 
        #       ones, as it's "a primary right against all parties to Section A"
        #     ? I don't see why it should be limited to the gain in the Piute reach...
        system.add_transaction(id=160, priority=116, limit=0, path=['PiuteReach_GAINS', 'PiuteReach', 'LowerReach', 'SBend'], 
                               expected_value=self.get_value('DJ', date))

        # Primary Barnson Spring (Piute Subreach) 61-2070
        # This needs to be pro-rated along with a 22 cfs right -- both are rights
        # to the gains in a specified reach, and the decree says what that quantity
        # should be.
        system.add_transaction(id=170, priority=117, limit=12, path=['PiuteReach_GAINS', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DL', date))
        
        # Piute Storage from East Fork - 61-2068
        system.add_transaction(id=180, priority=118, limit=limit180, path=['EastForkReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DI', date))

        # % Feeder Canal to Otter Creek Storage
        # Why the 33%? Is this an approximation of what was actually delivered to 
        # the reservoir or is it a limit on the water right?
        # TODO - update the logic according to what the 33% means...
        system.add_transaction(id=190, priority=119, limit=perc190*self.get_value('A', date), path=['EastForkReach', 'OtterCreekResv'], 
                               expected_value=self.get_value('DK', date))

        # Trxn 20 moved, temporaraly?

        # Trxns 21, 22 not included because they are used for reach storage,
        #   and I'm having those values be user-specified.

        # 61-858
        system.add_transaction(id=230, priority=123, limit=limit230, path=['SouthForkReach', 'PiuteReach', 'LowerReach', 'Piute&Losses'], 
                               expected_value=self.get_value('CX', date))
        
        # 61-2065 Mitchell Slough (SF Subreach)
        system.add_transaction(id=241, priority=124, limit=7.5, path=['SouthForkReach', 'PiuteReach', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('CW', date))
        
        # 61-2065 South Fork (SF Subreach)
        system.add_transaction(id=242, priority=124, limit=0.84, path=['SouthForkReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('CV', date))
        
        # Otter Creek Guarantee, 61-2103 et al
        system.add_transaction(id=250, priority=125, limit=13, path=['OtterCreekResv', 'OtterCreekReach', 'EastForkReach', 'PiuteReach', 'LowerReach', 'SBend'], 
                               expected_value=self.get_value('EL', date))

        # Trxns 26 is combined here with a previous entry


        # Primary storage:
        '''system.add_transaction(id=702, priority=pstore_priority, limit=None, limited_by_id=70, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EP2', date)
                               )
        system.add_transaction(id=712, priority=pstore_priority, limit=None, limited_by_id=71, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EQ2', date)
                               )
        system.add_transaction(id=722, priority=pstore_priority, limit=None, limited_by_id=72, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('ER2', date)
                               )
        system.add_transaction(id=732, priority=pstore_priority, limit=None, limited_by_id=73, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('ES2', date)
                               )
        system.add_transaction(id=742, priority=pstore_priority, limit=None, limited_by_id=74, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('ET2', date)
                               )
        system.add_transaction(id=752, priority=pstore_priority, limit=None, limited_by_id=75, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EU2', date)
                               )
        system.add_transaction(id=762, priority=pstore_priority, limit=None, limited_by_id=76, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EV2', date)
                               )
        system.add_transaction(id=772, priority=pstore_priority, limit=None, limited_by_id=77, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EW2', date)
                               )
        system.add_transaction(id=782, priority=pstore_priority, limit=None, limited_by_id=77, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('EX2', date)
                               )
        
        # 2nd Class
        system.add_transaction(id=802, priority=pstore_priority+1, limit=None, limited_by_id=80, path=['LowerReach', 'PiuteReach', 'PiuteResv'], 
                               expected_value= self.get_value('FB2', date)
                               )'''






        # Piute Storage, 63-3015
        system.add_transaction(id=131, priority=9131, limit=None, path=['PiuteReach', 'PiuteResv'], 
                        expected_value= self.get_value('FE2', date)
                        )
        
        # Exchange from Piute back up to Otter Creek
        system.add_transaction(id=10001, priority=10001, limit=None, 
                               path=['PiuteResv', 'PiuteReach', 'EastForkReach', 'OtterCreekReach', 'OtterCreekResv'] )
        

        # Otter Creek "Gain"
        # !! I wonder if this is a mistake in the WCAT model: 
        #    The entire Otter Ck gain is being assigned to Otter Creek storage, 
        #    even though some of this is released to E Fork. What is released below the resv cannot be held in storage...
        system.add_transaction(id=200, priority=120*1000, limit=None, path=['OtterCreekReach', 'OtterCreekResv'], 
                               expected_value=self.get_value('DU', date)
                               )
        

        system.solve()
        system.assert_variables_equal_expected(message=test_message)





    def test_combined_20230401(self):
        self.build_and_test_combined(date='2023-04-01')

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