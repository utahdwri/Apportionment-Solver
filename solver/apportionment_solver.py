from math import isclose
from .solve_lp_with_GLOP import LPSolver
from .data_models import (
    ApportionmentSolverArc, 
    ApportionmentSolverZone, 
    ApportionmentSolverVar, 
    ApportionmentSolverVarPathItem, 
    ApportionmentSolverVarGroup,
    ZoneTypes,
    ScheduleVariable,
    SequentialSchedule,
    ProportionalSchedule,
    SequentialScheduleItem,
    ProportionalScheduleItem,
)


# If a path has no cfs_limit, this large value will be used as its limit. Then before the apportionments are 
# sent back as results, any instances of this large value will be replaced with text to convey the idea 
# that the upper-bound is undetermined.
DEFAULT_PATH_UB = 1e10


class ApportionmentSolver:
    """Define and solve the apportionment problem."""

    def __init__(self):

        self.nodes : dict['str','ApportionmentSolverZone'] = {}
        self.arcs : dict['str','ApportionmentSolverArc'] = {}
        self.vars : dict['str','ApportionmentSolverVar'] = {}


    def __str__(self):

        def warn_if_value_is_incorrect(t:ApportionmentSolverVar):
            if t.expected_value is not None and t.value is not None:
                if abs(t.expected_value - t.value) > 1e-4:
                    return f'*** NOT EQUAL TO EXPECTED VALUE OF {t.expected_value:9.4f}'
            return ''

        out = ''
        for name in self.nodes:
            n:ApportionmentSolverZone = self.nodes[name]
            if n.is_source:
                out += '\n' + n.name + f'(\u0394S={n.storage_chg:9.4f}, gains-losses={n.net_reach_gains:9.4f})'
                for f in n.outflows:
                    out += f'\n {f.flow:9.4f} >> {f.to_zone.name}'
                    for t in f.forward_vars:
                        out += f'\n      {t.name: <26} = {t.value:9.4f}' + warn_if_value_is_incorrect(t)
                    for t in f.backward_vars:
                        out += f'\n      {t.name: <26} = {t.value:9.4f} *backwards' + warn_if_value_is_incorrect(t)

                for f in n.inflows:
                    out += f'\n {f.flow:9.4f} << {f.from_zone.name}'
                    for t in f.forward_vars:
                        out += f'\n      {t.name: <26} = {t.value:9.4f}' + warn_if_value_is_incorrect(t)
                    for t in f.backward_vars:
                        out += f'\n      {t.name: <26} = {t.value:9.4f} *backwards' + warn_if_value_is_incorrect(t)

        return out


    def log(self, message:str):
        if True:
            print('LOG', message)
    



    # --------------------------------------------------------------------------
    # New methods to reuse the same ApportionmentSolver object for each day
    # 5/12/2025
    # --------------------------------------------------------------------------
    def set_zone_storage_changes(self, storage_changes_by_zone_id):
        pass

    def set_interzone_flows(self, interzone_flows):
        pass

    def set_variable_limits(self, variable_limits):
        pass

    # --------------------------------------------------------------------------
    # New methods to build the apportionment graph (aka transaction flow graph)
    # 4/8/2025
    # --------------------------------------------------------------------------

    def load_accounting_graph(self, 
                              zones: list[dict[str,str]], 
                              interzone_flows: list[dict[str,str]], 
                              variables
                              ) -> None:
        """Build the accounting graph by adding each zone and interzone-flow. """

        for zone in zones:
            self.add_zone(
                name=zone['id'],
                type=ZoneTypes(zone['type']),
                is_source=zone['type'] == 'stream'
            )

        for flow in interzone_flows:
            self.add_connection(
                connection_name=flow['name'],
                from_name=flow['from_zone_id'],
                to_name=flow['to_zone_id'],
                flow=0 # Is this supposed to be defined here??
            )

        for variable in variables:
            self.add_transaction(
                id=variable['id'],
                priority=variable['priority_order'],
                upper_limit=variable['??'],
                lower_limit=0,
                apath=[]
            )


    def add_zone(self, name:str,
                 is_source:bool, # TODO - this is redundant with type.
                 type:ZoneTypes,
                 storage_chg:float = 0
                 ) -> ApportionmentSolverZone:
        """Create a zone (can represent a stream reach, a reservoir, an import, 
        or a use zone)."""

        # Create the node.
        self.nodes[name] = ApportionmentSolverZone(
            name=name, 
            is_source=is_source,
            storage_chg=storage_chg,
            type=type
        )

        return self.nodes[name]



    def connect_zones(self, arc_name:str, from_name:str, to_name:str, flow:float|None, allow_both_directions:bool=False):
        """Create an arc connecting two zones, and also create a slack variable
        to ensure the given flow is feasible. The arc_name is None then a name 
        will be automatically generated for the new connection.
        
        Returns the Arc and the Var.
        """

        if arc_name is None:
            arc_name = from_name + '>>' + to_name

        self._validate_existing_name(self.nodes, from_name)
        self._validate_existing_name(self.nodes, to_name)

        # Connect the two nodes with an arc.
        self._validate_new_name(self.arcs, arc_name)
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_zone=self.nodes[from_name],
            to_zone=self.nodes[to_name],
            flow=flow
        )

        # Create a variable representing flow through this connection that is
        # not covered by any other variable -- this is essentially a slack 
        # variable.
        flow_var_name = 'FLOW_' + from_name + '_TO_' + to_name
        self.vars[flow_var_name] = ApportionmentSolverVar(
            name=flow_var_name,
            path_id=None,
            priority=None,
            arc_path=[ApportionmentSolverVarPathItem(arc=self.arcs[arc_name], factor=1)],
            lb=0,
            ub=None,
            value=None
        )

        # If the flow variable goes from a non-source to a source, set the spill flag.
        if (not self.nodes[from_name].is_source and 
            not self.nodes[from_name].type == ZoneTypes.SYSTEM_GAIN_LOSS) and (self.nodes[to_name].is_source):
            self.vars[flow_var_name].is_spill = True

        if allow_both_directions:
            flow_var_name2 = 'FLOW_' + to_name + '_TO_' + from_name
            self.vars[flow_var_name2] = ApportionmentSolverVar(
                name=flow_var_name2,
                path_id=None,
                priority=None,
                arc_path=[ApportionmentSolverVarPathItem(arc=self.arcs[arc_name], factor=-1)],
                lb=0,
                ub=None,
                value=None
            )

            # If the flow variable goes from a non-source to a source, set the spill flag.
            if (not self.nodes[to_name].is_source and 
                not self.nodes[to_name].type == ZoneTypes.SYSTEM_GAIN_LOSS) and (self.nodes[from_name].is_source):
                self.vars[flow_var_name2].is_spill = True

        return self.arcs[arc_name], self.vars[flow_var_name]


    # --------------------------------------------------------------------------
    # Methods to build the apportionment graph (aka transaction flow graph)
    # --------------------------------------------------------------------------

    def add_reach(self, name:str, 
                  storage_chg:float=0, 
                  expected_gain:float|None=None,
                  expected_loss:float|None=None ) -> None:
        """Create a stream reach."""

        gains_zone_name = name + '_GAINS'
        losses_zone_name = name + '_LOSS'

        self.add_zone(name, True, ZoneTypes.STREAM, storage_chg)
        self.add_zone(gains_zone_name, False, ZoneTypes.SYSTEM_GAIN_LOSS, 0)
        self.add_zone(losses_zone_name, False, ZoneTypes.SYSTEM_GAIN_LOSS, 0)

        gain_arc, gain_var = self.connect_zones('GAINS_TO:'+name, gains_zone_name, name, None)
        loss_arc, loss_var = self.connect_zones('LOSSES_FROM:'+name, name, losses_zone_name, None)

        gain_var.expected_value=expected_gain
        loss_var.expected_value=expected_loss


    def add_reach_reservoir(self, connection_name:str, reach_name:str, 
                            resv_name:str, 
                            storage_chg:float, 
                            storage_loss:float=0
                            ):
        """Define an on-stream reservoir. An on-stream reaservoir is represented
        as a distinct node that is connected to the stream node."""
        resv_zone = self.add_zone(resv_name, False, ZoneTypes.STORAGE, storage_chg + storage_loss)
        resv_zone.storage_on_reach=reach_name
        self.connect_zones(connection_name, reach_name, resv_name, None, allow_both_directions=True)

        
    def add_reach_diversion(self, connection_name:str, reach_name:str, div_name:str, flow:float):
        """Define a measured artificial outflow from a stream reach."""
        self.add_zone(div_name, False, ZoneTypes.USE)
        self.connect_zones(connection_name, reach_name, div_name, flow)


    def add_reach_import(self, connection_name:str, reach_name:str, imp_name:str, flow:float):
        """Define a measured artificial inflow to a stream reach."""
        self.add_zone(imp_name, False, ZoneTypes.IMPORT)
        self.connect_zones(connection_name, imp_name, reach_name, flow)


    def add_connection(self, connection_name:str, from_name:str, to_name:str, flow:float):
        """Define how two stream reaches are connected."""
        self.connect_zones(connection_name, from_name, to_name, flow)


    def add_transaction(self, id:int, priority:float|None, upper_limit:float|None, 
                        lower_limit:float | None = 0,
                        apath:list | None = None,
                        limited_by_id:int| None  = None,
                        series_name:str | None = None,
                        child_series_name:str| None  = None, 
                        expected_value:float| None  = None 
                        ):
        """Define an authorized transaction (a flow variable with a priority).
        
        The expected_value is only used for evaluating tests.
        """

        # Convert the path list of names to a list of node objects.
        arc_path = None

        if apath is None:
            arc_path = [] # a path may be None if it hold sub-series.
        else:
            arc_path = []
            for item in apath:
                arc_name = item['flow_name']
                factor = item['factor']
                self._validate_existing_name(self.arcs, arc_name)
                arc_path.append(ApportionmentSolverVarPathItem(arc=self.arcs[arc_name], factor=factor))

        # Add the variable.
        var_name = 'TRXN_' + str(id)
        self.vars[var_name] = ApportionmentSolverVar(
            name=var_name,
            path_id=id,
            priority=priority,
            arc_path=arc_path,
            lb=lower_limit,
            ub=upper_limit,
            value=None,
            series=series_name,
            child_series=child_series_name,
            expected_value=expected_value
        )

        # If the variable is limited by another variable's limit, set up a group for that.
        if limited_by_id is not None:
            limiting_var_name = 'TRXN_' + str(limited_by_id)
            self._validate_existing_name(self.vars, limiting_var_name)
            limiting_parent_var = self.vars[limiting_var_name]

            # Create a group, if one doesn't already exist.
            if limiting_parent_var.other_limited_vars is None:
                limiting_parent_var.other_limited_vars =  ApportionmentSolverVarGroup(
                    members=[]
                )

            # Add the var to the group.
            limiting_parent_var.other_limited_vars.members.append( self.vars[var_name] )


    # --------------------------------------------------------------------------
    # Method to actually build and solve the system of linear equations.
    # --------------------------------------------------------------------------

    def solve(self) -> dict[str,float]:

        # This supports cases where the flow from the reach to on-stream 
        # reservoirs requires a mass balance calculation. (This is similar 
        # to the calculation of a reach's gain/loss, but this needs to be done
        # first so the gain/loss flow is the the only unknow in the mass balance.)
        self._calculate_reservoir_diversions()

        # The reach gains/losses are not measured directly, but we can add 
        # measurement constraints for these arc using simple mass balances.
        self._calculate_reach_gains_losses()



        # Convert the nodes, arcs, and variables into a system of linear 
        # equations that can be solved.
        engine = self._build_linear_equations()


        OLD_WAY = False
        if OLD_WAY:
            # We cannot allow any apportionment to force reservoir or import water to spill to natural flow.
            # So these spills should be calculated and fixed prior to the apportionments.
            self._minimize_reservoir_spills(engine)



            # Calculate the apportionments.
            self._calculate_apportionments(engine)
        else:


            self._build_nf_mass_balance_constraints(engine)
            print(engine.lp_string())
            self._calculate_apportionments(engine)

            self._remove_nf_mass_balance_constraints(engine)
            self._calculate_apportionments(engine)  


        # Now finalize the other variables (spills, unauthorized, nf gains/losses)
        self._solve_for_nonpath_vars(engine)


        return self._compile_tx_results() #apportionment results


    # --------------------------------------------------------------------------
    # Utility function for testing
    # --------------------------------------------------------------------------
    def assert_variables_equal_expected(self, message:str='') -> None:
        """Check if each of the variables match the expected 
        value to 4 decimal places. 

        Skips variables that don't have a defined expected value.
        
        Raises an exception if no variables have an expected value. """

        cnt = 0

        for var_name in self.vars:
            expected_value = self.vars[var_name].expected_value
            computed_value = self.vars[var_name].value
            if expected_value is not None:
                if computed_value is None:
                    msg = ( message +
                        f'Variable "{var_name}": computed (None) != expected'
                        f' ({expected_value})\n'+str(self)
                        )
                    assert computed_value is not None, msg
                else:
                    cnt += 1
                    msg = ( message +
                        f'Variable "{var_name}": computed ({computed_value}) != expected'
                        f' ({expected_value})\n'+str(self)
                        )
                    assert abs(expected_value - computed_value) < 1e-4, msg
        if cnt == 0:
            raise Exception('No variables were given an expected_value!')


    # --------------------------------------------------------------------------
    # Output problem definition into something that can be charted
    # --------------------------------------------------------------------------
    def get_variables(self):
        vars = [
            {
                'name': self.vars[x].name,
                'path_id': self.vars[x].path_id,
                'path': [{'flow_name':a.arc.name, 'factor':a.factor} for a in self.vars[x].arc_path]
            } for x in self.vars
        ]
        return vars

    def get_variable_values(self):
        var_values = {
            x:[self.vars[x].value] for x in self.vars
        }
        return var_values


    def to_sankey_data(self, use_expected_values:bool=False):
        graph = {
            "zones":{
                x:{
                    "is_source": self.nodes[x].is_source
                } for x in self.nodes},
            "subarcs":{
                x:{
                    "from": self.arcs[x].from_zone.name, 
                    "to": self.arcs[x].to_zone.name, 
                    "capacity":0
                } for x in self.arcs},
            "variables":{
                x:{
                    "path": [a.arc.name for a in self.vars[x].arc_path], 
                    "f": [a.factor for a in self.vars[x].arc_path],
                    "priority": self.vars[x].priority
                } for x in self.vars},
        }

        if not use_expected_values:
            var_values = {
                x:[self.vars[x].value] for x in self.vars
            }
        else:
            var_values = {
                x:[self.vars[x].expected_value] for x in self.vars
            }

        return graph, var_values

    # --------------------------------------------------------------------------
    # Helper functions
    # --------------------------------------------------------------------------

    def _validate_new_name(self, collection, name) -> None:
        """Raise an error if the given name is in the given collection."""
        if name is None or not isinstance(name, str):
            raise ValueError('A name is required!')
        if name in collection:
            raise ValueError(f'The name "{name}" is already beeing used!')
        
    def _validate_existing_name(self, collection, name) -> None:
        """Raise an error if the given name is NOT in the given collection."""
        if name is None or not isinstance(name, str):
            raise ValueError('A name is required!')
        if name not in collection:
            raise ValueError(f'The name "{name}" is not found!')


    def _calculate_reservoir_diversions(self):
        """In some cases, the flow between the reservoir node and the stream 
        node cannot be determined at the time the reservoir node is added; so 
        it must be determined after the network graph has been completed.
        """
        # Loop through each reservoir reach
        # Add mass balance constraints & coeficients.
        for name in self.nodes:
            n = self.nodes[name]
            if n.is_storage_node():
                if n.storage_on_reach is None:
                    raise ValueError(f'Storage node {n.name} is missing storage_on_reach attribute.')
                reach_node = self.nodes[n.storage_on_reach]
                resv_imports = 0 # Total inflow from the reservoir from nodes other than the reach node.
                resv_exports = 0 # Total outflow from the reservoir (not counting evap) to nodes other than the reach node.
                for a in n.inflows:
                    if a.from_zone != reach_node:
                        if a.flow is None:
                            raise ValueError(f'Interzone flow {a.name} was expected to have a numeric flow, but it is None.')
                        resv_imports += a.flow

                for a in n.outflows:
                    if a.to_zone != reach_node:
                        if a.flow is None:
                            raise ValueError(f'Interzone flow {a.name} was expected to have a numeric flow, but it is None.')
                        resv_exports += a.flow

                # Identify the arc that needs its flow calculated.
                resv_to_reach_arc = None
                for a in n.inflows:
                    if a.flow is None:
                        if resv_to_reach_arc is None:
                            resv_to_reach_arc = a
                        else:
                            raise Exception('There are multiple arcs flowing from the reservoir to the reach without a measured value!')
                
                if resv_to_reach_arc is None:
                    raise Exception('Something went wrong!')
                
                resv_to_reach_arc.flow = n.storage_chg + resv_exports - resv_imports
                


    def _calculate_reach_gains_losses(self):
        """The reach gains/losses are not measured directly, but we can add 
        measurement constraints for these interzone flows using mass balance 
        equations."""

        # Loop through each stream reach
        # Add mass balance constraints & coeficients.
        for name in self.nodes:
            n = self.nodes[name]
            if n.is_source:

                # calculate sum = meas_outflows + meas_increase_in_storage - meas_inflows
                reach_gain = None
                reach_loss = None
                sum = n.storage_chg
                for a, coef in ( [(a,-1) for a in n.inflows ] + 
                                 [(a, 1) for a in n.outflows] 
                                ):
                    if a.flow is None:
                        if coef == -1:
                            if reach_gain is not None:
                                raise Exception(f'There should only be one unmeasured inflow, but found {a.name} in addition to {reach_gain.name}.')
                            reach_gain = a
                        elif coef == 1:
                            if reach_loss is not None:
                                raise Exception(f'There should only be one unmeasured outflow, but found {a.name} in addition to {reach_loss.name}.')
                            reach_loss = a
                    else:
                        sum += coef * a.flow

                #
                if reach_gain is not None and reach_loss is not None:
                    if sum > 0:
                        reach_gain.flow = sum
                        reach_loss.flow = 0
                    else:
                        reach_gain.flow = 0
                        reach_loss.flow = -sum
                    n.net_reach_gains = sum

                else:
                    raise Exception('Something went wrong. Failed to identify the reach gains and losses.')



    def _build_linear_equations(self) -> LPSolver:
        """Add the variables and constraints to the linear solver engine."""
        
        engine = LPSolver()

        # Add all variables.
        for name in self.vars:
            v = self.vars[name]
            engine.add_variable(name=v.name, lb=v.lb, ub=v.ub)

        """
        # Add mass balance constraints & coeficients.
        for name in self.nodes:
            n = self.nodes[name]
            con_name = None

            # 1/6 - I've removed the reach mass balance constraints.
            # Do we need the handoff nodes? 
            if False: #n.type == 'HANDOFF':
                con_name = 'HDOFF_MB_' + n.name
                engine.add_constriant(name=con_name, lb=0, ub=0)

            if con_name is not None:
                coef_vals = {}
                for a, coef in ( [(a, 1) for a in n.inflows ] + 
                                 [(a,-1) for a in n.outflows] 
                                ):
                    for v, coef2 in ( [(v, coef) for v in a.forward_vars ] +
                                      [(v,-coef) for v in a.backward_vars]
                                     ):
                        # Note: Use an intermediate dictionary so if a variable 
                        #       is both an inflow and an outflow we want the 
                        #       coef to be zero (1 and -1 cancel out).
                        if v.name not in coef_vals:
                            coef_vals[v.name] = 0
                        coef_vals[v.name] += coef2

                for var_name in coef_vals:
                    engine.set_coeficient(con_name, var_name, coef_vals[var_name])
        """

        # Add measurement constraints & coeficients.
        for name in self.arcs:
            a = self.arcs[name]
            if a.flow is None:
                continue

            con_name = 'MEAS_' + a.name
            engine.add_constriant(name=con_name, lb=a.flow, ub=a.flow)

            for v, coef in ( [(v, 1) for v in a.forward_vars ] +
                             [(v,-1) for v in a.backward_vars]
                            ):
                engine.set_coeficient(con_name, v.name, coef)



        # Add constraint when one trxn variable should be limited together with another variable.
        for name in self.vars:
            v = self.vars[name]
            if v.other_limited_vars is not None:
                con_name = 'LIMITWITH_' + name
                engine.add_constriant(name=con_name, lb=0, ub=v.ub)
                engine.set_coeficient(con_name, name, 1)
                for v2 in v.other_limited_vars.members:
                    engine.set_coeficient(con_name, v2.name, 1)



        # Add constraint relating each priority series variable to their children variables.
        series_names = set()
        for name in self.vars:
            v = self.vars[name]
            if v.child_series is not None:
                series_names.add(v.child_series)
                con_name = 'SERIES_' + v.child_series
                engine.add_constriant(name=con_name, lb=0, ub=0)
                engine.set_coeficient(con_name, name, 1)
        for name in self.vars:
            v = self.vars[name]
            if v.series in series_names and v.series is not None:
                con_name = 'SERIES_' + v.series
                engine.set_coeficient(con_name, name, -1)


        # Calculate the gain/loss flow.
        # Add reach gain/loss constraint:
        '''
        If there is a net gain:
        loss = 0
        gain + trxn1 + trxn2 + ... = REACH INFLOW - REACH OUTFLOW

        If there is a net loss:
        loss = - REACH INFLOW + REACH OUTFLOW
        gain + trxn1 + trxn2 + ... = 0
        '''

        return engine


    def _build_nf_mass_balance_constraints(self, engine:LPSolver):
        """
        The mass balance constraint says:
        Sum[diversion transactions, including upstream] <= Max(0, NF)
        """

        # Add mass balance constraints & coeficients.
        for name in self.nodes:
            n:ApportionmentSolverZone = self.nodes[name]

            if n.is_source:

                # Need to sum up all of the upstream gains, subtract all the losses, and sum up all the storage changes.
                sum_tributary_gains = 0
                sum_tributary_losses = 0
                sum_tributary_storage_chg = 0

                tributary_source_zones = n.all_tributary_source_zones()
                for z in tributary_source_zones:
                    g, l = z.get_gains_losses()
                    sum_tributary_gains += g
                    sum_tributary_losses += l
                    sum_tributary_storage_chg += z.storage_chg

                # Calculate natural flow:
                natural_flow = sum_tributary_gains - sum_tributary_losses - sum_tributary_storage_chg

                constraint_ub = max(0, natural_flow)

                con_name = 'NFMB_' + n.name
                engine.add_constriant(name=con_name, ub=constraint_ub)

                for z in tributary_source_zones:
                    for flow in z.outflows:
                        for var in flow.forward_vars:
                            first_flow = var.arc_path[0].arc
                            first_fact = var.arc_path[0].factor
                            from_this_zone = (first_flow.from_zone == z and first_fact > 0) or (first_flow.to_zone == z and first_fact < 0)

                            if from_this_zone and var.priority is not None:
                                engine.set_coeficient(con_name, var.name, 1)


    def _remove_nf_mass_balance_constraints(self, engine:LPSolver):

        for constraint_name in engine.get_constraint_names():
            if constraint_name[:4] == 'NFMB':
                engine.update_constraint_ub(name=constraint_name, ub=None)


    def _minimize_reservoir_spills(self, engine:LPSolver):
        
        # TODO - Is it better to minimize each spill variable separately? Or is it ok to minimize them combined?


        # Determine the minimum spill sum
        spill_variables = []
        for name in self.vars:
            if self.vars[name].is_spill:
                spill_variables.append(name)

        if len(spill_variables) > 0:
            objective_value, blah = engine.solve_objective(spill_variables, maximization=False) # minimize
            
            # Use the solution as the upper bound to 
            # create a new constraint on the set of spills.
            engine.add_constriant(name='LIMIT_SPILLS', lb=0, ub=objective_value)
            for var_name in self.vars:
                if self.vars[var_name].is_spill:
                    engine.set_coeficient('LIMIT_SPILLS', var_name, 1)




    def _calculate_apportionments(self, engine:LPSolver, start_priority=None, stop_priority=None):

        # Convert the path data to a priority-ordered schedule to loop through.
        schedule: SequentialSchedule = self._get_schedule_series()

        self.log("\nSchedule: " + str(schedule))


        # Solve.          
        for x in schedule.series:
            priority = x.priority
            item = x.item

            # Skip trxn vars with priorities earlier than start_priority. 
            if start_priority is not None and start_priority > priority:
                continue
            
            # Skip trxn vars with priorities later than stop_priority. 
            if stop_priority is not None and stop_priority < priority:
                continue

            self.log("\nPriority: {}".format(priority) )
            
            if type(item) is ProportionalSchedule:
                self._maximize_series(engine, item)
            elif type(item) is SequentialSchedule:
                self._maximize_series(engine, item)
            elif type(item) is ScheduleVariable:
                self._maximize_var(engine, item.var_name)
            
            self.log("\nCompleted iteration for priority: {}".format(priority) )


    def _solve_for_nonpath_vars(self, engine:LPSolver):

        # NOTE: I get away with not bothering to seperate the non-path variables
        #       from the path variables in this code because the path variables 
        #       have already been maximized and updated so they can not be less 
        #       than their max values.
        all_variables = [var_name for var_name in self.vars]
        blah, variable_values = engine.solve_objective(all_variables, maximization=False)
        for var_name in variable_values:
            solved_value = variable_values[var_name]
            self.vars[var_name].value = solved_value
            self.log(f' - maxed {var_name} to {solved_value}')


    def _compile_tx_results(self) -> dict[str,float]:
        """Get a list of all the variables expressed as tranactions.
        
        Returns
        -------
        list of transactions, each transactions expressed as a dictionary.
        
        """
        tx_results = {}

        for var_name in self.vars:
            v = self.vars[var_name]
            tx_results[var_name] = v.value

        return tx_results


    def _minimize_minus_vars(self, engine:LPSolver, var_names:list[str]) -> dict:
        origional_ub = {}
        for var_name in var_names:
            for minus_var in self.vars[var_name].minus_vars:
                if minus_var not in origional_ub:
                    origional_ub[minus_var] = engine.vars[minus_var].ub()
                    engine.minimize_and_update_variable(minus_var)
        return origional_ub
    
    def _reset_minus_vars(self, engine:LPSolver, origional_ub):
        for minus_var in origional_ub:
           engine.update_variable_bounds(minus_var, ub=origional_ub[minus_var])


    #
    def _maximize_var(self, engine:LPSolver, var_name):

        ## minimize all minus vars
        origional_ub = self._minimize_minus_vars(engine, [var_name])


        # Find the maximum feasible.
        new_value = engine.maximize_and_update_variable(var_name)

        # Update where we save the calculated values. 
        self.vars[var_name].value = new_value
        self.log(f' - maxed {var_name} to {new_value}')


        ## reset all minus vars
        self._reset_minus_vars(engine, origional_ub)


    def _maximize_series(self, engine:LPSolver, series: ProportionalSchedule | SequentialSchedule):
        """Maximize the given series of variables (either a sequential or 
        proportional series) until all the variables in the series are 
        maximized.
        """
        maxed_vars = []
        var_names, factors = self._get_next_iter(series, maxed_vars)
        while var_names:
            self.log('*** var_names, factors: ' + str(var_names) +', ' + str(factors))

            #?? Is there a cleaner way to do this? Just pass the list of factors?
            proportion_factors = {}
            for i, var_name in enumerate(var_names):
                proportion_factors[var_name] = factors[i]

            # 1st, deal with the edge case where every proportion_factor is zero.
            proportion_factors_sum = sum([v for k, v in proportion_factors.items()])
            if proportion_factors_sum == 0:
                for var_name, v in proportion_factors.items():
                    engine.update_variable_bounds(var_name, lb=0)
                    self.vars[var_name].value = 0
                    self.log(f' - initialized {var_name} to {0}')
                return

            # What if a proportion_factor is too close to 0 and will cause numerical issues?
            for var_name, v in proportion_factors.items():
                if v > 0 and v < 0.000001:
                    raise ValueError(f"Proportion factor for Variable {var_name} is too small and may cause numerical issues" + str(proportion_factors))



            ## minimize all minus vars
            origional_ub = self._minimize_minus_vars(engine, var_names)


            # Solve 
            var_values = engine.maximize_group_by_proportions(var_names, proportion_factors)

            # Update the variables. Set only the lb for now, since we might 
            # be able to increase this variable further in a future iteration.
            for var_name, var_value in var_values.items():
                engine.update_variable_bounds(var_name, lb=var_value)
                self.vars[var_name].value = var_value
                self.log(f' - maxed {var_name} to {var_value}')



            # Get a list of the variables that are now maximized.
            maxed_vars = maxed_vars + self._get_newly_maxed_vars(engine, var_names)
            #print('$$$$$$$$$$$$$$$$$$$$$$*****************', engine.lp_string())

            # This function will check to see if the series can further be
            # maximized, possibly by dropping a constrained variable (in a 
            # proportional series) or by replacing a constrained variable by the
            # next in line (for a sequential series). It returns the info needed
            # to continue on into another loop iteration.
            var_names, factors = self._get_next_iter(series, maxed_vars)

            ## reset all minus vars
            self._reset_minus_vars(engine, origional_ub)

        # get the vars from the series
        # get the factor from the series
        # Loop until the series
    

    def _get_newly_maxed_vars(self, engine:LPSolver, var_names):
        """Return a list of which of the given variables are now maximized."""
        maxed_var_names = []
        for var_name in var_names:
            if self._is_var_maxed(engine, var_name):
                maxed_var_names.append(var_name)
        return maxed_var_names
    
    
    # TODO - move or update
    def _is_var_maxed(self, engine:LPSolver, var_name):
        # We need to check if the given variable is as large as the constraints will allow. 
        #

        # Try to maximize the given variable; then check if the maximized value is different;
        # If the variable can be increased from its lb then return False.
        # If the variable can't be increased, it is maximized and return True.

        variable = self.vars[var_name]
        objective_value, blah = engine.solve_objective([var_name], maximization=True)
        if variable.value is None:
            return False
        else:
            return isclose(variable.value, objective_value, abs_tol=1e-4)


    def _get_next_iter(self, schedule: ProportionalSchedule | SequentialSchedule, maxed_vars):
        """Returns two lists for the next iteration.
        If there are no remaining variables to maximize, returns two empty 
        lists. 
        """
        var_names = []
        factors = []

        # If it is a sequential series, return the params for the next item.
        if type(schedule) is SequentialSchedule:
            subseries =  schedule.series
            for x in subseries:
                item = x.item

                if type(item) is SequentialSchedule or type(item) is ProportionalSchedule:
                    var_names, factors = self._get_next_iter(item, maxed_vars)
                elif type(item) is ScheduleVariable: 
                    if item.var_name not in maxed_vars:
                        var_names.append(item.var_name)
                        factors.append(1)
                if len(var_names)>0:
                    break

        # If it is a proportional series, return the list of paths and a list
        # of each's proportion. If the proportional series has any sub-series,
        # this will involve identifying which variable(s) from the subseries 
        # need to be considered and their factor(s).
        elif type(schedule) is ProportionalSchedule:
            subseries = schedule.series
            for x in subseries:
                item = x.item
                factor = x.factor

                if type(item) is SequentialSchedule or type(item) is ProportionalSchedule:
                    svar_names, sfactors = self._get_next_iter(item, maxed_vars)
                    var_names += svar_names
                    sum_sfactors = sum(sfactors)
                    if sfactors:
                        x = 0
                        if sum_sfactors > 0:
                            x = factor / sum_sfactors
                        factors += [f * x for f in sfactors]

                elif type(item) is ScheduleVariable: 
                    if item.var_name not in maxed_vars:
                        var_names.append(item.var_name)
                        factors.append(factor)

        return var_names, factors
    




    # Convert the paths dictionary to an ordered schedule list by sorting the 
    # paths by priority while grouping paths with the same priority.
    #!!!
    def _get_schedule_series(self) -> SequentialSchedule:
        
        all_series_priorities = {}

        for var_name in self.vars:
            v = self.vars[var_name]
            
            # deterimine s and p.
            s = v.series
            if s is None:
                s = "" 
            p = v.priority

            if p is None:
                continue
            
            # Add the item to the priority list of the series that it belongs 
            # with.
            if s not in all_series_priorities:
                all_series_priorities[s] = {}
            if p not in all_series_priorities[s]:
                all_series_priorities[s][p] = []

            # ... And populate a list of all the pathIds in that series 
            # with that priority as the dict value.
            all_series_priorities[s][p].append(var_name)

        #print(all_series_priorities)

        # define a recursive function to help.
        def _as_output(s) -> SequentialSchedule:
            if s not in all_series_priorities:
                return SequentialSchedule(series=[])

            series: list[SequentialScheduleItem] = []
            priorities = list(all_series_priorities[s].keys())
            priorities.sort()
            for p in priorities:
                item = {
                   "priority": p
                }

                # If there is only one item with this priority, it must be either a 
                # variable or a sequential subseries:
                if len(all_series_priorities[s][p]) == 1:
                    var_name = all_series_priorities[s][p][0]
                    cs = self.vars[var_name].child_series
                    if cs is not None:
                        sequential_subseries = _as_output(cs)
                        series.append(SequentialScheduleItem(priority=p, item=sequential_subseries)) # How to add a 'var_name'?
                    else:
                        series.append(SequentialScheduleItem(priority=p, item=ScheduleVariable(var_name)))

                # Otherwise, it must be a proportional subseries:
                else:
                    item["var_name"] = None
                    proportional_subseries: list[ProportionalScheduleItem] = []
                    cfs_sum = 0
                    for var_name in all_series_priorities[s][p]:
                        ub = self.vars[var_name].ub
                        if ub is not None:
                            cfs_limit = float(ub)
                        else:
                            cfs_limit = DEFAULT_PATH_UB # if there is no limit specified, use this large value.
                            #               (Note: The priority groups should be formulated to prevent paths with cfs-limits 
                            #                      from being grouped with paths without cfs-limits.)  
                        cfs_sum += cfs_limit
                        
                        cs = self.vars[var_name].child_series
                        if cs is None:
                            citem = ProportionalScheduleItem(
                                factor=cfs_limit,
                                item=ScheduleVariable(var_name)
                            )

                        else:
                            citem = ProportionalScheduleItem(
                                factor=cfs_limit,
                                item=_as_output(cs)
                            )
                        proportional_subseries.append(citem)
                    
                    # normalize the factor (Is this really necessary?)
                    for citem in proportional_subseries:
                        if cfs_sum > 0:
                            citem.factor /= cfs_sum

                    series.append(SequentialScheduleItem(priority=p, item=ProportionalSchedule(series=proportional_subseries)))

            return SequentialSchedule(series=series)

        return _as_output('')


        # So now a variable can represent the sum of a series! 
        #  - To check if such a variable is maximized will require new logic.

        
    def _is_natural_flow_apportionment_var(self, var_name:str):
        """Return True if the given variable originates from a reach zone, otherwise False."""

        v = self.vars[var_name]
        first_arc = v.arc_path[0].arc
        first_fact = v.arc_path[0].factor
        source_zone = first_arc.from_zone
        if first_fact < 0:
            source_zone = first_arc.to_zone

        return source_zone.is_source

