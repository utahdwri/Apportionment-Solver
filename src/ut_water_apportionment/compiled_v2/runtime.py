"""Frozen parameterized v2 LP backend.

Preparation compiles each encountered structural objective once.  Numeric LP
bounds are represented by parameter slots inside the frozen IR, so later days
reuse the same transformed equations/kernels and refresh only bound/RHS values.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Callable

from .lp_engine import LPSolver as ScipyLPSolver
from .compiler import (
    V2CannotCompile,
    V2CompilationOptions,
    compile_objective,
    objective_signature,
)
from .program import V2Program


@dataclass
class V2CompilationSession:
    options: V2CompilationOptions = field(default_factory=V2CompilationOptions)
    programs: list[V2Program] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)
    _cache: dict[tuple, list[V2Program]] = field(default_factory=dict, repr=False)
    frozen: bool = False
    preparing: bool = False

    def next_name(self) -> str:
        return f"V2P{len(self.programs) + 1}"

    def begin_preparation(self) -> None:
        self.preparing = True
        self.frozen = False
        self.stats.clear()

    def finish_preparation(self) -> None:
        self.preparing = False
        if self.options.freeze_after_prepare:
            self.frozen = True
        self.stats["prepared_program_count"] = len(self.programs)

    def reset_execution_stats(self) -> None:
        for key in list(self.stats):
            if key.startswith("execution_"):
                del self.stats[key]

    @staticmethod
    def _program_applicable(program: V2Program, engine) -> bool:
        model = getattr(program, "model", None)
        if model is None:
            return True
        try:
            parameters = model.runtime_parameters(engine)
            model.check_guards(parameters)
        except (ValueError, KeyError):
            return False
        return True

    def _record_program(self, signature: tuple, program: V2Program) -> V2Program:
        self._cache.setdefault(signature, []).append(program)
        self.programs.append(program)
        self.stats["compiled_programs"] += 1
        if program.__class__.__name__ == "DirectScalarProgram":
            self.stats["direct_programs"] += 1
        else:
            self.stats["reduced_lp_kernels"] += 1
        for key, value in program.stats.items():
            self.stats[key] += value
        return program

    def resolve(
        self,
        engine,
        *,
        variable_names: list[str],
        maximization: bool,
        weights: dict[str, float] | None,
    ) -> V2Program:
        signature = objective_signature(
            engine,
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
        )
        variants = self._cache.get(signature, [])
        for program in variants:
            if self._program_applicable(program, engine):
                if self.preparing:
                    self.stats["preparation_cache_hits"] += 1
                else:
                    self.stats["execution_cache_hits"] += 1
                return program

        if self.frozen:
            self.stats["execution_cache_misses"] += 1
            raise V2CannotCompile(
                "Frozen v2 plan encountered a parameter region/objective "
                "structure that was not seen during preparation. Recompile "
                "the plan for this schedule/parameter region."
            )

        # The first variant may use guarded regional simplifications to expose
        # compact spreadsheet-like equations. If a later preparation state
        # violates those guards, compile one conservative unguarded variant for
        # the same structural objective. Runtime then selects the applicable
        # frozen variant without compiling anything new.
        options = self.options
        if variants:
            options = replace(options, enable_guarded_redundancy=False)
            self.stats["prepared_unguarded_variants"] += 1

        program = compile_objective(
            engine,
            name=self.next_name(),
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
            options=options,
        )
        return self._record_program(signature, program)

    def formulas(self) -> str:
        lines = [
            "COMPILED V2 — FROZEN PARAMETERIZED IR",
            "======================================",
            "",
            "SolverInput -> production LP -> parameterized compiler LP ->",
            "frozen algebraic substitutions/presolve -> direct equation or reduced LP kernel.",
            "",
            "Numeric variable/constraint bounds are runtime parameters. The coefficient",
            "matrix and compiler substitutions below are frozen during preparation.",
            "No objective compilation occurs during plan.solve().",
            "",
        ]
        if not self.programs:
            lines.append("(no objective structures were encountered during preparation)")
        for program in self.programs:
            lines.append(program.text())
            lines.append("")
        return "\n".join(lines).rstrip()

    def report(self) -> dict:
        return {
            "program_count": len(self.programs),
            "prepared_program_count": len(self.programs),
            "direct_programs": self.stats["direct_programs"],
            "reduced_lp_kernels": self.stats["reduced_lp_kernels"],
            "equality_eliminated": self.stats["equality_eliminated"],
            "fixed_eliminated": self.stats["fixed_eliminated"],
            "slack_eliminated": self.stats["slack_eliminated"],
            "monotone_eliminated": self.stats["monotone_eliminated"],
            "whole_day_lp_fallbacks": 0,
            "runtime_compilation": not self.frozen,
            "frozen": self.frozen,
            "preparation_cache_hits": self.stats["preparation_cache_hits"],
            "prepared_unguarded_variants": self.stats["prepared_unguarded_variants"],
            "execution_cache_hits": self.stats["execution_cache_hits"],
            "execution_cache_misses": self.stats["execution_cache_misses"],
        }


class V2LPSolver(ScipyLPSolver):
    """LP protocol implementation backed by frozen v2 objective programs."""

    def __init__(self, *, session: V2CompilationSession, tolerance: float | None = None):
        super().__init__(tolerance=tolerance, method="highs-ds", presolve=True)
        self.v2_session = session
        # Marks constructor-time bounds that later become runtime state. The
        # parameterized model uses this to distinguish a structural zero from a
        # measurement/current-allocation value that happens to be zero today.
        self._v2_dynamic_bound_sides: set[tuple[str, str, str]] = set()
        # Keep the compiled execution sequence structural rather than dependent
        # on today's NF exhaustion. A zero-capacity formula simply returns the
        # already committed value.
        self.force_explicit_priority_solves = True

    def update_variable_bounds(self, name: str, lb: float | None = None, ub: float | None = None) -> None:
        current_lb, current_ub = self.get_variable_bounds(name)
        if lb is not None and (
            abs(lb - current_lb) > 1e-15 or name in self._last_solution_values
        ):
            self._v2_dynamic_bound_sides.add(("variable", name, "lower"))
        if ub is not None and (
            abs(ub - current_ub) > 1e-15 or name in self._last_solution_values
        ):
            self._v2_dynamic_bound_sides.add(("variable", name, "upper"))
        super().update_variable_bounds(name, lb=lb, ub=ub)

    def update_constraint_ub(self, name: str, ub: float | None = None) -> None:
        from math import inf
        current = self.get_constraint_bounds(name)[1]
        incoming = inf if ub is None else ub
        if incoming != current:
            self._v2_dynamic_bound_sides.add(("constraint", name, "upper"))
        super().update_constraint_ub(name, ub=ub)

    def update_constraint_lb(self, name: str, lb: float | None = None) -> None:
        from math import inf
        current = self.get_constraint_bounds(name)[0]
        incoming = -inf if lb is None else lb
        if incoming != current:
            self._v2_dynamic_bound_sides.add(("constraint", name, "lower"))
        super().update_constraint_lb(name, lb=lb)

    def solve_objective(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> tuple[float, dict[str, float]]:
        program = self.v2_session.resolve(
            self,
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
        )
        result = program.execute(self)
        self.solve_count += 1
        if self.v2_session.preparing:
            self.v2_session.stats["preparation_objective_calls"] += 1
        else:
            self.v2_session.stats["execution_objective_calls"] += 1
        self._last_solution_values.update(result.requested_values)
        return result.objective_value, result.requested_values

    def get_last_variable_reduced_cost(self, variable_name: str) -> float | None:
        return None

    def get_last_solve_constraint_evidence(self, variable_name: str, tolerance: float = 1e-6) -> list[dict]:
        return []


def v2_factory(session: V2CompilationSession) -> Callable[..., V2LPSolver]:
    def factory(*, tolerance=None, **_kwargs):
        return V2LPSolver(session=session, tolerance=tolerance)

    return factory
