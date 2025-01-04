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
        out = ''
        for name in self.nodes:
            n:ApportionmentSolverNode = self.nodes[name]
            if n.type == 'REACH':
                out += '\n' + n.name
                for f in n.outflows:
                    out += f'\n {f.flow} >> {f.to_node.name}'
                    for t in f.forward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value}'
                    for t in f.backward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value} *backwards'

                for f in n.inflows:
                    out += f'\n {f.flow} << {f.from_node.name}'
                    for t in f.forward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value}'
                    for t in f.backward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value} *backwards'

        return out

    def log(self, message):
        print('LOG', message)

    # --------------------------------------------------------------------------
    # Methods to build the apportionment graph (aka transaction flow graph)
    # --------------------------------------------------------------------------

    def add_reach(self, name:str, 
                  storage_chg:float=0, 
                  expected_gain:float=None) -> None:
        """Create a stream reach."""

        self._validate_new_name(self.nodes, name)

        # Create the node.
        self.nodes[name] = ApportionmentSolverNode(
            name=name, 
            type='REACH',
            storage_chg=storage_chg
        )

        # Create a gain-node. Every reach node has an acompaning gains/loss node.
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

        # Create a variable representing the net gain (or loss) to this reach.
        gain_var_name = 'GAIN_' + name
        self.vars[gain_var_name] = ApportionmentSolverVar(
            name=gain_var_name,
            path_id=None,
            priority=None,
            node_path=[self.nodes[gain_name], self.nodes[name]],
            lb=None,
            ub=None,
            value=None,
            expected_value=expected_gain
        )

    def add_reach_reservoir(self, reach_name:str, resv_name:str, storage_chg:float, storage_loss:float=0):
        """Define an on-stream reservoir. An on-stream reaservoir is represented
        as a distinct node that is connected to the stream node."""

        self._validate_existing_name(self.nodes, reach_name)
        self._validate_new_name(self.nodes, resv_name)

        # Create the node.
        self.nodes[resv_name] = ApportionmentSolverNode(
            name=resv_name, 
            type='STORAGE'
        )

        # Connect the reservoir node to the stream node.
        arc_name = reach_name + '>>' + resv_name
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_node=self.nodes[reach_name],
            to_node=self.nodes[resv_name],
            flow=storage_chg + storage_loss
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

    def add_reach_connection(self, from_reach_name:str, to_reach_name:str, flow:float):
        """Define how two stream reaches are connected."""

        self._validate_existing_name(self.nodes, from_reach_name)
        self._validate_existing_name(self.nodes, to_reach_name)

        # Connect the two stream reaches.
        arc_name = from_reach_name + '>>' + to_reach_name
        self._validate_new_name(self.arcs, arc_name)
        self.arcs[arc_name] = ApportionmentSolverArc(
            name=arc_name, 
            from_node=self.nodes[from_reach_name],
            to_node=self.nodes[to_reach_name],
            flow=flow
        )

        # Create a variable representing this flow.
        flow_var_name = 'FLOW_' + from_reach_name + '_TO_' + to_reach_name
        self.vars[flow_var_name] = ApportionmentSolverVar(
            name=flow_var_name,
            path_id=None,
            priority=None,
            node_path=[self.nodes[from_reach_name], self.nodes[to_reach_name]],
            lb=0,
            ub=None,
            value=None
        )

    def add_transaction(self, id:int, priority:float, limit:float, path:list[str],
                        expected_value:float=None ):
        """Define an authorized transaction (a flow variable with a priority).
        
        The expected_value is only used for running tests.
        """

        node_path = []
        for node_name in path:
            self._validate_existing_name(self.nodes, node_name)
            node_path.append(self.nodes[node_name])

        var_name = 'TRXN_' + str(id)
        self.vars[var_name] = ApportionmentSolverVar(
            name=var_name,
            path_id=id,
            priority=priority,
            node_path=node_path,
            lb=0,
            ub=limit,
            value=None,
            expected_value=expected_value
        )


    # --------------------------------------------------------------------------
    # Method to actually build and solve the system of linear equations.
    # --------------------------------------------------------------------------

    def solve(self):

        # Convert the nodes, arcs, and variables into a system of linear 
        # equations that can be solved.
        engine = self._build_linear_equations()

        # Calculate reach gains/losses
        self._calculate_reach_nf(engine)

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
    def assert_variables_equal_expected(self):
        """Check if each of the variables match the expected 
        value to 4 decimal places. 
        
        Raises an exception if no variables have an expected value. """

        cnt = 0

        for var_name in self.vars:
            expected_value = self.vars[var_name].expected_value
            computed_value = self.vars[var_name].value
            if expected_value is not None:
                cnt += 1
                msg = (f'Computed value ({computed_value}) does not equal expected'
                       f' value ({expected_value}) for variable "{var_name}".'
                       '\n'+str(self)
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

            if n.type == 'REACH':
                con_name = 'REACH_MB_' + n.name
                engine.add_constriant(name=con_name, lb=n.storage_chg, ub=n.storage_chg)
            elif n.type == 'HANDOFF':
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

        return engine



    def _calculate_reach_nf(self, engine):
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
        # TODO - consider if this method is unnecesary if unmeasured diversions 
        # can be dealt with elsewhere...


        # Minimize the reach gains. 
        # (This only has an effect if there are unmetered diversions.
        # It will make it so there is no water remaining in the reach 
        # to be apportioned to these unmetered diversions.)
        gain_variables = [name for name in self.vars if name[0:5] == 'GAIN_']
        blah, variable_values = engine.solve_objective(gain_variables, minimization=True)

        # Set the reach-gain (or loss) values to constants.
        for var_name, calc_value in variable_values.items():
            engine.update_variable_bounds(var_name, lb=calc_value, ub=calc_value)
            self.log('{} value = {}'.format(var_name, calc_value))

            # And store the value in the zone object.
            self.vars[var_name].value = calc_value


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

            #?? Is there a cleaner way to do this? Just pass the list of factors?
            proportion_factors_by_var_names = {}
            for i, var_name in enumerate(var_names):
                proportion_factors_by_var_names[var_name] = factors[i]


            self._maximize_vars_inner(engine, var_names, proportion_factors_by_var_names)

            maxed_vars = maxed_vars + self._get_newly_maxed_vars(engine, var_names)

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
            first_node_name = v.node_path[0].name
            last_node_name = v.node_path[-1].name

            if last_node_name in handoffs_by_id:
                handoffs_by_id[last_node_name]["to_vars"].append(var_name)
            if first_node_name in handoffs_by_id:
                handoffs_by_id[first_node_name]["from_vars"].append(var_name)

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






@dataclass
class ApportionmentSolverNode:
    """A Node in the apportionment Graph."""
    name: str
    type: str
    storage_chg: float = 0
    
    def __post_init__(self):
        # Create variables.
        self.inflows:list['ApportionmentSolverArc'] = []
        self.outflows:list['ApportionmentSolverArc'] = []


@dataclass
class ApportionmentSolverArc:
    """An Arc in the apportionment Graph."""
    name: str
    from_node: ApportionmentSolverNode
    to_node: ApportionmentSolverNode
    flow: float

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

