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
    # Limits bound *per-objective reduced* compilation cost, not the size of an
    # accepted SolverInput. Large source models remain supported when presolve
    # can remove irrelevant variables, or through ordinary LP fallback.
    max_variables: int = 512
    max_rows: int = 5000
    max_pairs: int = 100000
    max_fraction_bits: int = 4096
    max_plans: int = 512
    max_program_coefficients: int = 1000000
    max_total_coefficients: int = 5000000
    # If symbolic projection is too expensive, retain the already-presolved
    # residual LP as a small numeric kernel instead of restarting the entire
    # accounting day. These limits bound that local fallback representation,
    # not the source model.
    max_kernel_variables: int = 5000
    max_kernel_rows: int = 100000
    # Soft symbolic limits. Crossing these does not reject the objective; it
    # converts the already-presolved residual problem into a ReducedLPProgram.
    # Keeping these well below the hard projection budgets prevents a locally
    # cheap-looking Fourier-Motzkin step from exploding into thousands of
    # exact-Fraction rows before the runtime clock is checked again.
    max_symbolic_variables_before_kernel: int = 20
    max_symbolic_rows_before_kernel: int = 750
    max_symbolic_pairs_before_kernel: int = 5000
    # Compilation is a reusable warm-up cost. Prefer spending minutes building
    # formulas over falling back merely because a difficult objective needs a
    # few seconds of exact projection.
    max_seconds_per_plan: float = 120.0
    max_total_compile_seconds: float = 600.0
    # Once a reduced LP kernel is available there is little value in letting a
    # single Fourier-Motzkin projection run for minutes. This is a *soft*
    # symbolic cutoff: the objective becomes a cached reduced LP kernel rather
    # than causing a whole-day fallback. The total plan may still spend minutes
    # compiling many tractable formulas.
    max_symbolic_seconds_before_kernel: float = 0.05

    def __post_init__(self):
        for name, value in vars(self).items():
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            if name not in {
                "max_seconds_per_plan",
                "max_total_compile_seconds",
                "max_symbolic_seconds_before_kernel",
            } and not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")


@dataclass(frozen=True)
class Row:
    a: tuple
    p: tuple
    # Rows are dictionary/set keys throughout projection. Fraction hashing is
    # comparatively expensive, so cache the combined tuple hash after its first
    # use. These fields deliberately do not participate in equality.
    _hash: int | None = field(default=None, init=False, repr=False, compare=False)
    _negative: "Row | None" = field(
        default=None, init=False, repr=False, compare=False
    )

    def __hash__(self):
        cached = self._hash
        if cached is None:
            cached = hash((self.a, self.p))
            object.__setattr__(self, "_hash", cached)
        return cached

    def negative(self):
        cached = self._negative
        if cached is not None:
            return cached
        negative = Row(tuple(-v for v in self.a), tuple(-v for v in self.p))
        object.__setattr__(self, "_negative", negative)
        object.__setattr__(negative, "_negative", self)
        return negative


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

    def _canonical_row(self, row):
        """Return one normalized row, or ``None`` for the all-zero row.

        Positive scaling does not change an inequality. Using a reciprocal and
        multiplication is exactly equivalent to dividing every coefficient by
        the first nonzero magnitude, but is substantially cheaper for Fraction.
        """

        self.check_time()
        scale = next((abs(c) for c in row.a + row.p if c), None)
        if scale is None:
            return None
        if scale != 1:
            inverse = Fraction(scale.denominator, scale.numerator)
            row = Row(
                tuple(c * inverse for c in row.a),
                tuple(c * inverse for c in row.p),
            )
        if any(
            max(c.numerator.bit_length(), c.denominator.bit_length())
            > self.options.max_fraction_bits
            for c in row.a + row.p
        ):
            raise CannotCompile("rational coefficient size budget reached")
        return row

    def _finish_rows(self, result):
        if len(result) > self.options.max_rows:
            raise CannotCompile("projected row budget reached")
        self.peak_rows = max(self.peak_rows, len(result))
        return list(result)

    def canonical(self, rows):
        """Canonicalize and exactly deduplicate an arbitrary row stream."""

        result = {}
        for row in rows:
            row = self._canonical_row(row)
            if row is None:
                continue
            result[row] = None
            if len(result) > self.options.max_rows:
                raise CannotCompile("projected row budget reached")
        return self._finish_rows(result)

    def _merge_generated(self, carried, generated):
        """Deduplicate canonical carried rows plus newly generated rows.

        Rows whose eliminated-variable coefficient is already zero are carried
        unchanged. They were canonicalized in the previous iteration, so doing
        that exact Fraction work again is pure overhead.
        """

        result = {row: None for row in carried}
        if len(result) > self.options.max_rows:
            raise CannotCompile("projected row budget reached")
        for row in generated:
            row = self._canonical_row(row)
            if row is None:
                continue
            result[row] = None
            if len(result) > self.options.max_rows:
                raise CannotCompile("projected row budget reached")
        return self._finish_rows(result)

    @staticmethod
    def _substitute_equality(row, equation, variable):
        """Eliminate ``variable`` with exact cross-multiplication.

        ``equation`` represents an equality because both it and its negative
        are present. Multiplying the inequality row by ``abs(e)`` is a positive
        scaling, and adding any multiple of an equality is exact. This avoids
        the Fraction division in ``row.a[v] / equation.a[v]``.
        """

        a = row.a[variable]
        e = equation.a[variable]
        scale = abs(e)
        equation_multiplier = -a if e > 0 else a
        return Row(
            tuple(
                scale * x + equation_multiplier * y
                for x, y in zip(row.a, equation.a, strict=True)
            ),
            tuple(
                scale * x + equation_multiplier * y
                for x, y in zip(row.p, equation.p, strict=True)
            ),
        )

    @staticmethod
    def _pair(upper, lower, variable):
        """Fourier-Motzkin pair without Fraction division.

        If ``cp > 0`` and ``lower.a[v] == -cn < 0``, then
        ``cn * upper + cp * lower`` eliminates the variable. Both row scales
        are positive, so this is exactly the same inequality as the usual
        normalized ``upper/cp + lower/cn`` form.
        """

        cp = upper.a[variable]
        cn = -lower.a[variable]
        return Row(
            tuple(
                cn * x + cp * y
                for x, y in zip(upper.a, lower.a, strict=True)
            ),
            tuple(
                cn * x + cp * y
                for x, y in zip(upper.p, lower.p, strict=True)
            ),
        )

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
                carried = [r for r in rows if not r.a[v]]
                generated = (
                    self._substitute_equality(r, equation, v)
                    for r in rows
                    if r.a[v]
                )
                rows = self._merge_generated(carried, generated)
            else:
                v = min(
                    remaining,
                    key=lambda i: (
                        sum(r.a[i] > 0 for r in rows)
                        * sum(r.a[i] < 0 for r in rows),
                        i,
                    ),
                )
                positive = [r for r in rows if r.a[v] > 0]
                negative = [r for r in rows if r.a[v] < 0]
                if len(positive) * len(negative) > self.options.max_pairs:
                    raise CannotCompile("elimination pair budget reached")
                carried = [r for r in rows if not r.a[v]]
                generated = (
                    self._pair(upper, lower, v)
                    for upper in positive
                    for lower in negative
                )
                rows = self._merge_generated(carried, generated)
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
        self.matrix = np.asarray(
            self.upper + self.lower + self.conditions, dtype=float
        )

    def interval(self, parameters):
        if not self.matrix.size:
            return -inf, inf
        with np.errstate(over="ignore", invalid="ignore"):
            values = self.matrix @ parameters
            # Bound the rounding error of each dot product from the magnitude
            # of the terms in that row. This is much tighter than scaling every
            # check by the largest unrelated parameter, while still allowing
            # tiny cancellation errors from float evaluation of exact formulas.
            row_scales = np.abs(self.matrix) @ np.abs(parameters)
        if not np.isfinite(values).all() or not np.isfinite(row_scales).all():
            raise CannotCompile("nonfinite formula evaluation")

        # A dot product of n terms incurs O(n*eps) rounding error. Keep a
        # generous safety factor because NumPy/BLAS may sum in a different
        # order, but make the tolerance local to each projected row.
        nterms = max(1, self.matrix.shape[1])
        tolerances = (
            64.0
            * np.finfo(float).eps
            * nterms
            * np.maximum(1.0, row_scales)
        )

        nu, nl = len(self.upper), len(self.lower)
        upper_values = values[:nu]
        lower_values = values[nu : nu + nl]
        condition_values = values[nu + nl :]

        if nu:
            upper_index = int(np.argmin(upper_values))
            hi = float(upper_values[upper_index])
            hi_tolerance = float(tolerances[upper_index])
        else:
            hi = inf
            hi_tolerance = 0.0

        if nl:
            lower_index = int(np.argmax(lower_values))
            lo = float(lower_values[lower_index])
            lo_tolerance = float(tolerances[nu + lower_index])
        else:
            lo = -inf
            lo_tolerance = 0.0

        if hi < lo:
            if lo - hi > hi_tolerance + lo_tolerance:
                raise CannotCompile("compiled domain/interval check failed")
            # Exact projection can produce mathematically identical lower and
            # upper expressions that differ by a few ulps when evaluated as
            # floats. Collapse that tiny inverted interval to its midpoint.
            midpoint = (hi + lo) / 2.0
            lo = hi = midpoint

        condition_tolerances = tolerances[nu + nl :]
        if any(
            value < -tolerance
            for value, tolerance in zip(
                condition_values, condition_tolerances, strict=True
            )
        ):
            raise CannotCompile("compiled domain/interval check failed")

        return lo, hi

    def text(self, labels):
        def expression(row):
            return (
                " + ".join(
                    f"({c}) * {s}"
                    for s, c in zip(labels, row, strict=True)
                    if c
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
        lines.extend(
            "REQUIRE " + expression(row) + " >= 0" for row in self.conditions
        )
        return "\n".join(lines)
