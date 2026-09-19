from .compile.compile import compile, CompileOptions
from .models import SolverInput, SolverOutput


def solve(
    input: SolverInput,
    options:CompileOptions = CompileOptions(),
    *,
    check_expected_values: bool = False,
) -> SolverOutput:
    """Compile and execute the solver."""
    plan = compile(input, options)
    return plan.solve(check_expected_values=check_expected_values)

