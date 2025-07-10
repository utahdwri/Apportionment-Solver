
def query(system_id=None, beg_date=None, end_date=None, downstream_boundary_nodes=[], 
          upstream_boundary_nodes=[], zones=[], test=False):
    """ Query the problem input from the database and populate the object attributes.

    Parameters
    ----------
    system_id (optional, int) - if specified, inputs are limited to those 
        that pertain to the distribution system with this id.
    
    beg_date - (string) YYYY-MM-DD

    end_date - (string) YYYY-MM-DD

    downstream_boundary_nodes (optional, list of nodeIds) 
        Exclude features exclusively downstream of these given nodes.

    upstream_boundary_nodes (optional, list of nodeIds) 
        Exclude features exclusively upstream of these given nodes.

    test (optional, boolean) - if true, use the test database server.

    Returns
    -------
    flowlines (dict)

    nodes (dict)

    paths (dict)

    all_measurements (dict)
    """

    
    # Create the pyodbc connection.
    import pyodbc
    from .query_database_passwords import PROD_DB_CON_STR, TEST_DB_CON_STR 
    con_str = PROD_DB_CON_STR
    if test:
        con_str = TEST_DB_CON_STR
    cnxn = pyodbc.connect( con_str )


    # Add the flowlines and nodes.
    flowlines, nodes = query__flowlines_nodes(cnxn, system_id)  


    # Remove flowlines and nodes that are outside of the area we're interested in. 
    if len(downstream_boundary_nodes) + len(upstream_boundary_nodes) > 0:

        # Check which natural flowlines are within the area...
        natural_flowlines = {}
        for id in flowlines:
            if flowlines[id]['is_natural']:
                natural_flowlines[id] = flowlines[id]
        inside_stream_flowlines, inside_stream_nodes = traverse(downstream_boundary_nodes, upstream_boundary_nodes, natural_flowlines)

        # ... and remove any natural flowline not within the area.
        for id in list(natural_flowlines.keys()):
            if id not in inside_stream_flowlines:
                del flowlines[id]

        # Now do it again, but include the canals. (The net effect of doing this twice, first with only natural flowlines,
        # is we exclude natural flowlines that may be connected down- or up-stream via a canal or import/export.)
        inside_flowlines, inside_nodes = traverse(downstream_boundary_nodes, upstream_boundary_nodes, flowlines)
        
        #print('all flowlines: ' + str(flowlines.keys()))
        print('inside_flowlines: ' + str(inside_flowlines))

        for id in list(flowlines.keys()):
            if id not in inside_flowlines:
                del flowlines[id]

        for id in list(nodes.keys()):
            if id not in inside_nodes:
                del nodes[id]



    # Add the paths. 
    paths = query__paths(cnxn, flowlines, nodes) 

    # Now query the measurements.
    station_ids = []
    for zone in zones:
        station_ids += zone['in_measurements']
        station_ids += zone['out_measurements']
    station_ids = list(set(station_ids))
    #all_measurements = query__meas(cnxn, beg_date, station_ids)
    all_measurements = query__meas2(cnxn, beg_date, end_date, station_ids)  


    # Save the data out to json so I can inspect it.
    import jsonpickle
    with open('output/query_data.json', 'w') as f:
        f.write(jsonpickle.encode({
            "flowlines":flowlines, 
            "nodes":nodes, 
            "paths":paths, 
            "measurements": all_measurements
        }, unpicklable=False, indent=2))

    return flowlines, nodes, paths, all_measurements


def traverse(downstream_boundary_nodes, upstream_boundary_nodes, flowlines):
    """Get a list of flowline and node ids that are upstream (or downstream) from the given node."""

    # Create a dictionary to make looking up connections more efficient.
    downstream = {} # key:value is nodeId:[(nodeId, flowlineId), ...]
    upstream = {}   # key:value is nodeId:[(nodeId, flowlineId), ...]

    for flowlineId in flowlines:
        from_node = flowlines[flowlineId]["from_node"]
        to_node = flowlines[flowlineId]["to_node"]
        
        if to_node not in downstream:
            downstream[to_node] = []
        if from_node not in downstream:
            downstream[from_node] = []
        
        if to_node not in upstream:
            upstream[to_node] = []
        if from_node not in upstream:
            upstream[from_node] = []

        downstream[to_node].append((from_node, flowlineId))
        upstream[from_node].append((to_node, flowlineId))

    def go_up(nodeId):
        if nodeId in downstream:
            return downstream[nodeId]
        else:
            return []

    def go_down(nodeId):
        if nodeId in upstream:
            return upstream[nodeId]
        else:
            return []
    # 
    fontier = set()
    contained_flowlines = set()
    contained_nodes = set()

    for id in downstream_boundary_nodes:
        contained_nodes.add(id)
        for nodeId, flowlineId in go_up(id):
            fontier.add(nodeId)
            contained_nodes.add(nodeId)
            contained_flowlines.add(flowlineId)

    for id in upstream_boundary_nodes:
        contained_nodes.add(id)
        for nodeId, flowlineId in go_down(id):
            fontier.add(nodeId)
            contained_nodes.add(nodeId)
            contained_flowlines.add(flowlineId)

    # Create a recursive function to do the traversal.
    def recurse(fontier, contained_flowlines, contained_nodes, cnt):
        #print('frontier: #' + str(cnt) + ': ' + str(fontier))
        new_frontier = set()
        for id in fontier:
            for nodeId, flowlineId in go_up(id):
                if nodeId not in contained_nodes:
                    new_frontier.add(nodeId)
                contained_nodes.add(nodeId)
                contained_flowlines.add(flowlineId)

            for nodeId, flowlineId in go_down(id):
                if nodeId not in contained_nodes:
                    new_frontier.add(nodeId)
                contained_nodes.add(nodeId)
                contained_flowlines.add(flowlineId)
        
        if len(new_frontier) > 0 and cnt < 1000000:
            recurse(new_frontier, contained_flowlines, contained_nodes, cnt+1)
    
    # Run the recursive function
    recurse(fontier, contained_flowlines, contained_nodes, 0)

        
    return list(contained_flowlines), list(contained_nodes)


def query__flowlines_nodes(cnxn, system_id=None, paths=None):
    """ Query and add the nodes and flowlines. 

    Parameters
    ----------
    cnxn (pyodbc Connection) - An opened database connection object.

    system_id (optional, int) - If specified, only nodes and flowlines for 
        the specified distribution system will be added. If not specified, 
        all nodes and flowlines will be added.
    
    paths (dict pathId:dict) - if this is provided, the query will ensure 
        that all the nodes and flowlines that are used by any of the paths 
        are included.

    Returns
    -------
    db_flowlines

    db_nodes

    """

    cursor = cnxn.cursor()

    if system_id is None:
        system_id = -1

    rows = cursor.execute(""" 
    declare @systemId int = ?;
    declare @effectiveDate date = current_timestamp;

    select FlowlineId, FromNode, ToNode, FlowlineName, FlowlineType
            , nna.NodeType, nna.lon, nna.lat
            , nnb.NodeType, nnb.lon, nnb.lat
    from (
        select recordId as FlowlineId, FromNode, ToNode, FlowlineName, FlowlineType
        from wrNetDB..Flowlines nf
        where BegDate <= @effectiveDate and EndDate > @effectiveDate
    ) nf
    left join (
        select recordId as nodeIdA, NodeType, _systemId, lat, lon
        from wrNetDB..Nodes
        where BegDate <= @effectiveDate and EndDate > @effectiveDate
    ) nna ON nf.FromNode = nna.nodeIdA
    left join (
        select recordId as nodeIdB, NodeType, _systemId, lat, lon
        from wrNetDB..Nodes
        where BegDate <= @effectiveDate and EndDate > @effectiveDate
    ) nnb ON nf.ToNode = nnb.nodeIdB
    where (   nna._systemId = @systemId 
            OR nnb._systemId = @systemId 
            OR @systemId = -1
            );
    """, system_id).fetchall()
    
    db_flowlines = {}
    db_nodes = {}

    for row in rows:
        id = parse_db_id(row[0])
        from_node = parse_db_id(row[1])
        to_node = parse_db_id(row[2])

        if id not in db_flowlines:
            db_flowlines[id] = {
                "from_node": from_node,
                "to_node": to_node,
                "name": row[3],
                "is_natural": (row[4] == 0)
            }

        if from_node not in db_nodes:
            db_nodes[from_node] = {
                "id": from_node,
                "type": row[5],
                "coords": (row[6], row[7]) #(lon,lat)
            }

        if to_node not in db_nodes:
            db_nodes[to_node] = {
                "id": to_node,
                "type": row[8],
                "coords": (row[9],row[10])#(lon,lat)
            }


    del rows, cursor

    if paths is not None:
        # Get a list of any missing flowlines -- referenced by a path but not in db_flowlines.
        missing_flowlineIds = []
        for pathId in paths:
            for flowlineId in paths[pathId]['forward_flowlines']:
                if flowlineId not in db_flowlines:
                    missing_flowlineIds.append(flowlineId)
            for flowlineId in paths[pathId]['backward_flowlines']:
                if flowlineId not in db_flowlines:
                    missing_flowlineIds.append(flowlineId)

        # Query the missing flowlines.
        new_db_flowlines = query__flowlines_from_list(cnxn, missing_flowlineIds)
        db_flowlines = {**db_flowlines, **new_db_flowlines}

        # Get a list of any missing nodes -- referenced by a path or a flowlines but not in db_nodes. 
        missing_nodeIds = []
        for pathId in paths:
            for nodeId in paths[pathId]['to_nodes']:
                if nodeId not in db_flowlines:
                    missing_nodeIds.append(nodeId)
            for nodeId in paths[pathId]['from_nodes']:
                if nodeId not in db_flowlines:
                    missing_nodeIds.append(nodeId)
        for flowlineId in db_flowlines:
            nodeIdA = db_flowlines[flowlineId]['from_node']
            nodeIdB = db_flowlines[flowlineId]['to_node']
            if nodeIdA not in db_nodes:
                missing_nodeIds.append(nodeIdA)
            if nodeIdB not in db_nodes:
                missing_nodeIds.append(nodeIdB)
        
        # Query the missing nodes.
        new_db_nodes = query__nodes_from_list(cnxn, missing_nodeIds)
        db_nodes = {**db_nodes, **new_db_nodes}

    return db_flowlines, db_nodes


def query__nodes_from_list(cnxn, node_ids):
    """ 
    Parameters
    ----------
    cnxn (pyodbc Connection) - An opened database connection object.
    
    node_ids - list of node ids to query and include in the retuned dict

    Returns
    -------
    db_nodes

    """
    

    where_clause = "( " + ' OR '.join('recordId='+str(int(id)) for id in node_ids) + ' )'
    if len(node_ids) == 0:
        where_clause = "( 1=0 )"

    cursor = cnxn.cursor()

    rows = cursor.execute("""
    declare @effectiveDate date = current_timestamp;

    select recordId, NodeType, lon, lat
    from wrNetDB..Nodes 
    where BegDate <= @effectiveDate and EndDate > @effectiveDate
        and (""" + where_clause + """)
    """).fetchall()
    
    db_nodes = {}
    for row in rows:
        id = str(row[0])
        if id not in db_nodes:
            db_nodes[id] = {
                "type": row[1],
                "coords": (row[2], row[3])
            }

    return db_nodes


def query__flowlines_from_list(cnxn, flowline_ids):
    """ 
    Parameters
    ----------
    cnxn (pyodbc Connection) - An opened database connection object.
    
    flowline_ids - list of flowline ids to query and include in the retuned dict

    Returns
    -------
    db_flowlines

    """
    

    where_clause = "( " + ' OR '.join('recordId='+str(int(id)) for id in flowline_ids) + ' )'
    if len(flowline_ids) == 0:
        where_clause = "( 1=0 )"

    cursor = cnxn.cursor()

    rows = cursor.execute(""" 
    declare @effectiveDate date = current_timestamp;

    select recordId, FromNode, ToNode, FlowlineName, FlowlineType
    from wrNetDB..Flowlines
    where BegDate <= @effectiveDate and EndDate > @effectiveDate
        and (""" + where_clause + """)
    """).fetchall()
    
    db_flowlines = {}
    for row in rows:
        id = parse_db_id(row[0])
        if id not in db_flowlines:
            db_flowlines[id] = {
                "from_node": row[1],
                "to_node": row[2],
                "name": row[3],
                "is_natural": (row[4] == 0)
            }

    return db_flowlines


def query__paths_old(cnxn, system_id=None):
    """ Query and add the paths. 

    Add all paths that start at or end at any added nodes, or that traverse 
    any added flowline.

    Parameters
    ----------
    cnxn (pyodbc Connection) - An opened database connection object.

    system_id (optional, int) - If specified, only paths for 
        the specified distribution system will be added. If not specified, 
        all paths will be added.

    Returns
    -------
    paths

    """

    db_paths = {}

    cursor = cnxn.cursor()

    if system_id is None:
        system_id = -1

    # First, add all the paths for this system. Don't worry yet about 
    # populating the lists of nodes and flowlines. 
    rows = cursor.execute(""" 
    DECLARE @systemId int = ?;
    select p.recordId, p.Wrnum, p.PriorityOrder, p.MaxDivRateCFS
    from wrNetDB..Paths p
    inner join (         
        select distinct y.PathId  /* LOOK FOR PATHS PASSING FROM A NODE IN VIEW */
        from wrNetDB..Nodes nn
        inner join wrNetDB..Flowlines x ON x.FromNode = nn.recordId   
        inner join wrNetDB..PathLines y ON y.FlowlineID = x.recordId  
        where (nn._systemId=@systemId OR @systemId=-1)  
        union   
        select distinct y.PathId   /* LOOK FOR PATHS PASSING TO A NODE IN VIEW */   
        from wrNetDB..Nodes nn
        inner join wrNetDB..Flowlines x ON x.ToNode = nn.recordId 
        inner join wrNetDB..PathLines y ON y.FlowlineID = x.recordId  
        where (nn._systemId=@systemId OR @systemId=-1)   
        union   
        select distinct y.PathId   /* LOOK FOR PATHS BEGINING OR ENDING AT A NODE IN VIEW */
        from wrNetDB..Nodes nn
        inner join wrNetDB..PathPoints y ON y.NodeId = nn.recordId
        where (nn._systemId=@systemId OR @systemId=-1) 
    ) x ON p.recordId = x.PathId
    """, system_id).fetchall()
    
    for row in rows:

        path_id = parse_db_id(row[0])
        wrnum = row[1]

        db_paths[path_id] = { 
            "wrnum": wrnum, 
            "priority": row[2], 
            "cfs_limit": row[3], 
            "from_nodes":[], 
            "to_nodes":[], 
            "forward_flowlines": [], 
            "backward_flowlines":[] 
        }


    # Get SQL where clause to use in subsequent queries.
    # e.g. "WHERE ( Path=1 OR PathId=4 OR PathId=12 )"
    where_pathId_clause = "WHERE ( " + ' OR '.join('PathId='+str(int(path_id)) for path_id in db_paths.keys()) + ' )'
    if len(db_paths) == 0:
        where_pathId_clause = "WHERE 1=0"

    # Now get all the path points (both beg- and end-points)
    rows = cursor.execute(""" 
    select PathId, NodeId, PointType
    from wrNetDB..PathPoints 
    """ + where_pathId_clause).fetchall()

    for row in rows:
        path_id = parse_db_id(row[0])
        node_id = parse_db_id(row[1])
        pt_type = row[2]

        if pt_type == 1:
            db_paths[path_id]['from_nodes'].append(node_id)
        if pt_type == 2:
            db_paths[path_id]['to_nodes'].append(node_id)


    # Now get all the flowlines (both forward and backward)
    rows = cursor.execute(""" 
    select PathId, FlowlineID, FlowDir
    from wrNetDB..PathLines
    """ + where_pathId_clause).fetchall()

    for row in rows:
        path_id = parse_db_id(row[0])
        flowline_id = parse_db_id(row[1])
        flowline_dir = row[2]

        if flowline_dir == 1:
            db_paths[path_id]['forward_flowlines'].append(flowline_id)
        if flowline_dir == -1:
            db_paths[path_id]['backward_flowlines'].append(flowline_id)

    del rows, cursor

    return db_paths
def query__paths(cnxn, flowlines, nodes):
    """ Query and add the paths. 

    Add all paths that start at or end at any added nodes, or that traverse 
    any added flowline.

    Parameters
    ----------
    cnxn (pyodbc Connection) - An opened database connection object.

    flowlines - include paths that traverse any of these flowlines.
    
    nodes - include paths that begin or end at any of these nodes.

    Returns
    -------
    paths

    """

    db_paths = {}

    cursor = cnxn.cursor()

    def where_id_in_list(id_list, col_name):
        if len(id_list) == 0:
            return 'select 0 {} where 1=0'.format(col_name)
        elif len(id_list) == 1:
            return 'select {} {}'.format(int(id_list[0]), col_name)
        else:
            return 'select {} {}'.format(int(id_list[0]), col_name) + '\n  union select ' + '\n  union select '.join([str(int(x)) for x in id_list[1:]])

    # First, add all the paths. Don't worry yet about 
    # populating the lists of nodes and flowlines. 
    #print(list(flowlines.keys()))
    rows = cursor.execute(""" 
    select p.recordId, p.Wrnum, p.PriorityOrder, p.MaxDivRateCFS
    from wrNetDB..Paths p
    inner join (         
        select distinct pl.PathId  /* LOOK FOR PATHS PASSING THROUGH ONE OF THE SELECTED FLOWLINES */
        from wrNetDB..PathLines pl
        inner join (
          """ + where_id_in_list(list(flowlines.keys()), 'flowlineId') + """
        ) x ON x.flowlineId = pl.FlowlineID     
        union
        select distinct pp.PathId   /* LOOK FOR PATHS BEGINING OR ENDING AT A NODE IN VIEW */
        from wrNetDB..PathPoints pp
        inner join (
          """ + where_id_in_list(list(nodes.keys()), 'nodeId') + """
        ) y ON y.nodeId = pp.NodeId
    ) x ON p.recordId = x.PathId
    order by recordId
    """).fetchall()
    
    for row in rows:

        path_id = parse_db_id(row[0])
        wrnum = row[1]

        db_paths[path_id] = { 
            "wrnum": wrnum, 
            "priority": row[2], 
            "cfs_limit": row[3], 
            "from_nodes":[], 
            "to_nodes":[], 
            "forward_flowlines": [], 
            "backward_flowlines":[] 
        }


    # Get SQL where clause to use in subsequent queries.
    # e.g. "WHERE ( Path=1 OR PathId=4 OR PathId=12 )"
    where_pathId_clause = "WHERE ( " + ' OR '.join('PathId='+str(int(path_id)) for path_id in db_paths.keys()) + ' )'
    if len(db_paths) == 0:
        where_pathId_clause = "WHERE 1=0"

    # Now get all the path points (both beg- and end-points)
    rows = cursor.execute(""" 
    select PathId, NodeId, PointType
    from wrNetDB..PathPoints 
    """ + where_pathId_clause).fetchall()

    for row in rows:
        path_id = parse_db_id(row[0])
        node_id = parse_db_id(row[1])
        pt_type = row[2]

        if node_id not in nodes:
            print('NOTE: Node #{} is referenced by path #{} but not included in the selected area.'.format(node_id, path_id))
        else:
            if pt_type == 1:
                db_paths[path_id]['from_nodes'].append(node_id)
            if pt_type == 2:
                db_paths[path_id]['to_nodes'].append(node_id)


    # Now get all the flowlines (both forward and backward)
    rows = cursor.execute(""" 
    select PathId, FlowlineID, FlowDir
    from wrNetDB..PathLines
    """ + where_pathId_clause).fetchall()

    for row in rows:
        path_id = parse_db_id(row[0])
        flowline_id = parse_db_id(row[1])
        flowline_dir = row[2]

        if flowline_dir == 1:
            db_paths[path_id]['forward_flowlines'].append(flowline_id)
        if flowline_dir == -1:
            db_paths[path_id]['backward_flowlines'].append(flowline_id)

    del rows, cursor

    return db_paths


def query__meas(cnxn, date, station_ids):
    """Given a list of station_ids, return a list of measurement objects."""

    if len(date) != 10:
        raise Exception('Bad date value: ' + date + '\nDate must be 10-char YYYY-MM-DD')

    db_flow_meas = {}

    where_stnid = ' OR '.join('SM.STATION_ID='+str(int(id)) for id in station_ids)
    if len(where_stnid) == 0:
        where_stnid = '1=0'

    cursor = cnxn.cursor()

    #date = '2022-06-01'
    date_MM_DD = date[5:7] + date[8:10]

    sql = """ 
    declare @MeasDate date = '""" + date + """';

    select SM.STATION_ID, SM.STATION_NAME, NA.FlowlineId, NA.FlowlineDist, NA.NodeId, DR.STATION_VALUE
    from dvrtDB..STATION_MASTER SM
    left join wrNetDB..Addresses NA ON SM.ADDRESS_ID = NA.recordId
    left join (
        SELECT STATION_ID, RV_""" + date_MM_DD + """ as STATION_VALUE
        FROM   dvrtDB..DAILY_RECORDS
        WHERE  ( RECORD_YEAR = year(@MeasDate) )
    ) DR ON SM.STATION_ID = DR.STATION_ID
    where (""" + where_stnid + """) 
    ORDER BY SM.STATION_ID;
    """
    #print(sql)

    rows = cursor.execute(sql).fetchall()
    for row in rows:
        station_id = parse_db_id(row[0])
        db_flow_meas[station_id] = {
            "id": station_id,
            "name": row[1].strip(),
            "flowlineId": parse_db_id(row[2]),
            "dist_from_top": row[3],
            "nodeId": parse_db_id(row[4]),
            "timeseries": [
                {"date": date, "value": row[5]}
            ]
        }
    
    #print('db_flow_meas', db_flow_meas)
    return db_flow_meas


def query__meas2(cnxn, beg_date, end_date, station_ids):
    """Given a list of station_ids and a beg-end date range, return a list of measurement objects."""

    if len(beg_date) != 10:
        raise Exception('Bad beg_date value: {}\nDate must be 10-char YYYY-MM-DD')
    if len(end_date) != 10:
        raise Exception('Bad end_date value: {}\nDate must be 10-char YYYY-MM-DD')

    db_flow_meas = {}

    where_stnid = ' OR '.join('SM.STATION_ID='+str(int(id)) for id in station_ids)
    if len(where_stnid) == 0:
        where_stnid = '1=0'

    cursor = cnxn.cursor()

    sql = """ 
    select SM.STATION_ID, SM.STATION_NAME, NA.FlowlineId, NA.FlowlineDist, NA.NodeId, DR.RECORD_DATE, DR.DAILY_VALUE
    from dvrtDB..STATION_MASTER SM
    left join wrNetDB..Addresses NA ON SM.ADDRESS_ID = NA.recordId
    left join (
        SELECT STATION_ID, RECORD_DATE, DAILY_VALUE
        FROM   dvrtDB..DAILY_RECORDS_PIVOT
        WHERE  ( RECORD_DATE >= ? AND RECORD_DATE <= ? )
    ) DR ON SM.STATION_ID = DR.STATION_ID
    where (""" + where_stnid + """) 
    ORDER BY SM.STATION_ID, RECORD_DATE;
    """

    db_flow_meas = {}
    rows = cursor.execute(sql, beg_date, end_date).fetchall()
    for row in rows:
        station_id = parse_db_id(row[0])
        if station_id not in db_flow_meas:
            db_flow_meas[station_id] = {
                "id": station_id,
                "name": row[1].strip(),
                "flowlineId": parse_db_id(row[2]),
                "dist_from_top": row[3],
                "nodeId": parse_db_id(row[4]),
                "timeseries": []
            }
        db_flow_meas[station_id]['timeseries'].append({
            "date": str(row[5]), 
            "value": row[6]
        })
    #print('db_flow_meas', db_flow_meas)
    return db_flow_meas


def parse_db_id(db_id):
    """Given a nodeId or flowlineId or pathId from the database, return an id
    value compatible with the general solver (meaning a string).
    """
    id = db_id
    if id is not None:
        id = str(id)
    return id