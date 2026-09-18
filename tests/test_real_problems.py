import unittest
from ut_water_apportionment import (
    compile,
    SolverInput,
    SolverOutput,
)
from ut_water_apportionment.loss_models import LossDefinition


def solve(input: SolverInput, *, check_expected_values: bool = False) -> SolverOutput:
    from time import perf_counter

    #input.beg_date = input.beg_date[:-5] + '01-01'
    #input.end_date = input.end_date[:-5] + '12-31'

    t0 = perf_counter()

    compiled_system = compile(input)

    print(f'Compile Time: {perf_counter() - t0}')
    t0 = perf_counter()

    results = compiled_system.solve(
        check_expected_values=check_expected_values
    )

    print(f'Execute Time: {perf_counter() - t0}')

    return results



class RealProblems(unittest.TestCase):
    """

    """

    def test_uinta(self):
        from pathlib import Path

        filepath = Path("tests") / "real_problem_files" / "uinta.json"
        input = SolverInput.from_json(filepath)
        results = solve(input)

    def test_duchesne(self):
        from pathlib import Path

        filepath = Path("tests") / "real_problem_files" / "duchesne.json"
        input = SolverInput.from_json(filepath)
        results = solve(input)