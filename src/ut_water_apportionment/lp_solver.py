"""LP backend selection and the common solver interface.

Backends are imported lazily so importing :mod:`apportionment_solver` does not
require every optional solver dependency to be installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib.util import find_spec
from typing import Callable, Protocol, TypeAlias


class LPSolverError(Exception):
    """The requested LP was infeasible, unbounded, or otherwise unsolved."""


class SolverBackendUnavailableError(RuntimeError):
    """A requested LP backend is not installed or could not be imported."""


class SolverBackend(str, Enum):
    AUTO = "auto"
    HIGHSPY = "highspy"
    GLOP = "glop"
    SCIPY = "scipy"


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


@dataclass(frozen=True)
class ResolvedSolverBackend:
    """A validated backend name and its lazily imported solver class."""

    name: SolverBackend
    factory: LPSolverFactory


@dataclass(frozen=True)
class _BackendSpec:
    backend: SolverBackend
    required_module: str
    install_hint: str
    loader: Callable[[], LPSolverFactory]


def _load_highspy() -> LPSolverFactory:
    from .lp_solver_HIGHSPY import LPSolver

    return LPSolver


def _load_glop() -> LPSolverFactory:
    from .lp_solver_GLOP import LPSolver

    return LPSolver


def _load_scipy() -> LPSolverFactory:
    from .lp_solver_SCIPY import LPSolver

    return LPSolver


_BACKEND_SPECS: dict[SolverBackend, _BackendSpec] = {
    SolverBackend.HIGHSPY: _BackendSpec(
        backend=SolverBackend.HIGHSPY,
        required_module="highspy",
        install_hint="pip install 'apportionment-solver[highs]'",
        loader=_load_highspy,
    ),
    SolverBackend.GLOP: _BackendSpec(
        backend=SolverBackend.GLOP,
        required_module="ortools.linear_solver.pywraplp",
        install_hint="pip install ortools",
        loader=_load_glop,
    ),
    SolverBackend.SCIPY: _BackendSpec(
        backend=SolverBackend.SCIPY,
        required_module="scipy.optimize",
        install_hint="pip install 'apportionment-solver[scipy]'",
        loader=_load_scipy,
    ),
}

# This project repeatedly changes bounds and re-solves the same LP. Prefer
# persistent native models, then use SciPy's matrix-rebuilding wrapper as the
# compatibility fallback.
AUTO_BACKEND_ORDER: tuple[SolverBackend, ...] = (
    SolverBackend.HIGHSPY,
    SolverBackend.GLOP,
    SolverBackend.SCIPY,
)

_BACKEND_ALIASES = {
    "highs": SolverBackend.HIGHSPY,
    "highspy": SolverBackend.HIGHSPY,
    "ortools": SolverBackend.GLOP,
    "or-tools": SolverBackend.GLOP,
    "glop": SolverBackend.GLOP,
    "scipy": SolverBackend.SCIPY,
    "auto": SolverBackend.AUTO,
}


def _normalize_backend(backend: SolverBackend | str) -> SolverBackend:
    if isinstance(backend, SolverBackend):
        return backend

    normalized = backend.strip().lower()
    try:
        return _BACKEND_ALIASES[normalized]
    except KeyError as exc:
        valid = ", ".join(item.value for item in SolverBackend)
        raise ValueError(
            f"Unknown solver backend {backend!r}. Expected one of: {valid}."
        ) from exc


def _module_is_available(module_name: str) -> bool:
    try:
        return find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _load_backend(backend: SolverBackend) -> ResolvedSolverBackend:
    spec = _BACKEND_SPECS[backend]
    if not _module_is_available(spec.required_module):
        raise SolverBackendUnavailableError(
            f"LP backend '{backend.value}' is not available. "
            f"Install it with: {spec.install_hint}"
        )

    try:
        factory = spec.loader()
    except (ImportError, ModuleNotFoundError, OSError) as exc:
        raise SolverBackendUnavailableError(
            f"LP backend '{backend.value}' is installed but could not be loaded: "
            f"{exc}"
        ) from exc

    return ResolvedSolverBackend(name=backend, factory=factory)


def resolve_solver_backend(
    backend: SolverBackend | str = SolverBackend.AUTO,
) -> ResolvedSolverBackend:
    """Resolve an explicit backend or select the best installed backend.

    ``auto`` uses :data:`AUTO_BACKEND_ORDER`. Explicit selection never falls
    back silently; it raises :class:`SolverBackendUnavailableError` instead.
    """

    requested = _normalize_backend(backend)
    if requested is not SolverBackend.AUTO:
        return _load_backend(requested)

    failures: list[str] = []
    for candidate in AUTO_BACKEND_ORDER:
        try:
            return _load_backend(candidate)
        except SolverBackendUnavailableError as exc:
            failures.append(str(exc))

    details = "\n - ".join(failures)
    raise SolverBackendUnavailableError(
        "No supported LP backend is available. Attempted:\n - " + details
    )


def available_solver_backends() -> list[str]:
    """Return installed backends in automatic preference order."""

    available: list[str] = []
    for backend in AUTO_BACKEND_ORDER:
        try:
            _load_backend(backend)
        except SolverBackendUnavailableError:
            continue
        available.append(backend.value)
    return available
