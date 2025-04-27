import unittest
from solver.apportionment_solver import ApportionmentSolver

class UpperSevier(unittest.TestCase):


    def get_value(self, name, date):
        import pandas as pd
        if not hasattr(self, '_df'):
            self._df = pd.read_csv("tests/example_problems/UPS-Leah_2023.csv", skiprows=1)
        
        df = self._df

        value = df.loc[df['Unnamed: 0'] == date][name].values[0]
        return float(value)
    
    


    def get_limit(self, date, dates, vals):
        mm_dd = date[-5:]
        for i in range(1,len(dates)):
            if mm_dd >= dates[i-1] and mm_dd < dates[i]:
                return vals[i-1]
        return 0
    


    def build(self, date:str):
        """       

        """
        from datetime import datetime, timedelta

        system = ApportionmentSolver()

        # ------------------------------------------------------------------------------------------
        # NETWORK:
        # ------------------------------------------------------------------------------------------

        ACFT2CFS = 1/1.9835


        prev_date = datetime.strptime(date, '%Y-%m-%d').date() - timedelta(days=1)
        ds = 0
        try:
            ds = self.get_value('N', date) - self.get_value('N', str(prev_date))
        except:
            pass

        system.add_reach('UpperReach', storage_chg=0)
        system.add_reach('LowerReach', storage_chg=ds )
        system.add_reach('A')

        # Otter Creek Reach
        system.add_reach_reservoir('UpperReach>OtterCreekResv', 'UpperReach', 'OtterCreekResv', 
                                   storage_chg= self.get_value('B', date) - self.get_value('D', date),
                                   #storage_loss=self.get_value('L', date)*ACFT2CFS 
                                   )

        # The transaction for var BO requires that these be merged together as one. We could split them 
        # out again, but we'd need to split BO into multiple seperate transactions.
        system.add_reach_diversion('>KingstonCombined', 'UpperReach', 'KingstonCombined', self.get_value('G', date) + self.get_value('H', date) + self.get_value('J', date) + self.get_value('K', date))
        
        system.add_reach_reservoir('UpperReach>PiuteResv', 'UpperReach', 'PiuteResv', 
                                   storage_chg=self.get_value('EV', date)*ACFT2CFS,
                                   storage_loss=self.get_value('AD', date)*ACFT2CFS)

        #
        system.add_connection('UpperReach>LowerReach', 'UpperReach', 'LowerReach', self.get_value('N', date))

        # Lower Reach (Piute to Vermillion)
        system.add_reach_diversion('>ConvLoss'       , 'LowerReach', 'ConvLoss'       , self.get_value('S', date)*0.06)
        system.add_reach_diversion('>MonroeSouthBend', 'LowerReach', 'MonroeSouthBend', self.get_value('Q', date))
        system.add_reach_diversion('>SValley'        , 'LowerReach', 'SValley'        , self.get_value('S', date) - self.get_value('T', date) )
        system.add_reach_diversion('>Piute'          , 'LowerReach', 'Piute'          , self.get_value('T', date) )
        system.add_reach_diversion('>PiuteLosses'    , 'LowerReach', 'PiuteLosses'    , self.get_value('HL', date) )
        system.add_reach_diversion('>Joseph'         , 'LowerReach', 'Joseph'         , self.get_value('R', date))
        system.add_reach_diversion('>Monroe'         , 'LowerReach', 'Monroe'         , self.get_value('U', date))
        system.add_reach_diversion('>Brooklyn'       , 'LowerReach', 'Brooklyn'       , self.get_value('V', date))
        system.add_reach_diversion('>Elsinore'       , 'LowerReach', 'Elsinore'       , self.get_value('W', date))
        system.add_reach_diversion('>Richfield'      , 'LowerReach', 'Richfield'      , self.get_value('X', date))
        system.add_reach_diversion('>Annabella'      , 'LowerReach', 'Annabella'      , self.get_value('Y', date))
        system.add_reach_diversion('>Vermillion'     , 'LowerReach', 'Vermillion'     , self.get_value('Z', date))
        system.add_connection('LowerReach>A', 'LowerReach', 'A', self.get_value('AA', date))


        # ------------------------------------------------------------------------------------------
        # TRANSACTIONS:
        # ------------------------------------------------------------------------------------------
        season1 = self.get_limit(date, ['04-01','10-01'], [1, 0])
        season2 = self.get_limit(date, ['03-01','11-01'], [1, 0])
        season3 = self.get_limit(date, ['05-01','10-02'], [1, 0])
        season4 = self.get_limit(date, ['04-15','10-16'], [1, 0])
        season5 = self.get_limit(date, ['04-01','10-16'], [1, 0])
        season6 = self.get_limit(date, ['04-01','05-01'], [1, 0])
        season7 = self.get_limit(date, ['03-01','10-01'], [1, 0])



        # 1 BD. WR633018
        system.add_transaction(
            id = 1, 
            priority = 1, 
            upper_limit = season1 * 1.25, 
            apath = [{'factor':1,'connection_name':'>MonroeSouthBend'}], 
            expected_value = self.get_value('BD', date) 
        )
        
        # 2 BO. MUST BE RELEASED BY OTTER CREEK
        system.add_transaction(
            id = 2, 
            priority = 2, 
            upper_limit = season1 * (
                0.01413  # GT	WR613050
              + 0.01413  # GS	WR613051
              + 20.84    # CH	WR612253
              + 0.2089   # CG	WR612848
              + 0.0016   # GU	WR612938
            ),
            apath=[{'factor':-1,'connection_name':'UpperReach>OtterCreekResv'}, {'factor':1,'connection_name':'>KingstonCombined'}],
            expected_value = self.get_value('BO', date) 
        )

        # 3 BA.
        system.add_transaction(
            id = 3, 
            priority = 3, 
            upper_limit = season1 * (
                1.169  # GR WR612103
              + 0.746  # GQ WR612104
              + 0.1025 # GP WR612125
              + 0.0019 # GO WR612130
              + 1.34   # BB WR633153
              +10      # AQ WR612143
            ),
            apath=[{'factor':-1,'connection_name':'UpperReach>OtterCreekResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>MonroeSouthBend'}],
            expected_value = self.get_value('BA', date) 
        )
        
        
        # 4 ES. (AL+AS)-(BO+BA)
        # WR61339/2251
        ''' 
        I don't understand what this one is trying to do. Why mix water right 
        authorizations with actual storge deliveries?
        '''
        system.add_transaction(
            id = 4, 
            priority = 4, 
            upper_limit = (
                self.get_limit(date, ['01-01','04-01','06-01','07-01','10-01'], [1, 1.0/3, 1.0/2, 2.0/3, 1]) * self.get_value('B', date) # AL WR61339/2251
              + self.get_value('AS', date) # AS = GUARANTEED WATER TOTAL
            ),
            apath = [{'factor':1,'connection_name':'UpperReach>OtterCreekResv'}], 
            expected_value = self.get_value('ES', date) 
        )
        
        # 5 FT.  FM+FQ+FL+FC+IH+IK+GK
        system.add_transaction(
            id = 5, 
            priority = 5, 
            upper_limit = (
                12      # FM WR612070
              +  1.78   # FQ WR612069 
              +  0.84   # FL WR612067
              + self.get_limit(date, ['01-01','05-01'], [3, 1.66]) # FC WR612068
              +  9.3    # IH WR652562
              +  3.415  # IK WR633898
              +  0.92   # GK WR612131
            ),
            apath = [{'factor':1,'connection_name':'UpperReach>PiuteResv'}], 
            expected_value = self.get_value('FT', date) 

            # Some days the measured change in storage and the lack of deliveries does not allow any 
            # diversions into the reservoir. This reservoir mass balance constrain is not imposed in 
            # the WCAT version.
        )
        

        # 6 BE.
        # CLEAR CREEK CANAL - I am skipping this one for now.

        
        # 7 BF. SEVIER VALLEY CANAL
        system.add_transaction(
            id = 7, 
            priority = 7, 
            upper_limit = season1 * (
                 2    #[BF] WR633010
              + 50    #[BF] WR633011
              + 60    #[BF] WR633012
              + 3.14  #[BF] WR632812
            ), 
            apath = [{'factor':1,'connection_name':'>SValley'}], 
            expected_value = self.get_value('BF', date)
        )

        # 8 BR. JOSEPH CANAL
        system.add_transaction(
            id = 8, 
            priority = 7, 
            upper_limit = season1 * (
                 2.46  #[BR] WR633005
              +  1.09  #[BR] WR633006
              +  0.147 #[BR] WR633007
              + 25.876 #[BR] WR633009
            ), 
            apath = [{'factor':1,'connection_name':'>Joseph'}], 
            expected_value = self.get_value('BR', date)
        )
        

        # 9 FB. MSB TO WELLS CANAL
        system.add_transaction(
            id = 9, 
            priority = 7, 
            upper_limit = season1 * (
                10.9  #[FB] WR633008
            ), 
            apath = [{'factor':1,'connection_name':'>MonroeSouthBend'}], 
            expected_value = self.get_value('FB', date)
        )

        # 10 DT. MONROE CANAL
        system.add_transaction(
            id = 10, 
            priority = 7, 
            upper_limit = season1 * (
                47.9  #[DT] WR633004
            ), 
            apath = [{'factor':1,'connection_name':'>Monroe'}], 
            expected_value = self.get_value('DT', date)
        )
        
        # 11 BS. BROOKLYN CANAL
        system.add_transaction(
            id = 11, 
            priority = 7, 
            upper_limit = season1 * (
                29.77  #[BS] WR633003
            ), 
            apath = [{'factor':1,'connection_name':'>Brooklyn'}], 
            expected_value = self.get_value('BS', date)
        )
        
        # 12 BT. ELSINORE CANAL
        system.add_transaction(
            id = 12, 
            priority = 7, 
            upper_limit = season3 * (
                19.92  #[BT] WR633002
            ), 
            apath = [{'factor':1,'connection_name':'>Elsinore'}], 
            expected_value = self.get_value('BT', date)
        )
        
        # 13 BU. RICHFIELD CANAL
        system.add_transaction(
            id = 13, 
            priority = 7, 
            upper_limit = season1 * (
                85.9  #[BU] WR633000
            ), 
            apath = [{'factor':1,'connection_name':'>Richfield'}], 
            expected_value = self.get_value('BU', date)
        )
        
        # 14 BW. ANNABELLA CANAL
        system.add_transaction(
            id = 14, 
            priority = 7, 
            upper_limit = season1 * (
                30.4  #[BW] WR633001
            ), 
            apath = [{'factor':1,'connection_name':'>Annabella'}], 
            expected_value = self.get_value('BW', date)
        )
        
        # 15 CS. VERMILLION CANAL
        system.add_transaction(
            id = 15, 
            priority = 76, 
            upper_limit = (
                37.8  #[CS] WR633017
            ), 
            apath = [{'factor':1,'connection_name':'>Vermillion'}], 
            expected_value = self.get_value('CS', date)
        )

        # 16 BV. SECOND PRIMARY ALLOCATION
        system.add_transaction(
            id = 16, 
            priority = 16, 
            upper_limit = season5 * (
                68  #[BV] WR633013
            ), 
            apath = [{'factor':1,'connection_name':'>SValley'}], 
            expected_value = self.get_value('BV', date) 
        )
        
        # 17 BV. THIRD PRIMARY ALLOCATION
        #
        # ?? Do we really want to limit this to just season6? This might be a mistake.
        system.add_transaction(
            id = 17, 
            priority = 17, 
            upper_limit = season6 * (
                self.get_limit(date, ['04-01','05-01','10-01'], [11.5, 41.5, 0]) # WR633020
            ), 
            apath = [{'factor':1,'connection_name':'>MonroeSouthBend'}], 
            expected_value = self.get_value('AY', date) 
        )


        ## 50 NATURAL FLOW STORAGE
        #system.add_transaction(
        #    id = 50, 
        #    priority = 50, 
        #    upper_limit = season1 * (
        #        
        #    ), 
        #    path = ['LowerReach', 'UpperReach', 'PiuteResv'], 
        #    expected_value = self.get_value('BY', date) 
        #)


        # 90 STORAGE TO PRIMARY USERS
        system.add_transaction(
            id = 90, 
            priority = 90, 
            upper_limit = None, 
            apath = [{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>SValley'}], 
            expected_value = self.get_value('CU', date) 
        )
        system.add_transaction(
            id = 91, 
            priority = 91, 
            upper_limit = None, 
            apath = [{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>Joseph'}], 
            expected_value = self.get_value('CV', date) 
        )
        system.add_transaction(
            id = 92, 
            priority = 92, 
            upper_limit = None, 
            apath = [{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>MonroeSouthBend'}], # Wells?
            expected_value = self.get_value('AH', date) 
        )
        system.add_transaction(
            id = 93, 
            priority = 93, 
            upper_limit = None, 
            apath = [{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>Monroe'}],
            expected_value = self.get_value('CW', date) 
        )
        system.add_transaction(
            id = 94, 
            priority = 94, 
            upper_limit = None, 
            apath = [{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>Brooklyn'}],
            expected_value = self.get_value('CX', date) 
        )
        system.add_transaction(
            id = 95, 
            priority = 95, 
            upper_limit = None, 
            apath = [{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>Elsinore'}],
            expected_value = self.get_value('CY', date) 
        )
        system.add_transaction(
            id = 96, 
            priority = 96, 
            upper_limit = None, 
            apath = [{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>Richfield'}],
            expected_value = self.get_value('CZ', date) 
        )
        system.add_transaction(
            id = 97, 
            priority = 97, 
            upper_limit = None, 
            apath = [{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>Annabella'}],
            expected_value = self.get_value('DA', date) 
        )

        # Does Vermillion not have a storage acount?
        #system.add_transaction(
        #    id = 98, 
        #    priority = 98, 
        #    upper_limit = None, 
        #    path = ['PiuteResv', 'UpperReach', 'LowerReach', 'Vermillion'],
        #)

        system.add_transaction(
            id = 99, 
            priority = 99, 
            upper_limit = None, 
            apath = [{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>LowerReach'}, {'factor':1,'connection_name':'>Piute'}],
        )


        
        # Connect Piute Reservoir to Otter Creek Reservoir
        # 
        system.add_transaction(
            id = 999, 
            priority = 999, 
            upper_limit = None, 
            lower_limit = None, # Allow positive or negative value.
            apath=[{'factor':-1,'connection_name':'UpperReach>PiuteResv'}, {'factor':1,'connection_name':'UpperReach>OtterCreekResv'}]
        )

        return system

    def build_and_test(self,date:str, test_message=''):
        system = self.build(date, test_message)
        system.solve()
        system.assert_variables_equal_expected(message=test_message)


    def test_sankey(self):
        import json
        from datetime import date, timedelta
        graph = {}
        var_values = {"dates":[], "variables":{}, "arcs":{}}

        for d in (date(2023,4,1) + timedelta(n) for n in range(165)):
            # Run 
            try:
                yyyy_mm_dd = d.isoformat()
                print(yyyy_mm_dd)
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


            except Exception as e: # There was a confusing issue with GLOP that I put on the backburner...
                print("ERROR")
                pass
        with open('sankey.js', 'w') as f:
            f.write('let graph_data = ' + json.dumps(graph, indent=2))
            f.write('\n\n')
            f.write('let daily_values = ' + json.dumps(var_values, indent=2))
        #print(json.dumps(graph))
        #print(json.dumps(var_values))



    
    def test_combined_2023(self):
        from datetime import date, timedelta
        for d in (date(2023,1,3) + timedelta(n) for n in range(360)):
            yyyy_mm_dd = d.isoformat()
            print('Date:' + yyyy_mm_dd)
            self.build_and_test(date=yyyy_mm_dd, test_message=yyyy_mm_dd + ': ')

        '''
        Discrepency on 1/1 and 1/2 related to negative divertable flow in WCAT... there may be some 
        inconsistenecies with how FX is being calculated, particularly with how ES and FT are deducted 
        without any consideration about the diversion constraints on ES and FT.
        '''

    
    '''
    Do I need a tool to help me chart and visualize these results?
    '''