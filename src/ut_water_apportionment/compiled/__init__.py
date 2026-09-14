"""Public entry point for reusable, inspectable compiled accounting plans."""

from copy import deepcopy

from .projection import CompilationOptions
from .runtime import CompilationSession


class CompiledSolver:
    """A fully prepared cache of symbolic accounting objective programs.

    Construction traces the supplied SolverInput through its complete date
    range once and compiles every objective pattern encountered. Senior solved
    values are absorbed into residual constraint capacities, so junior formulas
    depend on ``constraint[...].remaining`` state rather than earlier
    transaction variables. The cache is frozen before ``solve()`` returns to
    the caller: runtime may evaluate cached formulas or safely fall back to LP,
    but it never performs new symbolic compilation.
    """

    def __init__(
        self,
        problem,
        *,
        options=None,
        solver_backend="auto",
        max_daily_apportionment=None,
    ):
        self._input = deepcopy(problem)
        self.solver_backend = solver_backend
        self.max_daily_apportionment = max_daily_apportionment
        self._session = CompilationSession(options)
        self._prepare()

    def _prepare(self):
        """Trace the supplied input once so the complete formula cache is built."""
        from ..solver import _solve

        # This is a compilation/warm-up pass. It deliberately executes the
        # production accounting schedule so temporary counterflow caps,
        # proportional active sets, spill locks, and finalization objectives are
        # encountered in the same order as a real solve. No result from this
        # pass is exposed; only the compiled programs and fallback coverage are
        # retained.
        _solve(
            deepcopy(self._input),
            solver_backend=self.solver_backend,
            max_daily_apportionment=self.max_daily_apportionment,
            generate_audit=False,
            check_expected_values=False,
            compiled_session=self._session,
        )
        self._session.finish_preparation()

    def solve(
        self, *, measurements=None, generate_audit=False, check_expected_values=False
    ):
        """Execute with fresh account state; optionally replace measurements.

        Limits/schedules/graph changes require a new plan. Changed measurement
        values may use different cached objective programs or trigger LP fallback.
        """
        from ..solver import _solve

        problem = deepcopy(self._input)
        if measurements is not None:
            problem.measurements = deepcopy(measurements)
            problem.__post_init__()
        self._session.stats.clear()
        self._session.last_events.clear()
        return _solve(
            problem,
            solver_backend=self.solver_backend,
            max_daily_apportionment=self.max_daily_apportionment,
            generate_audit=generate_audit,
            check_expected_values=check_expected_values,
            compiled_session=self._session,
        )

    def formulas(self):
        """Actual derived MIN/MAX expressions, domain conditions and fallbacks."""
        return self._session.formulas()

    def code(self):
        """Executable Python for cached objective programs, using named bounds."""
        return self._session.code()

    def execution_outline(self):
        """Readable schedule, iteration, and LP restart outline."""
        return self._session.execution_outline()

    def report(self):
        """Compilation coverage, costs, and dated LP fallback explanations."""
        return deepcopy(self._session.report())


def compile_solver_input(
    problem, *, options=None, solver_backend="auto", max_daily_apportionment=None
):
    """Derive a reusable alternate solve plan from a SolverInput instance."""
    return CompiledSolver(
        problem,
        options=options,
        solver_backend=solver_backend,
        max_daily_apportionment=max_daily_apportionment,
    )


__all__ = ["CompilationOptions", "CompiledSolver", "compile_solver_input"]
