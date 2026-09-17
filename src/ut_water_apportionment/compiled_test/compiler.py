"""Objective compiler and structural signatures for frozen v2 programs."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite

from .model import (
    CompilerModel,
    GuardPredicate,
    ParamExpr,
    ParametricConstraint,
    SymbolicExpr,
)
from .program import DirectScalarProgram, EqualPriorityProgram, ReducedLPProgram, V2Program
from .transforms import (
    collapse_transaction_path_variables,
    compile_residual_increment_ir,
    derived_slack_elimination,
    presolve,
)


@dataclass(frozen=True)
class V2CompilationOptions:
    enable_direct_scalar: bool = True
    enable_reduced_lp_kernel: bool = True
    freeze_after_prepare: bool = True
    enable_guarded_redundancy: bool = True
    enable_early_direct_sequential: bool = True
    enable_generated_python: bool = True


class V2CannotCompile(RuntimeError):
    pass


@dataclass
class SequentialDirectContext:
    """Shared residual IR used by the early sequential/MIN compiler.

    The generic objective compiler historically cloned and presolved the full
    logical model once per priority before discovering that an ordinary senior
    transaction was just a one-variable capacity calculation.  This context is
    prepared once per structural coefficient regime.  It rebases all logical
    transactions onto residual increments, projects deterministic reporting
    slacks, and records which transaction increments can be fixed at their
    canonical lower bound without tightening any live row.

    A scalar objective can then be compiled by inspecting only the sparse rows
    containing its logical transaction column.  If the proof is incomplete,
    the caller simply falls back to the existing generic compiler.
    """

    source_model: CompilerModel
    residual_model: CompilerModel
    transaction_names: frozenset[str]
    zero_safe_transactions: frozenset[str]
    redundant_lower_sides: frozenset[str]
    redundant_upper_sides: frozenset[str]
    rows_by_variable: dict[str, tuple[str, ...]] = field(default_factory=dict)
    preparation_stats: dict[str, int] = field(default_factory=dict)


def _constant_nonnegative_lower(variable) -> bool:
    return (
        variable.lower is not None
        and variable.lower.is_constant()
        and isfinite(variable.lower.constant_value_number())
        and variable.lower.constant_value_number() >= -1e-12
    )


def prepare_sequential_direct_context(
    base_logical_model: CompilerModel,
    *,
    transaction_names: set[str],
) -> SequentialDirectContext:
    """Prepare one shared residual model for direct sequential compilation.

    This work is deliberately objective-independent.  It is paid once per
    structural regime instead of once per priority.
    """

    residual = base_logical_model.copy()
    # Sequential scalar programs reconstruct only their requested source value.
    # Avoid updating every path-leg reconstruction while rebasing hundreds of
    # transactions; the one requested reconstruction is rebuilt on demand from
    # ``source_model`` by ``compile_early_direct_sequential``.
    residual.reconstruction.clear()
    residual_stats = compile_residual_increment_ir(
        residual,
        transaction_names=transaction_names,
        committed_names=set(),
    )
    slack_eliminated = derived_slack_elimination(residual, protected=set())

    active_transactions = frozenset(
        name for name in transaction_names if name in residual.variables
    )

    rows_by_variable_lists: dict[str, list[str]] = {}
    for constraint in residual.constraints.values():
        for variable_name, coefficient in constraint.coefficients.items():
            if coefficient.is_constant(0.0):
                continue
            rows_by_variable_lists.setdefault(variable_name, []).append(
                constraint.name
            )

    # Residual-state monotonicity gives stronger structural proofs than generic
    # parameter-domain presolve.  For example, an NF row that starts with
    # ``0 <= sum(nonnegative transaction increments)`` keeps a non-positive
    # remaining lower side forever because every committed increment subtracts
    # a nonnegative amount from that side.  Such a lower side therefore never
    # limits a later nonnegative increment and needs neither a runtime guard nor
    # a conservative alternate.  The upper-side rule is the exact mirror image.
    redundant_lower: set[str] = set()
    redundant_upper: set[str] = set()
    for name, constraint in residual.constraints.items():
        source = base_logical_model.constraints.get(name)
        if source is None:
            continue

        coefficients = [
            (variable_name, coefficient)
            for variable_name, coefficient in constraint.coefficients.items()
            if not coefficient.is_constant(0.0)
        ]
        all_residual_transactions = all(
            variable_name in active_transactions
            and _constant_nonnegative_lower(residual.variables[variable_name])
            for variable_name, _ in coefficients
        )
        if not all_residual_transactions:
            continue

        signs = [residual.coefficient_sign(coefficient) for _, coefficient in coefficients]
        if any(sign is None for sign in signs):
            continue

        source_lower_starts_nonpositive = False
        if source.lower is not None:
            if source.lower.is_constant():
                source_lower_starts_nonpositive = (
                    source.lower.constant_value_number() <= 1e-12
                )
            elif name.startswith("NF_ZONE_"):
                # Apportioner establishes every natural-flow availability row
                # with a zero lower bound at the start of each day.  The upper
                # side varies with available NF; the lower side does not.  The
                # source LP marks the side dynamic because it is installed
                # during day setup, so retain this domain invariant explicitly
                # here rather than turning it into a runtime guard.
                try:
                    source_lower_starts_nonpositive = (
                        abs(source.lower.evaluate(base_logical_model.parameter_defaults))
                        <= 1e-12
                    )
                except (KeyError, ZeroDivisionError):
                    source_lower_starts_nonpositive = False

        if (
            constraint.lower is not None
            and source_lower_starts_nonpositive
            and all(sign >= 0 for sign in signs)
        ):
            redundant_lower.add(name)

        if (
            constraint.upper is not None
            and source.upper is not None
            and source.upper.is_constant()
            and source.upper.constant_value_number() >= -1e-12
            and all(sign <= 0 for sign in signs)
        ):
            redundant_upper.add(name)

    # Classify zero/lower-bound-safe logical transactions once.  This is the
    # same mathematical proof as monotone_nonobjective_elimination(), but uses
    # the residual-side invariants above and is shared by every scalar objective.
    zero_safe: set[str] = set()
    for variable_name in active_transactions:
        variable = residual.variables[variable_name]
        if not (
            variable.lower is not None
            and variable.lower.is_constant()
            and abs(variable.lower.constant_value_number()) <= 1e-12
        ):
            continue

        safe = True
        for constraint_name in rows_by_variable_lists.get(variable_name, ()):
            constraint = residual.constraints[constraint_name]
            coefficient = constraint.coefficients[variable_name]
            sign = residual.coefficient_sign(coefficient)
            if sign is None:
                safe = False
                break
            if (
                constraint.upper is not None
                and constraint_name not in redundant_upper
                and sign < 0
            ):
                safe = False
                break
            if (
                constraint.lower is not None
                and constraint_name not in redundant_lower
                and sign > 0
            ):
                safe = False
                break
        if safe:
            zero_safe.add(variable_name)

    stats = dict(residual_stats)
    stats["slack_eliminated"] = slack_eliminated
    stats["sequential_residual_lower_sides_structural"] = len(redundant_lower)
    stats["sequential_residual_upper_sides_structural"] = len(redundant_upper)
    stats["sequential_zero_safe_transactions"] = len(zero_safe)

    return SequentialDirectContext(
        source_model=base_logical_model,
        residual_model=residual,
        transaction_names=active_transactions,
        zero_safe_transactions=frozenset(zero_safe),
        redundant_lower_sides=frozenset(redundant_lower),
        redundant_upper_sides=frozenset(redundant_upper),
        rows_by_variable={
            name: tuple(values) for name, values in rows_by_variable_lists.items()
        },
        preparation_stats=stats,
    )


def _residualized_source_expression(
    context: SequentialDirectContext,
    source_name: str,
) -> SymbolicExpr | None:
    source = context.source_model.reconstruction.get(source_name)
    if source is None:
        return None
    expression = source.copy()
    for variable_name, coefficient in list(expression.variables.items()):
        if variable_name not in context.transaction_names:
            return None
        base = context.residual_model.residual_increment_bases.get(variable_name)
        if base is None:
            return None
        expression.parameters = expression.parameters.plus(
            coefficient.multiplied(base)
        )
    return expression


def compile_early_direct_sequential(
    *,
    name: str,
    variable_names: list[str],
    maximization: bool,
    weights: dict[str, float] | None,
    context: SequentialDirectContext,
) -> DirectScalarProgram | None:
    """Compile a unique-priority objective directly from its sparse column.

    Returns ``None`` whenever the exact one-variable proof is incomplete.  The
    generic compiler remains the authoritative fallback for signed/counterflow,
    storage recourse, coupled equalities, or any other non-monotone structure.
    """

    if len(variable_names) != 1 or not maximization:
        return None
    if weights and any(abs(value - 1.0) > 1e-12 for value in weights.values()):
        return None

    requested_name = variable_names[0]
    objective = _residualized_source_expression(context, requested_name)
    if objective is None or len(objective.variables) != 1:
        return None

    target_name, objective_coefficient = next(iter(objective.variables.items()))
    target = context.residual_model.variables.get(target_name)
    if target is None:
        return None
    objective_sign = context.residual_model.coefficient_sign(objective_coefficient)
    if objective_sign != 1:
        return None
    if not (
        target.lower is not None
        and target.lower.is_constant()
        and abs(target.lower.constant_value_number()) <= 1e-12
    ):
        return None

    compact_constraints: dict[str, ParametricConstraint] = {}
    structural_proofs: list[GuardPredicate] = []

    for constraint_name in context.rows_by_variable.get(target_name, ()):
        constraint = context.residual_model.constraints[constraint_name]
        target_coefficient = constraint.coefficients.get(target_name)
        if target_coefficient is None or target_coefficient.is_constant(0.0):
            continue
        if context.residual_model.coefficient_sign(target_coefficient) is None:
            return None

        # Every coupled transaction must be globally safe at residual zero.
        # Therefore deleting it cannot improve feasibility for the target and
        # cannot reserve capacity that the senior objective actually needs.
        for other_name, other_coefficient in constraint.coefficients.items():
            if other_name == target_name or other_coefficient.is_constant(0.0):
                continue
            if other_name not in context.zero_safe_transactions:
                return None

        lower = constraint.lower
        upper = constraint.upper
        if constraint_name in context.redundant_lower_sides:
            source = context.source_model.constraints[constraint_name]
            if source.lower is not None:
                structural_proofs.append(
                    GuardPredicate(
                        ParamExpr.constant_value(0.0),
                        ">=",
                        source.lower.copy(),
                        (
                            f"{constraint_name} residual lower side stays redundant: "
                            "it starts non-positive and committed nonnegative "
                            "transaction increments can only decrease it"
                        ),
                    )
                )
            lower = None
        if constraint_name in context.redundant_upper_sides:
            source = context.source_model.constraints[constraint_name]
            if source.upper is not None:
                structural_proofs.append(
                    GuardPredicate(
                        ParamExpr.constant_value(0.0),
                        "<=",
                        source.upper.copy(),
                        (
                            f"{constraint_name} residual upper side stays redundant: "
                            "it starts non-negative and committed nonnegative "
                            "transactions with nonpositive coefficients can only increase it"
                        ),
                    )
                )
            upper = None
        if lower is None and upper is None:
            continue

        compact_constraints[constraint_name] = ParametricConstraint(
            name=constraint_name,
            lower=None if lower is None else lower.copy(),
            upper=None if upper is None else upper.copy(),
            coefficients={target_name: target_coefficient.copy()},
        )

    # A target with no row capacity and no finite variable upper bound would be
    # unbounded. Let the generic compiler preserve its existing diagnostics.
    if target.upper is None and not compact_constraints:
        return None

    compact = CompilerModel(
        variables={target_name: target.copy()},
        constraints=compact_constraints,
        reconstruction={requested_name: objective.copy()},
        source_variable_count=context.source_model.source_variable_count,
        parameter_defaults=context.residual_model.parameter_defaults,
        parameter_sources=context.residual_model.parameter_sources,
        parameter_domains=context.residual_model.parameter_domains,
        source_constraint_names=set(compact_constraints),
        notes=[
            (
                f"{target_name} compiled directly from its logical residual column; "
                "zero-safe junior/senior increments were never materialized in "
                "an objective-specific model"
            )
        ],
        guards=[],
        structural_proofs=structural_proofs,
        residual_increment_bases={
            target_name: context.residual_model.residual_increment_bases[target_name].copy()
        },
        uses_residual_state=True,
        directional_residual_constraints=set(
            context.residual_model.directional_residual_constraints
        ),
    )

    stats = {
        "early_direct_sequential_programs": 1,
        "early_direct_sequential_rows": len(compact_constraints),
        "early_direct_sequential_structural_sides_removed": len(structural_proofs),
    }
    return DirectScalarProgram(
        name=name,
        requested=(requested_name,),
        objective=objective,
        maximization=True,
        source_variable_count=context.source_model.source_variable_count,
        active_variable_count=1,
        stats=stats,
        model=compact,
        active_name=target_name,
    )


def _bound_shape(lower: float, upper: float) -> tuple[bool, bool, bool]:
    return isfinite(lower), isfinite(upper), isfinite(lower) and isfinite(upper) and abs(lower - upper) <= 1e-12




def _coefficient_signature(engine, constraint_name: str, variable_name: str, value: float):
    dynamic = getattr(engine, "_v2_dynamic_coefficients", {})
    metadata = dynamic.get((constraint_name, variable_name))
    if metadata is not None:
        slot, lower, upper = metadata
        return ("parameter", slot, round(float(lower), 14), round(float(upper), 14))
    return ("constant", round(float(value), 14))
def objective_signature(
    engine,
    *,
    variable_names: list[str],
    maximization: bool,
    weights: dict[str, float] | None,
) -> tuple:
    """Signature of the LP coefficient structure, excluding numeric state.

    Runtime bounds, committed lower bounds, measurements, and natural-flow
    availability are all parameters of the frozen programs.  They therefore
    must not participate in the cache key.  Likewise, inactive temporary rows
    (for example the lexicographic tie-break row) are ignored.
    """

    constraint_shape = []
    for name, constraint in engine.cons.items():
        lb = float(constraint.lb())
        ub = float(constraint.ub())
        if lb == float("-inf") and ub == float("inf"):
            continue
        dynamic = getattr(engine, "_v2_dynamic_coefficients", {})
        row_variable_names = set(constraint.coefficients) | {
            variable
            for constraint_name, variable in dynamic
            if constraint_name == name
        }
        coefficients = tuple(
            sorted(
                (
                    var,
                    _coefficient_signature(
                        engine, name, var, constraint.coefficients.get(var, 0.0)
                    ),
                )
                for var in row_variable_names
                if constraint.coefficients.get(var, 0.0) != 0
                or (name, var) in dynamic
            )
        )
        if not coefficients:
            continue
        constraint_shape.append((name, coefficients))
    return (
        tuple(variable_names),
        bool(maximization),
        tuple(sorted((weights or {}).items())),
        tuple(engine.vars),
        tuple(constraint_shape),
    )


def compile_objective(
    engine,
    *,
    name: str,
    variable_names: list[str],
    maximization: bool,
    weights: dict[str, float] | None,
    options: V2CompilationOptions,
    residual_transaction_names: set[str] | None = None,
    committed_transaction_names: set[str] | None = None,
    base_logical_model: CompilerModel | None = None,
    base_collapse_stats: dict[str, int] | None = None,
    sequential_direct_context: SequentialDirectContext | None = None,
) -> V2Program:
    if (
        options.enable_direct_scalar
        and options.enable_early_direct_sequential
        and sequential_direct_context is not None
    ):
        direct = compile_early_direct_sequential(
            name=name,
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
            context=sequential_direct_context,
        )
        if direct is not None:
            return direct

    if base_logical_model is None:
        model = CompilerModel.from_engine(engine)
        collapse_stats = collapse_transaction_path_variables(model)
    else:
        model = base_logical_model.copy()
        collapse_stats = dict(base_collapse_stats or {})

    # A scalar/objective kernel only returns the requested production values.
    # Carrying every other path reconstruction through residual rebasing and
    # presolve makes each priority pay O(total transactions) symbolic work and
    # retains dead expressions until the very end.  Prune them before any
    # objective-specific transformation; substitutions only need to update the
    # reconstructions that this program can actually return.
    requested_names = set(variable_names)
    model.reconstruction = {
        source_name: expression
        for source_name, expression in model.reconstruction.items()
        if source_name in requested_names
    }

    residual_stats: dict[str, int] = {}
    if residual_transaction_names:
        residual_stats = compile_residual_increment_ir(
            model,
            transaction_names=residual_transaction_names,
            committed_names=committed_transaction_names,
        )

    # Objective requests still use production-LP path-leg names.  Resolve them
    # through the reconstruction map so presolve protects the new logical
    # transaction variables instead of the path-leg variables that no longer
    # exist in the compiler model.
    initial_objective = model.objective_expression(variable_names, weights)
    protected = set(initial_objective.variables)
    stats = presolve(
        model,
        protected=protected,
        allow_guarded_redundancy=options.enable_guarded_redundancy,
    )
    stats.update(collapse_stats)
    stats.update(residual_stats)
    objective = model.objective_expression(variable_names, weights)
    active_objective_names = set(objective.variables)

    # A frozen objective program only needs reconstruction expressions for the
    # values it returns.  Dropping unrelated source reconstructions prevents
    # already-eliminated transactions from keeping dead runtime parameters
    # alive in the final kernel IR.
    for source_name in list(model.reconstruction):
        if source_name not in variable_names:
            model.reconstruction.pop(source_name, None)

    # Residual rebasing initially records a ``variable[...].current`` base for
    # every logical transaction.  Presolve may subsequently eliminate most of
    # those transactions (especially for a sequential scalar objective).  The
    # dead bases are not execution state: committed senior effects already live
    # in ResidualState, and reconstruction expressions retain any current-value
    # slots they actually need.  Keeping them here would make a one-variable
    # direct formula refresh O(total transactions) parameters on every priority.
    model.residual_increment_bases = {
        transaction_name: base
        for transaction_name, base in model.residual_increment_bases.items()
        if transaction_name in model.variables
    }

    if (
        options.enable_direct_scalar
        and len(active_objective_names) == 1
        and len(model.variables) == 1
    ):
        active_name = next(iter(active_objective_names))
        return DirectScalarProgram(
            name=name,
            requested=tuple(variable_names),
            objective=objective,
            maximization=maximization,
            source_variable_count=model.source_variable_count,
            active_variable_count=len(model.variables),
            stats=stats,
            model=model,
            active_name=active_name,
        )

    if not options.enable_reduced_lp_kernel:
        raise V2CannotCompile(
            f"{name} remains coupled after parameter-safe presolve: "
            f"{len(model.variables)} variables, {len(model.constraints)} rows"
        )

    return ReducedLPProgram(
        name=name,
        requested=tuple(variable_names),
        objective=objective,
        maximization=maximization,
        source_variable_count=model.source_variable_count,
        active_variable_count=len(model.variables),
        stats=stats,
        model=model,
    )



def compile_equal_priority_kernel(
    engine,
    *,
    name: str,
    member_transaction_names: list[str],
    priority: float,
    options: V2CompilationOptions,
    residual_transaction_names: set[str],
    committed_transaction_names: set[str] | None = None,
    base_logical_model: CompilerModel | None = None,
    base_collapse_stats: dict[str, int] | None = None,
) -> EqualPriorityProgram:
    """Compile one equal-priority cohort against logical residual variables.

    Unlike scalar objectives, the active subset and proportional factors can
    change during water filling.  The frozen program therefore preserves every
    cohort member as a logical transaction variable and applies the one-scalar
    common-increment inequalities numerically at runtime.  All expensive and
    structural work -- path collapse, residual rebasing, reporting-slack
    projection, and parameter-safe presolve -- is frozen here.
    """

    if base_logical_model is None:
        model = CompilerModel.from_engine(engine)
        collapse_stats = collapse_transaction_path_variables(model)
    else:
        model = base_logical_model.copy()
        collapse_stats = dict(base_collapse_stats or {})

    # EqualPriorityProgram reports logical member totals from residual bases,
    # not production path-leg reconstruction expressions.  Drop every source
    # reconstruction before rebasing so large cohorts do not update hundreds
    # of expressions that can never be read by the frozen program.
    model.reconstruction.clear()

    residual_stats = compile_residual_increment_ir(
        model,
        transaction_names=residual_transaction_names,
        committed_names=committed_transaction_names,
    )

    members = tuple(dict.fromkeys(member_transaction_names))
    missing = [member for member in members if member not in model.variables]
    if missing:
        raise V2CannotCompile(
            "Equal-priority cohort could not be represented by logical "
            "transaction variables: " + ", ".join(missing)
        )

    stats = presolve(
        model,
        protected=set(members),
        allow_guarded_redundancy=options.enable_guarded_redundancy,
    )
    stats.update(collapse_stats)
    stats.update(residual_stats)
    stats["equal_priority_logical_kernels"] = 1
    stats["equal_priority_logical_members"] = len(members)

    # Cohort execution returns logical transaction totals directly and never
    # reconstructs individual path legs.  Remove those dead expressions so
    # they cannot keep path-leg-only runtime parameters alive.
    for source_name in list(model.reconstruction):
        if source_name not in members:
            model.reconstruction.pop(source_name, None)

    # Only cohort members need absolute-value reconstruction at runtime. The
    # residual compiler initially records bases for every active transaction,
    # but retaining those dead bases would force each tiny cohort kernel to
    # refresh hundreds of unrelated ``variable[...].current`` parameters.
    model.residual_increment_bases = {
        member: model.residual_increment_bases[member]
        for member in members
        if member in model.residual_increment_bases
    }

    return EqualPriorityProgram(
        name=name,
        requested=members,
        objective=model.objective_expression([]),
        maximization=True,
        source_variable_count=model.source_variable_count,
        active_variable_count=len(model.variables),
        stats=stats,
        model=model,
        member_ids=members,
        priority=float(priority),
    )
