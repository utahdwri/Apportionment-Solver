"""Experimental v2 LP backend.

The accounting code still constructs the ordinary production LP.  Each
objective is copied into a compiler-owned model, transformed there, and then
executed either as a direct equation or as a reduced LP kernel.  There is no
whole-day LP restart in this backend.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from math import inf
from typing import Callable

from ..lp_solver_SCIPY import LPSolver as ScipyLPSolver
from .compiler import V2CompilationOptions, compile_objective
from .program import V2Program


@dataclass
class V2CompilationSession:
    options: V2CompilationOptions = field(default_factory=V2CompilationOptions)
    programs: list[V2Program] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)

    def next_name(self) -> str:
        return f"V2P{len(self.programs) + 1}"

    def record(self, program: V2Program) -> None:
        self.programs.append(program)
        self.stats["objective_calls"] += 1
        if program.__class__.__name__ == "DirectScalarProgram":
            self.stats["direct_programs"] += 1
        else:
            self.stats["reduced_lp_kernels"] += 1
        for key, value in program.stats.items():
            self.stats[key] += value

    def formulas(self) -> str:
        lines = [
            "COMPILED V2 — INITIAL BRANCH",
            "============================",
            "",
            "SolverInput -> production LP -> compiler-owned LP ->",
            "algebraic substitutions/presolve -> direct equation or reduced LP kernel.",
            "",
            "V2 does not restart the whole day with the production LP solver.",
            "Coupled objectives are solved only as transformed residual kernels.",
            "",
        ]
        if not self.programs:
            lines.append("(no objectives have been compiled yet)")
        for program in self.programs:
            lines.append(program.text())
            lines.append("")
        return "\n".join(lines).rstrip()

    def report(self) -> dict:
        return {
            "program_count": len(self.programs),
            "direct_programs": self.stats["direct_programs"],
            "reduced_lp_kernels": self.stats["reduced_lp_kernels"],
            "equality_eliminated": self.stats["equality_eliminated"],
            "fixed_eliminated": self.stats["fixed_eliminated"],
            "slack_eliminated": self.stats["slack_eliminated"],
            "redundant_sides_removed": self.stats["redundant_sides_removed"],
            "monotone_eliminated": self.stats["monotone_eliminated"],
            "whole_day_lp_fallbacks": 0,
            "runtime_compilation": True,
        }


class V2LPSolver(ScipyLPSolver):
    """Drop-in LP protocol implementation backed by the v2 compiler."""

    def __init__(
        self,
        *,
        session: V2CompilationSession,
        tolerance: float | None = None,
    ):
        super().__init__(tolerance=tolerance, method="highs-ds", presolve=True)
        self.v2_session = session

    def solve_objective(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> tuple[float, dict[str, float]]:
        program = compile_objective(
            self,
            name=self.v2_session.next_name(),
            variable_names=variable_names,
            maximization=maximization,
            weights=weights,
            options=self.v2_session.options,
        )
        result = program.execute()
        self.v2_session.record(program)
        self.solve_count += 1

        # Keep the compatibility state needed by the accounting orchestrator.
        self._last_solution_values.update(result.requested_values)
        return result.objective_value, result.requested_values

    def get_last_variable_reduced_cost(self, variable_name: str) -> float | None:
        return None

    def get_last_solve_constraint_evidence(
        self,
        variable_name: str,
        tolerance: float = 1e-6,
    ) -> list[dict]:
        # V2 intentionally starts without audit/dual plumbing.  The production
        # LP remains the oracle while the compiler representation stabilizes.
        return []


def v2_factory(session: V2CompilationSession) -> Callable[..., V2LPSolver]:
    def factory(*, tolerance=None, **_kwargs):
        return V2LPSolver(session=session, tolerance=tolerance)

    return factory
