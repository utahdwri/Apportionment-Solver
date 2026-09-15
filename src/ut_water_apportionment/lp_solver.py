"""Minimal LP protocol used by the v2 compiler.

The v2 branch has no selectable whole-day LP backends.  The only numerical
LP implementation is the internal SciPy engine used to construct the
production LP and execute reduced residual kernels.
"""

from __future__ import annotations

from typing import Callable, Protocol, TypeAlias


class LPSolverError(Exception):
    """The requested LP was infeasible, unbounded, or otherwise unsolved."""


class LPSolverProtocol(Protocol):
    """Operations used by :class:`Apportioner` from any LP implementation."""

    def add_variable(
        self,
        name: str,
        lb: float | None = 0,
        ub: float | None = None,
    ) -> None: ...

    def has_variable(self, name: str) -> bool: ...

    def get_variable_bounds(self, name: str) -> tuple[float, float]: ...

    def get_constraint_bounds(self, name: str) -> tuple[float, float]: ...

    def add_constraint(
        self,
        name: str,
        lb: float | None = None,
        ub: float | None = None,
    ) -> None: ...

    def set_coefficient(
        self,
        constraint_name: str,
        variable_name: str,
        coefficient: float | None,
    ) -> None: ...

    def solve_objective(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> tuple[float, dict[str, float]]: ...

    def solve_objective_value(
        self,
        variable_names: list[str],
        maximization: bool = True,
        weights: dict[str, float] | None = None,
    ) -> float: ...

    def maximize_and_update_variable(self, variable_name: str) -> float: ...

    def minimize_and_update_variable(self, variable_name: str) -> float: ...

    def update_variable_bounds(
        self,
        name: str,
        lb: float | None = None,
        ub: float | None = None,
    ) -> None: ...

    def update_constraint_ub(self, name: str, ub: float | None = None) -> None: ...

    def update_constraint_lb(self, name: str, lb: float | None = None) -> None: ...

    def get_constraint_names(self) -> list[str]: ...

    def lp_string(self) -> str: ...

    def maximize_group_by_proportions(
        self,
        variable_names: list[str],
        proportion_factors: dict[str, float],
    ) -> dict[str, float]: ...

    def get_last_variable_reduced_cost(
        self,
        variable_name: str,
    ) -> float | None: ...

    def get_last_solve_constraint_evidence(
        self,
        variable_name: str,
        tolerance: float = 1e-6,
    ) -> list[dict]: ...

    def is_constraint_tight(
        self,
        constraint_name: str,
        variable_name: str,
    ) -> bool: ...


LPSolverFactory: TypeAlias = Callable[..., LPSolverProtocol]
