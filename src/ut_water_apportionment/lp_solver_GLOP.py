from .lp_solver import LPSolverError


from ortools.linear_solver import pywraplp
from math import inf, isnan, isclose



class LPSolver:
    """Wrapper around the linear solver engine (currently using GLOP).
    This class could be used to solve linear optimization problems in general.
    """

    SOLVER_OPTIMAL = 0
    PRINT_SOLVER_MESSAGES = False

    def __init__(self, tolerance:float|None=None):
        self.solver = pywraplp.Solver.CreateSolver('GLOP')

        if tolerance:
            self.solver.SetSolverSpecificParametersAsString(f"""
            primal_feasibility_tolerance: {tolerance}
            dual_feasibility_tolerance: {tolerance}
            """)



        # The following will print extra info that might help debug an issue with GLOP.
        #self.solver.EnableOutput()


        # Keep a dictionary of variables and constraints for convenience.
        self.vars: dict[str,pywraplp.Variable] = {}
        self.cons: dict[str,pywraplp.Constraint] = {}

        #
        self._last_solution_values: dict[str, float] = {}
        self._last_variable_reduced_costs: dict[str, float | None] = {}
        self._last_constraint_activities: dict[str, float] = {}
        self._last_constraint_bounds: dict[str, tuple[float, float]] = {}
        self._last_constraint_dual_values: dict[str, float | None] = {}
        self._last_constraint_coefficients: dict[str, dict[str, float]] = {}

        # Track which variables go with which constraint.
        self._constraint_vars: dict[str, list[str]] = {}


    def add_variable(self, name:str, lb:float|None=0, ub:float|None=None) -> None:
        """Add a variable to the system of equations."""

        # Allow None to be used to indicate there is no lb or ub.
        if lb is None:
            lb = -inf
        if ub is None:
            ub = inf

        # The bounds must be a number!
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


    def get_variable_bounds(self, name:str) -> tuple[float, float]:
        """Return the lower and upper bounds as a tuple."""
        variable = self.vars[name]
        return (variable.lb(), variable.ub())


    def add_constraint(self, name:str, lb:float|None=None, ub:float|None=None) -> None:
        "Add a constraint to the system of equations."

        # Allow None to be used to indicate there is no lb or ub.
        if lb is None:
            lb = -inf
        if ub is None:
            ub = inf

        # The bounds must be a number!
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


    def set_coefficient(self, constraint_name:str, variable_name:str, coef:float|None) -> None:

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

        # Cache the relationship
        if constraint_name not in self._constraint_vars:
            self._constraint_vars[constraint_name] = []
        if variable_name not in self._constraint_vars[constraint_name]:
            self._constraint_vars[constraint_name].append(variable_name)

    def solve_objective(self,
                        variable_names:list[str],
                        maximization:bool=True,
                        weights: dict[str, float] | None = None
                        ) -> tuple[float, dict[str,float]]:
        """Return a solution to the max/minimization problem,
        or raise a SolverError exception."""

        weights = weights or {}

        # Prepare the objective.
        objective = self.solver.Objective()
        objective.Clear()

        if maximization:
            objective.SetMaximization()
        else:
            objective.SetMinimization()

        for variable_name in variable_names:
            variable = self.vars[variable_name]
            objective.SetCoefficient(variable, weights.get(variable_name, 1.0))

        # Solve the system.
        if LPSolver.PRINT_SOLVER_MESSAGES:
            self.solver.EnableOutput()
        status = self.solver.Solve()


        # Retrieve the solution results.
        if status == LPSolver.SOLVER_OPTIMAL:

            objective_value = objective.Value()

            variable_values = {}
            for variable_name in variable_names:
                variable = self.vars[variable_name]
                variable_values[variable_name] = variable.solution_value()

            self._last_solution_values = {
                name: var.solution_value()
                for name, var in self.vars.items()
            }

            self._last_variable_reduced_costs = {}
            for name, var in self.vars.items():
                try:
                    self._last_variable_reduced_costs[name] = var.reduced_cost()
                except Exception:
                    self._last_variable_reduced_costs[name] = None

            self._last_constraint_activities = {}
            self._last_constraint_bounds = {}
            self._last_constraint_dual_values = {}
            self._last_constraint_coefficients = {}
            for con_name, constraint in self.cons.items():
                coefficients = {
                    var_name: constraint.GetCoefficient(self.vars[var_name])
                    for var_name in self._constraint_vars.get(con_name, [])
                }
                activity = sum(
                    coef * self._last_solution_values.get(var_name, 0.0)
                    for var_name, coef in coefficients.items()
                )

                self._last_constraint_activities[con_name] = activity
                self._last_constraint_bounds[con_name] = (constraint.lb(), constraint.ub())
                self._last_constraint_coefficients[con_name] = coefficients

                try:
                    self._last_constraint_dual_values[con_name] = constraint.dual_value()
                except Exception:
                    self._last_constraint_dual_values[con_name] = None

        # Or if there is no optimal solution, raise the custom exception.
        else:
            status_text = {
                0 : "OPTIMAL",
                1 : "FEASIBLE",
                2 : "INFEASIBLE",
                3 : "UNBOUNDED",
                4 : "ABNORMAL",
            }
            raise LPSolverError('Could not find optimal solution! Status = ' +
                       str(status) + ':' + status_text[status]
                       + '\n\n' + self.lp_string())

        # Done.
        return objective_value, variable_values


    def maximize_and_update_variable(self, variable_name:str) -> float:
        """Maximize the given variable and then update its lower bound so we
        maintain the maximized value through subsequent operations."""

        # Find its maximum feasible value.
        _, variable_values = self.solve_objective([variable_name],
                                                   maximization=True)
        solved_value = variable_values[variable_name]

        # Update its value.
        self.update_variable_bounds(variable_name, lb=solved_value)

        # Return the variable value, not the possibly penalized objective value.
        return solved_value

    def minimize_and_update_variable(self, variable_name:str) -> float:
        """Minimize the given variable and then update its upper bound."""

        # Find its minimum feasible value.
        _, variable_values = self.solve_objective([variable_name],
                                                   maximization=False)
        solved_value = variable_values[variable_name]

        # Update its value.
        self.update_variable_bounds(variable_name, ub=solved_value)

        # Return the variable value, not the possibly penalized objective value.
        return solved_value

    def update_variable_bounds(self, name:str, lb:float|None=None, ub:float|None=None) -> None:

        variable = self.vars[name]

        # Ensure that we don't end up with lb > ub, even within rounding error,
        # because that will mess things up!
        _lb = lb if lb is not None else variable.lb()
        _ub = ub if ub is not None else variable.ub()

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
            # Bounds must be numeric.
            if isnan(lb):
                raise Exception(f'New lower bound value for variable {name} is NaN!')

            variable.SetLb(lb)

            # 2025-03-06 - The problem was becoming 'status: IMPRECISE' after lower
            # bounds were increased close to the upper bound, but with a very small
            # difference. This fixes that issue.
            # 2025-03-25 - For this issue, it's important to decrease the ub and not increase the lb!
            if isclose(lb, _ub, abs_tol=0.0001):
                if _ub > lb:
                    variable.SetUb(lb)

        if ub is not None:
            if isnan(ub):
                raise Exception(f'new upper bound value for variable {name} is NaN!')
            variable.SetUb(ub)


    def update_constraint_ub(self, name:str, ub:float|None=None) -> None:

        constraint = self.cons[name]
        if ub is None:
            ub = inf
        if isnan(ub):
            raise Exception(f'Upper bound value for constraint {name} is NaN!')
        constraint.SetUb(ub)


    def update_constraint_lb(self, name:str, lb:float|None=None) -> None:

        constraint = self.cons[name]
        if lb is None:
            lb = -inf
        if isnan(lb):
            raise Exception(f'Lower bound value for constraint {name} is NaN!')
        constraint.SetLb(lb)

    def get_constraint_names(self) -> list[str]:
        """Return a list of the constraints."""
        return list(self.cons.keys())


    def lp_string(self) -> str:
        """Return a LP format string representing the linear program. """
        value:str = self.solver.ExportModelAsLpFormat(False)
        #value = self.solver.ExportModelAsMpsFormat(True, True)
        return value


    def maximize_group_by_proportions(self, variable_names:list[str], proportion_factors:dict[str,float]) -> dict[str,float]:

        # List of constraints used only for merging purposes.
        merge_var, merge_constraints = self._merge(variable_names,
                                                   proportion_factors)

        # Solve.
        _, solved_values = self.solve_objective([merge_var.name()],
                                                maximization=True)
        solved_value = solved_values[merge_var.name()]


        # Now unmerge.
        var_values = self._unmerge(variable_names, proportion_factors,
                                   merge_var, merge_constraints, solved_value)

        return var_values


    def _merge(self, variable_names:list[str], proportion_factors:dict[str,float]):

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

            # Create or Reuse the constraint
            con_name = 'combined_' + variable_name
            if con_name not in self.cons:
                self.add_constraint(con_name, lb=-inf, ub=inf)

            # Set an inequality constraint that relates the merged
            # variable to this path variable:
            #
            #   var - f * merged >= 0
            #
            # It's not an equality ( = 0) because the variable may
            # have a constraint requiring it to be greater than
            # f * merged and we don't want to make the problem
            # infeasible.
            merge_constraint = self.cons[con_name]
            merge_constraint.SetBounds(thisvar.lb(), inf)
            merge_constraint.SetCoefficient(thisvar, 1)
            merge_constraint.SetCoefficient(merge_var, -proportion_factor)
            merge_constraints.append(merge_constraint)


        return merge_var, merge_constraints


    def _unmerge(self, variable_names:list[str], proportion_factors:dict[str,float], merge_var,
                 merge_constraints, solved_value) -> dict[str,float]:
        var_values = {}

        # Clear the merged constraints.
        #   Altering the problem like this will make it so we cannot
        #   retrieve values from the previous solution, so we have to
        #   have already gotten the results we need.
        for c in merge_constraints:
            c.Clear()
            c.SetBounds(-inf, inf)

        # Remove the merge_var.
        merge_var.SetBounds(lb=-inf, ub=inf) # TODO - This doesn't really remove the variable. Is there another way?


        # Update the variables.
        # Lock the value in.
        # Set only the lb, since we might be able to increase this variable in a future iteration.
        for variable_name in variable_names:
            # If multiple variables are being used together, use the combined variable with the factors.
            variable_delta = proportion_factors[variable_name] * solved_value

            initial_val = self.vars[variable_name].lb()

            var_values[variable_name] = initial_val + variable_delta

        return var_values




    def get_last_variable_reduced_cost(self, variable_name: str) -> float | None:
        return self._last_variable_reduced_costs.get(variable_name)


    def get_last_solve_constraint_evidence(self,
                                           variable_name: str,
                                           tolerance: float = 1e-6
                                           ) -> list[dict]:
        output = []

        for constraint_name, coefficients in self._last_constraint_coefficients.items():
            coefficient = coefficients.get(variable_name, 0.0)
            if coefficient == 0:
                continue

            activity = self._last_constraint_activities[constraint_name]
            lower_bound, upper_bound = self._last_constraint_bounds[constraint_name]

            lower_slack = None if lower_bound == -inf else activity - lower_bound
            upper_slack = None if upper_bound == inf else upper_bound - activity

            lower_is_tight = lower_slack is not None and isclose(lower_slack, 0, abs_tol=tolerance)
            upper_is_tight = upper_slack is not None and isclose(upper_slack, 0, abs_tol=tolerance)

            blocks_direct_increase = (
                (coefficient > 0 and upper_is_tight) or
                (coefficient < 0 and lower_is_tight)
            )

            output.append({
                'constraint_name': constraint_name,
                'coefficient': coefficient,
                'activity': activity,
                'lower_bound': None if lower_bound == -inf else lower_bound,
                'upper_bound': None if upper_bound == inf else upper_bound,
                'lower_slack': lower_slack,
                'upper_slack': upper_slack,
                'is_tight': lower_is_tight or upper_is_tight,
                'blocks_direct_increase': blocks_direct_increase,
                'dual_value': self._last_constraint_dual_values.get(constraint_name)
            })

        return output


    def is_constraint_tight(self, constraint_name: str, variable_name: str) -> bool:
        """ 4/18/2026 - Gemini:
        Rigorously checks if a specific constraint is 'tight' for a specific variable.
        It checks the upper bound if the variable adds to the constraint,
        and the lower bound if the variable subtracts from it.
        """
        if constraint_name not in self.cons or variable_name not in self.vars:
            return False

        constraint = self.cons[constraint_name]
        variable = self.vars[variable_name]

        # Get the variable's direction/factor in this specific constraint
        coef = constraint.GetCoefficient(variable)
        if coef == 0.0:
            return False # Variable isn't even in this constraint


        # Calculate the current total activity of the constraint
        activity = 0.0
        for v_name in self._constraint_vars.get(constraint_name, []):
            var_obj = self.vars[v_name]
            c = constraint.GetCoefficient(var_obj)
            activity += c * self._last_solution_values.get(v_name, 0.0)


        # If coef is positive, increasing the var pushes activity UP to the Upper Bound
        if coef > 0:
            ub = constraint.ub()
            return ub != float('inf') and isclose(activity, ub, abs_tol=1e-4)

        # If coef is negative, increasing the var pushes activity DOWN to the Lower Bound
        if coef < 0:
            lb = constraint.lb()
            return lb != float('-inf') and isclose(activity, lb, abs_tol=1e-4)

        return False

    def has_variable(self, name: str) -> bool:
        """Return whether a variable has been added to the model."""
        return name in self.vars

