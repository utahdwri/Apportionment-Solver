"""Public frozen-plan API for the second-generation compiler."""

from __future__ import annotations

from copy import deepcopy
from collections import Counter
from dataclasses import replace
from datetime import date as Date, timedelta

from ..graph_manager import GraphManager
from ..lag_utils import unlag_apportionments
from ..models import PathTrxn, SolverOutput, TrxnGroup, ZoneTypes
from ..natural_flow_calculator import NaturalFlowCalculator
from ..solver import _loop_through_date_range, assert_apportionments_equal_expected
from ..timeseries_manager import DailyDataManager
from ..trxn_schedule import TrxnSchedule
from .compiler import V2CompilationOptions, prepare_sequential_direct_context
from .codegen import build_generated_day_executor
from .execution_ir import (
    DayExecutionProgram,
    DerivedSlackMember,
    DerivedSlackRule,
    FrozenPathReconstruction,
    ResidualEffect,
)
from .model import CompilerModel
from .runtime import V2CompilationSession, v2_factory
from .program import EqualPriorityProgram
from .transforms import collapse_transaction_path_variables



def _add_transition(
    dates: set[Date],
    day: Date,
    *,
    start: Date,
    end: Date,
) -> None:
    if start <= day <= end:
        dates.add(day)


def _add_consecutive_presence_transitions(
    dates: set[Date],
    values: dict[str, float],
    *,
    start: Date,
    end: Date,
) -> None:
    """Add starts/ends of exact-date presence runs without scanning days.

    External natural-flow boundaries are structural cut edges only on dates
    for which a boundary value exists.  Consecutive dates with a value have
    the same boundary-presence structure, so one representative is enough.
    """

    active = sorted(
        Date.fromisoformat(value)
        for value in values
        if start <= Date.fromisoformat(value) <= end
    )
    if not active:
        return

    run_start = active[0]
    previous = active[0]
    for day in active[1:]:
        if day != previous + timedelta(days=1):
            _add_transition(dates, run_start, start=start, end=end)
            _add_transition(
                dates, previous + timedelta(days=1), start=start, end=end
            )
            run_start = day
        previous = day

    _add_transition(dates, run_start, start=start, end=end)
    _add_transition(dates, previous + timedelta(days=1), start=start, end=end)


def _structural_regime_dates(problem) -> list[str]:
    """Return representative dates only where LP coefficients can change.

    Daily measurements, transaction limits, natural-flow quantities, storage
    values, and committed residuals are runtime bound parameters.  They do not
    create compiler regimes.  The current production LP has only two sources
    of date-selected coefficient structure:

    * the presence/absence of external natural-flow boundary cut edges.

    Time-varying constant fractional losses are coefficient parameters in the
    frozen IR and therefore do not create a structural regime.

    This function derives those transitions from model metadata rather than
    iterating through the solve date range.
    """

    start = Date.fromisoformat(problem.beg_date)
    end = Date.fromisoformat(problem.end_date)
    dates: set[Date] = {start}

    for daily_values in problem.external_natural_flows.values():
        _add_consecutive_presence_transitions(
            dates, daily_values, start=start, end=end
        )

    return [day.isoformat() for day in sorted(dates)]


def _coefficient_regime_signature(engine) -> tuple:
    """Fingerprint LP sparsity/parameter identity, never runtime values."""

    dynamic = getattr(engine, "_v2_dynamic_coefficients", {})
    constraints = []
    for name, constraint in sorted(engine.cons.items()):
        coefficients = []
        variable_names = set(constraint.coefficients) | {
            variable
            for constraint_name, variable in dynamic
            if constraint_name == name
        }
        for variable in sorted(variable_names):
            value = float(constraint.coefficients.get(variable, 0.0))
            metadata = dynamic.get((name, variable))
            if metadata is not None:
                slot, lower, upper = metadata
                coefficients.append(
                    (variable, "parameter", slot, float(lower), float(upper))
                )
            elif abs(value) > 1e-15:
                coefficients.append((variable, "constant", round(value, 15)))
        constraints.append((name, tuple(coefficients)))
    return (tuple(sorted(engine.vars)), tuple(constraints))


def _build_logical_model(engine) -> tuple[CompilerModel, dict[str, int]]:
    """Build one shared path-collapsed compiler model for this regime.

    Objective compilation clones this immutable logical starting point instead
    of rebuilding and recollapsing the production path-leg model for every
    priority.  All objective-specific residual/presolve transforms happen on
    the clone.
    """

    model = CompilerModel.from_engine(engine)
    collapse_stats = collapse_transaction_path_variables(model)
    return model, collapse_stats


def _build_residual_effects(
    model: CompilerModel,
) -> dict[str, tuple[ResidualEffect, ...]]:
    """Derive logical-transaction residual updates from collapsed compiler IR."""

    effects: dict[str, list[ResidualEffect]] = {}
    for constraint in model.constraints.values():
        equality = (
            constraint.lower is not None
            and constraint.upper is not None
            and constraint.lower.equivalent(constraint.upper)
        )
        for variable_name, coefficient in constraint.coefficients.items():
            if coefficient.is_constant(0.0):
                continue
            effects.setdefault(variable_name, []).append(
                ResidualEffect(
                    constraint_name=constraint.name,
                    coefficient=coefficient.copy(),
                    has_lower=constraint.lower is not None,
                    has_upper=constraint.upper is not None,
                    equality=equality,
                )
            )
    return {name: tuple(values) for name, values in effects.items()}


def _build_frozen_path_reconstruction(
    model: CompilerModel,
    engine,
    trxn_manager: TrxnSchedule,
) -> FrozenPathReconstruction:
    """Freeze final path-leg reconstruction expressions at compile time."""

    expressions: dict[str, object] = {}
    logical_sources: dict[str, str] = {}

    for trxn in trxn_manager.all_trxns:
        if isinstance(trxn, PathTrxn):
            anchor = trxn_manager.get_anchor_var(trxn)
            if anchor:
                logical_sources[trxn.id] = anchor
            for path_item in trxn.path:
                source_name = f"{trxn.id}___{path_item.flow_id}"
                expression = model.reconstruction.get(source_name)
                if expression is not None:
                    expressions[source_name] = expression.copy()
        elif isinstance(trxn, TrxnGroup):
            logical_sources[trxn.id] = trxn.id

    needed_values: set[str] = set()
    for expression in expressions.values():
        needed_values.update(expression.variables)

    value_sources: list[tuple[str, str]] = []
    for name in sorted(needed_values):
        source_name = logical_sources.get(name, name)
        if source_name not in engine.vars:
            raise KeyError(
                "Frozen path reconstruction could not map compiler variable "
                f"{name!r} to a production value source"
            )
        value_sources.append((name, source_name))

    # Keep only reconstruction expressions in the parameter model. With
    # CompilerModel._all_slots() operating on live IR references, this means
    # runtime parameter refresh touches only coefficients that actually appear
    # in the final continuity reconstruction.
    parameter_model = CompilerModel(
        variables={},
        constraints={},
        reconstruction={
            name: expression.copy() for name, expression in expressions.items()
        },
        source_variable_count=model.source_variable_count,
        parameter_defaults=dict(model.parameter_defaults),
        parameter_sources=dict(model.parameter_sources),
        parameter_domains=dict(model.parameter_domains),
    )

    return FrozenPathReconstruction(
        parameter_model=parameter_model,
        expressions=tuple(
            (name, expression.copy())
            for name, expression in sorted(expressions.items())
        ),
        value_sources=tuple(value_sources),
    )


def _build_derived_slack_rules(
    engine,
    trxn_manager: TrxnSchedule,
    graph_manager: GraphManager,
) -> tuple[DerivedSlackRule, ...]:
    """Build deterministic slack-output rules directly from LP measurement rows."""

    from ..apportioner import PREFIX_MEASURE

    natural_zones = {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}
    grouped: dict[str, list[DerivedSlackMember]] = {}
    flow_ids: dict[str, str] = {}

    for trxn in trxn_manager.all_trxns:
        if not (isinstance(trxn, PathTrxn) and trxn.is_slack):
            continue
        if len(trxn.path) != 1:
            raise ValueError(
                f"Reporting slack {trxn.id!r} must have exactly one path leg"
            )
        path_item = trxn.path[0]
        anchor = trxn_manager.get_anchor_var(trxn)
        if not anchor:
            continue
        constraint_name = PREFIX_MEASURE + path_item.flow_id
        constraint = engine.cons.get(constraint_name)
        if constraint is None:
            continue
        coefficient = float(constraint.coefficients.get(anchor, 0.0))
        if abs(coefficient) <= 1e-15:
            continue

        flow = graph_manager.get_flow_by_id(path_item.flow_id)
        if path_item.factor >= 0:
            physical_from = graph_manager.get_zone_by_id(flow.from_zone)
            physical_to = graph_manager.get_zone_by_id(flow.to_zone)
        else:
            physical_from = graph_manager.get_zone_by_id(flow.to_zone)
            physical_to = graph_manager.get_zone_by_id(flow.from_zone)

        spill_to_natural = (
            physical_from.type not in natural_zones
            and physical_to.type in natural_zones
        )
        member = DerivedSlackMember(
            transaction_id=trxn.id,
            anchor_variable=anchor,
            flow_id=path_item.flow_id,
            coefficient=coefficient,
            spill_to_natural=spill_to_natural,
            receiving_zone_id=(physical_to.id if spill_to_natural else None),
        )
        grouped.setdefault(constraint_name, []).append(member)
        flow_ids[constraint_name] = path_item.flow_id

    return tuple(
        DerivedSlackRule(
            constraint_name=name,
            flow_id=flow_ids[name],
            members=tuple(sorted(members, key=lambda member: member.transaction_id)),
        )
        for name, members in sorted(grouped.items())
    )


class V2CompiledSolver:
    """Prepared v2 plan with a structurally frozen parameterized LP/IR.

    Preparation never solves an accounting day and never uses objective
    results to discover control flow. It constructs the production LP for each
    distinct date-dependent coefficient regime, marks runtime state as
    parameters, and freezes the scalar priority objectives directly from the
    transaction structure. Runtime equal-priority loops and spill/finalization
    work use declared auxiliary kernels rather than creating compiler programs.
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
        self._execution_program = DayExecutionProgram()
        self._display_schedule = None
        self._display_effects = None
        self._display_slack_rules: tuple[DerivedSlackRule, ...] = ()
        self._prepare()
        self._session.defer_production_nf_coefficients = (
            self._can_defer_production_nf_coefficients()
        )

        # Runtime execution reuses the immutable structural managers prepared
        # once here.  Per-run clones below contain only mutable account,
        # cumulative, and current-day state; they do not re-copy/re-validate
        # hundreds or thousands of transaction objects.
        self._runtime_graph_manager = GraphManager(
            deepcopy(self._input.accounting_graph)
        )
        self._runtime_trxn_template = TrxnSchedule(
            self._runtime_graph_manager,
            self._input.txns,
            self.max_daily_apportionment,
        )
        self._runtime_data_template = DailyDataManager(
            self._runtime_graph_manager,
            self._input.measurements,
            self._input.external_natural_flows,
        )

        # #10: lower the frozen indexed IR into executable Python once.  The
        # interpreter remains available as a reference/fallback through the
        # compilation option, but normal daily execution uses this generated
        # routine.
        self._generated_executor = None
        if self._session.options.enable_generated_python:
            self._generated_executor = build_generated_day_executor(
                session=self._session,
                execution_program=self._execution_program,
                trxn_manager=self._runtime_trxn_template,
                graph_manager=self._runtime_graph_manager,
            )


    def _can_defer_production_nf_coefficients(self) -> bool:
        """Prove that daily production-LP NF coefficient writes are unnecessary.

        Equal-priority programs are addressed directly by frozen cohort IR, so
        unlike sequential scalar objectives they do not depend on the source LP
        structural fingerprint at runtime.  Dynamic NF coefficients must still
        remain materialized whenever either a frozen kernel or residual effect
        reads them. Auxiliary production-LP solves are handled lazily by
        :class:`Apportioner`.
        """
        if not self._session.programs or not all(
            isinstance(program, EqualPriorityProgram)
            for program in self._session.programs
        ):
            return False

        for program in self._session.programs:
            model = getattr(program, "model", None)
            if model is None:
                continue
            for reader in model._runtime_readers():
                if reader[0] == "coefficient" and reader[2].startswith("NF_ZONE_"):
                    return False

        for effects in self._execution_program.effects_by_regime.values():
            for transaction_effects in effects.values():
                for effect in transaction_effects:
                    if any(
                        slot.startswith("coefficient[NF_ZONE_")
                        for slot in effect.coefficient.slots()
                    ):
                        return False
        return True

    def _run(self, problem, *, check_expected_values: bool = False) -> SolverOutput:
        from ..apportioner import Apportioner

        graph_manager = self._runtime_graph_manager
        natural_flow_calculator = NaturalFlowCalculator(graph_manager)
        data_manager = self._runtime_data_template.clone_runtime(
            measurements=problem.measurements,
            external_natural_flows=problem.external_natural_flows,
        )
        trxn_manager = self._runtime_trxn_template.clone_runtime(graph_manager)

        apportionment_results = []
        for date in _loop_through_date_range(problem.beg_date, problem.end_date):
            data_manager.set_day(date)
            trxn_manager.begin_day(date)
            apportioner = Apportioner(
                graph_manager,
                trxn_manager,
                data_manager,
                natural_flow_calculator,
                lp_solver_factory=v2_factory(self._session),
                generate_audit=False,
            )
            apportioner.update_daily_bounds()
            schedule = trxn_manager.build_schedule(date)
            apportioner.apply_nf_mass_balance_constraints(date)
            if len(self._execution_program.effects_by_regime) == 1:
                regime_signature = next(
                    iter(self._execution_program.effects_by_regime)
                )
            else:
                regime_signature = _coefficient_regime_signature(apportioner.engine)
            if self._generated_executor is not None:
                self._generated_executor.execute(
                    apportioner,
                    schedule=schedule,
                    regime_signature=regime_signature,
                )
            else:
                self._execution_program.execute(
                    apportioner,
                    date=date,
                    schedule=schedule,
                    regime_signature=regime_signature,
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
            self._prepare_structurally()
        finally:
            self._session.finish_preparation()

    def _prepare_structurally(self) -> None:
        """Freeze scalar priority objectives without tracing numerical solves."""

        problem = deepcopy(self._input)
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

            # Build one structural LP per topology regime, not one per day.
        # Measurements, NF amounts, limits, storage, committed residuals, and
        # time-varying fractional-loss coefficients are runtime parameters.
        # Representative dates are needed only for genuinely structural
        # changes such as external-boundary cut-edge presence.
        regime_signatures: set[tuple] = set()
        for date in _structural_regime_dates(problem):
            data_manager.set_day(date)
            trxn_manager.begin_day(date)
            from ..apportioner import (
                Apportioner,
                PREFIX_ACCOUNT_IN,
                PREFIX_ACCOUNT_OUT,
                PREFIX_MEASURE,
            )

            apportioner = Apportioner(
                graph_manager,
                trxn_manager,
                data_manager,
                natural_flow_calculator,
                lp_solver_factory=v2_factory(self._session),
                generate_audit=False,
            )
            engine = apportioner.engine

            # Most bidirectional slack pairs are pure reporting residuals and
            # impose no allocation restriction after existential projection.
            # Keep only rows touching storage as transient directional-residual
            # proxy rows because the historical counterflow ambiguity convention
            # uses them while allocating storage-related transactions.
            engine._v2_directional_residual_constraints = {
                PREFIX_MEASURE + flow.id
                for flow in graph_manager.graph.interzone_flows
                if flow.bidirectional
                and (
                    graph_manager.get_zone_by_id(flow.from_zone).type == ZoneTypes.STORAGE
                    or graph_manager.get_zone_by_id(flow.to_zone).type == ZoneTypes.STORAGE
                )
            }

            # Daily bounds are parameters, not structural constants. Calling
            # update_daily_bounds records the normal LP bound sides but does
            # not solve or branch on any objective result.
            apportioner.update_daily_bounds()
            apportioner.apply_nf_mass_balance_constraints(date)

            # A frozen scalar objective must work on the first pass (junior
            # lower bounds are zero) and on the second spill-reallocation pass
            # (other transactions may already be committed). Parameterize both
            # transaction bounds for that reason.
            for trxn in trxn_manager.all_trxns:
                if isinstance(trxn, PathTrxn):
                    if trxn.is_slack:
                        continue
                    for path_item in trxn.path:
                        name = f"{trxn.id}___{path_item.flow_id}"
                        engine._v2_dynamic_bound_sides.add(("variable", name, "lower"))
                        engine._v2_dynamic_bound_sides.add(("variable", name, "upper"))
                elif isinstance(trxn, TrxnGroup):
                    engine._v2_dynamic_bound_sides.add(("variable", trxn.id, "lower"))
                    engine._v2_dynamic_bound_sides.add(("variable", trxn.id, "upper"))

            # Storage/exchange ambiguity handling temporarily minimizes the
            # components flowing in the opposing direction and locks their
            # upper bounds while a priority variable is maximized. Reporting
            # slack columns that appear here are transient residual proxies;
            # their reported values are still derived afterward from leftover
            # measurements. These temporary bounds therefore need runtime slots.
            for trxn in trxn_manager.all_trxns:
                if isinstance(trxn, PathTrxn) and trxn.is_slack:
                    continue
                for minus_var in trxn_manager.get_minus_vars([trxn]):
                    anchor = trxn_manager.get_anchor_var(minus_var)
                    if anchor:
                        engine._v2_dynamic_bound_sides.add(
                            ("variable", anchor, "upper")
                        )

            # Storage-to-natural reporting residuals are derived after pass one
            # and then fixed while their natural-flow credit is reallocated on
            # pass two. Parameterize both sides so the same frozen formulas see
            # that derived physical residual.
            natural_zones = {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}
            for trxn in trxn_manager.all_trxns:
                if not (
                    isinstance(trxn, PathTrxn)
                    and trxn.is_slack
                    and len(trxn.path) == 1
                ):
                    continue
                path_item = trxn.path[0]
                flow = graph_manager.get_flow_by_id(path_item.flow_id)
                from_zone = graph_manager.get_zone_by_id(flow.from_zone)
                to_zone = graph_manager.get_zone_by_id(flow.to_zone)
                if path_item.factor < 0:
                    from_zone, to_zone = to_zone, from_zone
                if from_zone.type not in natural_zones and to_zone.type in natural_zones:
                    anchor = trxn_manager.get_anchor_var(trxn)
                    if anchor:
                        engine._v2_dynamic_bound_sides.add(
                            ("variable", anchor, "lower")
                        )
                        engine._v2_dynamic_bound_sides.add(
                            ("variable", anchor, "upper")
                        )

            # Account capacities are constructor-time state and can change as
            # balances carry across days. Mark even a current zero as dynamic.
            for name in engine.cons:
                if name.startswith((PREFIX_ACCOUNT_OUT, PREFIX_ACCOUNT_IN)):
                    engine._v2_dynamic_bound_sides.add(("constraint", name, "upper"))

            regime_signature = _coefficient_regime_signature(engine)
            logical_model, collapse_stats = _build_logical_model(engine)
            effects = _build_residual_effects(logical_model)
            slack_rules = _build_derived_slack_rules(
                engine, trxn_manager, graph_manager
            )
            path_reconstruction = _build_frozen_path_reconstruction(
                logical_model, engine, trxn_manager
            )
            self._execution_program.add_regime(
                regime_signature,
                effects,
                slack_rules,
                path_reconstruction,
                constraint_names=engine.cons.keys(),
                residual_parameter_model=logical_model,
            )
            if self._display_schedule is None:
                self._display_schedule = trxn_manager.build_schedule(date)
                self._display_effects = effects
                self._display_slack_rules = slack_rules
            if regime_signature in regime_signatures:
                self._session.stats["structural_duplicate_regimes"] += 1
                continue
            regime_signatures.add(regime_signature)
            self._session.stats["structural_regimes"] += 1

            # Freeze only structurally sequential priorities. Equal-priority
            # cohorts are represented by one structural water-filling kernel;
            # compiling a separate scalar formula for hundreds of cohort
            # members would be expensive and those formulas are not part of
            # the normal execution graph anyway.
            allocation_transactions = [
                trxn
                for trxn in trxn_manager.all_trxns
                if not (isinstance(trxn, PathTrxn) and trxn.is_slack)
                and isinstance(trxn, (PathTrxn, TrxnGroup))
                and (
                    isinstance(trxn, TrxnGroup)
                    or trxn_manager.get_anchor_var(trxn) is not None
                )
            ]
            priority_counts = Counter(
                trxn.priority for trxn in allocation_transactions
            )
            residual_transaction_names = {
                trxn.id for trxn in allocation_transactions
            }
            sequential_direct_context = None
            if (
                self._session.options.enable_early_direct_sequential
                and any(priority_counts[trxn.priority] == 1 for trxn in allocation_transactions)
            ):
                sequential_direct_context = prepare_sequential_direct_context(
                    logical_model,
                    transaction_names=residual_transaction_names,
                )
                self._session.stats["sequential_direct_contexts"] += 1
                for key, value in sequential_direct_context.preparation_stats.items():
                    self._session.stats[key] += value
            # A senior transaction can be removed from a later residual kernel
            # only when its committed value is lexicographically frozen in the
            # same feasible region. Storage/counterflow ambiguity temporarily
            # minimizes and locks opposing-direction variables, so objectives
            # participating in that convention can see different feasible
            # regions. Keep those transactions as zero-based *recourse
            # increments* in the final IR rather than incorrectly fixing them.
            directionally_ambiguous_transactions = {
                trxn.id
                for trxn in allocation_transactions
                if trxn_manager.get_minus_vars([trxn])
            }

            # Equal-priority cohorts are compiled once over logical transaction
            # residual variables. Runtime water filling can activate any subset
            # of the cohort without rebuilding path-leg merge rows.
            equal_priority_groups: dict[float, list[PathTrxn | TrxnGroup]] = {}
            for candidate in allocation_transactions:
                if priority_counts[candidate.priority] > 1:
                    equal_priority_groups.setdefault(candidate.priority, []).append(
                        candidate
                    )

            for priority, members in equal_priority_groups.items():
                member_ids = [member.id for member in members]
                # If the cohort itself participates in directional ambiguity,
                # preserve senior residual recourse just as scalar kernels do.
                # Otherwise those seniors are already represented by ResidualState
                # and can disappear from the cohort kernel.
                committed_transaction_names: set[str] = set()
                if not any(
                    member.id in directionally_ambiguous_transactions
                    for member in members
                ):
                    committed_transaction_names = {
                        candidate.id
                        for candidate in allocation_transactions
                        if candidate.priority < priority
                        and candidate.id not in directionally_ambiguous_transactions
                    }
                self._session.prepare_equal_priority(
                    engine,
                    member_transaction_names=member_ids,
                    priority=priority,
                    residual_transaction_names=residual_transaction_names,
                    committed_transaction_names=committed_transaction_names,
                    regime_signature=regime_signature,
                    base_logical_model=logical_model,
                    base_collapse_stats=collapse_stats,
                )
                self._session.stats["equal_priority_kernel_members"] += len(
                    member_ids
                )

            for trxn in trxn_manager.all_trxns:
                if isinstance(trxn, PathTrxn):
                    if trxn.is_slack:
                        continue
                    target = trxn_manager.get_anchor_var(trxn)
                elif isinstance(trxn, TrxnGroup):
                    target = trxn.id
                else:
                    continue
                if not target:
                    continue
                if priority_counts[trxn.priority] > 1:
                    continue
                committed_transaction_names: set[str] = set()
                if trxn.id not in directionally_ambiguous_transactions:
                    committed_transaction_names = {
                        candidate.id
                        for candidate in allocation_transactions
                        if candidate.priority < trxn.priority
                        and candidate.id not in directionally_ambiguous_transactions
                    }
                self._session.prepare_objective(
                    engine,
                    variable_names=[target],
                    maximization=True,
                    residual_transaction_names=residual_transaction_names,
                    committed_transaction_names=committed_transaction_names,
                    regime_signature=regime_signature,
                    base_logical_model=logical_model,
                    base_collapse_stats=collapse_stats,
                    sequential_direct_context=(
                        sequential_direct_context
                        if trxn.id not in directionally_ambiguous_transactions
                        else None
                    ),
                )

    def solve(self, *, measurements=None, check_expected_values: bool = False) -> SolverOutput:
        # The compiled plan owns an immutable deep copy of the original input.
        # Runtime managers share its frozen graph/transaction structure, so a
        # full deepcopy here would only duplicate large static data.  ``replace``
        # preserves the previous SolverInput date-range validation for an
        # alternate measurement collection without copying unrelated structure.
        problem = (
            self._input
            if measurements is None
            else replace(self._input, measurements=measurements)
        )
        self._session.reset_execution_stats()
        return self._run(problem, check_expected_values=check_expected_values)

    def code(self) -> str:
        """Return the executable Python generated from the frozen indexed IR.

        ``formulas()`` remains the semantic/compiler view with transaction and
        constraint names.  ``code()`` is the final lowered runtime form: plain
        Python arithmetic over indexed parameter/residual arrays plus calls to
        prebuilt kernels for structures that cannot be reduced to scalar math.
        """
        if self._generated_executor is None:
            # Code generation can be disabled for interpreter equivalence tests;
            # build a source view lazily without changing the configured solve path.
            generated = build_generated_day_executor(
                session=self._session,
                execution_program=self._execution_program,
                trxn_manager=self._runtime_trxn_template,
                graph_manager=self._runtime_graph_manager,
            )
            return generated.source
        return self._generated_executor.source

    def formulas(self) -> str:
        routine = ""
        if self._display_schedule is not None and self._display_effects is not None:
            routine = self._execution_program.text(
                self._display_schedule,
                self._display_effects,
                self._display_slack_rules,
                assignment_renderer=self._session.routine_assignment_lines,
            )
        definitions = self._session.formulas()
        if routine:
            return routine + "\n\n\n" + definitions
        return definitions

    def report(self) -> dict:
        report = self._session.report()
        residual_layouts = self._execution_program.residual_layout_by_regime.values()
        report["indexed_residual_layouts"] = len(
            self._execution_program.residual_layout_by_regime
        )
        report["indexed_residual_slots_total"] = sum(
            len(layout.names) for layout in residual_layouts
        )
        residual_parameter_layouts = (
            self._execution_program.residual_parameter_layout_by_regime.values()
        )
        report["indexed_residual_parameter_layouts"] = len(
            self._execution_program.residual_parameter_layout_by_regime
        )
        report["indexed_residual_parameter_slots_total"] = sum(
            len(layout.slot_names) for layout in residual_parameter_layouts
        )
        program_layout_sizes = [
            len(program.model.indexed_parameter_layout().slot_names)
            for program in self._session.programs
            if getattr(program, "model", None) is not None
        ]
        report["indexed_parameter_layouts"] = len(program_layout_sizes)
        report["indexed_parameter_slots_max"] = max(program_layout_sizes, default=0)
        report["generated_python"] = self._generated_executor is not None
        report["generated_python_regimes"] = self._session.stats["generated_python_regimes"]
        report["generated_python_direct_assignments"] = self._session.stats["generated_python_direct_assignments"]
        report["generated_python_kernel_calls"] = self._session.stats["generated_python_kernel_calls"]
        report["execution_generated_python_days"] = self._session.stats["execution_generated_python_days"]
        report["execution_generated_direct_assignments"] = self._session.stats["execution_generated_direct_assignments"]
        return report


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
