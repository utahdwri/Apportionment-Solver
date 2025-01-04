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

        system.add_reach_connection('ThreeCreeks', 'B', self.get_value('N', date))

        system.add_reach_connection('C', 'B', self.get_value('M', date))
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
        system.add_reach_connection('B', 'A', self.get_value('Z', date))

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

   

    def build_and_test_combined(self, date:str):
        """       

        """

        system = ApportionmentSolver_v2()

        # ------------------------------------------------------------------------------------------
        # NETWORK:
        # ------------------------------------------------------------------------------------------

        system.add_reach('EastForkReach', storage_chg=self.get_value('DC', date)-self.get_value('DV', date))
        system.add_reach('SouthForkReach')
        system.add_reach('PiuteReach', storage_chg=self.get_value('DG', date)-self.get_value('DF', date))
        system.add_reach('ThreeCreeks')
        system.add_reach('LowerReach', 
                         expected_gain=self.get_value('AL', date)-self.get_value('N', date)) # note: Jared does not count 'imports' in his divertable flow calculation
        system.add_reach('A')

        # East Fork
        system.add_reach_reservoir('EastForkReach', 'OtterCreekResv', 
                                   storage_chg=self.get_value('AA', date),
                                   storage_loss=self.get_value('AB', date) )
        system.add_reach_diversion('EastForkReach', 'KingstonDiv', 
                                   flow=self.get_value('F1', date) )

        #
        system.add_reach_connection('EastForkReach', 'PiuteReach', 
                                    flow=self.get_value('G', date) )
        system.add_reach_connection('SouthForkReach', 'PiuteReach', 
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
        system.add_reach_connection('PiuteReach', 'LowerReach', self.get_value('M', date))
        system.add_reach_connection('ThreeCreeks', 'LowerReach', self.get_value('N', date))

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
        system.add_reach_connection('LowerReach', 'A', self.get_value('Z', date))


        # ------------------------------------------------------------------------------------------
        # TRANSACTIONS:
        # ------------------------------------------------------------------------------------------
        limit40 = self.get_limit(date, ['04-01','04-16','05-01','10-01'],[30, 31.25, 1.25, 0])
        limit73 = self.get_limit(date, ['04-01','05-01','10-01'], [55.14, 5.14, 0])
        limit80 = self.get_limit(date, ['05-01','10-01'], [68, 0])


        ''' 1/3 - rather than shepherd the flows to piute, I need to constrain some transactions to only take 
                  from the gains of a reach.

        # 1/1 - Shepherd South Fork flows to Piute. WCAT model does not count this inflow to Piute Reach 
        # as divertable flow so I need to make it unavailable to subsequent transactions. I cannot 
        # send it downstream because on 2023-04-01 there is no outflow below Piute. So maybe this inflow
        # belongs to Piute?
        # ??? - Why is South Fork excluded from divertable flow calcs?
        system.add_transaction(id=1, priority=1, limit=None, path=['SouthForkReach','PiuteReach','PiuteResv'], 
                               expected_value=self.get_value('H', date))
        
        # 1/1 - Similarly, Inflow to Piute Reach from East Fork cannot be apportioned to rights in Piute Reach...
        # ??? Why not? Would it be better to represent this as transactions comming from the reach gain?
        system.add_transaction(id=2, priority=1, limit=None, path=['EastForkReach','PiuteReach','PiuteResv'], 
                               expected_value=self.get_value('G', date))'''

        # Taylor Fish Pond Springs
        # This 4cfs was essentially moved from near Marysvale. Why is it first 
        # in priority?
        system.add_transaction(id=10, priority=101, limit= 4.0, path=['LowerReach', 'SValley'], 
                               expected_value=self.get_value('BM', date))

        # Three Creeks Release (from a tributary reservoir). 
        # Why is this a high priority transaction as opposed to a storage delivery?
        # How can we keep the unused portion from being sent back to natural flow?
        system.add_transaction(id=20, priority=102, limit=None, path=['ThreeCreeks', 'LowerReach', 'SValley'], 
                               expected_value=self.get_value('BL', date))
        # The WCAT model does not allow unused Three Creeks Releases to be apportioned to other rights,
        # so the following will ensure that any unused portion continues downstream.
        system.add_transaction(id=21, priority=102.1, limit=None, path=['ThreeCreeks', 'LowerReach', 'A'] )

        # Flowing Wells
        system.add_transaction(id=30, priority=103, limit= 1.5, path=['LowerReach', 'Brooklyn'], 
                               expected_value=self.get_value('BN', date))

        # 1st Priority
        system.add_transaction(id=40, priority=104, limit=limit40, path=['LowerReach', 'SBend'], 
                               expected_value= self.get_value('BO', date) # lower reach
                                             + self.get_value('DJ', date) # Piute reach
                                             + self.get_value('EF', date) # Upper reach
                               )

        # Primary
        system.add_transaction(id=70, priority=107, limit= 1.2, path=['LowerReach', 'Mills'], 
                               expected_value= self.get_value('BQ', date)
                                             + self.get_value('EP1', date)
                               )
        system.add_transaction(id=71, priority=107, limit=10.9, path=['LowerReach', 'SBend'], 
                               expected_value= self.get_value('BR', date)
                                             + self.get_value('EQ1', date)
                               )
        system.add_transaction(id=72, priority=107, limit=28.5, path=['LowerReach', 'Joseph'], 
                               expected_value= self.get_value('BT', date)
                                             + self.get_value('ER1', date)
                               )
        system.add_transaction(id=73, priority=107, limit=limit73, path=['LowerReach', 'SValley'], 
                               expected_value= self.get_value('BU', date)
                                             + self.get_value('ES1', date)
                               )
        system.add_transaction(id=74, priority=107, limit=47.9, path=['LowerReach', 'Monroe'], 
                               expected_value= self.get_value('BV', date)
                                             + self.get_value('ET1', date)
                               )
        system.add_transaction(id=75, priority=107, limit=29.8, path=['LowerReach', 'Brooklyn'], 
                               expected_value= self.get_value('BW', date)
                                             + self.get_value('EU1', date)
                               )
        system.add_transaction(id=76, priority=107, limit=19.9, path=['LowerReach', 'Elsinore'], 
                               expected_value= self.get_value('BX', date)
                                             + self.get_value('EV1', date)
                               )
        system.add_transaction(id=77, priority=107, limit=85.9, path=['LowerReach', 'Richfield'], 
                               expected_value= self.get_value('BY', date)
                                             + self.get_value('EW1', date)
                               )
        system.add_transaction(id=78, priority=107, limit=30.4, path=['LowerReach', 'Annabella'], 
                               expected_value= self.get_value('BZ', date)
                                             + self.get_value('EX1', date)
                               )
        system.add_transaction(id=79, priority=107, limit=37.8, path=['LowerReach', 'Vermillion'], 
                               expected_value= self.get_value('CA', date)
                                             + self.get_value('EY', date)
                               )

        # Second Class
        system.add_transaction(id=80, priority=108, limit=limit80, path=['LowerReach', 'SValley'], 
                               expected_value= self.get_value('CC', date)
                                             + self.get_value('FB1', date)
                               )

        # Third Class
        system.add_transaction(id=90, priority=109, limit=11.5, path=['LowerReach', 'SValley'], 
                               expected_value= self.get_value('CD', date)
                                             + self.get_value('FC', date)
                               )

        # New Storage Zone A 
        system.add_transaction(id=100, priority=110, limit=None, path=['LowerReach', 'A'], 
                               expected_value=self.get_value('CE', date))
        
        # Piute High Water Apportionment
        system.add_transaction(id=110, priority=111, limit=None, path=['LowerReach', 'Piute&Losses'], 
                               expected_value=self.get_value('CF', date))
        





        # Otter Creek Guarantee
        # This is a (decreed) storage delivery, so does it need to have such a high priority?
        # This delivery is actually to the Piute Diversion (in the lower reach)
        system.add_transaction(id=120, priority=112, limit=0.92, 
            path=['OtterCreekResv', 'EastForkReach', 'PiuteReach', 'LowerReach', 'Piute&Losses'], 
                               expected_value=self.get_value('EO', date))
        
        # Price Spring (Piute Subreach)	61-2069 to Piute Storage
        # Note: Since the right is for a spring, it's not entitled to any water 
        #       in the river, just the gain (from the spring, but I guess limiting
        #       it to the reach gain is good enough). The spring is shown on the hydro, 
        #       close to 61-105. I suppose this right is given this preferential
        #       position in the priority ordering because it is an import of sorts.
        system.add_transaction(id=150, priority=115, limit=1.78, path=['PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DM', date))

        # 1st Priority (Piute Subreach)	63-3018 to South Bend Diversion
        #       Seems like this right should be senior to all of the proportional 
        #       ones, as it's "a primary right against all parties to Section A"
        #     ? I don't see why it should be limited to the gain in the Piute reach...
        system.add_transaction(id=160, priority=116, limit=0, path=['PiuteReach', 'LowerReach', 'SBend'], 
                               expected_value=self.get_value('DJ', date))

        # Primary Barnson Spring (Piute Subreach) 61-2070
        # This needs to be pro-rated along with a 22 cfs right -- both are rights
        # to the gains in a specified reach, and the decree says what that quantity
        # should be.
        system.add_transaction(id=170, priority=117, limit=12, path=['PiuteReach', 'PiuteResv'], 
                               expected_value=self.get_value('DL', date))
        
        


        system.solve()
        system.assert_variables_equal_expected()



    def test_lower_reach_20230401(self):
        self.build_and_test_lower_reach(date='2023-04-01')
    def test_lower_reach_20230411(self):
        self.build_and_test_lower_reach(date='2023-04-11')
    def test_lower_reach_20230421(self):
        self.build_and_test_lower_reach(date='2023-04-21')

    def test_lower_reach_20230501(self):
        self.build_and_test_lower_reach(date='2023-05-01')
    def test_lower_reach_20230601(self):
        self.build_and_test_lower_reach(date='2023-06-01')
    def test_lower_reach_20230701(self):
        self.build_and_test_lower_reach(date='2023-07-01')



    def test_combined_20230401(self):
        self.build_and_test_combined(date='2023-04-01')
    def test_combined_20230501(self):
        self.build_and_test_combined(date='2023-05-01')