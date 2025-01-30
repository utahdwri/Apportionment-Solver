from dataclasses import dataclass
from math import inf, isclose
from .linear_solver import LinearSolver, LinearSolverError
from .globals import Globals


class ApportionmentSolver_v2:
    """Define and solve the apportionment problem."""

    def __init__(self):

        self.nodes : dict['str','ApportionmentSolverNode'] = {}
        self.arcs : dict['str','ApportionmentSolverArc'] = {}
        self.vars : dict['str','ApportionmentSolverVar'] = {}


    def __str__(self):

        def warn_if_value_is_incorrect(t):
            if t.expected_value is not None and t.value is not None:
                if abs(t.expected_value - t.value) > 1e-4:
                    return f'*** NOT EQUAL TO EXPECTED VALUE OF {t.expected_value}'
            return ''

        out = ''
        for name in self.nodes:
            n:ApportionmentSolverNode = self.nodes[name]
            if n.type == 'REACH':
                out += '\n' + n.name + f'(\u0394S={n.storage_chg}, gains-losses={n.net_reach_gains})'
                for f in n.outflows:
                    out += f'\n {f.flow} >> {f.to_node.name}'
                    for t in f.forward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value}' + warn_if_value_is_incorrect(t)
                    for t in f.backward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value} *backwards' + warn_if_value_is_incorrect(t)

                for f in n.inflows:
                    out += f'\n {f.flow} << {f.from_node.name}'
                    for t in f.forward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value}' + warn_if_value_is_incorrect(t)
                    for t in f.backward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value} *backwards' + warn_if_value_is_incorrect(t)

        return out

    def log(self, message):
        print('LOG', message)

    # --------------------------------------------------------------------------
    # Methods to build the apportionment graph (aka transaction flow graph)
    # --------------------------------------------------------------------------

    def add_reach(self, name:str, 
                  storage_chg:float=0, 
                  expected_gain:float=None,
                  expected_loss:float=None ) -> None:
        """Create a stream reach."""

        self._validate_new_name(self.nodes, name)

        # Create the node.
        self.nodes[name] = ApportionmentSolverNode(
            name=name, 
            type='REACH',
            storage_chg=storage_chg
        )

        # Create a gain-node. Every reach node has an acompaning gains node.
        gain_name = name + '_GAINS'
        self.nodes[gain_name] = ApportionmentSolverNode(
            name=gain_name, 
            type='GAINS'
        )

        # Connect the gain-node to the stream node.
        arc_name = gain_name + '>>' + name
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_node=self.nodes[gain_name],
            to_node=self.nodes[name],
            flow=None
        )

        # Create a loss-node. Every reach node has an acompaning loss node.
        loss_name = name + '_LOSS'
        self.nodes[loss_name] = ApportionmentSolverNode(
            name=loss_name, 
            type='LOSS'
        )

        # Connect the loss-node to the stream node.
        arc_name = name + '>>' + loss_name
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_node=self.nodes[name],
            to_node=self.nodes[loss_name],
            flow=None
        )

        # Create a variable representing the unapportioned inflow from the
        # gain node into this reach.
        gain_var_name = 'GAIN_' + name
        self.vars[gain_var_name] = ApportionmentSolverVar(
            name=gain_var_name,
            path_id=None,
            priority=None,
            node_path=[self.nodes[gain_name], self.nodes[name]],
            lb=0,
            ub=None,
            value=None,
            expected_value=expected_gain
        )

        # Create a variable representing the unapportioned inflow from the
        # gain node into this reach.
        loss_var_name = 'LOSS_' + name
        self.vars[loss_var_name] = ApportionmentSolverVar(
            name=loss_var_name,
            path_id=None,
            priority=None,
            node_path=[self.nodes[name], self.nodes[loss_name]],
            lb=0,
            ub=None,
            value=None,
            expected_value=expected_loss
        )

    def add_reach_reservoir(self, reach_name:str, resv_name:str, storage_chg:float, storage_loss:float=0):
        """Define an on-stream reservoir. An on-stream reaservoir is represented
        as a distinct node that is connected to the stream node."""

        self._validate_existing_name(self.nodes, reach_name)
        self._validate_new_name(self.nodes, resv_name)

        # Create the node.
        self.nodes[resv_name] = ApportionmentSolverNode(
            name=resv_name, 
            type='STORAGE',
            storage_chg=storage_chg + storage_loss,
            storage_on_reach=reach_name
        )

        # Connect the reservoir node to the stream node.
        arc_name = reach_name + '>>' + resv_name
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_node=self.nodes[reach_name],
            to_node=self.nodes[resv_name],
            flow=None
        )

        # Create a variable representing unauthorized diversions to the reservoir.
        unauth_var_name = 'UNAUTH_' + resv_name
        self.vars[unauth_var_name] = ApportionmentSolverVar(
            name=unauth_var_name,
            path_id=None,
            priority=None,
            node_path=[self.nodes[reach_name], self.nodes[resv_name]],
            lb=0,
            ub=None,
            value=None
        )

        # Create a variable representing uncontrolled releases from the reservoir.
        spill_var_name = 'SPILL_' + resv_name
        self.vars[spill_var_name] = ApportionmentSolverVar(
            name=spill_var_name,
            path_id=None,
            priority=None,
            node_path=[self.nodes[resv_name], self.nodes[reach_name]],
            lb=0,
            ub=None,
            value=None
        )

    def add_handoff(self, reach_name:str, handoff_name:str):
        """Add a change handoff node."""

        self._validate_existing_name(self.nodes, reach_name)
        self._validate_new_name(self.nodes, handoff_name)

        # Create the node.
        self.nodes[handoff_name] = ApportionmentSolverNode(
            name=handoff_name, 
            type='HANDOFF'
        )

        # Connect the reservoir node to the stream node.
        arc_name = reach_name + '>>' + handoff_name
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_node=self.nodes[reach_name],
            to_node=self.nodes[handoff_name],
            flow=0
        )

        
    def add_reach_diversion(self, reach_name:str, div_name:str, flow:float):
        """Define a measured artificial outflow from a stream reach."""

        self._validate_existing_name(self.nodes, reach_name)
        self._validate_new_name(self.nodes, div_name)

        # Create the node.
        self.nodes[div_name] = ApportionmentSolverNode(
            name=div_name, 
            type='DIVERSION'
        )

        # Connect the diversion node to the stream reach.
        arc_name = reach_name + '>>' + div_name
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_node=self.nodes[reach_name],
            to_node=self.nodes[div_name],
            flow=flow
        )

        # Create a variable representing unauthorized diversions.
        unauth_var_name = 'UNAUTH_' + div_name
        self.vars[unauth_var_name] = ApportionmentSolverVar(
            name=unauth_var_name,
            path_id=None,
            priority=None,
            node_path=[self.nodes[reach_name], self.nodes[div_name]],
            lb=0,
            ub=None,
            value=None
        )

    def add_reach_import(self, reach_name:str, imp_name:str, flow:float):
        """Define a measured artificial inflow to a stream reach."""

        self._validate_existing_name(self.nodes, reach_name)
        self._validate_new_name(self.nodes, imp_name)

        # Create the node.
        self.nodes[imp_name] = ApportionmentSolverNode(
            name=imp_name, 
            type='IMPORT'
        )

        # Connect the node to the stream reach.
        arc_name = imp_name + '>>' + reach_name
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_node=self.nodes[imp_name],
            to_node=self.nodes[reach_name],
            flow=flow
        )

        # Create a variable representing uncontrolled releases.
        spill_var_name = 'SPILL_' + imp_name
        self.vars[spill_var_name] = ApportionmentSolverVar(
            name=spill_var_name,
            path_id=None,
            priority=None,
            node_path=[self.nodes[imp_name], self.nodes[reach_name]],
            lb=0,
            ub=None,
            value=None
        )

    def add_connection(self, from_name:str, to_name:str, flow:float):
        """Define how two stream reaches are connected."""
        # 1/20/2025 - This function was updated and renamed so it could be used 
        #             to connect any two nodes, not just two reach nodes. 

        self._validate_existing_name(self.nodes, from_name)
        self._validate_existing_name(self.nodes, to_name)

        # Connect the two nodes.
        arc_name = from_name + '>>' + to_name
        self._validate_new_name(self.arcs, arc_name)
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_node=self.nodes[from_name],
            to_node=self.nodes[to_name],
            flow=flow
        )



        # Create a variable representing this flow.
        flow_var_name = 'FLOW_' + from_name + '_TO_' + to_name
        self.vars[flow_var_name] = ApportionmentSolverVar(
            name=flow_var_name,
            path_id=None,
            priority=None,
            node_path=[self.nodes[from_name], self.nodes[to_name]],
            lb=0,
            ub=None,
            value=None
        )

    def add_transaction(self, id:int, priority:float, limit:float, path:list[str],
                        limited_by_id:int = None,
                        series_name:str = None,
                        child_series_name:str = None, 
                        expected_value:float = None ):
        """Define an authorized transaction (a flow variable with a priority).
        
        The expected_value is only used for running tests.
        """

        # Convert the path list of names to a list of node objects.
        node_path = []
        if path is not None: # a path may be None if it hold sub-series.
            for node_name in path:
                self._validate_existing_name(self.nodes, node_name)
                node_path.append(self.nodes[node_name])

        # Add the variable.
        var_name = 'TRXN_' + str(id)
        self.vars[var_name] = ApportionmentSolverVar(
            name=var_name,
            path_id=id,
            priority=priority,
            node_path=node_path,
            lb=0,
            ub=limit,
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

    def solve(self):

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

        # We cannot allow any apportionment to force reservoir or import water to spill to natural flow.
        # So these spills should be calculated and fixed prior to the apportionments.
        self._minimize_reservoir_spills(engine)

        # Relax handoff constraints.
        # Temporaraly allow inflow to a change handoff to exceed outflow.
        self._relax_handoff_constraints(engine)


        # Calculate the apportionments.
        self._calculate_apportionments(engine)

        # Now finalize the other variables (spills, unauthorized, nf gains/losses)
        self._solve_for_nonpath_vars(engine)


        return self._compile_tx_results() #apportionment results


    # --------------------------------------------------------------------------
    # Utility function for testing
    # --------------------------------------------------------------------------
    def assert_variables_equal_expected(self, message=''):
        """Check if each of the variables match the expected 
        value to 4 decimal places. 

        Skips variables that don't have a defined expected value.
        
        Raises an exception if no variables have an expected value. """

        cnt = 0

        for var_name in self.vars:
            expected_value = self.vars[var_name].expected_value
            computed_value = self.vars[var_name].value
            if expected_value is not None:
                cnt += 1
                msg = ( message +
                       f'Variable "{var_name}": computed ({computed_value}) != expected'
                       f' ({expected_value})\n'+str(self)
                       )
                assert abs(expected_value - computed_value) < 1e-4, msg

        if cnt == 0:
            raise Exception('No variables were given an expected_value!')


    # --------------------------------------------------------------------------
    # Helper functions
    # --------------------------------------------------------------------------

    def _validate_new_name(self, collection, name):
        """Raise an error if the given name is in the given collection."""
        if name is None or not isinstance(name, str):
            raise ValueError('A name is required!')
        if name in collection:
            raise ValueError(f'The name "{name}" is already beeing used!')
        
    def _validate_existing_name(self, collection, name):
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

                reach_node = self.nodes[n.storage_on_reach]
                resv_imports = 0 # Total inflow from the reservoir from nodes other than the reach node.
                resv_exports = 0 # Total outflow from the reservoir (not counting evap) to nodes other than the reach node.
                for a in n.inflows:
                    if a.from_node != reach_node:
                        resv_imports += a.flow

                for a in n.outflows:
                    if a.to_node != reach_node:
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
        measurement constraints for these arc using simple mass balances."""

        # Loop through each stream reach
        # Add mass balance constraints & coeficients.
        for name in self.nodes:
            n = self.nodes[name]
            if n.type == 'REACH':

                # calculate sum = meas_outflows + meas_increase_in_storage - meas_inflows
                reach_gain = None
                reach_loss = None
                sum = n.storage_chg
                for a, coef in ( [(a,-1) for a in n.inflows ] + 
                                 [(a, 1) for a in n.outflows] 
                                ):
                    if a.flow is None and coef == -1:
                        if reach_gain is not None:
                            raise Exception('Something went wrong. There should only be one unmeasured inflow.')
                        reach_gain = a
                    elif a.flow is None and coef == 1:
                        if reach_loss is not None:
                            raise Exception('Something went wrong. There should only be one unmeasured outflow.')
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



    def _build_linear_equations(self) -> LinearSolver:
        """Add the variables and constraints to the linear solver engine."""
        
        engine = LinearSolver()

        # Add all variables.
        for name in self.vars:
            v = self.vars[name]
            engine.add_variable(name=v.name, lb=v.lb, ub=v.ub)


        # Add mass balance constraints & coeficients.
        for name in self.nodes:
            n = self.nodes[name]
            con_name = None

            # 1/6 - I've removed the reach mass balance constraints.
            # Do we need the handoff nodes? 
            if n.type == 'HANDOFF':
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


        # Add measurement constraints & coeficients.
        for name in self.arcs:
            a = self.arcs[name]
            if a.flow is None:
                continue
            if a.to_node.type == 'HANDOFF':
                continue # we've effectively already added this constraint previously 

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
            if v.series in series_names:
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


    def _minimize_reservoir_spills(self, engine):
        
        # TODO - Is it better to minimize each spill variable separately? Or is it ok to minimize them combined?


        # Determine the minimum spill sum
        spill_variables = []
        for name in self.vars:
            if name[0:6] == 'SPILL_':
                spill_variables.append(name)

        if len(spill_variables) > 0:
            objective_value, blah = engine.solve_objective(spill_variables, minimization=True)
            
            # Use the solution as the upper bound to 
            # create a new constraint on the set of spills.
            engine.add_constriant(name='LIMIT_SPILLS', lb=0, ub=objective_value)
            for var_name in self.vars:
                if var_name[0:6] == 'SPILL_':
                    engine.set_coeficient('LIMIT_SPILLS', var_name, 1)



    def _relax_handoff_constraints(self, engine):
        """ Handoff constraints are INFLOWS - OUTFLOWS = 0

        So we relax to allow outflows to be calculated after inflows (rather 
        than at the same time) by setting 0 <= INFLOWS - OUTFLOWS, i.e. by
        removing the upper bound of the constraint.

        """
        for conId in engine.get_constraint_names():
            if conId[0:9] == 'HDOFF_MB_':
                engine.update_constraint_bounds(conId, ub=inf)


    def _calculate_apportionments(self, engine, start_priority=None, stop_priority=None):

        # Convert the path data to a priority-ordered schedule to loop through.
        schedule = self._get_schedule_series()
        handoffs_by_latest_withdrawal_priority = self._get_handoffs()

        self.log("\nSchedule: " + str(schedule))
        self.log("\nHandoffs_by_latest_withdrawal_priority: " + str(handoffs_by_latest_withdrawal_priority))


        # Solve.          
        for item in schedule:

            # Skip trxn vars with priorities earlier than start_priority. 
            if start_priority is not None and start_priority > item["priority"]:
                continue
            
            # Skip trxn vars with priorities later than stop_priority. 
            if stop_priority is not None and stop_priority < item["priority"]:
                continue

            self.log("\nPriority: {}".format(item["priority"]) )

            #

            if ('proportional_subseries' in item or 
                'sequential_subseries' in item    ):
                self._maximize_series(engine, item)
            
            elif item["var_name"] is not None:
                self._maximize_var(engine, item["var_name"])

            print('!', self._compile_tx_results())
            print('!!', engine.lp_string())

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
                    engine.update_constraint_bounds(conId, ub=0)

                    print('***',engine.lp_string())

                    # Check if the problem is still feasible.
                    try:
                        to_handoff_variables = []
                        for var_name in handoff["to_vars"]:
                            to_handoff_variables.append(var_name)
                        engine.solve_objective(to_handoff_variables, maximization=True)

                    except LinearSolverError:
                        # If not feasible, make a note ...
                        handoffs_with_extra_water.append( handoff )

                        # And allow inflows into the handoff node to be reduced (so the above constraint is possible)
                        for var_name in handoff["to_vars"]:
                            engine.update_variable_bounds(var_name, lb=0)


                # If so, we need to loop back and apportion it.
                if len(handoffs_with_extra_water) > 0:
                    start_p = min([x['earliest_deposit_priority'] for x in handoffs_with_extra_water])
                    stop_p = item['priority']
                    self.log("Need to re-apportion this water. Looping back to: {}".format(start_p))
                    self._calculate_apportionments(engine, start_p, stop_p)

            
            self.log("\nCompleted iteration for priority: {}".format(item["priority"]) )


    def _solve_for_nonpath_vars(self, engine):

        # NOTE: I get away with not bothering to seperate the non-path variables
        #       from the path variables in this code because the path variables 
        #       have already been maximized and updated so they can not be less 
        #       than their max values.
        all_variables = [var_name for var_name in self.vars]
        blah, variable_values = engine.solve_objective(all_variables, minimization=True)
        for var_name in variable_values:
            solved_value = variable_values[var_name]
            self.vars[var_name].value = solved_value


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


    #
    def _maximize_var(self, engine, var_name):

        # Find the maximum feasible.
        new_value = engine.maximize_and_update_variable(var_name)

        # Update where we save the calculated values. 
        self.vars[var_name].value = new_value


    def _maximize_series(self, engine, series):
        """Maximize the given series of variables (either a sequential or 
        proportional series) until all the variables in the series are 
        maximized."""
        maxed_vars = []
        var_names, factors = self._get_next_iter(series, maxed_vars)
        while var_names:
            print('*** var_names, factors', var_names, factors)

            #?? Is there a cleaner way to do this? Just pass the list of factors?
            proportion_factors_by_var_names = {}
            for i, var_name in enumerate(var_names):
                proportion_factors_by_var_names[var_name] = factors[i]


            self._maximize_vars_inner(engine, var_names, proportion_factors_by_var_names)

            maxed_vars = maxed_vars + self._get_newly_maxed_vars(engine, var_names)
            print('$$$$$$$$$$$$$$$$$$$$$$*****************', engine.lp_string())

            # This function will check to see if the series can further be
            # maximized, possibly by dropping a constrained variable (in a 
            # proportional series) or by replacing a constrained variable by the
            # next in line (for a sequential series). It returns the info needed
            # to continue on into another loop iteration.
            var_names, factors = self._get_next_iter(series, maxed_vars)


        # get the vars from the series
        # get the factor from the series
        # Loop until the series
    

    def _get_newly_maxed_vars(self, engine, var_names):
        """Return a list of which of the given variables are now maximized."""
        maxed_var_names = []
        for var_name in var_names:
            if self._is_var_maxed(engine, var_name):
                maxed_var_names.append(var_name)
        return maxed_var_names
    
    
    # TODO - move or update
    def _is_var_maxed(self, engine, var_name):
        # We need to check if the given variable is as large as the constraints will allow. 
        #

        # Try to maximize the given variable; then check if the maximized value is different;
        # If the variable can be increased from its lb then return False.
        # If the variable can't be increased, it is maximized and return True.

        variable = self.vars[var_name]
        try:
            objective_value, blah = engine.solve_objective([var_name], maximization=True)
        except Exception:
            return True
        return isclose(variable.value, objective_value, abs_tol=1e-4)


    def _get_next_iter(self, series, maxed_vars):
        """Returns two lists for the next iteration.
        If there are no remaining variables to maximize, returns two empty 
        lists. 
        """
        var_names = []
        factors = []

        #If it is a sequential series, return the params for the next item.
        if 'sequential_subseries' in series:
            subseries =  series['sequential_subseries']
            for item in subseries:
                if ('sequential_subseries' in item or 
                    'proportional_subseries' in item ):
                    var_names, factors = self._get_next_iter(item, maxed_vars)
                elif item['var_name'] not in maxed_vars:
                    var_names.append(item['var_name'])
                    factors.append(1)
                if len(var_names)>0:
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
                    svar_names, sfactors = self._get_next_iter(item, maxed_vars)
                    var_names += svar_names
                    if sfactors:
                        x = item['factor'] / sum(sfactors)
                        factors += [f * x for f in sfactors]

                elif item['var_name'] not in maxed_vars:
                    var_names.append(item['var_name'])
                    factors.append(item['factor'])


        return var_names, factors
    

    def _maximize_vars_inner(self, engine, var_names, proportion_factors):

        # Solve 
        var_values = engine.maximize_group_by_proportions(var_names, proportion_factors)

        # Update the variables. 
        # Set only the lb for now, since we might be able to increase this variable more in a future iteration.
        for var_name, var_value in var_values.items():
            engine.update_variable_bounds(var_name, lb=var_value)
            self.vars[var_name].value = var_value
        


    # Convert the paths dictionary to an ordered schedule list by sorting the 
    # paths by priority while grouping paths with the same priority.
    #!!!
    def _get_schedule_series(self):
        
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
        def _as_output(s):
            if s not in all_series_priorities:
                return []

            output = []
            priorities = list(all_series_priorities[s].keys())
            priorities.sort()
            for p in priorities:
                item = {
                   "priority": p
                }
                if len(all_series_priorities[s][p]) == 1:
                    var_name = all_series_priorities[s][p][0]
                    item["var_name"] = var_name
                    cs = self.vars[var_name].child_series
                    if cs is not None:
                        item["sequential_subseries"] = _as_output(cs)
                else:
                    item["var_name"] = None
                    item["proportional_subseries"] = []
                    cfs_sum = 0
                    for var_name in all_series_priorities[s][p]:
                        try:
                            cfs_limit = float(self.vars[var_name].ub)
                        except Exception:
                            cfs_limit = Globals.DEFAULT_PATH_UB # if there is no limit specified, use this large value.
                            #               (Note: The priority groups should be formulated to prevent paths with cfs-limits 
                            #                      from being grouped with paths without cfs-limits.)  
                        cfs_sum += cfs_limit
                        
                        citem = {"factor":cfs_limit, "var_name":var_name}
                        cs = self.vars[var_name].child_series
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

        
    ## TODO - fix this to use updated/rearanged objects.
    ##      - replace pathId with var_name

    def _get_handoffs(self):
    
        """ Does it make sense for a path to end at multiple handoff caches? 
            Or a handoff cache and other end-points? If so, 
            how will we know how much flow is available to take from the cache?
        """


        pri_vars = {name:self.vars[name] for name in self.vars if self.vars[name].priority is not None}


        # Need the earliest priority of a deposit into the handoff cache. 
        # Need the latest priority of a withdrawl from the handoff cache. 
        # Need lists of the paths that deposit into the cache and withdrawal from the cache.

        handoffs_by_id = {} # key is the priority when the 
                            # value is another dictionary

        # Add an item for each handoff, and populate "to_vars"
        for node_name in self.nodes:
            n = self.nodes[node_name]
            if n.type == 'HANDOFF':
                handoffs_by_id[node_name] = {
                    "id": node_name,
                    "earliest_deposit_priority": None,   # this is populated later
                    "latest_withdrawal_priority": None,  # this is populated later
                    "from_vars": [],                    # this is populated later
                    "to_vars": []                       # this is populated later
                }
        # Populate "to_vars"
        for var_name in pri_vars:
            v = pri_vars[var_name]
            try:
                first_node_name = v.node_path[0].name
                last_node_name = v.node_path[-1].name

                if last_node_name in handoffs_by_id:
                    handoffs_by_id[last_node_name]["to_vars"].append(var_name)
                if first_node_name in handoffs_by_id:
                    handoffs_by_id[first_node_name]["from_vars"].append(var_name)
            except:
                pass

        # ... now update "earliest_deposit_priority" and "latest_withdrawal_priority"
        for id in handoffs_by_id:
            maxp_to = max([pri_vars[x].priority for x in handoffs_by_id[id]["to_vars"]])
            minp_to = min([pri_vars[x].priority for x in handoffs_by_id[id]["to_vars"]])
            maxp_from = max([pri_vars[x].priority for x in handoffs_by_id[id]["from_vars"]])
            minp_from = min([pri_vars[x].priority for x in handoffs_by_id[id]["from_vars"]])

            if maxp_to > minp_from:
                raise ValueError('Handoff ['+str(id)+'] has a deposite path that is junior to a withdrawal path!')

            handoffs_by_id[id]["earliest_deposit_priority"] = minp_to
            handoffs_by_id[id]["latest_withdrawal_priority"] = maxp_from


        # Now rearange so the data is sorted by "latest_withdrawal_priority"
        handoffs_by_latest_withdrawal_priority = {}
        for id in handoffs_by_id:
            p = handoffs_by_id[id]['latest_withdrawal_priority']
            if p not in handoffs_by_latest_withdrawal_priority:
                handoffs_by_latest_withdrawal_priority[p] = []
            handoffs_by_latest_withdrawal_priority[p].append( handoffs_by_id[id] )


        return handoffs_by_latest_withdrawal_priority



'''@dataclass
class PrioritySeries:
    """ """
    priority: int
    proportional_subseries: list
    sequential_subseries: list'''



@dataclass
class ApportionmentSolverNode:
    """A Node in the apportionment Graph."""
    name: str
    type: str
    storage_chg: float = 0
    
    # If this is set, it indicates the node is an on-stream storage node. The 
    # value indicates the name of the stream reach.
    storage_on_reach: str | None = None
    
    def __post_init__(self):
        # Create variables.
        self.inflows:list['ApportionmentSolverArc'] = []
        self.outflows:list['ApportionmentSolverArc'] = []

    def is_storage_node(self):
        """Return whether or not this node is a storage node."""
        return self.storage_on_reach is not None


@dataclass
class ApportionmentSolverArc:
    """An Arc in the apportionment Graph."""
    name: str
    from_node: ApportionmentSolverNode
    to_node: ApportionmentSolverNode
    flow: float | None # (Only arcs related to GAINS, LOSSES, or STORAGE nodes can have a None flow specified initially.)

    def __post_init__(self):
        # Create another variable
        self.forward_vars = []
        self.backward_vars = []

        # Link the related nodes to this arc.
        self.from_node.outflows.append(self)
        self.to_node.inflows.append(self)


@dataclass
class ApportionmentSolverVar:
    """A variable/transaction in the apportionment Graph. 
    These are what we aim to solve for!"""
    name: str
    path_id: int
    priority: float
    node_path: list[ApportionmentSolverNode]
    lb: float
    ub: float 
    value: float = None
    series: str = None
    child_series: str = None
    expected_value: float = None
    other_limited_vars:'ApportionmentSolverVarGroup' = None

    def __post_init__(self):
        # Add references to this Var to each traversed Arc.
        for i in range(1, len(self.node_path)):
            a = self.node_path[i-1]
            b = self.node_path[i]

            found = False
            for arc in a.outflows:
                if arc.to_node == b:
                    found = True
                    arc.forward_vars.append(self)
                    break
            if found:
                continue
            for arc in a.inflows:
                if arc.from_node == b:
                    arc.backward_vars.append(self)
                    break

@dataclass
class ApportionmentSolverVarGroup:
    """
    """
    members: list[ApportionmentSolverVar]