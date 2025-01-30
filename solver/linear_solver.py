
from ortools.linear_solver import pywraplp
from math import inf, isnan, isclose


class LinearSolver:
    """Wrapper around the linear solver engine (currently using GLOP).
    This class could be used to solve linear optimization problems in general.
    """

    SOLVER_OPTIMAL = 0
    PRINT_SOLVER_MESSAGES = False
    
    def __init__(self):
        self.solver = pywraplp.Solver.CreateSolver('GLOP')

        # Keep a dictionary of variables and constraints for convenience.
        self.vars = {}
        self.cons = {}


    def add_variable(self, name:str, lb:float=0, ub:float=None) -> None:
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


    def add_constriant(self, name:str, lb:float=None, ub:float=None) -> None:
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
        """Return a solution to the max/minimization problem, 
        or raise a SolverError exception."""

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
        if LinearSolver.PRINT_SOLVER_MESSAGES:
            self.solver.EnableOutput()
        status = self.solver.Solve()

        # Retrieve the solution results.
        if status == LinearSolver.SOLVER_OPTIMAL:

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
            raise LinearSolverError('Could not find optimal solution! Status = ' + 
                       str(status) + ':' + status_text[status] 
                       + '/n/n' + self.lp_string())

        print(self.lp_string())

        # Done.
        return objective_value, variable_values


    def maximize_and_update_variable(self, variable_name):
        """Maximize the given variable and then update its lower bound so we 
        maintain the maximized value through subsequent operations."""

        # Find its maximum feasible value.
        objective_value, blah = self.solve_objective([variable_name], 
                                                     maximization=True)

        # Update its value.
        self.update_variable_bounds(variable_name, lb=objective_value)

        # Return.
        return objective_value


    def update_variable_bounds(self, name:str, lb:float=None, ub:float=None):

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

        # Bounds must be numeric.
        if lb is not None:
            if isnan(lb):
                raise Exception(f'New lower bound value for variable {name} is NaN!')
            variable.SetLb(lb)
        if ub is not None:
            if isnan(ub):
                raise Exception(f'new upper bound value for variable {name} is NaN!')
            variable.SetUb(ub)


    def update_constraint_bounds(self, name:str, lb:float=None, ub:float=None):
        
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
        """Return a list of the constraints."""
        return self.cons.keys()


    def lp_string(self) -> str:
        """Return a LP format string representing the linear program. """
        value = self.solver.ExportModelAsLpFormat(False)
        return value
    
    
    def maximize_group_by_proportions(self, variable_names, proportion_factors):

        # List of constraints used only for merging purposes.
        merge_var, merge_constraints = self._merge(variable_names, 
                                                   proportion_factors)

        # Solve.
        objective_value, blah = self.solve_objective([merge_var.name()], 
                                                     maximization=True)

        # Now unmerge.
        var_values = self._unmerge(variable_names, proportion_factors, 
                                   merge_var, merge_constraints, objective_value)

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
            merge_constraint.SetBounds(thisvar.lb(), inf) 
            merge_constraint.SetCoefficient(thisvar, 1)
            merge_constraint.SetCoefficient(merge_var, -proportion_factor)
            merge_constraints.append(merge_constraint)
        
        return merge_var, merge_constraints


    def _unmerge(self, variable_names, proportion_factors, merge_var, 
                 merge_constraints, solved_value):
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



class LinearSolverError(Exception):
    """An exception indicating the solver cannot solve a requested problem, 
    perhaps because the problem is infeasible, unbounded, or not properly 
    defined."""
    pass
