from .compile.compile import compile
from .models import SolverInput, SolverOutput


def solve(
    input: SolverInput,
    *,
    check_expected_values: bool = False,
    max_daily_apportionment: float | None = None,
    compilation_options=None,
) -> SolverOutput:
    """Compile and execute the solver."""

    plan = compile(                                                                     # TODO - add compilation_options and max_daily_apportionment!
        input,
        #options=compilation_options,
        #max_daily_apportionment=max_daily_apportionment,
    )
    return plan.solve(check_expected_values=check_expected_values)

