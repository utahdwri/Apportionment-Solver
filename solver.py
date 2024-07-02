""" 


Does a reservoir zone (and the node & flowline) get correctly proccessed to the 
correct zones?




 - Divide the entire state into basins or "systems" (We should have accounting even for self-regulating systems)

SOME TERMINOLOGY:

    - Basins?
        * GSL
          - Bear River
          - Weber/Ogden
          - Jordan
        * Great Basin
          - Sevier
          - 
        * Upper Colorado
        * Lower Colorado
        * Columbia

    - A "System" is a main river or stream. When the system is actively regulated, we call it a 
      Distribution System.

        * 21,23,25,29            Bear River
        * 35,31                  Weber/Ogden
        * 51,53,54,55,57,59      Jordan/Provo/Spanish Fork/etc.

        * 63,65,66,68,69         Sevier/San Pitch
        * 67,71,73,75,77         Sevier/Great Basin/Beaver
        * 13,14,15,16,17,18,19   Great Basin/West Desert

        * 41,43,45,47,49         Upper Colorado
          89,90,91,92,93,94,95,97,99,05,09 
        * 81,85                  Lower Colorado - Virgin River/Kanab Creek

        * 11                     Columbia River (Raft River/Clear Creek)


    - A "Balance Reach" (or "Segment" or "Reach"?) is segment of the System on which the mass 
      balance equation can be computed. A System may have one or more Balance Reaches.
      (You cannot use the fundamental accounting equation w/o this, right? So I think this reach
      will always exist.)
    
    - A "Zone" is portion of and entirely within a "Balance Reach". There are 3 types of zones: 
        "Inflow", "Outflow", and "Storage".

        Inflow -  Usually a single zone containing the natural streams. Gain/loss to the stream is 
                  not measured directly, but computed as the "Balance Reach" residual. That's why
                  we will usually have only one Inflow Zone. Perhaps there may be additional Inflow
                  zones for water import into the system...

        Outflow - Defined by the locations of diversion measurements, both diversions from a stream
                  and from a reservoir.

        Storage - Defined by the node or multiple nodes.

    - Each "Account" is associated with a single "Zone", but there may be multiple accounts for the 
      same Zone. If it is important to group together several accounts that are in different zones, 
      I'm sure we can find a way to accomplish that. But we cannot allow an account to belong to 
      multiple zones because Accunts are fundamentally about organizing Transactions, and we don't 
      want to introduce ambiguity in that.
        - Each Account is associated with a feature or multiple features.
        - Although each Account is associated with a feature (Reach, Diversion, Reservoir), in some 
          cases there may be multiple accounts for a single feature

    - A "Transaction" puts a name to a specified quantity of water. All Transactions describe water 
      flowing between accounts, have a date, and have the quantity of water.
    
    



THINGS TODO

    - Need to be able to pass in account & zone definitions. 
    - Need to either (1) require paths to be simple, or (2) support complex paths.
    - Need to allow multiple paths to share a cfs limit object.
    - May need or want to allow a heiarchial priority scheme (for delivery losses, shares & exchanges that are part of base rights)
    - What if the network changes in some way during the calculation period (new or modified flowline, path, or measurement location)? Need to address this somehow...
    - Figure out what to do with storage accounts that cary a balance...

    - Put the calculation details inside of the transaction object.
    - Fully test step-by-step output description, especially for shared-priority iterations

    
9/15/2023 Thoughts:
    - "Measurement Account" - defined by the location of measuring devises. 
    - "Apportionment Account" - subaccounts within a Measurement Account. 
            For example, a reservoir would have a "Measurement Account" because of the stage/contents measurement. This would 
            represent the total water diverted into the reservoir, released from the reservoir, and stored in the reservoir.
            But there may be several sub-accounts representing storage apportionments for the various users. Since these inflows
            and outflows and storage balances cannot be measured, they are called "Apportionment Accounts".

            
    VAR NAME                DATE           FROM ACNT      TO ACNT        VALUE      
    --------------------    -----------    -----------    -----------    -----------
    PATH_WR@2               2023-06-01     REACH 2        USE AT 2              2.00
    PATH_WR@4               2023-06-01     REACH 3        USE AT 4              4.00
    PATH_WR@5a              2023-06-01     REACH 3        USE AT 5              0.67
    PATH_WR@5b              2023-06-01     REACH 3        USE AT 5              1.33
    PATH_WR@5c              2023-06-01     REACH 3        USE AT 5              2.00
    PATH_WR@3               2023-06-01     REACH 2        8                     0.00
    PATH_SD@5               2023-06-01     8              USE AT 5              0.00
    PATH_SD@4               2023-06-01     8              USE AT 4              0.00
    GAIN_REACH_1            2023-06-01                    REACH 1               5.00
    GAIN_REACH_2            2023-06-01                    REACH 2               4.00
    GAIN_REACH_3            2023-06-01                    REACH 3               2.00
    GAIN_REACH_4            2023-06-01                    REACH 4              -1.00
    NF_DLVY_1_TO_2          2023-06-01     REACH 1        REACH 2               5.00
    UNAUTH_2_TO_5           2023-06-01     REACH 2        USE AT 2              0.00
    NF_DLVY_2_TO_3          2023-06-01     REACH 2        REACH 3               7.00
    NF_SPILL_8_TO_2         2023-06-01     8              REACH 2               0.00
    UNAUTH_2_TO_8           2023-06-01     REACH 2        8                     0.00
    UNAUTH_3_TO_6           2023-06-01     REACH 3        USE AT 4              0.00
    NF_DLVY_3_TO_4          2023-06-01     REACH 3        REACH 4               1.00
    UNAUTH_3_TO_7           2023-06-01     REACH 3        USE AT 5              0.00

    
"""

from dataclasses import dataclass
from math import inf, isclose, isnan
from copy import deepcopy

DEFAULT_ZONE_ID = '-1'

# NOTE: Non-essential functions require and import additional modules. They are 
# not imported here so a user can use the core functionality of this code 
# without needing to install additional modules that are not neccessary. 
# For example: 
#       json
#       jsonpickle
#       networkx as nx
#       matplotlib.pyplot as plt

# -----------------------------------------------------------------------------


def solve (flowlines=None, nodes=None, paths=None, measurements=None
          , zones=None
          , day=None, account_starting_balances=None
          , log_file=None, write_output_files=False):
    """Solve a distribution accounting problem."""

    # Configure logging.
    logging = None
    if log_file is not None:
        import logging
        logging.basicConfig(filename=log_file, level=logging.DEBUG, filemode="w", format='%(message)s')

    #
    input:ApportionmentProblem = ApportionmentProblem()
    input.load_objects(flowlines, nodes, paths, measurements)
    input.init_zones(zones)

    solver = ApportionmentSolver(date=day, logging=logging)
    solver.set_input(input)

    # Write output, if requested.
    if write_output_files:
        input.export_results('output/solver-data.js')
        input.export_input('output/network-data.json', include_implicit=True)

    transaction_results = solver.do_computations()
    input.transaction_results = transaction_results

    # Write output, if requested.
    if write_output_files:

        solver.log(str(transaction_results))
        solver.log_tx()

        input.export_results('output/solver-data.js')
        input.export_input('output/network-data.json', include_implicit=True)

    # Return results.
    transactions = solver.compile_tx_results()

    return ApportionmentResults(input, transactions)


def solve_period (flowlines=None, nodes=None, paths=None, measurements=None
          , zones=None, first_day=None, last_day=None, account_starting_balances=None
          , log_file=None, write_output_files=False):
    """Solve a distribution accounting problem for a period of days."""

    # Get a list of the days in the period range.
    day_list = get_date_series(first_day, last_day)
    
    transactions = []
    for day in day_list:

        # Calculate the transaction values for each day.
        my_results = solve(flowlines, nodes, paths, measurements,
                               zones, day, account_starting_balances,
                               log_file, write_output_files)
        
        # And merge the resulting transactions into one list.
        transactions = transactions + my_results.transactions

    return transactions


def get_date_series (first_day:str, last_day:str) -> list:
    from datetime import date, timedelta

    series = []

    beg_date = date.fromisoformat(first_day)
    end_date = date.fromisoformat(last_day)

    if end_date < beg_date:
        raise ValueError("last_day must not be before first_day!")
    
    d = beg_date
    while d <= end_date:
        series.append(d.isoformat())
        d = d + timedelta(days=1)

    return series




class ApportionmentResults:
    """Class to organize result data and provide helpful result-processing utilities."""

    def __init__(self, input, transactions) -> None:
        self.transactions = transactions
        self.variables = input.variables


    def to_df(self):
        """Returns a pandas DataFrame with these columns:
           -  variable
           -  date
           -  from_account
           -  to_account
           -  value
        """
        import pandas as pd
        df = pd.DataFrame.from_dict(self.transactions)
        return df


# -----------------------------------------------------------------------------

class MeasurementManager:
    """Class to help retrieve measurement data in the context of the water right network.
       We should be able to get data using the flowlineId, nodeId, and measurementId (i.e. stationId).
    """
    """Edited to allow multiple days of data."""

    def __init__(self, measurement_data) -> None:

        self.measurements = []

        for id, data_row in measurement_data.items():
            allow_negative = False
            if 'nodeId' in data_row and data_row['nodeId'] is not None:
                allow_negative = True
            ms = MeasurementSeries(measurementId=id, params=data_row, allow_negative=allow_negative)
            self.measurements.append(ms)


    def add_flowline_measurement_for_net_storage(self, flowlineId, nodeId):
        storage_change = self.filter(nodeId=nodeId, type='measured_storage_change')
        storage_loss = self.filter(nodeId=nodeId, type='measured_storage_loss')
        if len(storage_change) != 1 or len(storage_loss) != 1:
            raise Exception('Cannot find one (and only one) measurement for storage change and storage loss for node {}.'.format(nodeId))
        storage_change = storage_change[0]
        storage_loss = storage_loss[0]

        timeseries = []
        for date in storage_change.timeseries:
            timeseries.append({
                "date": date,
                "value": storage_change.timeseries[date] + storage_loss.timeseries[date]
            })

        ms = MeasurementSeries(measurementId='storage-diversion-from-node:'+str(nodeId), params={
            "timeseries": timeseries,
            "flowlineId": flowlineId
        }, allow_negative=True)
        self.measurements.append(ms)

    def filter(self, flowlineId=None, nodeId=None, type=None):
        # Populate this list of all the measurement-series that match the given criteria.
        matching_series = []
        for m in self.measurements:
            if flowlineId is not None and m.flowlineId != flowlineId:
                continue # skip this, flowlineId does not match!
            if nodeId is not None and m.nodeId != nodeId:
                continue # skip this, nodeId does not match!
            if type is not None and m.type != type:
                continue # skip this, type does not match!

            matching_series.append(m)
        
        return matching_series

    def byId (self, measurementId):
        """Return the MeasurementSeries object with the matching id. If no matching series is found, return None."""
        for m in self.measurements:
            if m.measurementId == measurementId:
                return m
        return None

    def get(self, flowlineId=None, nodeId=None, type=None, if_multiple=None, date=None):

        if date is None:
            raise ValueError('The \'date\' argument is required and cannot be None!')

        # Populate this list of all the measurement-series that match the given criteria.
        matching_series = self.filter(flowlineId=flowlineId, nodeId=nodeId, type=type)

        # Identify the one matching series, if possible.
        the_one = None
        if len(matching_series) == 1:
            the_one = matching_series[0]
        elif len(matching_series) > 1:
            if if_multiple == 'PICK_UPSTREAM_MEASUREMENT':
                try:
                    matching_series.sort(key=lambda x:x.dist_from_top )
                    the_one = matching_series[0]
                except:
                    raise ValueError('Failed to identify upsream measurement! \n' + '\n'.join([str(x) for x in matching_series]) )
            else:
                raise ValueError('There are multiple measurements at the requested feature, but no valid value for the if_multiple argument was provided.')

        # Now return the value for the specified date from the timeseries.
        if the_one is not None:
            return the_one.get_value(date)
        else:
            return None
    

    def is_flowline_measured(self, id):
        return len(self.filter(flowlineId=id)) > 0
    
    def is_node_measured(self, id, type):
        return len(self.filter(nodeId=id, type=type)) > 0


class MeasurementSeries:

    def __init__(self, measurementId, params, allow_negative=True) -> None:
        self.timeseries = {}
        self.measurementId = measurementId
        self.flowlineId = None
        self.nodeId = None
        self.type = None
        self.dist_from_top = None

        if 'flowlineId' in params:
            self.flowlineId = params['flowlineId']
            self.type = 'flow'
            if 'dist_from_top' in params:
                self.dist_from_top = params['dist_from_top']
        elif 'nodeId' in params and 'type' in params:
            self.nodeId = params['nodeId']
            self.type = params['type']

        if 'timeseries' in params:
            for date_value in params['timeseries']:
                date = date_value['date']
                value = date_value['value']
                self.add_value(date, value, change_negative_to_zero= not allow_negative)

    def add_value(self, date, value, change_negative_to_zero=False):
        
        if change_negative_to_zero and value < 0:
            print('Station #{} has a negative value of {} for {}. Zero will be used instead.'.format(self.measurementId, value, date))
            value = 0
            

        self.timeseries[date] = value

    def get_value(self, date):
        if date not in self.timeseries:
            return None
            #raise Exception('No value for measurement id #{} for {}. Values are available for these dates: {}'.format(self.measurementId, date, self.timeseries.keys())) 
        else:
            return self.timeseries[date]

    def __str__(self) -> str:
        return 'Measurement Id #{} | Flowline Id:{} at dist:{} | Node Id:{} | Timeseries Count:{}'.format(
            self.measurementId,
            self.flowlineId,
            self.dist_from_top,
            self.nodeId,
            len(self.timeseries)
            ) 


class WRNetwork:
    """Represents explicit and implicit components of the Water Right Network.
        - 'Explicit components' are defined (in the dictionaries passed to load_objects)
        - 'Implicit components' are infered. 'Reservoir nodes' and 'changes nodes'
          are examples, along with the implicit flowlines connecting them to
          the explicitly defined nodes. 
    """

    def __init__(self):
        # Primary data structures:
        self.measurement_manager = None

        self.flowlines = {}
        self.nodes = {}
        self.paths = {}

        # 
        self.unnamed_cnt = 0


    def load_objects(self, flowlines, nodes, paths, all_measurements):
        """Load data from dictionary objects."""

        # Deep copy the inputs so that they can be modified as part of the loading 
        # routine without causing unexpected changes to input objects.
        flowlines = deepcopy(flowlines)
        nodes = deepcopy(nodes)
        paths = deepcopy(paths)
        all_measurements = deepcopy(all_measurements)
        

        # Add nodes first so Flowlines can reference them.
        self.add_nodes(nodes)

        # Add flowlines next (before paths), so paths can reference flowlines and nodes.
        self.add_flowlines(flowlines)

        # Add paths last.
        self.add_paths(paths)

        
        # Add Measurements.
        self.measurement_manager = MeasurementManager(all_measurements)

        # 
        self.add_reservoir_nodes()
        self.add_change_nodes()

        
        # Process paths (populate _is_traversed_by_path).
        for path_id in self.paths:
            for flowlineId in self.paths[path_id].forward_flowlines:
                if flowlineId in self.flowlines:
                    self.flowlines[flowlineId]._is_traversed_by_path = True
            for flowlineId in self.paths[path_id].backward_flowlines:
                if flowlineId in self.flowlines:
                    self.flowlines[flowlineId]._is_traversed_by_path = True

        # 
        self.process_node_inflows_outflows()


    def add_nodes(self, nodes):

        for nodes_id in nodes:

            # Make sure we pass in the id.
            nodes[nodes_id]['id'] = nodes_id

            # And add/override the 'is_implicit' attribute
            nodes[nodes_id]['is_implicit'] = False

            self.add_node(**nodes[nodes_id])

    def add_node(self, type=0, id=None, coords=None, is_implicit=False, 
                 storage=False, handoff=False, 
                 measured_storage_change=None, measured_storage_loss=None, **other_args):
        """ Add a flowline object.

        Parameters
        ----------
        type - (int) A numeric code that signifies the type of node.

        id - (Optional string) A unique id for the flowline.

        coords (Optional (float,float)) Lon,Lat coordinates Only used for visualization purposes

        other_args - allow the function to accept other arbitrary arguments without crashing. This function may be importing data that is 
        being used for additional purposes.

        Returns
        -------
        node_object - a dict that represents the node.

        """

        # Make sure the input arguments are ok.
        if id is None:
            # Come up with a unique id.
            x = len(self.nodes) + 1
            id = 'N:' + str(x)
            while id in self.nodes:
                x += 1
                id = 'N:' + str(x)
        if id in self.nodes:
            raise ValueError('The supplied node "id" of "{}" is already being used!'.format(id))

        # Add the flowline.
        new_node = Node(type=type, id=id, coords=coords, is_implicit=is_implicit, 
                              storage=storage, handoff=handoff,
                              measured_storage_change=measured_storage_change, 
                              measured_storage_loss=measured_storage_loss)

        self.nodes[id] = new_node

        return id

    def add_flowlines(self, flowlines):
        for flowline_id in flowlines:

            # Make sure we pass in the id.
            flowlines[flowline_id]['id'] = flowline_id

            # And add/override the 'is_implicit' attribute
            flowlines[flowline_id]['is_implicit'] = False

            self.add_flowline(**flowlines[flowline_id])

    def add_flowline(self, from_node, to_node, id=None, name="", is_natural=True, is_implicit=False, is_bidirectional=False, **other_args):
        """ Add a flowline object.

        Parameters
        ----------
        from_node - (int) The unique id of the node that this flowline moves water away from.

        to_node - (int) The unique id of the node that this flowline moves water to.

        id - (int) A unique id for the flowline. If one is not provided, it will chose one itself that begins with 'UNNAMED_' and then a number.

        name - (optional, string) A common name of the stream or canal that this flowline represents.

        is_natural - (otional, boolean) Whether the flowline represents a natural stream (True) or a canal/diversion for conveyance (False).
            Another name could be 'is_source'.

        is_implicit - implicit flowlines are not explicitly defined in the input, but are added to:
            link all sources to a single implicit source node,
            link all sinks to a single implicit sink node

        is_bidirectional (optional, boolean) If True, this flowline can convey flow in either direction. For example, implicit bidirectional 
            flowlines are used to represent the connection between the implicit reservoir node and the stream, where you can have flow 
            to (positive) or from(negative) the reservoir, and this positive or negative value is essentially measured/estimated.

        other_args - allow the function to accept other arbitrary arguments without crashing. This function may be importing data that is 
        being used for additional purposes.

        Returns
        -------
        flowline_object - a dict that represents the flowline.

        """
       
        # Make sure the input arguments are ok.
        if id is None:
            # Come up with a unique id.
            self.unnamed_cnt += 1
            id = 'UNNAMED_' + str(self.unnamed_cnt)
            while id in self.flowlines:
                self.unnamed_cnt += 1
                id = 'UNNAMED_' + str(self.unnamed_cnt)
        if from_node is None:
            raise ValueError('Cannot create a new flowline with parameter "from_node" is None!')
        if to_node is None:
            raise ValueError('Cannot create a new flowline with parameter "to_node" is None!')
        if id in self.flowlines:
            raise ValueError('The supplied flowline "id" of "{}" is already being used!'.format(id))

        new_flowline = Flowline(from_node, to_node, id, name, is_natural, is_implicit, is_bidirectional)
        
        self.flowlines[id] = new_flowline

        return id

    def add_paths(self, paths):
        for path_id in paths:
            path = paths[path_id]

            path['from_change']  = path['from_change'] if 'from_change' in path else None
            path['to_change'] = path['to_change'] if 'to_change' in path else None

            path['from_account']  = path['from_account'] if 'from_account' in path else None
            path['to_account'] = path['to_account'] if 'to_account' in path else None

            path['series'] = path['series'] if 'series' in path else None
            path['child_series'] = path['child_series'] if 'child_series' in path else None

            self.paths[path_id] = Path(path_id, wrnum=path['wrnum'], priority=path['priority']
                , cfs_limit = path['cfs_limit'], from_nodes = path['from_nodes'], to_nodes = path['to_nodes']
                , forward_flowlines=path['forward_flowlines'], backward_flowlines=path['backward_flowlines']
                , from_change = path['from_change'], to_change = path['to_change']
                , from_account = path['from_account'], to_account = path['to_account']
                , series = path['series'], child_series = path['child_series'] )


    def add_reservoir_nodes(self):
        """For every node marked as having storage:

        * Create a off-stream storage node.
        * Add a diversion-to-reservoir flowline.
        * Add a reservoir-to-delivery flowline.
        * Add a reservoir-to-losses flowline (for evaporation, etc.).
        * Adjust the water right and delivery paths to use the new node and flowlines. 

        """
        for nodeId in list(self.nodes.keys()):
            node = self.nodes[nodeId]

            if node.storage:

                # Create a off-stream storage node.
                storage_nodeId = self.add_node(storage=True, is_implicit=True)

                # Add a flowline for diversion-to-reservoir flows (forward) and reservoir-to-deliveries flows (backwards). 
                to_storage_flowlineId = self.add_flowline(from_node=nodeId, to_node=storage_nodeId, is_natural=False, is_implicit=True, is_bidirectional=True)

                # Add a measurement. 
                self.measurement_manager.add_flowline_measurement_for_net_storage(to_storage_flowlineId, nodeId)

                # Adjust the water right and delivery paths to use the new node and flowlines. 
                for pathId in self.paths:
                    path = self.paths[pathId]
                    # NOTE: A path diverting into the reservoir will often have from_node=to_node. In these cases, we want to only adjust the path to_node
                    if nodeId in path.to_nodes:
                        path.to_nodes.remove(nodeId)
                        path.to_nodes.append(storage_nodeId)
                        path.forward_flowlines.append(to_storage_flowlineId)
                    elif nodeId in path.from_nodes:
                        path.from_nodes.remove(nodeId)
                        path.from_nodes.append(storage_nodeId)
                        path.backward_flowlines.append(to_storage_flowlineId)

    def add_change_nodes(self):
        """For every path marked with a to-change or from-change:

        * Create (if not previously done) an off-stream change handoff node for each 
            to- and from- node involved with the change. 
        * Add a bidirectional flowline connecting the node and the change handoff.
        * Adjust the water right and delivery paths to use the change handoff node(s) and flowline(s). 

        """


        # Help add the change node and the connecting flowline in a way
        # that ensures there is only one for each change-node combo.
        added_change_elmts = {}
        def get_or_create_change_nodeId(chnum, nodeId):
            """ Get the ids of the implicit network elements for the change-handoff,
            adding these elements first if they do not yet exist.
            
            Parameters
            ----------
            chnum - str
                The change number.
            
            nodeId - str
                The node id.

            Returns
            -------
            change_nodeId - str

            change_flowlineId - str

            """

            # Check if the change/handoff node already has been added.
            if chnum in added_change_elmts:
                if nodeId in added_change_elmts[chnum]:
                    change_nodeId, change_flowlineId = added_change_elmts[chnum][nodeId]
                    return change_nodeId, change_flowlineId
                
            # If not already added, create one!
            change_nodeId = self.add_node(handoff=True, is_implicit=True)
            change_flowlineId = self.add_flowline(from_node=nodeId, to_node=change_nodeId, is_bidirectional=True, is_implicit=True, is_natural=False)
            if chnum not in added_change_elmts:
                added_change_elmts[chnum] = {}
            added_change_elmts[chnum][nodeId] = (change_nodeId, change_flowlineId)
            return change_nodeId, change_flowlineId



        # Loop through all the base rights that SUPPLY a change,
        # ... creating change-handoff nodes & flowlines,
        # ... adding the flowlines to the path.forward_flowlines,
        # ... updating the path.to_nodes
        for pathId in self.paths:
            path : Path = self.paths[pathId]
            chnum = path.to_change
            
            if chnum is not None:
                to_change_nodeIds = []
                for nodeId in path.to_nodes:
                    chg_nodeId, chg_flowlineId = get_or_create_change_nodeId(chnum, nodeId)
                    to_change_nodeIds.append(chg_nodeId)
                    path.forward_flowlines.append(chg_flowlineId)
                path.to_nodes = to_change_nodeIds

        # Loop through all the changes that complete a change,
        # ... creating change-handoff nodes & flowlines,
        # ... adding the flowlines to the path.backward_flowlines,
        # ... updating the path.from_nodes
        for pathId in self.paths:
            path : Path = self.paths[pathId]
            chnum = path.from_change

            if chnum is not None:
                to_change_nodeIds = []
                for nodeId in path.from_nodes:
                    chg_nodeId, chg_flowlineId = get_or_create_change_nodeId(chnum, nodeId)
                    to_change_nodeIds.append(chg_nodeId)
                    path.backward_flowlines.append(chg_flowlineId)
                path.from_nodes = to_change_nodeIds


    def process_node_inflows_outflows(self):
        """To each Node object, add pointers to the inflows and outflows flowlines."""

        nodes = self.nodes
        flowlines = self.flowlines

        # Create the inflows and outflows lists.
        for nodeId in nodes:
            nodes[nodeId].inflows = []
            nodes[nodeId].outflows = []

        # Populate them.
        for flowlineId in flowlines:
            fromNode = flowlines[flowlineId].from_node
            toNode = flowlines[flowlineId].to_node

            if not fromNode in nodes:
                continue # 10/24/2023 - dont raise an exception since we may be working with only a subset of the network
                #raise Exception("Could not find fromNode '{}' in list!".format(fromNode)) 
            if not toNode in nodes:
                continue # 10/24/2023 - dont raise an exception since we may be working with only a subset of the network
                #raise Exception("Could not find toNode '{}' in list!".format(toNode)) 

            nodes[toNode].inflows.append(flowlineId)
            nodes[fromNode].outflows.append(flowlineId)


    def visualize(self, output_png):
        """Create a chart to show what data we're dealing with."""
        import networkx as nx
        import matplotlib.pyplot as plt

        G = nx.MultiDiGraph()
        edge_color = []
        for flowlineId in self.flowlines:
            from_node = self.flowlines[flowlineId].from_node
            to_node = self.flowlines[flowlineId].to_node
            G.add_edge(from_node, to_node)
            color = (1,0,0)
            if self.flowlines[flowlineId].is_natural:
                color = (0,0,1)
            edge_color.append(color)

        pos = {}
        for node_id in self.nodes:
            if self.nodes[node_id].coords:
                pos[node_id] = self.nodes[node_id].coords
        
        

        plt.figure(figsize=(50,30))
        nx.draw(G, pos=pos, node_size=3, width=0.5, edge_color=edge_color)

        plt.savefig(output_png, format="PNG")


    def export_input(self, file, include_implicit=False):
        """ Exports the problem input to a file using JSON format.

        Parameters
        ----------
        file (string) a file to write JSON to.

        include_implicit (Boolean, Default is False) If True, implicit flowlines and nodes will be included.
        
        """
        import jsonpickle

        output = {
            'flowlines':{},
            'nodes':{},
        }

        for id, data in self.flowlines.items():
            if include_implicit or not data.is_implicit:
                output['flowlines'][id] = data

        for id, data in self.nodes.items():
            if include_implicit or not data.is_implicit:
                output['nodes'][id] = data

        output['paths'] = self.paths
        
        with open(file, 'w') as f:
            #f.write(json.dumps(output, indent=2))
            f.write(jsonpickle.encode(output, unpicklable=False, indent=2))



class ApportionmentProblem(WRNetwork):

    def __init__(self):

        WRNetwork.__init__(self)

        # Derived intermediate data structures:
        self.zones = {}
        self.subarcs = {}
        self.variables = {}

        # New data structures
        self.accounts = AccountList()   #???? TODO! Use real accounts & zones instead of 'subset' accounts!



    # -------------------------------------------------------------------
    # the following deals more with the derived/intermediate data

    def validate_zone_input(self, zones):


        # Ensure each measurement is used as an input to no more than one zone and
        # as an outflow from no more than one zone.
        zone_ins = {}
        zone_outs = {}
        out_measurements_nozone = []
        in_measurements_nozone = []

        for idx, zone_input in enumerate(zones):

            for measId in zone_input['in_measurements']:
                if measId in zone_ins:
                    raise Exception('Measurment id #{} is specified as inflow to'
                                    ' multiple zones!'.format(measId))
                else:
                    zone_ins[measId] = idx

            for measId in zone_input['out_measurements']:
                if measId in zone_outs:
                    raise Exception('Measurment id #{} is specified as outflow from'
                                    ' multiple zones!'.format(measId))
                else:
                    zone_outs[measId] = idx

        # Now check for measurements that have an inflow zone but no outflow zone
        # (or vice versa) and create a default zone if neccessary.
        for measId in zone_ins:
            if measId not in zone_outs:
                out_measurements_nozone.append(measId)
        for measId in zone_outs:
            if measId not in zone_ins:
                in_measurements_nozone.append(measId)

        if len(in_measurements_nozone) + len(out_measurements_nozone) > 0:
            zones.append({
                "id": DEFAULT_ZONE_ID,
                "name": "None",
                "type": "inflow",
                "in_measurements": in_measurements_nozone,            
                "out_measurements": out_measurements_nozone,
            })
            
        return zones


    def init_zones(self, zones):
        """Given the zones input json, build self.accounts, self.zones, and
        self.link data structures.
        """

        zones = self.validate_zone_input(zones)

        semiprocessed_zones = {}
        for idx, zone_input in enumerate(zones):

            zone = Zone()
            zone.name = zone_input['name']
            zone.in_measurements = zone_input['in_measurements']
            zone.out_measurements = zone_input['out_measurements']
            if "id" in zone_input:
                zone.id = zone_input['id']
            else:
                zone.id = str(idx)

            # Create a zone within the given measurement bounds.
            self._define_zone_between_measurements(zone)
            semiprocessed_zones[zone.id] = zone
        

        
        self.zones = self._init_zones_2(semiprocessed_zones)

        self.subarcs = self.determine_arcs(self.zones)
        self.variables = self.add_variables(self.zones, self.subarcs)
    


    def _init_zones_2(self, zones):
        
        for id in zones:
            zone = zones[id]

            from_zones = []
            to_zones = []
            has_natural_flowline = False
            is_storage = False
            change_handoffs = []

            for flowlineId in zone.in_flowlines:
                from_node_id = self.flowlines[flowlineId].from_node
                from_zone_id = self.nodes[from_node_id]._zone
                from_zones.append( from_zone_id )

                if self.flowlines[flowlineId].is_natural:
                    has_natural_flowline = True

            for flowlineId in zone.out_flowlines:
                to_node_id = self.flowlines[flowlineId].to_node
                to_zone_id = self.nodes[to_node_id]._zone
                to_zones.append( to_zone_id )
                if self.flowlines[flowlineId].is_natural:
                    has_natural_flowline = True

            
            for flowlineId in zone.interior_flowlines:
                if self.flowlines[flowlineId].is_natural:
                    has_natural_flowline = True

            if len(zone.interior_nodes) == 1:
                nodeId = list(zone.interior_nodes)[0]
                node = self.nodes[nodeId]
                if node.storage and node.is_implicit:
                    is_storage = True


            # A change handoff should be added for each chnum referenced by
            # "to_change" or "from_change" attributes in the path that ends or
            # begins (respectively) at a node within the zone.
            def add_change_handoff(chnum, nodes):
                zone_ids = set()
                for node_id in nodes:
                    nodes_zone = self.nodes[node_id]._zone
                    zone_ids.add(nodes_zone)
                if len(zone_ids) == 1:
                    change_handoffs.append(chnum)
                elif len(zone_ids) == 0:
                    raise Exception('Cannot process change_handoffs for '
                                    'path {} because its to_nodes do not '
                                    'belong to any zone!')
                else:
                    raise Exception('Cannot process change_handoffs for '
                                    'path {} because its to_nodes belong '
                                    'to more than one zone!')
                
            for path_id, path in self.paths.items():
                if path.to_change is not None:
                    add_change_handoff(path.to_change, path.to_nodes)
                if path.from_change is not None:
                    add_change_handoff(path.from_change, path.from_nodes)

            zone.from_zones = list(set(from_zones)) # make the list distinct.
            zone.to_zones = list(set(to_zones)) # make the list distinct.
            zone.has_natural_flowline = has_natural_flowline
            zone.is_storage = is_storage
            zone.change_handoffs = list(set(change_handoffs))

        return zones


    def _define_zone_between_measurements(self, zone, exclude_streams=False, exclude_diversions=False):
        
        in_measurements = zone.in_measurements
        out_measurements = zone.out_measurements

        interior_nodes = []
        boundary_flowlines = []

        for measurementId in in_measurements:
            # Get the flowline of the measurement.
            meas: MeasurementSeries = self.measurement_manager.byId(measurementId)
            if meas.flowlineId is not None:
                if meas.flowlineId in self.flowlines:
                    flowlineId = meas.flowlineId
                    interior_node = self.flowlines[flowlineId].to_node

                    boundary_flowlines.append(flowlineId)
                    interior_nodes.append(interior_node)

            elif meas.nodeId is not None:
                if meas.nodeId in self.nodes:
                    # An in-flow that is measured at a node means this is a 
                    # storage zone. So we need to:
                    # - find the implicit storage node and mark it as interior
                    # - find the implicit flowline and mark it as an inflow
                    if meas.type == 'measured_storage_change':
                        ms:MeasurementSeries = self.measurement_manager.byId('storage-diversion-from-node:'+str(meas.nodeId))
                        if ms is not None:
                            flowlineId = ms.flowlineId
                            interior_node = self.flowlines[flowlineId].to_node

                            boundary_flowlines.append(flowlineId)
                            interior_nodes.append(interior_node)



        for measurementId in out_measurements:
            # Get the flowline of the measurement.
            meas: MeasurementSeries = self.measurement_manager.byId(measurementId)
            if meas.flowlineId is not None:
                if meas.flowlineId in self.flowlines:
                    flowlineId = meas.flowlineId
                    interior_node = self.flowlines[flowlineId].from_node
                    
                    boundary_flowlines.append(flowlineId)
                    interior_nodes.append(interior_node)

            elif meas.nodeId is not None:
                if meas.nodeId in self.nodes:
                    # An out-flow that is measured at a node means this is either 
                    # a diversion to storage or storage loss. 
                    if meas.type == 'measured_storage_change':
                        # - find the implicit storage node and mark it as interior
                        # - find the implicit flowline and mark it as an inflow
                        ms:MeasurementSeries = self.measurement_manager.byId('storage-diversion-from-node:'+str(meas.nodeId))
                        if ms is not None:
                            flowlineId = ms.flowlineId
                            interior_node = self.flowlines[flowlineId].from_node

                            boundary_flowlines.append(flowlineId)
                            interior_nodes.append(interior_node)

        return self._create_subset(zone, interior_nodes, boundary_flowlines, exclude_streams, exclude_diversions)
    


    def _create_subset(self, zone, interior_nodes, boundary_flowlines, exclude_streams=False, exclude_diversions=False):

        def getNextNodes(starting_nodeIds):
            """Return a list of the upstream and downstream nodes.
            """
            next_forward_nodes = {}
            next_backward_nodes = {}

            for starting_nodeId in starting_nodeIds:
                node = self.nodes[starting_nodeId]
                for flowlineId in node.outflows:
                    # we want to ignore diversions (non-natural) flowlines that are not used by any paths.
                    if self.flowlines[flowlineId]._is_traversed_by_path or self.flowlines[flowlineId].is_natural:
                        next_nodeId = self.flowlines[flowlineId].to_node
                        next_forward_nodes[flowlineId] = next_nodeId
                for flowlineId in node.inflows:
                    # we want to ignore diversions (non-natural) flowlines that are not used by any paths.
                    if self.flowlines[flowlineId]._is_traversed_by_path or self.flowlines[flowlineId].is_natural:
                        next_nodeId = self.flowlines[flowlineId].from_node
                        next_backward_nodes[flowlineId] = next_nodeId

            return next_forward_nodes, next_backward_nodes


        def add_next_nodes(flowlineId, nodeId, fontier_nodes, subset, direction):

            if direction == 'FORWARD':
                subset_boundary_flowlines = subset.out_flowlines
            elif direction == 'BACKWARD':
                subset_boundary_flowlines = subset.in_flowlines

            if not flowlineId in subset.interior_flowlines:

                # Is the next node approached using a flowline that is marked as a subset boundary?
                if flowlineId in boundary_flowlines:
                    subset_boundary_flowlines.append(flowlineId)
                elif exclude_streams and self.flowlines[flowlineId].is_natural:
                    subset_boundary_flowlines.append(flowlineId)
                elif exclude_diversions and not self.flowlines[flowlineId].is_natural:
                    subset_boundary_flowlines.append(flowlineId)
                
                # Otherwise, Is the next node already part of a subset?
                elif self.nodes[nodeId]._zone is not None:
                    # Is it part of this subset?
                    if self.nodes[nodeId]._zone == subset.id:
                        subset.interior_flowlines.add(flowlineId)

                    # Or part of a different subset?
                    else:
                        subset_boundary_flowlines.append(flowlineId)

                else:
                    subset.interior_flowlines.add(flowlineId)
                    # Has the next node never been processed before?
                    if nodeId not in subset.interior_nodes:
                        fontier_nodes[nodeId] = True
                        subset.interior_nodes.add(nodeId)


        # Prepare to loop...
        fontier_nodes = {nodeId:True for nodeId in interior_nodes}
        zone.interior_nodes.update(interior_nodes)
        next_forward_nodes, next_backward_nodes = getNextNodes(interior_nodes)

        # Traverse the network, node to node, until there are no more nodes to visit that may be part of this same subset.
        while len(fontier_nodes) > 0:
            fontier_nodes = {}
            for flowlineId, nodeId in next_forward_nodes.items():
                add_next_nodes(flowlineId, nodeId, fontier_nodes, zone, 'FORWARD')

            for flowlineId, nodeId in next_backward_nodes.items():
                add_next_nodes(flowlineId, nodeId, fontier_nodes, zone, 'BACKWARD')

            next_forward_nodes, next_backward_nodes = getNextNodes(fontier_nodes.keys())

        # Check if the interior-nodes collection contains any special nodes that are implicit inflows/outflows. 
        for nodeId in zone.interior_nodes:
            node_type = self.nodes[nodeId].type
            if node_type == 4 or node_type == 5:
                zone.inflow_nodes.append(nodeId)
            if node_type == 1:
                zone.storage_nodes.append(nodeId)
            if node_type == 3 or node_type == 2:
                zone.use_nodes.append(nodeId)

        #
        for nodeId in zone.interior_nodes:
            self.nodes[nodeId]._zone = zone.id
        

        return zone


    def determine_arcs(self, zones):
        """Generate the arcs dataset describing connections between zones.

        For each one, we need:
        - is it measured?
        - can the flow be less than zero? Or does the arc represent a type of connection where water can flow either way?
        - from zones 
        - to subset
        - 
        """
        
        subarcs = {}

        for id in zones:
            zone = zones[id]

            for flowlineId in zone.out_flowlines:

                from_node = self.flowlines[flowlineId].from_node
                to_node = self.flowlines[flowlineId].to_node

                from_zone = self.nodes[from_node]._zone
                to_zone = self.nodes[to_node]._zone

                if from_zone is None or to_zone is None:
                    continue # This arc must not be needed. Skip it!

                if from_zone != to_zone:

                    from_type = ''
                    if zones[from_zone].has_natural_flowline:
                        from_type = 'SOURCE'
                    elif zones[from_zone].is_storage:
                        from_type = 'STORAGE'
                    else:
                        from_type = 'DIV'
                        
                    to_type = ''
                    if zones[to_zone].has_natural_flowline:
                        to_type = 'SOURCE'
                    elif zones[to_zone].is_storage:
                        to_type = 'STORAGE'
                    else:
                        to_type = 'DIV'

                    subarc_type = from_type + '->' + to_type

                    subarcs[flowlineId] = Subarc(from_zone, to_zone)
                    subarcs[flowlineId].type = subarc_type

        return subarcs


    def add_variables(self, zones, subarcs):

        paths = self.paths
        nodes = self.nodes

        variables = {}

        def flowlines_to_subarcs(flowlines):
            return flowlines

        ''' Path variables. 
        -----------------------------------------------------------------------
        For apportionments and storage deliveries)
        '''
        for pathId in paths:
            
            # If a valid 'cfs_limit' number is specified, use it for the upper-bound,
            try: 
                ub = float(paths[pathId].cfs_limit)
            except: 
                ub = Globals.DEFAULT_PATH_UB 

            # For each 'junction':
            # TODO complex paths

            if len(paths[pathId].from_nodes) == 0:
                print('Path #{} does not have any "from_nodes"! Cannot create variable for it!'.format(pathId))
                continue

            if len(paths[pathId].to_nodes) == 0:
                print('Path #{} does not have any "to_nodes"! Cannot create variable for it!'.format(pathId))
                continue

            # temp fix for simple paths:
            from_zone = nodes[ paths[pathId].from_nodes[0] ]._zone
            to_zone = nodes[ paths[pathId].to_nodes[0] ]._zone

            if from_zone is not None:
                from_zone = zones[from_zone]

            if to_zone is not None:
                to_zone = zones[to_zone]

            forward_flowlines = paths[pathId].forward_flowlines
            backward_flowlines = paths[pathId].backward_flowlines

            from_account =  paths[pathId].from_account
            to_account = paths[pathId].to_account


            v = Variable(id='PATH_'+pathId, type='PATH', 
                         lb=0, 
                         ub=ub, 
                         from_zone=from_zone, 
                         to_zone=to_zone, 
                         from_change=paths[pathId].from_change,
                         to_change=paths[pathId].to_change,
                         forward_subarcs=flowlines_to_subarcs(forward_flowlines), 
                         backward_subarcs=flowlines_to_subarcs(backward_flowlines),
                         #from_account=from_account, 
                         #to_account=to_account
                         )
            variables[v.id] = v


        ''' Reach gain/loss variables. 
        -----------------------------------------------------------------------
        Gains are positive(+), losses are negative(-).
        '''
        for id in zones:

            if zones[id].has_natural_flowline:
                v = Variable(id='GAIN_REACH_'+id, 
                             type='GAIN_REACH', 
                             lb=None, 
                             ub=None, 
                             from_zone=None,
                             to_zone=zones[id])
                variables[v.id] = v


        for flowlineId in subarcs:

            subarc = subarcs[flowlineId]
            from_zoneId = subarc.from_zone
            to_zoneId = subarc.to_zone
            subarc_type = subarc.type
            

            ''' Reach connection variables. 
            -----------------------------------------------------------------------
            Natural flow in an upstream reach should be available and shepherded 
            to a downstream senior right.
            '''
            if subarc_type == 'SOURCE->SOURCE':
                v = Variable(id='NF_DLVY_'+from_zoneId+'_TO_'+to_zoneId, 
                             type='NF_DLVY', 
                             lb=0, 
                             ub=None, 
                             from_zone=zones[from_zoneId], 
                             to_zone=zones[to_zoneId], 
                             forward_subarcs=flowlines_to_subarcs([flowlineId]))
                variables[v.id] = v


            '''Spill variables. 
            -----------------------------------------------------------------------
            If a reservoir water or import water is introduced to the natural 
            stream but not diverted, it becomes natural flow to be apportioned by 
            priority.  
            '''
            if subarc_type == 'DIV->SOURCE':
                v = Variable(id='NF_SPILL_'+from_zoneId+'_TO_'+to_zoneId, 
                             lb=0, 
                             ub=None, 
                             from_zone=zones[from_zoneId], 
                             to_zone=zones[to_zoneId], 
                             forward_subarcs=[flowlineId])
                variables[v.id] = v
            if subarc_type == 'SOURCE->STORAGE':
                v = Variable(id='NF_SPILL_'+to_zoneId+'_TO_'+from_zoneId, 
                             lb=0, 
                             ub=None, 
                             from_zone=zones[to_zoneId], 
                             to_zone=zones[from_zoneId], 
                             backward_subarcs=flowlines_to_subarcs([flowlineId]))
                variables[v.id] = v
            


            '''Unauthorized diversion variables.
            -----------------------------------------------------------------------
            Any other water removed from the natural system without a diversion or 
            delivery path should be marked as unauthorized.
            '''
            if subarc_type == 'SOURCE->DIV':
                v = Variable(id='UNAUTH_'+from_zoneId+'_TO_'+to_zoneId, 
                            lb=0, 
                            ub=None, 
                            from_zone=zones[from_zoneId], 
                            to_zone=zones[to_zoneId], 
                            forward_subarcs=flowlines_to_subarcs([flowlineId]))
                variables[v.id] = v
            if subarc_type == 'SOURCE->STORAGE':
                v = Variable(id='UNAUTH_'+from_zoneId+'_TO_'+to_zoneId, 
                            lb=0, 
                            ub=None, 
                            from_zone=zones[from_zoneId], 
                            to_zone=zones[to_zoneId], 
                            forward_subarcs=flowlines_to_subarcs([flowlineId]))
                variables[v.id] = v


        return variables




    def export_results(self, file):
        """ Exports the problem input to a file using JSON format.

        Parameters
        ----------
        file (string) a file to write JSON to.
        
        """
        import jsonpickle

        output = {}

        output['zones'] = self.zones
        output['subarcs'] = self.subarcs
        output['variables'] = self.variables

        with open(file, 'w') as f:
            #f.write(json.dumps(output, indent=2))
            f.write('json = ' + jsonpickle.encode(output, unpicklable=False, indent=2 ))




class Globals:
    
    # If a path has no cfs_limit, this large value will be used as its limit. Then before the apportionments are 
    # sent back as results, any instances of this large value will be replaced with text to convey the idea 
    # that the upper-bound is undetermined.
    DEFAULT_PATH_UB = 1e10 # 

    LOG_MODEL_DEFS = True


class ApportionmentSolver:
    """ """

    def __init__(self, date, logging=None):

        print('Date: {}'.format(date))

        self.date = date
        self.logging = logging
        self.input = None
        self.mysolver = MatrixLinearSystemSolver()
        self.results = ResultCollection() # This object helps manage the solved output.

    def set_input(self, input):
        """Set up the system of linear equations that will be solved.
        """

        self.input = input
        measurement_manager = self.input.measurement_manager
        zones = self.input.zones


        #
        # Constraints:
        #


        def add_to_reach_mass_balance_equation(v:Variable):
            ''' Mass Balance @ "Internal" Reaches
            -----------------------------------------------------------------------
            Inflow must equal outflow in certain reaches. 
            '''
            from_zone=v.from_zone
            to_zone=v.to_zone

            if from_zone is not None:
                con_names = self.mysolver.get_constraint_names()

                if v.from_change is not None:
                    con_name = 'HDOFF_MB_' + from_zone.id + '_' + v.from_change
                    if con_name not in con_names:
                        self.mysolver.add_constriant(name=con_name, lb=0, ub=0)
                    self.mysolver.set_coeficient(con_name, v.id, -1)

                elif from_zone.has_natural_flowline:
                    con_name = 'REACH_MB_' + from_zone.id
                    if con_name not in con_names:
                        self.mysolver.add_constriant(name=con_name, lb=0, ub=0)
                    self.mysolver.set_coeficient(con_name, v.id, -1)

            if to_zone is not None:
                con_names = self.mysolver.get_constraint_names()

                if v.to_change is not None:
                    con_name = 'HDOFF_MB_' + to_zone.id + '_' + v.to_change
                    if con_name not in con_names:
                        self.mysolver.add_constriant(name=con_name, lb=0, ub=0)
                    self.mysolver.set_coeficient(con_name, v.id, 1)

                elif to_zone.has_natural_flowline:
                    con_name = 'REACH_MB_' + to_zone.id
                    if con_name not in con_names:
                        self.mysolver.add_constriant(name=con_name, lb=0, ub=0)
                    self.mysolver.set_coeficient(con_name, v.id, 1)
            



        def add_to_measurement_equation(var_name, flowlineId, coef):
            ''' Flow Measurement = Sum of Parts
            -----------------------------------------------------------------------
            '''
            meas_value = measurement_manager.get(flowlineId=flowlineId, if_multiple='PICK_UPSTREAM_MEASUREMENT', date=self.date)
            if meas_value is not None:

                #if meas_value < 0:
                #    meas_value = 0 
                #    print('Negative measured value of {} for flowlineId {} was changed to zero!'.format(meas_value, flowlineId))

                con_name = 'PATHMEAS_' + flowlineId

                if con_name not in self.mysolver.get_constraint_names():
                    self.mysolver.add_constriant(name=con_name, lb=meas_value, ub=meas_value)
                self.mysolver.set_coeficient(con_name, var_name, coef)



        #
        # Variables:
        #
        for vId in self.input.variables:
            v = self.input.variables[vId]

            self.mysolver.add_variable(name=v.id, lb=v.lb, ub=v.ub)

            add_to_reach_mass_balance_equation(v)

            for subarcId in v.forward_subarcs:
                flowlineId = subarcId # TODO!! fix this! The program is conflating subarcs and flowlines.
                add_to_measurement_equation(v.id, flowlineId, 1)
            for subarcId in v.backward_subarcs:
                flowlineId = subarcId # see above
                add_to_measurement_equation(v.id, flowlineId,-1)




    # Where to write out log messages for debugging and stuff.
    def log(self, msg):
        if self.logging:
            self.logging.info(msg)

    def log_model(self, *args):
        if Globals.LOG_MODEL_DEFS:
            for msg in args:
                self.log(msg)
            self.log( self.mysolver.lp_string() )

    def log_var_values(self):
        
        self.log("\nVARIABLE VALUES REPORT\n")
        self.log("    {:20s}    {:11s}    {:11s}".format('VAR NAME', 'MIN VALUE', 'MAX VALUE'))
        self.log("    {:20s}    {:11s}    {:11s}".format('--------------------', '-----------', '-----------'))

        for var_name in self.input.variables:

            # get the max value.
            try:
                max_value, blah = self.mysolver.solve_objective([var_name], maximization=True)
                max_value = format(max_value, '11.2f')
            except:
                max_value = 'ERROR'

            # get the min value.
            try:
                min_value, blah = self.mysolver.solve_objective([var_name], minimization=True)
                min_value = format(min_value, '11.2f')
            except:
                min_value = 'ERROR'

            self.log("    {:20s}    {:11s}    {:11s}".format(var_name, min_value, max_value))

    def log_tx(self):
        transactions = self.compile_tx_results()
        
        self.log("\nVARIABLE VALUES REPORT\n")
        self.log("    {:20s}    {:11s}    {:11s}    {:11s}    {:11s}".format('VAR NAME', 'DATE', 'FROM ACNT', 'TO ACNT', 'VALUE'))
        self.log("    {:20s}    {:11s}    {:11s}    {:11s}    {:11s}".format('--------------------', '-----------', '-----------', '-----------', '-----------'))

        def format_str(val):
            if val is not None:
                return format(val, '11s')
            else:
                return format('', '11s')

        def format_float(val):
            if val is not None:
                return format(val, '11.2f')
            else:
                return format('', '11s')
            
        for tx in transactions:
            self.log("    {:20s}    {:11s}    {:11s}    {:11s}    {:11s}".format(
                format_str( tx.variable ), 
                format_str( tx.date ), 
                format_str( tx.from_account ), 
                format_str( tx.to_account ),
                format_float( tx.value ) ))


    # TODO - move or update
    def is_var_maxed(self, variable_name):
        # We need to check if the given variable is as large as the constraints will allow. 
        #

        # Try to maximize the given variable; then check if the maximized value is different;
        # If the variable can be increased from its lb then return False.
        # If the variable can't be increased, it is maximized and return True.

        variable = self.input.variables[variable_name]
        try:
            objective_value, blah = self.mysolver.solve_objective([variable_name], maximization=True)
        except Exception as e:
            return True
        return isclose(variable.value, objective_value, abs_tol=1e-4)



    # ###



    def do_computations(self):
        
        # Calculate reach gains/losses
        self.calculate_reach_nf()

        # We cannot allow any apportionment to force reservoir or import water to spill to natural flow.
        # So these spills should be calculated and fixed prior to the apportionments.
        self.minimize_reservoir_spills()

        # Relax handoff constraints.
        # Temporaraly allow inflow to a change handoff to exceed outflow.
        self.relax_handoff_constraints()

        # Calculate the apportionments.
        self.calculate_apportionments()

        # Now finalize the other variables (spills, unauthorized, nf gains/losses)
        self.solve_for_nonpath_vars()

        #


    def calculate_reach_nf(self):
        '''
        There must be a measurement between every source reach.
        Between reaches and reservoir zones, the connection must be measured via a change in storage measurement.
        Between reaches and change handoff zones, the net flow is zero. (The constraint cannot be in its relaxed form.)
        
        Minimize the sum of reach gain/losses. Constrain each gain/loss variable to equal the calculated value.
            - diversions, illegal uses, spills, 


        If any diversions or reservoirs are not measured, minimizing the reach gain/loss has the effect of assuming 
        no diversion.

        I think this step can go before or after self.minimize_reservoir_spills
        because everything comming to or from a reach is measured 

        '''


        # Minimize the reach gains. 
        # (This only has an effect if there are unmetered diversions.
        # It will make it so there is no water remaining in the reach 
        # to be apportioned to these unmetered diversions.)
        reach_variables = [varId for varId in self.input.variables if varId[0:11] == 'GAIN_REACH_']
        blah, variable_values = self.mysolver.solve_objective(reach_variables, minimization=True)

        # Set the reach-gain (or loss) values to constants.
        for var_name, calc_value in variable_values.items():
            self.mysolver.update_variable(var_name, lb=calc_value, ub=calc_value)
            self.log('{} value = {}'.format(var_name, calc_value))

            # And store the value in the zone object.
            id = self.input.variables[var_name].to_zone.id
            self.input.zones[id].reach_gain = calc_value


    def minimize_reservoir_spills(self):
        
        # TODO - Is it better to minimize each spill variable separately? Or is it ok to minimize them combined?


        # Determine the minimum spill sum
        spill_variables = []
        for varId in self.input.variables:
            if varId[0:9] == 'NF_SPILL_':
                spill_variables.append(varId)

        if len(spill_variables) > 0:
            objective_value, blah = self.mysolver.solve_objective(spill_variables, minimization=True)
            
            # Use the solution as the upper bound to 
            # create a new constraint on the set of spills.
            self.mysolver.add_constriant(name='LIMIT_SPILLS', lb=0, ub=objective_value)
            for varId in self.input.variables:
                if varId[0:9] == 'NF_SPILL_':
                    self.mysolver.set_coeficient('LIMIT_SPILLS', varId, 1)



    def relax_handoff_constraints(self):
        """ Handoff constraints are INFLOWS - OUTFLOWS = 0

        So we relax to allow outflows to be calculated after inflows (rather 
        than at the same time) by setting 0 <= INFLOWS - OUTFLOWS, i.e. by
        removing the upper bound of the constraint.

        """
        for conId in self.mysolver.get_constraint_names():
            if conId[0:9] == 'HDOFF_MB_':
                self.mysolver.update_constraint(conId, ub=inf)


    def calculate_apportionments(self, start_priority=None, stop_priority=None):

        paths = self.input.paths

        # Convert the path data to a priority-ordered schedule to loop through.
        schedule = self.get_schedule_series(paths)
        handoffs_by_latest_withdrawal_priority = self.get_handoffs()

        self.log("\nSchedule: " + str(schedule))
        self.log("\nHandoffs_by_latest_withdrawal_priority: " + str(handoffs_by_latest_withdrawal_priority))


        # Solve.          
        for item in schedule:

            # Skip paths with priorities earlier than start_priority. 
            if not start_priority is None and start_priority > item["priority"]:
                continue
            
            # Skip paths with priorities later than stop_priority. 
            if not stop_priority is None and stop_priority < item["priority"]:
                continue

            self.log("\nPriority: {}".format(item["priority"]) )
            #self.log('Maximizing Path(s): ' + str(','.join(item["paths"])) + '\n')

            #

            if ('proportional_subseries' in item or 
                'sequential_subseries' in item    ):
                self.maximize_series(item)
            
            elif item["pathId"] is not None:
                pathId = item["pathId"]
                self.maximize_var(pathId)

            
            
            # If a handoff is now complete, 
            priority = item['priority']
            if priority in handoffs_by_latest_withdrawal_priority:
                handoffs = handoffs_by_latest_withdrawal_priority[priority]

                # ##
                self.log("Completed Handoffs: " + str(handoffs) + "\n")

                # Can the handoff slack variable be minimized to zero?             (new: After enforcing in=out at the handoff node, can the delivery be met?
                # If no, that means there is un-claimed water at the handoff node. (new: Or do diversions into the handoff node need to be reduced?
                handoffs_with_extra_water = []
                for handoff in handoffs:
                    conId = 'HDOFF_MB_' + str(handoff['id'])


                    # Close the handoff.
                    # From now on, require that IN=OUT at the change-handoff node.
                    self.mysolver.update_constraint(conId, ub=0)


                    # Check if the problem is still feasible.
                    try:
                        to_handoff_variables = []
                        for path_name in handoff["to_paths"]:
                            to_handoff_variables.append('PATH_'+path_name)
                        self.mysolver.solve_objective(to_handoff_variables, maximization=True)

                    except SolverError as e: #TODO - the tests are not getting to this code! Add new tests!
                        # If not feasible, make a note ...
                        handoffs_with_extra_water.append( handoff )

                        # And allow inflows into the handoff node to be reduced (so the above constraint is possible)
                        for path_name in handoff["to_paths"]:
                            self.mysolver.update_variable('PATH_'+path_name, lb=0)


                # If so, we need to loop back and apportion it.
                if len(handoffs_with_extra_water) > 0:
                    start_p = min([x['earliest_deposit_priority'] for x in handoffs_with_extra_water])
                    stop_p = item['priority']
                    self.log("Need to re-apportion this water. Looping back to: {}".format(start_p))
                    self.calculate_apportionments( start_p, stop_p)

            
            self.log("\nCompleted iteration for priority: {}".format(item["priority"]) )

    def solve_for_nonpath_vars(self):

        # NOTE: I get away with not bothering to seperate the non-path variables
        #       from the path variables in this code because the path variables 
        #       have already been maximized and updated so they can not be less 
        #       than their max values.
        all_variables = [variable_name for variable_name in self.input.variables]
        blah, variable_values = self.mysolver.solve_objective(all_variables, minimization=True)
        for variable_name in variable_values:
            solved_value = variable_values[variable_name]
            self.input.variables[variable_name].value = solved_value


    def compile_tx_results(self) -> list:
        """Get a list of all the variables expressed as tranactions.
        
        Returns
        -------
        list of transactions, each transactions expressed as a dictionary.
        
        """
        transactions = []

        for variable_name in self.input.variables:
            var_tx = self.input.variables[variable_name].as_transaction(self.date)
            transactions.append(var_tx)

        return transactions

    # ###

    #
    def maximize_var(self, pathId):

        varId = 'PATH_'+pathId
        variable = self.input.variables['PATH_'+pathId]

        result_item = self.results.add([variable])

        # 
        pre_limits = self.check_path_constraints([pathId])
        result_item.set_pre(pre_limits)
        
        # Find the maximum feasible.
        new_value = self.mysolver.maximize_and_update_variable(varId)

        # Update where we save the calculated values. 
        variable.value = new_value

        #
        post_limits = self.check_path_constraints([pathId])
        result_item.set_post(post_limits)
        self.log(str(result_item))

    def maximize_series(self, series):
        """Maximize the given series of variables (either a sequential or 
        proportional series) until all the variables in the series are 
        maximized."""
        maxed_vars = []
        varIds, factors = self.get_next_iter(series, maxed_vars)
        result_collection = self.results.add_multi()
        while varIds:

            pathIds = [varId.replace('PATH_','') for varId in varIds]

            #?? Is there a cleaner way to do this? Just pass the list of factors?
            proportion_factors_by_varIds = {}
            for i, pathId in enumerate(pathIds):
                proportion_factors_by_varIds[varIds[i]] = factors[i]


            self.maximize_vars_inner(pathIds, varIds, proportion_factors_by_varIds, result_collection)

            maxed_vars = maxed_vars + self.get_newly_maxed_vars(varIds)

            # This function will check to see if the series can further be
            # maximized, possibly by dropping a constrained variable (in a 
            # proportional series) or by replacing a constrained variable by the
            # next in line (for a sequential series). It returns the info needed
            # to continue on into another loop iteration.
            varIds, factors = self.get_next_iter(series, maxed_vars)


        # get the vars from the series
        # get the factor from the series
        # Loop until the series
    
    def get_newly_maxed_vars(self, varIds):
        """Return a list of which of the given varIds is now maximized."""
        maxed_varIds = []
        for varId in varIds:
            if self.is_var_maxed(varId):
                maxed_varIds.append(varId)
        return maxed_varIds
    

    def get_next_iter(self, series, maxed_vars):
        """Returns two lists for the next iteration.
        If there are no remaining variables to maximize, returns two empty 
        lists. 
        """
        varIds = []
        factors = []

        #If it is a sequential series, return the params for the next item.
        if 'sequential_subseries' in series:
            subseries =  series['sequential_subseries']
            for item in subseries:
                if ('sequential_subseries' in item or 
                    'proportional_subseries' in item ):
                    varIds, factors = self.get_next_iter(item, maxed_vars)
                elif item['varId'] not in maxed_vars:
                    varIds.append(item['varId'])
                    factors.append(1)
                if len(varIds)>0:
                    break

        # If it is a proportional series, return the list of paths and a list
        # of each's proportion. If the proportional series has any sub-series,
        # this will involve identifying which variable(s) from the subseries 
        # need to be considered and their factor(s).
        elif 'proportional_subseries' in series:
            subseries = series['proportional_subseries']
            for item in subseries:
                if ('sequential_subseries' in item or 
                    'proportional_subseries' in item ):
                    svarIds, sfactors = self.get_next_iter(item, maxed_vars)
                    varIds += svarIds
                    if sfactors:
                        x = item['factor'] / sum(sfactors)
                        factors += [f * x for f in sfactors]

                elif item['varId'] not in maxed_vars:
                    varIds.append(item['varId'])
                    factors.append(item['factor'])


        return varIds, factors
    
    def maximize_vars_inner(self, pathIds, varIds, proportion_factors_by_varIds, result_collection):

        # PRE info for result item...
        variables = [self.input.variables[varId] for varId in varIds]
        result_item = result_collection.add(variables)
        result_item.set_pre(self.check_path_constraints(pathIds))

        # Solve 
        var_values = self.mysolver.maximize_group_by_proportions(varIds, proportion_factors_by_varIds)

        # Update the variables. 
        # Set only the lb for now, since we might be able to increase this variable more in a future iteration.
        for var_name, var_value in var_values.items():
            self.mysolver.update_variable(var_name, lb=var_value)
            self.input.variables[var_name].value = var_value
        

        # POST info for result item...
        result_item.set_post(self.check_path_constraints(pathIds))
        self.log(str(result_item))



    # Convert the paths dictionary to an ordered schedule list by sorting the 
    # paths by priority while grouping paths with the same priority.
    #!!!
    def get_schedule_series(self, paths):
        
        """ NEW FORMAT? WILL THIS WORK?

        paths = {
           "#1": {"series":None, "wrnum":"02-1", "priority":1, ... },
           "#2": {"series":None, "wrnum":"02-2", "priority":1, "child_series":"child" },
           "#3": {"series":None, "wrnum":"02-3", "priority":2, ... },
           "#4": {"series":"child", "wrnum":"02-2A", "priority":88, ... },
           "#5": {"series":"child", "wrnum":"02-2B", "priority":89, ... },
        }

        schedule = [
            {
                "priority": 1,
                "pathId": None,
                "proportional_subseries":[
                    {
                        "factor": ...,
                        "path": "#1"
                    },
                    {
                        "factor": ...,
                        "path": "#2", 
                        "sequential_subseries":[
                            {
                                "priority": 88, 
                                "path": "#4"
                            },
                            {
                                "priority": 89, 
                                "path": "#5"
                            },
                        ]
                    },
                ]
            },
            {
                "priority": 2,
                "path": "#3",
            },
        ]
        """

        
        all_series_priorities = {}

        for pathId in paths:
            
            # deterimine s and p.
            s = paths[pathId].series
            if s is None:
                s = "" 
            p = paths[pathId].priority
            
            # Add the item to the priority list of the series that it belongs 
            # with.
            if s not in all_series_priorities:
                all_series_priorities[s] = {}
            if p not in all_series_priorities[s]:
                all_series_priorities[s][p] = []

            # ... And populate a list of all the pathIds in that series 
            # with that priority as the dict value.
            all_series_priorities[s][p].append(pathId)


        # define a recursive function to help.
        def _as_output(s):
            """
            TODO - This output includes both pathId and varId -- maybe we don't
                   need both?
            """
            output = []
            priorities = list(all_series_priorities[s].keys())
            priorities.sort()
            for p in priorities:
                item = {
                   "priority": p
                }
                if len(all_series_priorities[s][p]) == 1:
                    pathId = all_series_priorities[s][p][0]
                    item["pathId"] = pathId
                    item["varId"] = 'PATH_' + pathId
                    cs = paths[pathId].child_series
                    if cs is not None:
                        item["sequential_subseries"] = _as_output(cs)
                else:
                    item["pathId"] = None
                    item["varId"] = None,
                    item["proportional_subseries"] = []
                    cfs_sum = 0
                    for pathId in all_series_priorities[s][p]:
                        try:
                            cfs_limit = float(paths[pathId].cfs_limit)
                        except:
                            cfs_limit = Globals.DEFAULT_PATH_UB # if there is no limit specified, use this large value.
                            #               (Note: The priority groups should be formulated to prevent paths with cfs-limits 
                            #                      from being grouped with paths without cfs-limits.)  
                        cfs_sum += cfs_limit
                        
                        citem = {"factor":cfs_limit, "pathId":pathId, "varId":'PATH_' + pathId}
                        cs = paths[pathId].child_series
                        if cs is not None:
                            citem["sequential_subseries"] = _as_output(cs)
                        item["proportional_subseries"].append(citem)
                    
                    # normalize the factor (Is this really necessary?)
                    for citem in item["proportional_subseries"]:
                        if cfs_sum > 0:
                            citem["factor"] /= cfs_sum

                output.append(item)
            return output

        return _as_output('')


        # So now a variable can represent the sum of a series! 
        #  - To check if such a variable is maximized will require new logic.

        

    def get_handoffs(self):

        """ Does it make sense for a path to end at multiple handoff caches? 
            Or a handoff cache and other end-points? If so, 
            how will we know how much flow is available to take from the cache?
        """


        paths = self.input.paths
        nodes = self.input.nodes

        zones = self.input.zones

        # Need the earliest priority of a deposit into the handoff cache. 
        # Need the latest priority of a withdrawl from the handoff cache. 
        # Need lists of the paths that deposit into the cache and withdrawal from the cache.

        handoffs_by_id = {} # key is the priority when the 
                            # value is another dictionary

        # Add an item for each handoff, and populate "to_paths"
        for pathId in paths:
            path = paths[pathId]
            if path.to_change is not None:
                for last_node in path.to_nodes:
                    if last_node in nodes:
                        id = nodes[last_node]._zone + '_' + path.to_change
                        if not id in handoffs_by_id:
                            handoffs_by_id[id] = {
                                "id": id,
                                "earliest_deposit_priority": None,   # this is populated later
                                "latest_withdrawal_priority": None,  # this is populated later
                                "from_paths": [],                    # this is populated later
                                "to_paths": []                       # this is populated later
                            }
                        handoffs_by_id[id]["to_paths"].append(pathId)

        # ... now populate "from_paths"
        for pathId in paths:
            path = paths[pathId]
            if path.from_change is not None:
                for first_node in path.from_nodes:
                    if first_node in nodes:
                        id = nodes[first_node]._zone + '_' + path.from_change
                        if id in handoffs_by_id:
                            handoffs_by_id[id]["from_paths"].append(pathId)
                        else:
                            pass # there are no paths into the handoff node, so this handoff is inactive and we will skip it.
            
        # ... now update "earliest_deposit_priority" and "latest_withdrawal_priority"
        for id in handoffs_by_id:
            maxp_to = max([paths[x].priority for x in handoffs_by_id[id]["to_paths"]])
            minp_to = min([paths[x].priority for x in handoffs_by_id[id]["to_paths"]])
            maxp_from = max([paths[x].priority for x in handoffs_by_id[id]["from_paths"]])
            minp_from = min([paths[x].priority for x in handoffs_by_id[id]["from_paths"]])

            if maxp_to > minp_from:
                raise ValueError('Handoff ['+str(id)+'] has a deposite path that is junior to a withdrawal path!')

            handoffs_by_id[id]["earliest_deposit_priority"] = minp_to
            handoffs_by_id[id]["latest_withdrawal_priority"] = maxp_from


        # Now rearange so the data is sorted by "latest_withdrawal_priority"
        handoffs_by_latest_withdrawal_priority = {}
        for id in handoffs_by_id:
            p = handoffs_by_id[id]['latest_withdrawal_priority']
            if not p in handoffs_by_latest_withdrawal_priority:
                handoffs_by_latest_withdrawal_priority[p] = []
            handoffs_by_latest_withdrawal_priority[p].append( handoffs_by_id[id] )


        return handoffs_by_latest_withdrawal_priority



    def check_path_constraints(self, pathIds):
        """Given a list of path ids, return a constraints dictionary 
        (key=remaining constraint name, value=cfs remaining)
        """

        #
        varIds = ['PATH_' + pathId for pathId in pathIds]
        ##
        wr_limits = self.evaluate_constraints_wr_limits(varIds)

        #
        meas_flowlineIds = set()
        for varId in varIds:
            for subarcId in self.input.variables[varId].forward_subarcs:
                flowlineId = subarcId # TODO!!
                meas_flowlineIds.add(flowlineId)
        ##
        meas_limits = self.evaluate_constraints_meas_limits(list(meas_flowlineIds))

        #
        src_subset_ids = set()
        for varId in varIds:
            from_zone = self.input.variables[varId].from_zone
            if from_zone is not None and from_zone.has_natural_flowline:
                src_subset_ids.add(from_zone.id)
        ##
        nf_available_limits = self.evaluate_constraints_nf_available(list(src_subset_ids))


        # Return a combination of all the limits.
        return {**wr_limits, **meas_limits, **nf_available_limits}


    def evaluate_constraints_wr_limits(self, varIds):
        # WR Limit 
        #   How much remains under the path/water-right limit? Does this constraint govern?
        limits = {}

        for varId in varIds:
            pathId = varId.replace('PATH_','')
            path = self.input.paths[pathId]
            variable = self.input.variables[varId]
            try:
                value = path.cfs_limit - variable.value
            except:
                value = inf
            limits['REM WR:'+pathId] = value

        return limits

    def evaluate_constraints_meas_limits(self, meas_flowlineIds):
        # Diversion & delivery measurement limits
        #   Does the sum of paths equal the measurement?

        limits = {}
        for subarcId in meas_flowlineIds:
            if subarcId in self.input.subarcs: # TODO - This shouldn't be needed if the input is valid
                measured_value = self.input.measurement_manager.get(flowlineId=subarcId, date=self.date)
                try:
                    sum_of_solved_paths = self.get_sum_of_solved_paths(subarcId)
                    value = measured_value - sum_of_solved_paths
                except:
                    value = inf
                limits['REM MEAS@'+subarcId] = value

        return limits
                
    def evaluate_constraints_nf_available(self, src_subset_ids):
        # Supply limit
        #   How much flow can we put into the 'unauthorized' variables coming from this zone?
        limits = {}

        if len(src_subset_ids) > 0:

            unauthorized_variables_from_src = []
            for varId in self.input.variables:
                if self.input.variables[varId].from_zone is None:
                    continue
                if self.input.variables[varId].from_zone.id not in src_subset_ids:
                    continue
                if varId[0:7] == 'UNAUTH_':
                    #continue
                    unauthorized_variables_from_src.append(varId)

            try:
                objective_value, blah = self.mysolver.solve_objective(unauthorized_variables_from_src, maximization=True)
            except: 
                objective_value = inf
            limits['REM SUPPLY'] = objective_value

            
            #self.log( self.mysolver.lp_string() ) #???

        return limits


    def get_sum_of_solved_paths(self, subarcId):

        forward_varIds = []
        backward_varIds = []

        # Loop through all the variables and find the ones that use this subarc.
        # TODO - For complex paths, this would be different!
        for varId in self.input.variables:
            if varId[0:5] != 'PATH_':
                continue # We're only interested in path variables.

            if subarcId in self.input.variables[varId].forward_subarcs:
                forward_varIds.append(varId)

            if subarcId in self.input.variables[varId].backward_subarcs:
                backward_varIds.append(varId)

        # 
        sum = 0
        for varId in forward_varIds:
            if self.input.variables[varId].value is not None:
                sum += self.input.variables[varId].value
        for varId in backward_varIds:
            if self.input.variables[varId].value is not None:
                sum -= self.input.variables[varId].value
        
        return sum



# -----------------------------------------------------------------------------





class Flowline:
    def __init__(self, from_node:int, to_node:int, id:str, name:str=""
                 , is_natural:bool=True, is_implicit:bool=False, is_bidirectional:bool=False, **other_args):

        self.from_node = from_node 
        self.to_node = to_node
        self.id = id
        self.name = name
        self.is_natural = is_natural 
        self.is_implicit = is_implicit
        self.is_bidirectional = is_bidirectional
        self._is_traversed_by_path = False


class Node:
    def __init__(self, type=0, id=None, coords=None, is_implicit=False, 
                storage=False, handoff=False, 
                measured_storage_change=None, measured_storage_loss=None, **other_args):
        """
        
        """
        self.id = id
        self.type = type
        self.coords = coords
        self.handoff = handoff
        self.storage = storage
        self.is_implicit = is_implicit
        self._zone = None
        self.inflows = []
        self.outflows = []


class Path:
    def __init__(self, id, wrnum, priority, cfs_limit, 
                 from_nodes=[], to_nodes=[], 
                 forward_flowlines=[], backward_flowlines=[],
                 from_change=None, to_change=None,
                 from_account=None, to_account=None,
                 series=None, child_series=None):

        self.id = id
        self.wrnum = wrnum
        self.priority = priority
        self.cfs_limit = cfs_limit
        self.get_cfs_limit = Path.parse_cfs(cfs_limit)
        self.from_nodes = from_nodes
        self.to_nodes = to_nodes
        self.forward_flowlines = forward_flowlines
        self.backward_flowlines = backward_flowlines
        self.from_change = from_change
        self.to_change = to_change
        self.series = series
        self.child_series = child_series

        self.from_account: Account = from_account
        self.to_account: Account = to_account


    def parse_cfs(cfs_input):

        # 1st, check if it's just a plain number.
        try:
            number = float(cfs_input)
        except:
            pass
        else: # No error, so it is a number!
            def constant_value_getter(yyyy_mm_dd):
                return number
            return constant_value_getter

        # Next, check if it's valid json.
        import json
        try:
            obj = json.loads(cfs_input)
        except:
            pass
        else: # No errors, so it is valid json!

            base = 1
            if 'base' in obj:
                base = float(obj['base'])

            seasonal_factors = None
            if 'season' in obj:
                seasonal_factors = obj['season']
                seasonal_factors.sort(key=lambda x:x[0] )

            ts = None
            if 'ts' in obj:
                ts = obj['ts']


            def seasonal_value_getter(yyyy_mm_dd):
                factors = []

                # Add the base factor, if provided.
                if base is not None:
                    factors.append(base)

                # Determine and add the seasonal factor, if provided.
                if seasonal_factors is not None:
                    seasonal_factor = 0 # We assume the right is not allowed (zero cfs) for days prior to the first provided beg-date

                    mm = yyyy_mm_dd[5:7]
                    dd = yyyy_mm_dd[8:10]
                    mmdd = mm + dd

                    for beg_mmdd, factor in seasonal_factors:
                        if beg_mmdd <= mmdd:
                            seasonal_factor = factor
                        else:
                            break

                    factors.append(seasonal_factor)
                
                # Determine and add the ts factor, if provided.
                if ts is not None:
                    ts_factor = 0 
                    if yyyy_mm_dd in ts:
                        ts_factor = float(ts[yyyy_mm_dd])
                    factors.append(ts_factor)

                # Multiply the various factors together to get the result.
                value = None
                if len(factors) > 0:
                    value = 1
                    for f in factors:
                        value = value * f 
                return value

            return seasonal_value_getter


class Zone:
    
    __cnt: int = 0 # to give each object a unique id

    def __init__(self, type=None, name="", in_measurements=[], out_measurements=[]
                 , change_measurements=[], accounts=[]):

        # Each Zone gets an ID.
        Zone.__cnt += 1
        self.id = str(Zone.__cnt)

        # Each Zone can have a name.
        self.name = name

        # There are 3 possible types: "inflow", "outflow", "storage"
        self.type = type


        self.interior_nodes: set = set()
        self.interior_flowlines: set = set()
        self.in_flowlines = []
        self.out_flowlines = []
        self.storage_nodes = []
        self.use_nodes = []
        self.inflow_nodes = []
        self.from_zones = []
        self.to_zones = []
        self.has_natural_flowline = False
        self.is_storage = False
        self.reach_gain = None # This is a calculated value, unlike most of the other attributes here.
        self.change_handoffs = []

    def __str__(self) -> str:
        return '''Zone id, name = {}, {}
        type = {}
        interior_nodes = {}
        interior_flowlines = {}
        in_flowlines = {}
        out_flowlines = {}
        storage_nodes = {}
        use_nodes = {}
        inflow_nodes = {}
        from_zones = {}
        to_zones = {}
        has_natural_flowline = {}
        is_storage = {}
        reach_gain = {}
        change_handoffs = {}

        '''.format(self.id, self.name
                  , self.type
                  , self.interior_nodes
                  , self.interior_flowlines
                  , self.in_flowlines
                  , self.out_flowlines
                  , self.storage_nodes
                  , self.use_nodes
                  , self.inflow_nodes
                  , self.from_zones
                  , self.to_zones
                  , self.has_natural_flowline
                  , self.is_storage
                  , self.reach_gain
                  , self.change_handoffs)

@dataclass
class Account:
    """Class for representing an account"""
    id: str
    name: str
    zone: Zone

class AccountList:
    def __init__(self) -> None:
        self.dict = {}

    def add(self, account:Account):
        id = account.id
        if id in self.dict:
            raise ValueError("AccountList already has an Account with id of {} so cannot add this account!".format(id))
        self.dict[id] = account

    def get(self, id):
        value = None
        if id in self.dict:
            value = self.dict[id]
        return value

    def __str__(self) -> str:
        return ', '.join( [str(x.id) + ':' + x.name for x in self.dict] )


class Variable:
    def __init__(self, id, type='', lb=None, ub=None, 
                 from_zone=None, to_zone=None, 
                 from_change=None, to_change=None,
                 forward_subarcs=[], backward_subarcs=[]):
        
        self.id = id
        self.type = type
        self.lb = lb
        self.ub = ub

        # These will eventually need to be moved to self.segments 
        self.from_zone = from_zone
        self.to_zone = to_zone

        #
        self.from_change = from_change
        self.to_change = to_change

        self.forward_subarcs = forward_subarcs
        self.backward_subarcs = backward_subarcs

        # 
        self.segments = []

        # For solver output.
        self.value = 0.0
        self.details = []

        #
        self.from_account = None
        self.to_account = None


    def set_value(self, new_value, step, pre_limits, post_limits):
        """
        """

        # 
        d = {
            "step": step,
            "value": (self.value, new_value),
            "limited_by": []
        }

        for key in post_limits:
            d[key] = (pre_limits[key], post_limits[key])

            if isclose(post_limits[key], 0):
                d['limited_by'].append(key)

        self.details.append(d)
        self.value = new_value

        return d


    def as_transaction(self, date):

        from_account_name = None
        if self.from_zone is not None:
            from_account_name = self.from_zone.name

        to_account_name = None 
        if self.to_zone is not None:
            to_account_name = self.to_zone.name

        return Transaction(variable=self.id, 
                           from_account=from_account_name, 
                           to_account=to_account_name, 
                           date=date, value=self.value, 
                           memo='')


@dataclass
class Transaction:
    """Class for representing a transaction - a named flow from one account to another."""
    variable: str
    from_account: str
    to_account: str
    date: str
    value: float
    memo: str



class Subarc:
    def __init__(self, from_zone, to_zone, type=''):
        self.from_zone = from_zone
        self.to_zone = to_zone
        self.type = type
        self.measurement_name = None
        self.measurement_id = None
        self.value = None
        self.variables = []
    
    #def get_measurement_id():



class ResultCollection:
    def __init__(self) -> None:

        # An 'iteration' is a single step in the solving process
        self.iterations = []

    def add(self, variables):
        step = len(self.iterations) + 1
        new_result = ResultIteration(variables, step)
        self.iterations.append(new_result)
        return new_result
    
    def add_multi(self):
        new_collection = ResultCollection()
        self.iterations.append(new_collection)
        return new_collection


class ResultIteration:
    def __init__(self, variables, step) -> None:
        self.variables : list(Variable) = variables
        self.step = step
        self.limits = {}
        self.values = {}
        self.limited_by = []

        for variable in variables:
            variable.details.append(self)

    def set_pre(self, pre_limits):
        for variable in self.variables:
            self.values[variable.id] = [variable.value, None]
        for name in pre_limits:
            self.limits[name] = [pre_limits[name], None]

    def set_post(self, post_limits):
        for variable in self.variables:
            self.values[variable.id][1] = variable.value
        for name in post_limits:
            if name in self.limits:
                self.limits[name][1] = post_limits[name]
        self.limited_by = self.get_critical_limits()

    def get_critical_limits(self):
        critical_limits = []
        for limit_name in self.limits:
            post_limit = self.limits[limit_name][1]
            if post_limit is not None:
                if isclose(self.limits[limit_name][1], 0, abs_tol=0.0001):
                    critical_limits.append(limit_name)
        return critical_limits
    
    def get_variables_title(self):
        title = ''
        if len(self.variables) == 1:
            title = 'variable ' + self.variables[0].id
        elif len(self.variables) > 1:
            title = 'variables ' + ', '.join([variable.id for variable in self.variables])
        return title 
    
    def __str__(self) -> str:
        s = 'Step ' + str(self.step) + ' for ' + self.get_variables_title() + ':\n'

        s += '     {:20s}  {:>10s}  {:>10s}\n'.format('', 'STARTING', 'ENDING' )
        s += '     --------------------------------------------\n'
        for variable in self.variables:
            s += '     {:20s}  {:10.4f}  {:10.4f}\n'.format('*'+ variable.id +'*', self.values[variable.id][0], self.values[variable.id][1] )
        s += '     --------------------------------------------\n'
        for name in self.limits:
            s += '     {:20s}  {:10.4f}  {:10.4f}\n'.format(name, self.limits[name][0], self.limits[name][1] )
        s += '     --------------------------------------------\n'
        s += '     LIMITED BY: ' + ', '.join(self.get_critical_limits())

        return s + '\n\n'

# -----------------------------------------------------------------------------


from ortools.linear_solver import pywraplp
from math import inf

class SolverError(Exception):
    """A custom exception that gets raised when the solver cannot solve a 
    requested problem, perhaps because the problem is infeasible, unbounded, or
    not properly defined. Each instance should have a message that describes
    the error.
    """
    pass

class MatrixLinearSystemSolver:

    SOLVER_OPTIMAL = 0
    PRINT_SOLVER_MESSAGES = False
    
    def __init__(self):
        self.solver = pywraplp.Solver.CreateSolver('GLOP')
        self.vars = {}
        self.cons = {}


    def add_variable(self, name:str, lb:float=0, ub:float=None) -> None:
        
        if lb is None:
            lb = -inf
        if ub is None:
            ub = inf

        if isnan(lb):
            raise Exception(f'Lower bound value for variable {name} is NaN!')
        if isnan(ub):
            raise Exception(f'Upper bound value for variable {name} is NaN!')

        # Check that the name isn't already being used.
        if name in self.vars:
            raise ValueError('New variable name `'+name+'` is already being used!')

        # Create the variable and set some attributes.
        var = self.solver.NumVar(lb, ub, name ) # Create the solver variable object

        # Register the variable in our vars dictionary.
        self.vars[name] = var


    def add_constriant(self, name:str, lb:float=None, ub:float=None) -> None:
        if lb is None:
            lb = -inf
        if ub is None:
            ub = inf

        if isnan(lb):
            raise Exception(f'Lower bound value for constraint {name} is NaN!')
        if isnan(ub):
            raise Exception(f'Upper bound value for constraint {name} is NaN!')

        # Check that the name isn't already being used.
        if name in self.cons:
            raise ValueError('New condition name `'+name+'` is already being used!')

        # Create a new constraint and set its bounds.
        con = self.solver.Constraint(name)
        con.SetBounds(lb, ub)

        # Register the constraint in our cons dictionary.
        self.cons[name] = con


    def set_coeficient(self, constraint_name, variable_name, coef) -> None:
        if coef is None:
            raise Exception(f'New coef for constraint {constraint_name} is None!')
        if isnan(coef):
            raise Exception(f'New coef for constraint {constraint_name} is NaN!')

        #print(f'set_coeficient: {constraint_name} or {variable_name} = {coef}')

        # Retrieve the variable and constraint objects.
        variable = self.vars[variable_name]
        constraint = self.cons[constraint_name]
        
        # Set or update the coeficient in the condition for the variable.
        constraint.SetCoefficient(variable, coef)


    def solve_objective(self, variable_names, maximization=False, minimization=False):
        """Return a solution to the max/minimization problem, or raise a SolverError exception."""
        if maximization and minimization:
            raise ValueError('Arguments "maximization" and "minimization" cannot both be True!')
        if not maximization and not minimization:
            raise ValueError('Either "maximization" or "minimization" must be True!')

        # Prepare the objective.
        objective = self.solver.Objective()
        objective.Clear()
        if maximization:
            objective.SetMaximization()
        if minimization:
            objective.SetMinimization()
        for variable_name in variable_names:
            variable = self.vars[variable_name]
            objective.SetCoefficient(variable, 1)

        # Solve the system.
        if MatrixLinearSystemSolver.PRINT_SOLVER_MESSAGES:
            self.solver.EnableOutput()
        status = self.solver.Solve()

        # Retrieve the solution results.
        if status == MatrixLinearSystemSolver.SOLVER_OPTIMAL:

            objective_value = objective.Value()

            variable_values = {}
            for variable_name in variable_names:
                variable = self.vars[variable_name]
                variable_values[variable_name] = variable.solution_value()

        # Or if there is no optimal solution, raise the custom exception.
        else:
            status_text = {
                0 : "OPTIMAL",
                1 : "FEASIBLE",
                2 : "INFEASIBLE",
                3 : "UNBOUNDED",
                4 : "ABNORMAL",
            }
            raise SolverError('Could not find optimal solution! Status = ' + 
                       str(status) + ':' + status_text[status] 
                       + '/n/n' + self.lp_string())

        # Done.
        return objective_value, variable_values


    def maximize_and_update_variable(self, variable_name):
        """Update the variable to its feasible maximum."""

        # Find its maximum feasible value.
        objective_value, blah = self.solve_objective([variable_name], maximization=True)

        # Update its value.
        self.update_variable(variable_name, lb=objective_value)

        # Return.
        return objective_value


    def update_variable(self, name:str, lb:float=None, ub:float=None):

        variable = self.vars[name]

        # Ensure that we don't end up with lb > ub, even within rounding error,
        # because that will mess things up!
        _lb = lb if lb else variable.lb()
        _ub = ub if ub else variable.ub()
        if _lb > _ub:
            small_error = isclose(_lb - _ub, 0, abs_tol=0.0001)
            if small_error and ub is None:
                lb = variable.ub() # snap the new lb to the unchange ub
            elif small_error and lb is None:
                ub = variable.lb() # snap the new ub to the unchange lb
            else:
                raise ValueError(f"Cannot update lb or ub for variable {name} "
                                 "becuase lb > ub : {_lb} > {_ub}.")


        if lb is not None:
            if isnan(lb):
                raise Exception(f'New lower bound value for variable {name} is NaN!')
            variable.SetLb(lb)
        if ub is not None:
            if isnan(ub):
                raise Exception(f'new upper bound value for variable {name} is NaN!')
            variable.SetUb(ub)

    def update_constraint(self, name:str, lb:float=None, ub:float=None):
        
        constraint = self.cons[name]
        if lb is not None:
            if isnan(lb):
                raise Exception(f'Lower bound value for constraint {name} is NaN!')
            constraint.SetLb(lb)
        if ub is not None:
            if isnan(ub):
                raise Exception(f'Upper bound value for constraint {name} is NaN!')
            constraint.SetUb(ub)

    def get_constraint_names(self) -> list:
        return self.cons.keys()

    def lp_string(self) -> str:
        """Return a LP format string representing the linear program. """
        #return self.solver.ExportModelAsMpsFormat(False,False)
        value = self.solver.ExportModelAsLpFormat(False)
        #value = value + '\n\n*vars:'
        #for var in self.vars:
        #    value += f'\n{var} lb:{self.vars[var].lb()}  ub:{self.vars[var].ub()} value:{self.vars[var].solution_value()}'
        return value
    
    
    def maximize_group_by_proportions(self, variable_names, proportion_factors):

        # List of constraints used only for merging purposes.
        merge_var, merge_constraints = self._merge(variable_names, proportion_factors)

        # Solve.
        objective_value, blah = self.solve_objective([merge_var.name()], maximization=True)

        # Now unmerge.
        var_values = self._unmerge(variable_names, proportion_factors, merge_var, merge_constraints, objective_value )

        return var_values
    

    def _merge(self, variable_names, proportion_factors):

        # Get the combined variable. Create it if doesn't exist already.
        if 'combined' not in self.vars:
            self.add_variable('combined', lb=0 )
        merge_var =  self.vars['combined']

        merge_constraints = []

        # And replace the variable coefiecents.
        for variable_name in variable_names:
            thisvar =  self.vars[variable_name]
            proportion_factor = 1
            proportion_factor = proportion_factors[variable_name]
            if isnan(proportion_factor):
                raise Exception('Merge proportion factor is NaN!')

            # Set an inequality constraint that relates the merged 
            # variable to this path variable:
            #   
            #   var - f * merged >= 0   
            #
            # It's not an equality ( = 0) because the variable may 
            # have a constraint requiring it to be greater than 
            # f * merged and we don't want to make the problem 
            # infeasible. 
            merge_constraint = self.solver.Constraint('combined_'+variable_name)
            merge_constraint.SetBounds(0, inf) 
            merge_constraint.SetCoefficient(thisvar, 1)
            merge_constraint.SetCoefficient(merge_var, -proportion_factor)
            merge_constraints.append(merge_constraint)
        
        return merge_var, merge_constraints

    def _unmerge(self, variable_names, proportion_factors, merge_var, merge_constraints, solved_value):
        var_values = {}

        # Clear the merged constraints. 
        #   Altering the problem like this will make it so we cannot 
        #   retrieve values from the previous solution, so we have to 
        #   have already gotten the results we need.
        for c in merge_constraints:
            c.Clear()

        # Remove the merge_var.
        merge_var.SetBounds(lb=-inf, ub=inf) # TODO - This doesn't really remove the variable. Is there another way?
        

        # Update the variables. 
        # Lock the value in.
        # Set only the lb, since we might be able to increase this variable in a future iteration.
        for variable_name in variable_names:
            # If multiple variables are being used together, use the combined variable with the factors.
            variable_value = proportion_factors[variable_name] * solved_value

            var_values[variable_name] = variable_value

        return var_values

# -----------------------------------------------------------------------------

