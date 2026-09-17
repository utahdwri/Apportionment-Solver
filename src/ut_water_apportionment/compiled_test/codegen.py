"""Python source generation for the frozen v2 execution program.

The compiler/debug IR intentionally keeps semantic names.  This module lowers
that already-frozen program one final time into ordinary Python statements over
the indexed parameter/residual layouts.  Direct scalar priorities become plain
``min``/``max`` arithmetic and residual-array updates; equal-priority and
coupled cases call their prebuilt frozen kernels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import inf, isfinite
from typing import Callable

from ..models import PathTrxn, TrxnGroup, ZoneTypes
from ..lp_solver import LPSolverError
from .execution_ir import (
    AssignTransaction,
    DeriveReportingSlacks,
    FinalDerivedOutputs,
    ResidualEffect,
    ResidualState,
    V2ExecutionContext,
)
from .program import DirectScalarProgram, V2Program


@dataclass(frozen=True)
class _GeneratedDirectSpec:
    var: PathTrxn | TrxnGroup
    target: str
    program: DirectScalarProgram
    effects: tuple[ResidualEffect, ...]
    layout_index: int


@dataclass
class GeneratedRegimeRuntime:
    """Objects referenced by one generated regime function.

    The source contains arithmetic and integer offsets; immutable Python objects
    that cannot sensibly be embedded as source literals live here.
    """

    session: object
    effects: dict[str, tuple[ResidualEffect, ...]]
    slack_rules: tuple
    path_reconstruction: object
    residual_layout: object
    residual_parameter_layout: object | None
    vars: tuple[PathTrxn | TrxnGroup, ...]
    parameter_layouts: tuple[object, ...]
    needs_second_pass: bool

    def start_context(self, apportioner) -> V2ExecutionContext:
        residual_parameters = (
            None
            if self.residual_parameter_layout is None
            else self.residual_parameter_layout.read(apportioner.engine)
        )
        context = V2ExecutionContext(
            apportioner=apportioner,
            residual_state=ResidualState.from_engine(
                apportioner.engine, self.residual_layout
            ),
            effects=self.effects,
            residual_parameters=residual_parameters,
        )
        engine = apportioner.engine
        engine.v2_residual_state = context.residual_state
        engine.v2_residual_effects = self.effects
        engine.v2_residual_parameters = residual_parameters
        return context

    def execute_assignment(self, context: V2ExecutionContext, var_index: int) -> None:
        var = self.vars[var_index]
        AssignTransaction(var, self.effects.get(var.id, ())).execute(context)

    @staticmethod
    def execute_equal_priority(context: V2ExecutionContext, item) -> None:
        # ``item`` may be CorePropSchedule or the nested CoreSeqSchedule used
        # when a cohort mixes unlimited and limited members.  Apportioner owns
        # that water-filling control flow; its numeric solves are frozen v2
        # EqualPriorityProgram kernels.
        context.apportioner.maximize_series(item)

    def derive_slacks(self, context: V2ExecutionContext) -> None:
        DeriveReportingSlacks(
            self.slack_rules, apply_spill_credit=True
        ).execute(context)

    def final_outputs(self, context: V2ExecutionContext) -> None:
        FinalDerivedOutputs(
            self.slack_rules, self.path_reconstruction
        ).execute(context)


@dataclass
class GeneratedDayExecutor:
    """Compiled Python functions for every frozen coefficient regime."""

    source: str
    functions: dict[tuple, Callable]
    runtimes: dict[tuple, GeneratedRegimeRuntime]
    direct_assignments: int = 0
    kernel_calls: int = 0

    @staticmethod
    def _for_regime(mapping: dict, signature: tuple):
        value = mapping.get(signature)
        if value is not None:
            return value
        if len(mapping) == 1:
            return next(iter(mapping.values()))
        raise KeyError("No generated Python regime for runtime LP structure")

    def execute(self, apportioner, *, schedule, regime_signature: tuple) -> None:
        fn = self._for_regime(self.functions, regime_signature)
        runtime = self._for_regime(self.runtimes, regime_signature)
        apportioner.engine.v2_regime_signature = regime_signature
        fn(apportioner, schedule, runtime)
        apportioner.engine.v2_session.stats["execution_generated_python_days"] += 1


def _strict_sign(expression, model, *, tolerance: float = 1e-12) -> int | None:
    """Return a sign only when the expression is structurally away from zero."""

    low, high = expression.interval(model.parameter_domains, {})
    if low > tolerance:
        return 1
    if high < -tolerance:
        return -1
    return None


def _indexed_expr_python(expression, slot_expressions: dict[int, str]) -> str:
    """Render one indexed expression after resolving slot indices to Python."""

    if expression.op == "affine":
        pieces: list[str] = []
        if abs(expression.constant) > 1e-15 or not expression.indices:
            pieces.append(repr(float(expression.constant)))
        for index, coefficient in zip(expression.indices, expression.coefficients):
            term = slot_expressions[index]
            if abs(coefficient - 1.0) <= 1e-15:
                pieces.append(term)
            elif abs(coefficient + 1.0) <= 1e-15:
                pieces.append(f"-({term})")
            else:
                pieces.append(f"({coefficient!r} * ({term}))")
        return " + ".join(pieces).replace("+ -", "- ") or "0.0"
    left = _indexed_expr_python(expression.args[0], slot_expressions)
    right = _indexed_expr_python(expression.args[1], slot_expressions)
    symbol = {"add": "+", "mul": "*", "div": "/"}[expression.op]
    return f"({left} {symbol} {right})"


def _symbolic_python(expression, active_name: str, active_local: str, slot_expressions: dict[int, str]) -> str:
    pieces: list[str] = []
    for name, coefficient in expression.variables:
        if name != active_name:
            raise ValueError(
                "generated direct reconstruction contains another active variable"
            )
        pieces.append(
            f"({_indexed_expr_python(coefficient, slot_expressions)}) * {active_local}"
        )
    parameter_text = _indexed_expr_python(expression.parameters, slot_expressions)
    if parameter_text != "0.0" or not pieces:
        pieces.append(parameter_text)
    return " + ".join(pieces).replace("+ -", "- ")


def _resolved_slot_expressions(program: DirectScalarProgram, residual_layout, statement_index: int):
    """Resolve local parameter slots to direct residual/bound expressions.

    This is the final #9 -> #10 lowering boundary. Source-constraint slots no
    longer get copied into a per-objective parameter array; they become direct
    integer residual-array references in generated Python.
    """

    slot_expressions: dict[int, str] = {}
    prelude: list[str] = []
    variable_locals: dict[str, tuple[str, str]] = {}
    constraint_locals: dict[str, tuple[str, str]] = {}

    for reader in program._parameter_layout.readers:
        kind = reader[0]
        index = reader[1]
        if kind == "source_constraint":
            _, _, name, side = reader
            residual_index = residual_layout.index.get(name)
            if residual_index is None:
                raise ValueError(f"generated source constraint {name!r} lacks residual slot")
            if side == "remaining":
                slot_expressions[index] = (
                    f"(0.5 * (r_lower[{residual_index}] + r_upper[{residual_index}]))"
                )
            elif side == "remaining_lower":
                slot_expressions[index] = f"r_lower[{residual_index}]"
            elif side == "remaining_upper":
                slot_expressions[index] = f"r_upper[{residual_index}]"
            else:
                raise KeyError(f"unknown source-constraint side {side!r}")
        elif kind == "variable":
            _, _, name, side = reader
            locals_pair = variable_locals.get(name)
            if locals_pair is None:
                local_no = len(variable_locals)
                lb = f"vb_{statement_index}_{local_no}_lb"
                ub = f"vb_{statement_index}_{local_no}_ub"
                variable_locals[name] = (lb, ub)
                prelude.append(f"{lb}, {ub} = engine.get_variable_bounds({name!r})")
                locals_pair = (lb, ub)
            lb, ub = locals_pair
            if side == "current":
                prelude.append(
                    f"if abs({lb} - {ub}) > 1e-8: raise ValueError('generated frozen current variable is not fixed')"
                )
                slot_expressions[index] = f"(0.5 * ({lb} + {ub}))"
            elif side == "remaining_lower":
                slot_expressions[index] = lb
            elif side == "remaining_upper":
                slot_expressions[index] = ub
            else:
                raise KeyError(f"unknown variable side {side!r}")
        elif kind == "constraint":
            _, _, name, side = reader
            locals_pair = constraint_locals.get(name)
            if locals_pair is None:
                local_no = len(constraint_locals)
                lb = f"cb_{statement_index}_{local_no}_lb"
                ub = f"cb_{statement_index}_{local_no}_ub"
                constraint_locals[name] = (lb, ub)
                prelude.append(f"{lb}, {ub} = engine.get_constraint_bounds({name!r})")
                locals_pair = (lb, ub)
            lb, ub = locals_pair
            if side == "remaining":
                prelude.append(
                    f"if abs({lb} - {ub}) > 1e-8: raise ValueError('generated frozen equality constraint is not fixed')"
                )
                slot_expressions[index] = f"(0.5 * ({lb} + {ub}))"
            elif side == "remaining_lower":
                slot_expressions[index] = lb
            elif side == "remaining_upper":
                slot_expressions[index] = ub
            else:
                raise KeyError(f"unknown constraint side {side!r}")
        elif kind == "constant":
            slot_expressions[index] = repr(float(reader[2]))
        elif kind == "coefficient":
            raise ValueError("dynamic coefficient direct codegen is not enabled")
        else:
            raise KeyError(f"unknown indexed runtime reader {kind!r}")

    return prelude, slot_expressions

def _direct_codegen_eligible(program: DirectScalarProgram) -> bool:
    model = program.model
    if model.guards:
        return False
    # Keep the existing conservative boundary for time-varying transformed
    # matrix coefficients.  Those uncommon cases continue through the prebuilt
    # exact kernel path until their coefficient algebra is fully direct-lowered.
    if any(reader[0] == "coefficient" for reader in model._runtime_readers()):
        return False
    objective_coefficient = program.objective.variables.get(program.active_name)
    if objective_coefficient is None or _strict_sign(objective_coefficient, model) is None:
        return False
    for constraint in model.constraints.values():
        coefficient = constraint.coefficients.get(program.active_name)
        if coefficient is None or coefficient.is_constant(0.0):
            continue
        if _strict_sign(coefficient, model) is None:
            return False
    reconstruction = program._indexed_reconstruction.get(program.requested[0])
    if reconstruction is None:
        return False
    return all(name == program.active_name for name, _coefficient in reconstruction.variables)


def _candidate_python(program: DirectScalarProgram, slot_expressions: dict[int, str]):
    """Return executable lower/upper interval candidates for a direct scalar."""

    lowers: list[str] = []
    uppers: list[str] = []
    if program._indexed_lower is not None:
        lowers.append(_indexed_expr_python(program._indexed_lower, slot_expressions))
    if program._indexed_upper is not None:
        uppers.append(_indexed_expr_python(program._indexed_upper, slot_expressions))

    indexed_by_name = {
        name: (lower, upper, coefficient)
        for name, lower, upper, coefficient in program._indexed_constraints
    }
    for name, constraint in program.model.constraints.items():
        coefficient = constraint.coefficients.get(program.active_name)
        if coefficient is None or coefficient.is_constant(0.0):
            continue
        sign = _strict_sign(coefficient, program.model)
        if sign is None:
            raise ValueError("non-strict coefficient sign in generated scalar")
        lower_expr, upper_expr, coefficient_expr = indexed_by_name[name]
        if coefficient_expr is None:
            continue
        coef = _indexed_expr_python(coefficient_expr, slot_expressions)
        if sign > 0:
            if lower_expr is not None:
                lowers.append(
                    f"({_indexed_expr_python(lower_expr, slot_expressions)}) / ({coef})"
                )
            if upper_expr is not None:
                uppers.append(
                    f"({_indexed_expr_python(upper_expr, slot_expressions)}) / ({coef})"
                )
        else:
            if upper_expr is not None:
                lowers.append(
                    f"({_indexed_expr_python(upper_expr, slot_expressions)}) / ({coef})"
                )
            if lower_expr is not None:
                uppers.append(
                    f"({_indexed_expr_python(lower_expr, slot_expressions)}) / ({coef})"
                )
    return lowers, uppers

def _aggregate(name: str, values: list[str], *, default: str) -> list[str]:
    if not values:
        return [f"{name} = {default}"]
    if len(values) == 1:
        return [f"{name} = {values[0]}"]
    fn = "max" if name.startswith("lower") else "min"
    lines = [f"{name} = {fn}("]
    lines.extend(f"    {value}," for value in values)
    lines.append(")")
    return lines


def _effect_coefficient_python(effect: ResidualEffect) -> str:
    if effect.coefficient.is_constant():
        return repr(float(effect.coefficient.constant_value_number()))
    if effect.indexed_coefficient is None:
        raise ValueError(
            "parameterized residual coefficient was not lowered to indexed IR"
        )
    return effect.indexed_coefficient.python_text(array_name="rp")


def _render_direct_assignment(
    spec: _GeneratedDirectSpec,
    *,
    statement_index: int,
    var_index: int,
    residual_layout,
) -> list[str]:
    program = spec.program
    lower_name = f"lower_{statement_index}"
    upper_name = f"upper_{statement_index}"
    active_name = f"active_{statement_index}"
    before_name = f"before_{statement_index}"
    value_name = f"value_{statement_index}"
    delta_name = f"delta_{statement_index}"

    prelude, slot_expressions = _resolved_slot_expressions(
        program, residual_layout, statement_index
    )
    lowers, uppers = _candidate_python(program, slot_expressions)
    objective_coefficient = program.objective.variables[program.active_name]
    objective_sign = _strict_sign(objective_coefficient, program.model)
    maximize_active = program.maximization == (objective_sign > 0)
    requested = program._indexed_reconstruction[spec.target]
    requested_text = _symbolic_python(
        requested, program.active_name, active_name, slot_expressions
    )

    lines: list[str] = []
    lines.append(f"# {spec.var.id}: generated from {program.name}")
    for formula_line in program.assignment_lines():
        lines.append(f"# {formula_line}")
    if program.parameter_slot_names:
        slots = ", ".join(
            f"{i}={name} -> {slot_expressions[i]}"
            for i, name in enumerate(program.parameter_slot_names)
        )
        lines.append(f"# resolved slots: {slots}")
    lines.append("try:")
    lines.extend(f"    {line}" for line in prelude)
    for line in _aggregate(lower_name, lowers, default="-inf"):
        lines.append("    " + line)
    for line in _aggregate(upper_name, uppers, default="inf"):
        lines.append("    " + line)
    lines.extend(
        [
            f"    scale_{statement_index} = max(1.0, abs({lower_name}) if isfinite({lower_name}) else 1.0, abs({upper_name}) if isfinite({upper_name}) else 1.0)",
            f"    if {upper_name} < {lower_name} - 1e-9 * scale_{statement_index}:",
            f"        raise LPSolverError('generated v2 direct scalar interval is infeasible')",
            f"    if {upper_name} < {lower_name}:",
            f"        middle_{statement_index} = 0.5 * ({lower_name} + {upper_name})",
            f"        {lower_name} = {upper_name} = middle_{statement_index}",
            f"    {active_name} = {'%s' % upper_name if maximize_active else '%s' % lower_name}",
            f"    {value_name} = {requested_text}",
            f"except (LPSolverError, ValueError, KeyError):",
            f"    runtime.execute_assignment(context, {var_index})",
            f"else:",
            f"    {before_name} = cur.get({spec.target!r}, 0.0)",
            f"    cur[{spec.target!r}] = {value_name}",
            f"    engine.solve_count += 1",
            f"    stats['execution_objective_calls'] += 1",
            f"    stats['execution_scalar_direct_index_hits'] += 1",
            f"    stats['execution_cache_hits'] += 1",
            f"    stats['execution_generated_direct_assignments'] += 1",
            f"    engine._last_solution_values[{spec.target!r}] = {value_name}",
            f"    engine.update_variable_bounds({spec.target!r}, lb={value_name})",
            f"    {delta_name} = {value_name} - {before_name}",
        ]
    )
    effects = spec.effects
    if effects:
        lines.append(f"    if abs({delta_name}) > 1e-15:")
        for effect in effects:
            if effect.constraint_index is None:
                raise ValueError("generated residual effect lacks frozen constraint index")
            coefficient = _effect_coefficient_python(effect)
            amount = (
                delta_name
                if coefficient == "1.0"
                else f"({coefficient}) * {delta_name}"
            )
            if effect.has_lower:
                lines.append(
                    f"        if isfinite(r_lower[{effect.constraint_index}]): r_lower[{effect.constraint_index}] -= {amount}"
                )
            if effect.has_upper:
                lines.append(
                    f"        if isfinite(r_upper[{effect.constraint_index}]): r_upper[{effect.constraint_index}] -= {amount}"
                )
        lines.append("        stats['execution_residual_commits'] += 1")
        lines.append(
            f"        stats['execution_residual_row_updates'] += {len(effects)}"
        )
    lines.append(
        f"    apportioner._apply_natural_flow_change(runtime.vars[{var_index}], {delta_name})"
    )
    return lines


def _indent(lines: list[str], spaces: int) -> list[str]:
    pad = " " * spaces
    return [pad + line if line else "" for line in lines]


def build_generated_day_executor(
    *,
    session,
    execution_program,
    trxn_manager,
    graph_manager,
) -> GeneratedDayExecutor:
    """Generate and ``exec`` the frozen day routine for every regime."""

    from ..apportioner import SLACK_TRXN_PRIORITY
    from ..models import CoreScheduleVariable

    all_source: list[str] = [
        "# Generated by compiled-v2. This is executable frozen execution IR.",
        "# Named formulas remain available through plan.formulas(); this source",
        "# is the lowered runtime form over indexed parameter/residual arrays.",
        "",
    ]
    functions: dict[tuple, Callable] = {}
    runtimes: dict[tuple, GeneratedRegimeRuntime] = {}
    total_direct = 0
    total_kernel = 0

    # Top-level priority group membership/order is structural.  Build a single
    # representative schedule only to identify the immutable transaction object
    # for unique-priority entries; equal-priority factors remain runtime data.
    template_schedule = trxn_manager.build_schedule(
        getattr(trxn_manager, "_prepared_date", None) or "1000-01-01"
    )

    for regime_index, signature in enumerate(execution_program.effects_by_regime):
        effects = execution_program.effects_for(signature)
        slack_rules = execution_program.slack_rules_for(signature)
        path_reconstruction = execution_program.path_reconstruction_for(signature)
        residual_layout = execution_program.residual_layout_for(signature)
        residual_parameter_layout = execution_program.residual_parameter_layout_for(signature)
        needs_second_pass = any(
            zone.type == ZoneTypes.STORAGE
            for zone in graph_manager.graph.zones
        ) or any(
            member.spill_to_natural
            for rule in slack_rules
            for member in rule.members
        )

        vars_list: list[PathTrxn | TrxnGroup] = []
        layouts: list[object] = []
        pass_lines: list[str] = []
        statement_index = 0

        for schedule_index, entry in enumerate(template_schedule.series):
            if entry.priority >= SLACK_TRXN_PRIORITY:
                break
            item = entry.item
            if isinstance(item, CoreScheduleVariable):
                var = item.var
                target = trxn_manager.get_anchor_var(var) if isinstance(var, PathTrxn) else var.id
                if not target:
                    continue
                var_index = len(vars_list)
                vars_list.append(var)
                variants = session._scalar_by_regime_target.get((signature, target), [])
                if not variants and len(execution_program.effects_by_regime) == 1:
                    # Objective signatures may have been deduplicated across a
                    # parameterized coefficient fingerprint; use the only frozen
                    # scalar bucket for this target in that case.
                    matches = [
                        bucket
                        for (regime, candidate_target), bucket in session._scalar_by_regime_target.items()
                        if candidate_target == target
                    ]
                    if len(matches) == 1:
                        variants = matches[0]
                sorted_variants = sorted(variants, key=session._scalar_variant_sort_key)
                direct = next(
                    (
                        program
                        for program in sorted_variants
                        if isinstance(program, DirectScalarProgram)
                        and _direct_codegen_eligible(program)
                        and not trxn_manager.get_minus_vars([var])
                    ),
                    None,
                )
                if direct is not None:
                    layout_index = len(layouts)
                    layouts.append(direct._parameter_layout)
                    spec = _GeneratedDirectSpec(
                        var=var,
                        target=target,
                        program=direct,
                        effects=effects.get(var.id, ()),
                        layout_index=layout_index,
                    )
                    pass_lines.extend(
                        _render_direct_assignment(
                            spec,
                            statement_index=statement_index,
                            var_index=var_index,
                            residual_layout=residual_layout,
                        )
                    )
                    pass_lines.append("")
                    total_direct += 1
                else:
                    program_names = ", ".join(p.name for p in sorted_variants) or "auxiliary kernel"
                    pass_lines.append(
                        f"# {var.id}: prebuilt frozen kernel ({program_names})"
                    )
                    pass_lines.append(
                        f"runtime.execute_assignment(context, {var_index})"
                    )
                    pass_lines.append("")
                    total_kernel += 1
                statement_index += 1
            else:
                # Equal-priority proportions/limited-vs-unlimited nesting are
                # day-dependent. The generated schedule calls the already-frozen
                # logical cohort kernels through Apportioner using today's item.
                pass_lines.append(
                    f"# priority {entry.priority:g}: prebuilt equal-priority logical kernel"
                )
                pass_lines.append(
                    f"runtime.execute_equal_priority(context, schedule.series[{schedule_index}].item)"
                )
                pass_lines.append("")
                total_kernel += 1

        runtime = GeneratedRegimeRuntime(
            session=session,
            effects=effects,
            slack_rules=slack_rules,
            path_reconstruction=path_reconstruction,
            residual_layout=residual_layout,
            residual_parameter_layout=residual_parameter_layout,
            vars=tuple(vars_list),
            parameter_layouts=tuple(layouts),
            needs_second_pass=needs_second_pass,
        )
        runtimes[signature] = runtime

        fn_name = f"execute_regime_{regime_index}"
        source_lines = [
            f"def {fn_name}(apportioner, schedule, runtime):",
            "    engine = apportioner.engine",
            "    context = runtime.start_context(apportioner)",
            "    cur = apportioner.cur_trxn_value",
            "    stats = engine.v2_session.stats",
            "    r_lower = context.residual_state.lower",
            "    r_upper = context.residual_state.upper",
            "    rp = context.residual_parameters",
            "",
            "    def allocation_pass():",
        ]
        if pass_lines:
            source_lines.extend(_indent(pass_lines, 8))
        else:
            source_lines.append("        pass")
        source_lines.extend(
            [
                "",
                "    allocation_pass()",
                "    runtime.derive_slacks(context)",
                "    if runtime.needs_second_pass:",
                "        allocation_pass()",
                "    runtime.final_outputs(context)",
                "",
            ]
        )
        all_source.extend(source_lines)

    source = "\n".join(all_source).rstrip() + "\n"
    namespace = {
        "LPSolverError": LPSolverError,
        "inf": inf,
        "isfinite": isfinite,
    }
    exec(compile(source, "<compiled-v2-generated>", "exec"), namespace)
    for regime_index, signature in enumerate(execution_program.effects_by_regime):
        functions[signature] = namespace[f"execute_regime_{regime_index}"]

    session.stats["generated_python_regimes"] = len(functions)
    session.stats["generated_python_direct_assignments"] = total_direct
    session.stats["generated_python_kernel_calls"] = total_kernel
    return GeneratedDayExecutor(
        source=source,
        functions=functions,
        runtimes=runtimes,
        direct_assignments=total_direct,
        kernel_calls=total_kernel,
    )
