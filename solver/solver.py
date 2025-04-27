'''

This is where it will all come together. This code will request the data using
API endpoints, then call the code to solve the accounting, and then save the 
results using the API endpoints.


Big issues to think through and fix:

- A ModelConnector may have multiple measurements, not a one-to-one like I did 
  for the local database.
  - I added a new db table.
  - need API to populate it when model is uploaded.
  - need API to delete it when model is deleted.
  - need API to use new DB table to query measurement ts.

- Am I supporting reservoirs yet? Make sure that will work before asking Collin 
  to add the proposed DB tables.

- There could in theory be multiple connections between the same zones, which is 
  not yet supported by the general solver.

  

'''

from .apportionment_solver import ApportionmentSolver

API_URL = 'http://127.0.0.1:8000/wr-net/'

class GeneralSolver:
    """ """


    def __init__(self):
        self.zones = {}
        self.connections = {}
        self.beg_date = None
        self.end_date = None
        self.transactions = {}
        self.timeseries = {}


    def build_problem(self, accounting_network):

        self.zones = accounting_network['zones']
        self.connections = accounting_network['connections']
        self.beg_date = accounting_network['beg_date']
        self.end_date = accounting_network['end_date']

        self.query_transactions()
        self.query_timeseries()


    def query_transactions(self):
        """Use the API to query the transactions traversing the accounting 
        network graph."""
        import requests

        url = API_URL + 'accounting/transactions'

        response = requests.post(url, timeout=30, json = {
            'zones': self.zones,
            'connections': self.connections,
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

        url = API_URL + 'accounting/measurements'

        response = requests.post(url, timeout=30, json = {
            'zones': self.zones,
            'connections': self.connections,
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
                        cfs_upper_limit:float = None,
                        cfs_lower_limit:float = 0,
                        annual_acft_limit:float = None,
                        annual_acft_limit_start:str = '0101',
                        wrnum:str = None
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
                system.add_zone(z['name'], is_source=False, storage_chg=0)

        # Add connections
        for f in self.connections:
            flow = self._get_flow_value(f['name'], date)
            system.connect_zones(f['name'], f['from_zone'], f['to_zone'], flow)


        
        # Add transactions
        for path_id in self.transactions:
            trxn = self.transactions[path_id]
            system.add_transaction(
                id = path_id,
                priority = trxn['priority_order'],
                upper_limit = self._get_transaction_upper_limit(trxn, date),
                apath = trxn['path']
            )
        
        return system

    def _get_transaction_upper_limit(self, t, date):
        """"""
        return 10 # TODO!

    def _get_flow_value(self, connection_name, date):
        from datetime import datetime

        beg_date = datetime.strptime(self.beg_date, "%Y-%m-%d").date()
        this_date = datetime.strptime(date, "%Y-%m-%d").date()

        day_idx = (this_date - beg_date).days

        return self.timeseries[connection_name][day_idx]




    def save_to_db(self, variables, results):
        """Use the API to save this accounting data to the database."""
        import requests

        api_headers = {"X-API-Key": "secret-key"}

        print({
            'zones': self.zones,
            'connections': self.connections,
            'beg_date': self.beg_date,
            'end_date': self.end_date,
            'variables': variables
        })

        # Upload the model structure.
        response = requests.post(API_URL + 'wadda/model', timeout=30, json = {
            'zones': self.zones,
            'connections': self.connections,
            'beg_date': self.beg_date,
            'end_date': self.end_date,
            'variables': variables
        }, headers=api_headers)

        if not response:
            raise Exception("Failed to load model to database!")
        else:
            new_model_id = response.json()['model_id']
            print('new_model_id: ' + str(new_model_id))
        

        # Upload the values timeseries for the variables.
        url = API_URL + 'wadda/model/'+ str(new_model_id) + '/results'
        response = requests.post(url, timeout=30, json=results, headers=api_headers)
        if not response:
            raise Exception("Failed to load model values to database!")
        