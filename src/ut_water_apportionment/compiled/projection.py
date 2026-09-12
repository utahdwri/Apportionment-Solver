"""Small, bounded symbolic LP projection; independent of accounting policy.

Every row means ``a @ variables <= p @ daily_parameters``. Eliminating a
variable preserves precisely those values that admit a feasible completion.
Parameters (measurements, bounds, prior commitments) are never eliminated.
"""

from dataclasses import dataclass, field
from fractions import Fraction
from math import inf, isfinite
from time import perf_counter

import numpy as np


class CannotCompile(RuntimeError):
    """Use the ordinary LP path; no partial allocation should be committed."""


@dataclass(frozen=True)
class CompilationOptions:
    # Limits bound *preparation* cost, not the size of an accepted SolverInput.
    # An input larger than these limits remains supported through LP fallback.
    max_variables: int = 32
    max_rows: int = 400
    max_pairs: int = 2500
    max_fraction_bits: int = 1024
    max_plans: int = 64
    max_program_coefficients: int = 50000
    max_total_coefficients: int = 250000
    max_seconds_per_plan: float = 1.0
    max_total_compile_seconds: float = 5.0

    def __post_init__(self):
        for name, value in vars(self).items():
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            if name not in {
                "max_seconds_per_plan",
                "max_total_compile_seconds",
            } and not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")


@dataclass(frozen=True)
class Row:
    a: tuple
    p: tuple

    def negative(self):
        return Row(tuple(-v for v in self.a), tuple(-v for v in self.p))


def rational(value):
    if not isfinite(value):
        raise CannotCompile("nonfinite coefficient")
    return Fraction(str(value))


class Projector:
    def __init__(self, options, deadline):
        self.options, self.deadline = options, deadline
        self.peak_rows = 0

    def check_time(self):
        if perf_counter() > self.deadline:
            raise CannotCompile("compilation time budget reached")

    def canonical(self, rows):
        result = {}
        for row in rows:
            self.check_time()
            # Positive scaling retains inequality direction; normalize for
            # exact duplicate removal, including rows containing only inputs.
            scale = next((abs(c) for c in row.a + row.p if c), None)
            if scale is None:
                continue
            row = Row(tuple(c / scale for c in row.a), tuple(c / scale for c in row.p))
            if any(
                max(c.numerator.bit_length(), c.denominator.bit_length())
                > self.options.max_fraction_bits
                for c in row.a + row.p
            ):
                raise CannotCompile("rational coefficient size budget reached")
            result[row] = None
            if len(result) > self.options.max_rows:
                raise CannotCompile("projected row budget reached")
        self.peak_rows = max(self.peak_rows, len(result))
        return list(result)

    def bounds(self, source, target):
        rows = self.canonical(source)
        remaining = set(range(len(source[0].a))) - {target}
        while remaining:
            self.check_time()
            pool = set(rows)
            equation = next(
                (
                    r
                    for r in rows
                    if any(r.a[i] for i in remaining) and r.negative() in pool
                ),
                None,
            )
            if equation is not None:
                # Use path, measurement, and parent equalities before pairing
                # inequalities. This is exact substitution and often much cheaper.
                v = min(
                    (i for i in remaining if equation.a[i]),
                    key=lambda i: sum(bool(r.a[i]) for r in rows),
                )

                def substitute(rows=rows, v=v, equation=equation):
                    for r in rows:
                        factor = r.a[v] / equation.a[v]
                        yield Row(
                            tuple(
                                x - factor * y
                                for x, y in zip(r.a, equation.a, strict=True)
                            ),
                            tuple(
                                x - factor * y
                                for x, y in zip(r.p, equation.p, strict=True)
                            ),
                        )

                output = substitute()
            else:
                v = min(
                    remaining,
                    key=lambda i: (
                        sum(r.a[i] > 0 for r in rows) * sum(r.a[i] < 0 for r in rows),
                        i,
                    ),
                )
                positive = [r for r in rows if r.a[v] > 0]
                negative = [r for r in rows if r.a[v] < 0]
                if len(positive) * len(negative) > self.options.max_pairs:
                    raise CannotCompile("elimination pair budget reached")

                def pair_rows(rows=rows, v=v, positive=positive, negative=negative):
                    yield from (r for r in rows if not r.a[v])
                    for upper in positive:
                        for lower in negative:
                            cp, cn = upper.a[v], -lower.a[v]
                            yield Row(
                                tuple(
                                    x / cp + y / cn
                                    for x, y in zip(upper.a, lower.a, strict=True)
                                ),
                                tuple(
                                    x / cp + y / cn
                                    for x, y in zip(upper.p, lower.p, strict=True)
                                ),
                            )

                output = pair_rows()
            rows = self.canonical(output)
            remaining.remove(v)
        upper, lower, conditions = [], [], []
        for r in rows:
            c = r.a[target]
            if c > 0:
                upper.append(tuple(x / c for x in r.p))
            elif c < 0:
                lower.append(tuple(x / c for x in r.p))
            else:
                conditions.append(r.p)
        return Bounds(tuple(upper), tuple(lower), tuple(conditions))


@dataclass
class Bounds:
    upper: tuple
    lower: tuple
    conditions: tuple
    matrix: np.ndarray = field(init=False, repr=False)

    def __post_init__(self):
        self.matrix = np.asarray(self.upper + self.lower + self.conditions, dtype=float)

    def interval(self, parameters):
        if not self.matrix.size:
            return -inf, inf
        with np.errstate(over="ignore", invalid="ignore"):
            values = self.matrix @ parameters
        if not np.isfinite(values).all():
            raise CannotCompile("nonfinite formula evaluation")
        nu, nl = len(self.upper), len(self.lower)
        hi = min(values[:nu], default=inf)
        lo = max(values[nu : nu + nl], default=-inf)
        # A small domain tolerance absorbs floating-point cancellation; an
        # uncertain or infeasible interval is delegated to the actual backend.
        scale = max(1.0, float(np.max(np.abs(parameters))))
        if hi < lo or any(v < -1e-9 * scale for v in values[nu + nl :]):
            raise CannotCompile("compiled domain/interval check failed")
        return float(lo), float(hi)

    def text(self, labels):
        def expression(row):
            return (
                " + ".join(
                    f"({c}) * {s}" for s, c in zip(labels, row, strict=True) if c
                )
                or "0"
            )

        lines = []
        for name, rows, empty in [
            ("upper = MIN", self.upper, "+infinity"),
            ("lower = MAX", self.lower, "-infinity"),
        ]:
            lines.append(name + "(")
            lines.extend("    " + expression(row) + "," for row in rows)
            if not rows:
                lines.append("    " + empty)
            lines.append(")")
        lines.extend("REQUIRE " + expression(row) + " >= 0" for row in self.conditions)
        return "\n".join(lines)
