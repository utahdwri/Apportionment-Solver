"""Objective compiler for the experimental v2 backend."""

from __future__ import annotations

from dataclasses import dataclass

from .model import CompilerModel
from .program import DirectScalarProgram, ReducedLPProgram, V2Program
from .transforms import presolve


@dataclass(frozen=True)
class V2CompilationOptions:
    """Initial v2 policy.

    V2 intentionally has no whole-day LP fallback.  Every objective is reduced
    independently; a simple one becomes a direct scalar equation and a coupled
    one becomes a small residual LP kernel.
    """

    enable_direct_scalar: bool = True
    enable_reduced_lp_kernel: bool = True


class V2CannotCompile(RuntimeError):
    pass


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
    stats = presolve(model, protected=protected)
    objective = model.objective_expression(variable_names, weights)
    active_objective_names = set(objective.coefficients)

    # If the requested objective itself collapsed to a constant, the kernel is
    # still a convenient feasibility check and reconstruction mechanism.
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
            f"{name} remains coupled after presolve: "
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
