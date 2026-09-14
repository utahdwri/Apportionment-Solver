"""Minimal use of the experimental compiled-equations-v2 branch."""

from ut_water_apportionment import compile_solver_input_v2


def run(problem):
    plan = compile_solver_input_v2(problem)
    result = plan.solve()
    print(plan.formulas())
    print(plan.report())
    return result
