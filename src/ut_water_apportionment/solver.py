from copy import deepcopy
from typing import Generator
import logging

from .models import (
    SolverInput, SolverOutput, Trxn, TrxnPathItem, ZoneTypes
)
from .graph_manager import GraphManager
from .natural_flow_calculator import NaturalFlowCalculator
from .timeseries_manager import DailyDataManager
from .trxn_schedule import TrxnSchedule
from .period_apportioner import PeriodApportioner
from .lp_solver import SolverBackend, resolve_solver_backend


logger = logging.getLogger(__name__)


# --- Public API ---

def solve(
    input: SolverInput,
    *,
    check_expected_values: bool = False,
    solver_backend: SolverBackend | str = SolverBackend.AUTO,
    max_daily_apportionment: float | None = None,
) -> SolverOutput:
    """Build and solve one sparse LP spanning the complete solve period.

    Transaction/path-component variables are stored in real-world measurement
    time.  Integer and fractional lags are represented directly in the
    period-wide continuity and natural-flow constraints, so final
    apportionments do not require post-solve unlagging.

    ``solver_backend`` may be ``"auto"``, ``"highspy"``, ``"glop"``, or
    ``"scipy"``. Automatic selection prefers native HiGHS, then GLOP, then
    SciPy's HiGHS interface. An explicitly requested unavailable backend raises
    an error rather than silently using a different implementation.
    """

    resolved_backend = resolve_solver_backend(solver_backend)
    logger.info(
        "Using LP backend: %s",
        resolved_backend.name.value,
    )

    # 1. Initialize network topology.
    graph_manager = GraphManager(
        deepcopy(input.accounting_graph)
    )

    # 2. Initialize physical/natural-flow services.
    natural_flow_calculator = NaturalFlowCalculator(
        graph_manager
    )
    data_manager = DailyDataManager(
        graph_manager,
        input.measurements,
        natural_flow_calculator,
        input.external_natural_flows,
    )

    # 3. Build transaction schedule/slack variables.
    trxn_manager = TrxnSchedule(
        graph_manager,
        input.txns,
        max_daily_apportionment,
    )

    # 4. Build ONE LP containing the full period.
    apportioner = PeriodApportioner(
        graph_manager,
        trxn_manager,
        data_manager,
        input.beg_date,
        input.end_date,
        lp_solver_factory=resolved_backend.factory,
    )

    # 5. Preserve the existing two-stage natural-flow/spill strategy, but each
    #    stage now sees every day and every temporal coupling simultaneously.
    apportioner.apply_nf_mass_balance_constraints()
    apportioner.calculate_period_apportionments()

    apportioner.remove_nf_mass_balance_constraints()
    apportioner.lock_spill_variables()
    apportioner.calculate_period_apportionments()

    # 6. Resolve remaining slack/non-anchor ambiguity.
    apportioner.solve_for_nonpath_vars()

    results = SolverOutput(
        apportionments=apportioner.get_variables(),
        apportionments_audit=apportioner.apportionments_audit,
        solver_backend=resolved_backend.name.value,
    )

    if check_expected_values:
        assert_apportionments_equal_expected(
            results,
            input,
            graph_manager,
            data_manager,
            trxn_manager,
        )

    return results


def assert_apportionments_equal_expected(
    results: SolverOutput,
    input: SolverInput,
    gm: GraphManager,
    dm: DailyDataManager,
    tm: TrxnSchedule,
) -> None:
    """Check defined expected values to 4 decimal places.

    For zero-lag tests, expected values retain their existing date semantics.
    Lagged tests should normally assert explicit real-world result dates because
    period-wide variables are now returned directly in measurement time.
    """

    message: str = ""
    cnt = 0

    for t in tm.traverse_vars(input.txns):
        if type(t) == Trxn:
            for p in t.path:
                if p.expected_values is not None:
                    idx = 0
                    for date_string in _loop_through_date_range(
                        input.beg_date,
                        input.end_date,
                    ):
                        expected_value = p.expected_values[idx]
                        computed_values = results.get_result_value(
                            date=date_string,
                            trxn_id=t.id,
                            flow_id=p.flow_id,
                        )

                        if not computed_values:
                            raise ValueError(
                                "(date, trxn_id, flow_id) of "
                                f"{(date_string, t.id, p.flow_id)} "
                                "not found."
                            )
                        if len(computed_values) > 1:
                            raise ValueError(
                                "Multiple results found"
                            )

                        computed_value = (
                            computed_values[0].value
                        )

                        if expected_value is not None:
                            cnt += 1
                            if (
                                abs(
                                    expected_value
                                    - computed_value
                                )
                                >= 1e-4
                            ):
                                msg = (
                                    message
                                    + f'Var "{t.id}": computed '
                                    f"({computed_value}) != expected "
                                    f"({expected_value}) on "
                                    f"{date_string}\n"
                                    + system_report_str(
                                        results,
                                        idx,
                                        date_string,
                                        gm,
                                        dm,
                                        tm,
                                    )
                                )
                                raise AssertionError(msg)

                        idx += 1

    if cnt == 0:
        raise Exception(
            "No trxn path-items were given an expected_value!"
        )


# --- Helper Methods ----------------------------------------------------


def _loop_through_date_range(
    beg_date: str,
    end_date: str,
) -> Generator[str, None, None]:
    """Iterate through each date from beg_date to end_date inclusive."""

    from datetime import datetime, timedelta

    a_date = datetime.strptime(
        beg_date,
        "%Y-%m-%d",
    ).date()
    b_date = datetime.strptime(
        end_date,
        "%Y-%m-%d",
    ).date()

    current_date = a_date
    while current_date <= b_date:
        yield current_date.isoformat()
        current_date += timedelta(days=1)


def system_report_str(
    results: SolverOutput,
    day_idx: int,
    date: str,
    gm: GraphManager,
    dm: DailyDataManager,
    tm: TrxnSchedule,
) -> str:
    """Display stream-zone flow/apportionment details for debugging."""

    def warn_if_value_is_incorrect(
        path_item: TrxnPathItem,
        value: float | None,
    ):
        if path_item.expected_values is not None:
            expected_value = (
                path_item.expected_values[day_idx]
            )
            if (
                expected_value is not None
                and value is not None
                and abs(expected_value - value) > 1e-4
            ):
                return (
                    "*** NOT EQUAL TO EXPECTED VALUE OF "
                    f"{expected_value:9.4f}"
                )
        return ""

    dm.set_day(date)

    out = ""

    for zone in gm.graph.zones:
        if zone.type != ZoneTypes.STREAM:
            continue

        storage_change = (
            dm.cur_storage_chg_by_id[
                zone.id
            ].measured
        )

        out += (
            "\n"
            + zone.id
            + f"(ΔS={storage_change:9.4f})"
        )

        for flow in gm.get_zone_outflows(zone.id):
            flow_value = (
                dm.cur_flows_by_id[
                    flow.id
                ].measured
            )
            out += (
                f"\n {flow_value:9.4f} >> "
                f"{flow.to_zone}"
            )

            for result in results.get_result_value(
                date=date,
                flow_id=flow.id,
            ):
                path_item = tm.get_path_item(
                    result.txn_id,
                    flow.id,
                )
                out += (
                    f"\n      {result.txn_id: <26} "
                    f"= {result.value:9.4f}   "
                    f"({result.reason})"
                )
                out += warn_if_value_is_incorrect(
                    path_item,
                    result.value,
                )

        for flow in gm.get_zone_inflows(zone.id):
            flow_value = (
                dm.cur_flows_by_id[
                    flow.id
                ].measured
            )
            out += (
                f"\n {flow_value:9.4f} << "
                f"{flow.from_zone}"
            )

            for result in results.get_result_value(
                date=date,
                flow_id=flow.id,
            ):
                path_item = tm.get_path_item(
                    result.txn_id,
                    flow.id,
                )
                out += (
                    f"\n      {result.txn_id: <26} "
                    f"= {result.value:9.4f}   "
                    f"({result.reason})"
                )
                out += warn_if_value_is_incorrect(
                    path_item,
                    result.value,
                )

    return out