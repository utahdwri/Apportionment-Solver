"""Objective compiler and structural signatures for frozen v2 programs."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .model import CompilerModel
from .program import DirectScalarProgram, ReducedLPProgram, V2Program
from .transforms import presolve


@dataclass(frozen=True)
class V2CompilationOptions:
    enable_direct_scalar: bool = True
    enable_reduced_lp_kernel: bool = True
    freeze_after_prepare: bool = True
    enable_guarded_redundancy: bool = True


class V2CannotCompile(RuntimeError):
    pass


def _bound_shape(lower: float, upper: float) -> tuple[bool, bool, bool]:
    return isfinite(lower), isfinite(upper), isfinite(lower) and isfinite(upper) and abs(lower - upper) <= 1e-12


def objective_signature(
    engine,
    *,
    variable_names: list[str],
    maximization: bool,
    weights: dict[str, float] | None,
) -> tuple:
    """Signature of the LP *structure*, excluding numeric bound values."""

    variable_shape = tuple(
        (name, isfinite(float(variable.lb())), isfinite(float(variable.ub())))
        for name, variable in engine.vars.items()
    )
    constraint_shape = tuple(
        (
            name,
            *_bound_shape(float(constraint.lb()), float(constraint.ub())),
            tuple(sorted((var, round(float(coef), 14)) for var, coef in constraint.coefficients.items() if coef != 0)),
        )
        for name, constraint in engine.cons.items()
    )
    dynamic = tuple(sorted(getattr(engine, "_v2_dynamic_bound_sides", set())))
    return (
        tuple(variable_names),
        bool(maximization),
        tuple(sorted((weights or {}).items())),
        variable_shape,
        constraint_shape,
        dynamic,
    )


def compile_objective(
    engine,
    *,
    name: str,
    variable_names: list[str],
    maximization: bool,
    weights: dict[str, float] | None,
    options: V2CompilationOptions,
) -> V2Program:
    model = CompilerModel.from_engine(engine)
    protected = set(variable_names)
    stats = presolve(
        model,
        protected=protected,
        allow_guarded_redundancy=options.enable_guarded_redundancy,
    )
    objective = model.objective_expression(variable_names, weights)
    active_objective_names = set(objective.variables)

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
