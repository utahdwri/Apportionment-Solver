import unittest
from ut_water_apportionment import (
    compile_solver_input_v2,
    SolverInput,
    SolverOutput,
)
from ut_water_apportionment.loss_models import LossDefinition


def solve(input: SolverInput, *, check_expected_values: bool = False) -> SolverOutput:
    """Run every retained production test through the frozen v2 compiler."""
    return compile_solver_input_v2(input).solve(
        check_expected_values=check_expected_values
    )



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