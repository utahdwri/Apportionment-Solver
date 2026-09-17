"""Numerical execution only. Formula compilation is deliberately deferred."""
from dataclasses import dataclass
from math import isfinite

import numpy as np
from scipy.optimize import linprog

from .lp import BlockLP, Maximize, Proportional, Slot

TOL = 1e-7


class BlockLPError(RuntimeError):
    pass


def value(scalar, state):
    return float(state[scalar.index]) if isinstance(scalar, Slot) else float(scalar)


@dataclass
class LPKernel:
    model: BlockLP

    def __post_init__(self):
        self.names = tuple(self.model.variables)
        self.index = {name: i for i, name in enumerate(self.names)}
        targets = (self.model.rule.coefficients if isinstance(self.model.rule, Maximize)
                   else self.model.rule.reference_cfs)
        for name in targets:
            if name not in self.index:
                raise ValueError(f"Unknown objective variable: {name}")
        for constraint in self.model.constraints:
            if set(constraint.coefficients) - self.index.keys():
                raise ValueError(f"Unknown variable in constraint {constraint.name}")
        if set(self.model.updates) - targets.keys():
            raise ValueError("Only target allocations may commit state updates")

    def _solve(self, state, weights=None, proportions=None):
        """Bind slots and solve this block, including reservation witnesses."""
        width = len(self.names) + (proportions is not None)
        bounds = []
        for variable in self.model.variables.values():
            lower = value(variable.lower, state)
            upper = None if variable.upper is None else value(variable.upper, state)
            if upper is not None and -TOL <= upper < 0 and lower == 0:
                upper = 0.0
            if not isfinite(lower) or (upper is not None and np.isnan(upper)):
                raise BlockLPError("Non-finite variable bound")
            bounds.append((lower, upper))
        eq, eq_rhs, ub, ub_rhs = [], [], [], []
        for constraint in self.model.constraints:
            row = np.zeros(width)
            for name, coefficient in constraint.coefficients.items():
                row[self.index[name]] = value(coefficient, state)
            if not np.isfinite(row).all():
                raise BlockLPError(f"Non-finite coefficient: {constraint.name}")
            lo = None if constraint.lower is None else value(constraint.lower, state)
            hi = None if constraint.upper is None else value(constraint.upper, state)
            if any(v is not None and not isfinite(v) for v in (lo, hi)):
                raise BlockLPError(f"Non-finite constraint bound: {constraint.name}")
            if lo is not None and hi is not None and lo == hi:
                eq.append(row); eq_rhs.append(lo)
            else:
                if hi is not None:
                    ub.append(row); ub_rhs.append(hi)
                if lo is not None:
                    ub.append(-row); ub_rhs.append(-lo)
        objective = np.zeros(width)
        if proportions is not None:
            bounds.append((0, None))
            objective[-1] = -1.0
            for name, factor in proportions.items():
                row = np.zeros(width)
                row[self.index[name]], row[-1] = -1.0, factor
                ub.append(row); ub_rhs.append(0.0)
        else:
            for name, coefficient in weights.items():
                objective[self.index[name]] = -coefficient
        result = linprog(
            objective, A_ub=np.asarray(ub) if ub else None,
            b_ub=np.asarray(ub_rhs) if ub else None,
            A_eq=np.asarray(eq) if eq else None,
            b_eq=np.asarray(eq_rhs) if eq else None,
            bounds=bounds, method="highs-ds",
        )
        if not result.success:
            raise BlockLPError(
                f"Block {list(self.model.updates)} failed: {result.message}"
            )
        return result.x

    def _commit(self, state, increments):
        # Evaluate every coefficient against the same pre-commit state. This
        # makes updates independent of dict order even when they share slots.
        changes = {}
        for name, increment in increments.items():
            if increment < -TOL or not isfinite(increment):
                raise BlockLPError(f"Invalid increment for {name}: {increment}")
            increment = max(0.0, float(increment))
            for slot, coefficient in self.model.updates.get(name, {}).items():
                changes[slot.index] = changes.get(slot.index, 0.0) + value(coefficient, state) * increment
        for index, change in changes.items():
            state[index] += change

    def execute(self, state):
        """Commit target allocations only; auxiliary solutions remain witnesses."""
        calls = 0
        if not self.names:
            return calls
        if isinstance(self.model.rule, Maximize):
            weights = {name: value(c, state) for name, c in self.model.rule.coefficients.items()}
            result = self._solve(state, weights=weights)
            self._commit(state, {name: result[self.index[name]] for name in weights})
            return 1
        if not isinstance(self.model.rule, Proportional):
            raise TypeError(f"Unknown allocation rule: {self.model.rule!r}")
        references = {name: value(c, state) for name, c in self.model.rule.reference_cfs.items()}
        if any(np.isnan(c) or c < 0 for c in references.values()):
            raise BlockLPError("Invalid proportional reference cfs")
        # Match the existing schedule: unlimited members share equally first;
        # finite members then share by their effective daily caps.
        phases = [
            {name: 1.0 for name, cfs in references.items() if np.isposinf(cfs)},
            {name: cfs for name, cfs in references.items() if isfinite(cfs) and cfs > 0},
        ]
        deferred = []
        for active in phases:
            while active:
                # Scale before summing to avoid overflow for very large caps.
                scale = max(active.values())
                total = sum(factor / scale for factor in active.values())
                factors = {name: (factor / scale) / total for name, factor in active.items()}
                tiny = [name for name, factor in factors.items() if factor < 1e-6]
                if tiny:
                    deferred.extend(tiny)
                    active = {name: c for name, c in active.items() if name not in tiny}
                    continue
                result = self._solve(state, proportions=factors)
                calls += 1
                self._commit(state, {name: factor * max(0.0, result[-1]) for name, factor in factors.items()})
                blocked = []
                # First implementation deliberately uses scalar LPs for exact
                # classification. A zero in one max-sum witness is ambiguous.
                for name in active:
                    witness = self._solve(state, weights={name: 1.0})
                    calls += 1
                    if witness[self.index[name]] <= TOL:
                        blocked.append(name)
                if not blocked:
                    raise BlockLPError("Proportional allocation made no blocking progress")
                active = {name: c for name, c in active.items() if name not in blocked}
        for name in deferred:
            result = self._solve(state, weights={name: 1.0})
            calls += 1
            self._commit(state, {name: result[self.index[name]]})
        return calls


def compile_lp_kernel(lp_model: BlockLP) -> LPKernel:
    return LPKernel(lp_model)
