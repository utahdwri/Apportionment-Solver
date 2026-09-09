"""SCIP backend for exact delivery-weighted, nonlinear cohort constraints.

The HiGHS wrapper supplies the editable linear model and common accounting API.
Each objective is compiled into SCIP, including the quadratic equalities. Only
proven infeasibility can trigger accounting feasibility relaxation.
"""

from math import inf, isfinite

from pyscipopt import Model, quicksum

from .lp_solver import LPSolverError
from .lp_solver_HIGHSPY import LPSolver as LinearModel


class LPSolver(LinearModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.quadratic_rows = {}
        self.binary_variables = set()
        self.last_optimality_gap = None

    def add_binary_variable(self, name):
        super().add_binary_variable(name)
        self.binary_variables.add(name)

    def add_quadratic_constraint(self, name, terms, linear=None, rhs=0):
        """Store sum(coef*x*y) + sum(linear) == rhs, never relaxed by fallback."""
        if name in self.quadratic_rows:
            raise ValueError(f"Duplicate quadratic constraint {name}")
        for x, y, coefficient in terms:
            if x not in self.vars or y not in self.vars or not isfinite(coefficient):
                raise ValueError(f"Invalid quadratic term in {name}")
        self.quadratic_rows[name] = (list(terms), dict(linear or {}), rhs)

    def solve_objective(self, variable_names, maximization=True, weights=None):
        self._set_objective(variable_names, maximization, weights or {})
        model = Model("water_accounting")
        model.hideOutput(not self.PRINT_SOLVER_MESSAGES)
        model.setRealParam("numerics/feastol", min(self.tolerance or 1e-10, 1e-10))
        # Keep zero/summation tests tighter than feasibility checks, including
        # after a previous priority optimum becomes a fixed variable bound.
        model.setRealParam("numerics/epsilon", 1e-12)
        model.setRealParam("numerics/sumepsilon", 1e-11)
        model.setRealParam("limits/gap", 0.0)
        model.setRealParam("limits/absgap", 0.0)
        # One serial solver per accounting problem, as in the HiGHS backend.
        model.setIntParam("parallel/maxnthreads", 1)
        # Rebuilt increments retire old auxiliary variables by fixing them at
        # zero. Substitute that known value instead of copying an ever-growing
        # set of inactive columns into each new SCIP model.
        fixed_zero = {
            name for name, var in self.vars.items() if var.lb() == var.ub() == 0
        }
        variables = {
            name: model.addVar(
                name=name,
                vtype="B" if name in self.binary_variables else "C",
                lb=None if var.lb() == -inf else var.lb(),
                ub=None if var.ub() == inf else var.ub(),
            )
            for name, var in self.vars.items()
            if name not in fixed_zero
        }
        expressions = variables | dict.fromkeys(fixed_zero, 0.0)
        for name, row in self.cons.items():
            if row.lb() == -inf and row.ub() == inf:
                continue
            expression = quicksum(
                c * expressions[v] for v, c in row.coefficients.items()
            )
            if row.lb() == row.ub():
                model.addCons(expression == row.lb(), name=name)
            else:
                if row.lb() != -inf:
                    model.addCons(expression >= row.lb(), name=name + "_lb")
                if row.ub() != inf:
                    model.addCons(expression <= row.ub(), name=name + "_ub")
        for name, (terms, linear, rhs) in self.quadratic_rows.items():
            expression = quicksum(
                c * expressions[x] * expressions[y] for x, y, c in terms
            )
            expression += quicksum(c * expressions[v] for v, c in linear.items())
            model.addCons(expression == rhs, name=name)
        model.setObjective(
            quicksum(c * expressions[v] for v, c in self._objective_costs.items()),
            "maximize" if maximization else "minimize",
        )
        model.optimize()
        self.solve_count += 1
        status = str(model.getStatus())
        if status == "infeasible":
            raise LPSolverError("SCIP proved the accounting model infeasible")
        if status != "optimal":
            raise RuntimeError(f"SCIP did not prove an optimum: {status}")
        solution = model.getBestSol()
        values = {
            name: max(
                self.vars[name].lb(),
                min(self.vars[name].ub(), float(model.getSolVal(solution, v))),
            )
            for name, v in variables.items()
        }
        values.update(dict.fromkeys(fixed_zero, 0.0))
        self._last_solution_values = values
        self._last_variable_reduced_costs = {}
        self._last_constraint_dual_values = {}
        self._last_constraint_activities = {
            name: sum(c * values[v] for v, c in row.coefficients.items())
            for name, row in self.cons.items()
        }
        self._saved_rows.clear()
        self._last_variable_constraints.clear()
        self.last_optimality_gap = float(model.getGap())
        self.last_run_time = float(model.getSolvingTime())
        return float(model.getObjVal()), {name: values[name] for name in variable_names}

    def maximize_group_by_proportions(self, variable_names, proportion_factors):
        values = super().maximize_group_by_proportions(
            variable_names, proportion_factors
        )
        # A shared objective can exceed a member's cap by solver tolerance.
        # Never promote that noise into an infeasible exact committed anchor.
        return {
            name: max(self.vars[name].lb(), min(self.vars[name].ub(), value))
            for name, value in values.items()
        }

    def lp_string(self):
        text = super().lp_string()
        rows = [
            f"{name}: "
            + " + ".join(f"{c:g}*{x}*{y}" for x, y, c in terms)
            + f" + {linear} = {rhs:g}"
            for name, (terms, linear, rhs) in self.quadratic_rows.items()
        ]
        return text + "\nQuadratic equalities:\n" + "\n".join(rows)
