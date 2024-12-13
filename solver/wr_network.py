from dataclasses import dataclass
from copy import deepcopy

class WRNetwork:
    """Represents components of the Water Right Network.
    
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

