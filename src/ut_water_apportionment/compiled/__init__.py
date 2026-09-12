"""Public entry point for reusable, inspectable compiled accounting plans."""

from copy import deepcopy

from .projection import CompilationOptions
from .runtime import CompilationSession


class CompiledSolver:
    """An isolated SolverInput plus a bounded cache of symbolic LP programs.

    Preparation derives the first day's individual objective programs without
    optimizing. Additional active-set/dated coefficient patterns are compiled
    on demand, within the same budgets. Repeated solve() calls reset accounting
    state but reuse the equations. This is a hybrid executable plan, not a claim
    that every possible daily branch has been symbolically enumerated.
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
        from ..apportioner import Apportioner
        from ..graph_manager import GraphManager
        from ..lp_solver import resolve_solver_backend
        from ..models import PathTrxn
        from ..natural_flow_calculator import NaturalFlowCalculator
        from ..timeseries_manager import DailyDataManager
        from ..trxn_schedule import TrxnSchedule
        from .projection import CannotCompile

        backend = resolve_solver_backend(self.solver_backend)
        gm = GraphManager(deepcopy(self._input.accounting_graph))
        dm = DailyDataManager(
            gm, self._input.measurements, self._input.external_natural_flows
        )
        tm = TrxnSchedule(gm, self._input.txns, self.max_daily_apportionment)
        day = self._input.beg_date
        dm.set_day(day)
        tm.begin_day(day)
        apportioner = Apportioner(
            gm,
            tm,
            dm,
            NaturalFlowCalculator(gm),
            lp_solver_factory=backend.factory,
            generate_audit=False,
        )
        apportioner.update_daily_bounds()
        apportioner.apply_nf_mass_balance_constraints(day)
        engine = apportioner.engine
        if len(engine.vars) > self._session.options.max_variables:
            self._session.warmup_reasons["initial model exceeds variable budget"] += 1
            return
        for t in tm.all_trxns:
            if getattr(t, "is_slack", False):
                continue
            target = tm.get_anchor_var(t) if isinstance(t, PathTrxn) else t.id
            if target:
                try:
                    self._session.program(engine, [target])
                except CannotCompile as error:
                    self._session.warmup_reasons[str(error)] += 1

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
