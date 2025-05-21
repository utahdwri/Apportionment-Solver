'''

This is where it will all come together. This code will request the data using
API endpoints, then call the code to solve the accounting, and then save the 
results using the API endpoints.

'''

from .apportionment_solver import ApportionmentSolver
from .data_models import ZoneTypes

API_URL = 'http://127.0.0.1:8000/wr-net/'
COALESCE_MISSING_FLOWS_TO_ZERO = True

class GeneralSolver:
    """ """


    def __init__(self):
        self.beg_date:str = ''
        self.end_date:str = ''
        self.zones = []
        self.interzonal_flows = []
        self.transactions = {}
        self.timeseries = {}


    #TODO - depricate and replace with query_system
    def build_problem(self, accounting_network): 

        self.zones = accounting_network['zones']
        self.interzonal_flows = accounting_network['interzonal_flows']
        self.beg_date = accounting_network['beg_date']
        self.end_date = accounting_network['end_date']

        self.query_transactions()
        self.query_timeseries()


    def query_system(self, system_name, beg_date, end_date):
        self.beg_date = beg_date
        self.end_date = end_date

        self.query_zones_and_flows(system_name)
        self.query_transactions()
        self.query_timeseries()


    def query_zones_and_flows(self, system_name):
        """Use the API to query the accounting network graph."""
        import requests

        url = API_URL + 'accounting-model2/accounting-graph/for-system/' + system_name + f'?beg_date={self.beg_date}&end_date={self.end_date}'

        response = requests.get(url, timeout=30)

        # Need to convert formats a little.
        zones = response.json()['zones']
        for id in zones:
            self.zones.append({
                'name': str(zones[id]['id']),
                'type': zones[id]['type']
            })

        # Need to convert formats a little.
        flows = response.json()['interzonal_flows']
        for id in flows:
            self.interzonal_flows.append({
                'name': str(flows[id]['id']),
                'from_zone': str(flows[id]['from_zone_id']),
                'to_zone': str(flows[id]['to_zone_id']),
                'uhd_mapping': flows[id]['uhd_mapping']
            })


    # TODO - replace with a query of the variables. Then update the apportionment_solver 
    # so we can pass the variables right into that solver. 
    def query_transactions(self):
        """Use the API to query the transactions traversing the accounting 
        network graph."""
        import requests

        url = API_URL + 'accounting-graph/transactions'

        import json
        print(json.dumps({
            'zones': self.zones,
            'interzonal_flows': self.interzonal_flows,
            'beg_date': self.beg_date,
            'end_date': self.end_date
        }))

        response = requests.post(url, timeout=30, json = {
            'zones': self.zones,
            'interzonal_flows': self.interzonal_flows,
            'beg_date': self.beg_date,
            'end_date': self.end_date
        })

        if not response:
            raise Exception("Failed to query transactions from API!")
        
        results_transactions = response.json()['transactions']

        for trxn in results_transactions:
            self.add_transaction(
                path_id=trxn['path_id'],
                beg_date=trxn['beg_date'],
                end_date=trxn['end_date'],
                priority_order=trxn['priority_order'], 
                path=trxn['path'],
                cfs_upper_limit=trxn['upper_limit'],
                wrnum=trxn['wrnum']
            )


    def query_timeseries(self):
        """Use the API to query the measurement data for each connection in 
        the accounting  network graph."""
        import requests

        url = API_URL + 'accounting-graph/measurements'

        response = requests.post(url, timeout=30, json = {
            'zones': self.zones,
            'interzonal_flows': self.interzonal_flows,
            'beg_date': self.beg_date,
            'end_date': self.end_date
        })

        if not response:
            raise Exception("Failed to query measurements from API!")
        
        results = response.json()['measurements']

        for connection_name in results:
            ts = results[connection_name]
            self.add_timeseries(connection_name, ts)

    
    def add_transaction(self, 
                        path_id:int, 
                        beg_date:str,
                        end_date:str,
                        priority_order:float, 
                        path:list[str],
                        cfs_upper_limit:float | None = None,
                        cfs_lower_limit:float = 0,
                        annual_acft_limit:float | None = None,
                        annual_acft_limit_start:str = '0101',
                        wrnum:str | None = None
                        ):
        """"""
        if path_id in self.transactions:
            raise ValueError('A transaction with path_id="'+str(path_id)+'" already exists.')
        
        self.transactions[path_id] = {
            'path_id': path_id,
            'beg_date': beg_date,
            'end_date': end_date,
            'priority_order': priority_order, 
            'path': path,
            'cfs_upper_limit': cfs_upper_limit,
            'cfs_lower_limit': cfs_lower_limit,
            'annual_acft_limit': annual_acft_limit,
            'annual_acft_limit_start': annual_acft_limit_start,
            'wrnum': wrnum
        }


    def add_timeseries(self, id, values:list): 
        """"""
        
        if id in self.timeseries:
            raise ValueError('A timeseries with id="'+str(id)+'" already exists.')
        
        self.timeseries[id] = values


    def solve(self):
        """"""
        # TODO - the way that the variable details (vars) is generated for each 
        # day and then returned is weird. Is it possible to generate it once? Or 
        # to genearte the system once and then just re-run it with different values?

        from datetime import date, datetime, timedelta
        graph = {}
        var_values = {"dates":[], "variables":{}, "arcs":{}}
        errors_cnt = 0

        start_date = datetime.strptime(self.beg_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(self.end_date, "%Y-%m-%d").date()

        current_date = start_date
        delta = timedelta(days=1)
        vars = None
        while current_date <= end_date:
            # Run 
            yyyy_mm_dd = current_date.isoformat()
            print('Running for:', yyyy_mm_dd)
            system = self.build_single_day_solver(date=yyyy_mm_dd)
            system.solve()

            vars = system.get_variables()

            # Extract the data.
            this_var_values = system.get_variable_values()

            # Merge this day's data with the previous data.
            var_values['dates'].append(yyyy_mm_dd)
            for v in this_var_values:
                if v not in var_values['variables']:
                    var_values['variables'][v] = []
                var_values['variables'][v].append( this_var_values[v][0] )

            current_date += delta
        return var_values, errors_cnt, vars


    def build_single_day_solver(self, date) -> ApportionmentSolver:
        """"""

        system = ApportionmentSolver()

        # Add reaches
        for z in self.zones:
            if z['type']=='stream':
                system.add_reach(z['name'], storage_chg=0)
            else:
                system.add_zone(z['name'], is_source=False, type=ZoneTypes(z['type']), storage_chg=0)

        # Add connections
        for f in self.interzonal_flows:
            flow_value = self._get_flow_value(f['name'], date)
            if COALESCE_MISSING_FLOWS_TO_ZERO and flow_value is None:
                flow_value = 0
            #print('flow_value', f['name'], date, flow_value)
            system.connect_zones(f['name'], f['from_zone'], f['to_zone'], flow_value)


        
        # Add transactions
        for path_id in self.transactions:
            trxn = self.transactions[path_id]
            upper_limit =  self._get_transaction_upper_limit(trxn, date)
            #print('_get_transaction_upper_limit', date, upper_limit, trxn['cfs_upper_limit'])
            system.add_transaction(
                id = path_id,
                priority = trxn['priority_order'],
                upper_limit =upper_limit,
                apath = trxn['path']
            )
        
        return system

    def _get_transaction_upper_limit(self, t, date):
        """"""
        for intv in t['cfs_upper_limit']:
            if date >= intv['beg_date'] and date < intv['end_date']:
                return intv['value']
        return None

    def _get_flow_value(self, connection_name, date):
        from datetime import datetime

        beg_date = datetime.strptime(self.beg_date, "%Y-%m-%d").date()
        this_date = datetime.strptime(date, "%Y-%m-%d").date()

        day_idx = (this_date - beg_date).days

        flow_value = self.timeseries[connection_name][day_idx]

        return flow_value




    def save_to_db(self, variables, results):
        """Use the API to save this accounting data to the database."""
        import requests
        import json

        api_headers = {"X-API-Key": "secret-key"}

        payload = {
            'zones': self.zones,
            'interzonal_flows': self.interzonal_flows,
            'beg_date': self.beg_date,
            'end_date': self.end_date,
            'variables': variables
        }
        # Upload the model structure.
        response = requests.post(API_URL + 'accounting-model/', timeout=30, json = payload, headers=api_headers)

        if not response:
            raise Exception(f"Failed to load model to database! {response.text}")
        else:
            new_model_id = response.json()['model_id']
            print('new_model_id: ' + str(new_model_id))
        

        # Upload the values timeseries for the variables.
        url = API_URL + 'accounting-model/'+ str(new_model_id) + '/results'
        response = requests.post(url, timeout=30, json=results, headers=api_headers)
        if not response:
            raise Exception("Failed to load model values to database!")
        