"""Public frozen-plan API for the second-generation compiler."""

from __future__ import annotations

from copy import deepcopy

from ..graph_manager import GraphManager
from ..lag_utils import unlag_apportionments
from ..models import SolverOutput
from ..natural_flow_calculator import NaturalFlowCalculator
from ..solver import _loop_through_date_range, _solve_day, assert_apportionments_equal_expected
from ..timeseries_manager import DailyDataManager
from ..trxn_schedule import TrxnSchedule
from .compiler import V2CompilationOptions
from .runtime import V2CompilationSession, v2_factory


class V2CompiledSolver:
    """Prepared v2 plan with a frozen parameterized LP/IR.

    ``compile_solver_input_v2`` performs one preparation traversal of the
    supplied schedule/date range. Objective structures encountered there are
    transformed once and cached with parameterized bounds/RHS. ``solve`` then
    replays only those frozen programs and supplies current bound values.
    """

    def __init__(
        self,
        problem,
        *,
        options: V2CompilationOptions | None = None,
        max_daily_apportionment: float | None = None,
    ):
        self._input = deepcopy(problem)
        self.max_daily_apportionment = max_daily_apportionment
        self._session = V2CompilationSession(options or V2CompilationOptions())
        self._prepare()

    def _run(self, problem, *, check_expected_values: bool = False) -> SolverOutput:
        graph_manager = GraphManager(deepcopy(problem.accounting_graph))
        natural_flow_calculator = NaturalFlowCalculator(graph_manager)
        data_manager = DailyDataManager(
            graph_manager,
            problem.measurements,
            problem.external_natural_flows,
        )
        trxn_manager = TrxnSchedule(
            graph_manager,
            problem.txns,
            self.max_daily_apportionment,
        )

        apportionment_results = []
        for date in _loop_through_date_range(problem.beg_date, problem.end_date):
            data_manager.set_day(date)
            trxn_manager.begin_day(date)
            apportioner = _solve_day(
                graph_manager,
                trxn_manager,
                data_manager,
                natural_flow_calculator,
                v2_factory(self._session),
                False,
                date,
            )
            trxn_manager.commit_day(apportioner.cur_trxn_value)
            apportionment_results.extend(apportioner.get_variables(date))

        output = SolverOutput(
            apportionments=unlag_apportionments(apportionment_results, data_manager.flow_lags),
            solve_steps=[],
            solver_backend="compiled-v2-frozen",
            solve_method="compiled_v2",
            compilation_report=self.report(),
        )
        if check_expected_values:
            assert_apportionments_equal_expected(
                output,
                problem,
                graph_manager,
                data_manager,
                trxn_manager,
            )
        return output

    def _prepare(self) -> None:
        self._session.begin_preparation()
        try:
            # Numeric values drive the accounting control flow during this
            # initial traversal, but they are not baked into the programs:
            # mutable LP bounds/RHS become parameter slots in the frozen IR.
            self._run(deepcopy(self._input))
        finally:
            self._session.finish_preparation()

    def solve(self, *, measurements=None, check_expected_values: bool = False) -> SolverOutput:
        problem = deepcopy(self._input)
        if measurements is not None:
            problem.measurements = deepcopy(measurements)
            problem.__post_init__()
        self._session.reset_execution_stats()
        return self._run(problem, check_expected_values=check_expected_values)

    def formulas(self) -> str:
        return self._session.formulas()

    def report(self) -> dict:
        return self._session.report()


def compile_solver_input_v2(
    problem,
    *,
    options: V2CompilationOptions | None = None,
    max_daily_apportionment: float | None = None,
) -> V2CompiledSolver:
    return V2CompiledSolver(
        problem,
        options=options,
        max_daily_apportionment=max_daily_apportionment,
    )
