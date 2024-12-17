from dataclasses import dataclass
from math import inf, isclose
from .linear_solver import LinearSolver, LinearSolverError
from .globals import Globals
from .variable import Variable


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
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value}'

                for f in n.inflows:
                    out += f'\n {f.flow} << {f.from_node.name}'
                    for t in f.forward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value}'
                    for t in f.backward_vars:
                        out += f'\n      {t.name} ({t.lb}, {t.ub}) = {t.value}'

        return out

    def log(self, message):
        print('LOG', message)

    # --------------------------------------------------------------------------
    # Methods to build the apportionment graph (aka transaction flow graph)
    # --------------------------------------------------------------------------

    def add_reach(self, name:str) -> None:
        """Create a stream reach."""

        self._validate_new_name(self.nodes, name)

        # Create the node.
        self.nodes[name] = ApportionmentSolverNode(
            name=name, 
            type='REACH'
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
            value=0
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
            value=0
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
            value=0
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
        """Define an outflow from a stream reach."""

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
            value=0
        )

    def add_reach_import(self, reach_name:str, imp_name:str, flow:float):
        """Define an inflow into a stream reach."""

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
            value=0
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
            value=0
        )

    def add_transaction(self, id:int, priority:float, limit:float, path:list[str] ):
        """Define an authorized transaction (a flow variable with a priority)."""

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
            value=0
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
                engine.add_constriant(name=con_name, lb=0, ub=0)
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
    value: float
    series: str = None
    child_series: str = None

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










class ApportionmentSolver:
    """ """


    def __init__(self, date, logging=None):

        print('Date: {}'.format(date))

        self.date = date
        self.logging = logging
        self.input = None
        self.engine = LinearSolver()
        self.results = ResultCollection() # This object helps manage the solved output.


    def set_input(self, input, measurement_manager):
        """Set up the system of linear equations that will be solved.
        """

        self.input = input


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
                con_names = self.engine.get_constraint_names()

                if v.from_change is not None:
                    con_name = 'HDOFF_MB_' + from_zone.id + '_' + v.from_change
                    if con_name not in con_names:
                        self.engine.add_constriant(name=con_name, lb=0, ub=0)
                    self.engine.set_coeficient(con_name, v.id, -1)

                elif from_zone.has_natural_flowline:
                    con_name = 'REACH_MB_' + from_zone.id
                    if con_name not in con_names:
                        self.engine.add_constriant(name=con_name, lb=0, ub=0)
                    self.engine.set_coeficient(con_name, v.id, -1)

            if to_zone is not None:
                con_names = self.engine.get_constraint_names()

                if v.to_change is not None:
                    con_name = 'HDOFF_MB_' + to_zone.id + '_' + v.to_change
                    if con_name not in con_names:
                        self.engine.add_constriant(name=con_name, lb=0, ub=0)
                    self.engine.set_coeficient(con_name, v.id, 1)

                elif to_zone.has_natural_flowline:
                    con_name = 'REACH_MB_' + to_zone.id
                    if con_name not in con_names:
                        self.engine.add_constriant(name=con_name, lb=0, ub=0)
                    self.engine.set_coeficient(con_name, v.id, 1)
            



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

                if con_name not in self.engine.get_constraint_names():
                    self.engine.add_constriant(name=con_name, lb=meas_value, ub=meas_value)
                self.engine.set_coeficient(con_name, var_name, coef)



        #
        # Variables:
        #
        for vId in self.input.variables:
            v = self.input.variables[vId]

            self.engine.add_variable(name=v.id, lb=v.lb, ub=v.ub)

            add_to_reach_mass_balance_equation(v)

            for subarcId in v.forward_subarcs:
                flowlineId = subarcId # TODO!! fix this! The program is conflating subarcs and flowlines.
                add_to_measurement_equation(v.id, flowlineId, 1)
            for subarcId in v.backward_subarcs:
                flowlineId = subarcId # see above
                add_to_measurement_equation(v.id, flowlineId,-1)




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
        blah, variable_values = self.engine.solve_objective(reach_variables, minimization=True)

        # Set the reach-gain (or loss) values to constants.
        for var_name, calc_value in variable_values.items():
            self.engine.update_variable_bounds(var_name, lb=calc_value, ub=calc_value)
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
            objective_value, blah = self.engine.solve_objective(spill_variables, minimization=True)
            
            # Use the solution as the upper bound to 
            # create a new constraint on the set of spills.
            self.engine.add_constriant(name='LIMIT_SPILLS', lb=0, ub=objective_value)
            for varId in self.input.variables:
                if varId[0:9] == 'NF_SPILL_':
                    self.engine.set_coeficient('LIMIT_SPILLS', varId, 1)



    def relax_handoff_constraints(self):
        """ Handoff constraints are INFLOWS - OUTFLOWS = 0

        So we relax to allow outflows to be calculated after inflows (rather 
        than at the same time) by setting 0 <= INFLOWS - OUTFLOWS, i.e. by
        removing the upper bound of the constraint.

        """
        for conId in self.engine.get_constraint_names():
            if conId[0:9] == 'HDOFF_MB_':
                self.engine.update_constraint_bounds(conId, ub=inf)


    def calculate_apportionments(self, start_priority=None, stop_priority=None):

        paths = self.input.wrnet.paths

        # Convert the path data to a priority-ordered schedule to loop through.
        schedule = self.get_schedule_series(paths)
        handoffs_by_latest_withdrawal_priority = self.get_handoffs()

        self.log("\nSchedule: " + str(schedule))
        self.log("\nHandoffs_by_latest_withdrawal_priority: " + str(handoffs_by_latest_withdrawal_priority))


        # Solve.          
        for item in schedule:

            # Skip paths with priorities earlier than start_priority. 
            if start_priority is not None and start_priority > item["priority"]:
                continue
            
            # Skip paths with priorities later than stop_priority. 
            if stop_priority is not None and stop_priority < item["priority"]:
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

            print('!!!', self.engine.lp_string())
            
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
                    self.engine.update_constraint_bounds(conId, ub=0)


                    # Check if the problem is still feasible.
                    try:
                        to_handoff_variables = []
                        for path_name in handoff["to_paths"]:
                            to_handoff_variables.append('PATH_'+path_name)
                        self.engine.solve_objective(to_handoff_variables, maximization=True)

                    except LinearSolverError: #TODO - the tests are not getting to this code! Add new tests!
                        # If not feasible, make a note ...
                        handoffs_with_extra_water.append( handoff )

                        # And allow inflows into the handoff node to be reduced (so the above constraint is possible)
                        for path_name in handoff["to_paths"]:
                            self.engine.update_variable_bounds('PATH_'+path_name, lb=0)


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
        blah, variable_values = self.engine.solve_objective(all_variables, minimization=True)
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


    #
    def maximize_var(self, pathId):

        varId = 'PATH_'+pathId
        variable = self.input.variables['PATH_'+pathId]

        ###result_item = self.results.add([variable])

        # 
        ###pre_limits = self.check_path_constraints([pathId])
        ###result_item.set_pre(pre_limits)
        
        # Find the maximum feasible.
        new_value = self.engine.maximize_and_update_variable(varId)

        # Update where we save the calculated values. 
        variable.value = new_value

        #
        ###post_limits = self.check_path_constraints([pathId])
        ###result_item.set_post(post_limits)
        ###self.log(str(result_item))


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
    
    
    # TODO - move or update
    def is_var_maxed(self, variable_name):
        # We need to check if the given variable is as large as the constraints will allow. 
        #

        # Try to maximize the given variable; then check if the maximized value is different;
        # If the variable can be increased from its lb then return False.
        # If the variable can't be increased, it is maximized and return True.

        variable = self.input.variables[variable_name]
        try:
            objective_value, blah = self.engine.solve_objective([variable_name], maximization=True)
        except Exception:
            return True
        return isclose(variable.value, objective_value, abs_tol=1e-4)


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
        ###variables = [self.input.variables[varId] for varId in varIds]
        ###result_item = result_collection.add(variables)
        ###result_item.set_pre(self.check_path_constraints(pathIds))

        # Solve 
        var_values = self.engine.maximize_group_by_proportions(varIds, proportion_factors_by_varIds)

        # Update the variables. 
        # Set only the lb for now, since we might be able to increase this variable more in a future iteration.
        for var_name, var_value in var_values.items():
            self.engine.update_variable_bounds(var_name, lb=var_value)
            self.input.variables[var_name].value = var_value
        

        # POST info for result item...
        ###result_item.set_post(self.check_path_constraints(pathIds))
        ###self.log(str(result_item))



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
                        except Exception:
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


        paths = self.input.wrnet.paths
        nodes = self.input.wrnet.nodes

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
                        if id not in handoffs_by_id:
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
            if p not in handoffs_by_latest_withdrawal_priority:
                handoffs_by_latest_withdrawal_priority[p] = []
            handoffs_by_latest_withdrawal_priority[p].append( handoffs_by_id[id] )


        return handoffs_by_latest_withdrawal_priority





    # --------------------------------------------------------------------------
    # Where to write out log messages for debugging and stuff.
    # --------------------------------------------------------------------------
    def log(self, msg):
        if self.logging:
            self.logging.info(msg)

    def log_model(self, *args):
        if Globals.LOG_MODEL_DEFS:
            for msg in args:
                self.log(msg)
            self.log( self.engine.lp_string() )

    def log_var_values(self):
        
        self.log("\nVARIABLE VALUES REPORT\n")
        self.log("    {:20s}    {:11s}    {:11s}".format('VAR NAME', 'MIN VALUE', 'MAX VALUE'))
        self.log("    {:20s}    {:11s}    {:11s}".format('--------------------', '-----------', '-----------'))

        for var_name in self.input.variables:

            # get the max value.
            try:
                max_value, blah = self.engine.solve_objective([var_name], maximization=True)
                max_value = format(max_value, '11.2f')
            except Exception:
                max_value = 'ERROR'

            # get the min value.
            try:
                min_value, blah = self.engine.solve_objective([var_name], minimization=True)
                min_value = format(min_value, '11.2f')
            except Exception:
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




    # --------------------------------------------------------------------------
    # Some functions for showing our work, which I think is overkill.
    # --------------------------------------------------------------------------

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
            path = self.input.wrnet.paths[pathId]
            variable = self.input.variables[varId]
            try:
                value = path.cfs_limit - variable.value
            except Exception:
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
                except Exception:
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
                objective_value, blah = self.engine.solve_objective(unauthorized_variables_from_src, maximization=True)
            except Exception: 
                objective_value = inf
            limits['REM SUPPLY'] = objective_value

            

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
        self.variables : list[Variable] = variables
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
