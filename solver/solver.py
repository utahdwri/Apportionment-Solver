""" 


Does a reservoir zone (and the node & flowline) get correctly proccessed to the 
correct zones?


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

from .apportionment_solver import ApportionmentSolver
from .globals import Globals
from .variable import Variable
from .wr_network import WRNetwork, Account, MeasurementSeries, Zone

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




# -----------------------------------------------------------------------------

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



class Subarc:
    def __init__(self, from_zone, to_zone, type=''):
        self.from_zone = from_zone
        self.to_zone = to_zone
        self.type = type
        self.measurement_name = None
        self.measurement_id = None
        self.value = None
        self.variables = []


# -----------------------------------------------------------------------------


