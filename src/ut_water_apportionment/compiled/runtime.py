"""Capture the production LP and reuse bounded symbolic objective programs.

No graph accounting rules are duplicated here. Parent equations, temporary
counterflow caps, proportional rows, and spill locks come from Apportioner and
the selected backend's existing methods.
"""

from collections import Counter
from dataclasses import dataclass
from math import isfinite
from time import perf_counter

import numpy as np

from .projection import CannotCompile, CompilationOptions, Projector, Row, rational


@dataclass
class Snapshot:
    names: tuple
    signature: tuple
    rows: tuple
    parameters: np.ndarray
    labels: tuple


def snapshot(engine, options):
    names = tuple(engine.vars)
    if len(names) > options.max_variables:
        raise CannotCompile(
            f"model has {len(names)} variables (budget {options.max_variables})"
        )
    if getattr(engine, "perminant_minus_var", None) is not None:
        raise CannotCompile("backend permanent-minus objective requires LP")
    indices = {name: i for i, name in enumerate(names)}
    rows, labels, parameters, signature = [], [], [], []

    def add(kind, name, coefficients, lb, ub):
        finite_lb, finite_ub = isfinite(lb), isfinite(ub)
        mode = (
            "equal"
            if finite_lb and lb == ub
            else ("lower" if finite_lb else "") + ("upper" if finite_ub else "")
        )
        if not mode:
            return
        signature.append((kind, name, coefficients, mode))
        if mode == "equal":
            labels.append(f"{kind}[{name}].value")
            parameters.append(lb)
            rows.extend(
                [
                    (coefficients, len(labels) - 1, 1),
                    (tuple(-c for c in coefficients), len(labels) - 1, -1),
                ]
            )
        else:
            if finite_lb:
                labels.append(f"{kind}[{name}].lower")
                parameters.append(lb)
                rows.append((tuple(-c for c in coefficients), len(labels) - 1, -1))
            if finite_ub:
                labels.append(f"{kind}[{name}].upper")
                parameters.append(ub)
                rows.append((coefficients, len(labels) - 1, 1))

    for name, var in engine.vars.items():
        coefficients = tuple(float(i == indices[name]) for i in range(len(names)))
        add("variable", name, coefficients, var.lb(), var.ub())
    for name, con in engine.cons.items():
        if not isfinite(con.lb()) and not isfinite(con.ub()):
            continue  # Inactive proportional rows impose no equations.
        if hasattr(con, "coefficients"):
            coefficients = tuple(con.coefficients.get(s, 0.0) for s in names)
        else:  # GLOP exposes coefficients through MPConstraint.
            coefficients = tuple(con.GetCoefficient(engine.vars[s]) for s in names)
        add("constraint", name, coefficients, con.lb(), con.ub())
    if len(rows) > options.max_rows:
        raise CannotCompile("source row budget reached")
    if not all(isfinite(c) for coefficients, _, _ in rows for c in coefficients):
        raise CannotCompile("nonfinite matrix coefficient")
    labels.append("objective_value")
    parameters.append(0.0)
    return Snapshot(
        names,
        (names, tuple(signature)),
        tuple(rows),
        np.asarray(parameters, dtype=float),
        tuple(labels),
    )


@dataclass
class ObjectiveProgram:
    name: str
    labels: tuple
    targets: tuple
    costs: tuple
    maximization: bool
    optimum: object
    ranges: tuple
    peak_rows: int

    @property
    def coefficient_count(self):
        return sum(
            len(row)
            for bounds in (self.optimum, *self.ranges)
            for rows in (bounds.upper, bounds.lower, bounds.conditions)
            for row in rows
        )

    def evaluate(self, parameters):
        params = parameters.copy()
        lo, hi = self.optimum.interval(params)
        value = hi if self.maximization else lo
        if not isfinite(value):
            raise CannotCompile("compiled objective is unbounded")
        params[-1] = value
        if len(self.targets) == 1 and self.costs[0] != 0:
            return value, {self.targets[0]: value / self.costs[0]}
        values = {}
        for name, bounds in zip(self.targets, self.ranges, strict=True):
            lo, hi = bounds.interval(params)
            if (
                not isfinite(lo)
                or not isfinite(hi)
                or hi - lo > 1e-9 * max(1.0, abs(lo), abs(hi))
            ):
                # A unique objective sum need not identify its components. The
                # production backend's choice can change later allocations, so
                # restart the day rather than inventing a tie-breaking rule.
                raise CannotCompile(f"nonunique minimum/maximum components: {name}")
            values[name] = (lo + hi) / 2
        return value, values

    def text(self):
        lines = [
            f"PROGRAM {self.name}: "
            + ("MAXIMIZE " if self.maximization else "MINIMIZE ")
            + " + ".join(
                f"({c}) * {s}" for s, c in zip(self.targets, self.costs, strict=True)
            ),
            self.optimum.text(self.labels),
            "objective_value = " + ("upper" if self.maximization else "lower"),
        ]
        if len(self.targets) == 1 and self.costs[0] != 0:
            lines.append(f"{self.targets[0]} = objective_value / ({self.costs[0]})")
        else:
            for name, bounds in zip(self.targets, self.ranges, strict=True):
                lines += [
                    f"VALUE RANGE FOR {name}",
                    bounds.text(self.labels),
                    f"REQUIRE upper == lower; {name} = lower; otherwise RESTART_DAY_WITH_LP",
                ]
        return "\n".join(lines)

    def code(self):
        """Executable Python for this scalar objective and requested values."""

        def encoded(bounds):
            return repr(
                tuple(
                    tuple(tuple(float(c) for c in row) for row in rows)
                    for rows in (bounds.upper, bounds.lower, bounds.conditions)
                )
            )

        lines = [
            f"def {self.name}(parameters):",
            '    """Input: dict of the named live LP bounds; output: objective, values."""',
            f"    labels = {self.labels!r}",
            '    p = [float(parameters[s]) if s != "objective_value" else 0.0 for s in labels]',
            f"    lo, hi = _compiled_interval(p, *{encoded(self.optimum)})",
            "    value = " + ("hi" if self.maximization else "lo"),
            '    if not isfinite(value): raise CompiledFallback("unbounded objective")',
            "    p[-1] = value",
        ]
        if len(self.targets) == 1 and self.costs[0] != 0:
            lines.append(
                f"    return value, {{{self.targets[0]!r}: value / {self.costs[0]!r}}}"
            )
        else:
            lines.append("    values = {}")
            for name, bounds in zip(self.targets, self.ranges, strict=True):
                lines += [
                    f"    lo, hi = _compiled_interval(p, *{encoded(bounds)})",
                    "    if not isfinite(lo) or not isfinite(hi) or hi-lo > 1e-9*max(1., abs(lo), abs(hi)):",
                    f"        raise CompiledFallback({('nonunique component: ' + name)!r})",
                    f"    values[{name!r}] = (lo+hi)/2",
                ]
            lines.append("    return value, values")
        return "\n".join(lines)


def compile_objective(state, targets, costs, maximization, options, name, seconds):
    projector = Projector(options, perf_counter() + seconds)
    n, m = len(state.names), len(state.labels)
    zero = rational(0)
    rows = []
    for coefficients, parameter, sign in state.rows:
        p = tuple(rational(sign if j == parameter else 0) for j in range(m))
        rows.append(Row(tuple(rational(c) for c in coefficients) + (zero,), p))
    c = dict(zip(targets, costs, strict=True))
    coefficients = tuple(rational(c.get(s, 0.0)) for s in state.names)
    equation = Row(coefficients + (-rational(1),), (zero,) * m)
    optimum = projector.bounds(rows + [equation, equation.negative()], n)
    ranges = []

    def check_size():
        count = sum(
            len(row)
            for bounds in (optimum, *ranges)
            for rs in (bounds.upper, bounds.lower, bounds.conditions)
            for row in rs
        )
        if count > options.max_program_coefficients:
            raise CannotCompile("objective program coefficient budget reached")

    check_size()
    if len(targets) != 1 or costs[0] == 0:
        # Restrict the original system to the optimal objective value, then
        # project each requested component. This detects ambiguous endpoint
        # caps, spill components, and final residual values.
        face = Row(coefficients + (zero,), (zero,) * (m - 1) + (rational(1),))
        for target in targets:
            ranges.append(
                projector.bounds(
                    rows + [face, face.negative()], state.names.index(target)
                )
            )
            check_size()
    return ObjectiveProgram(
        name,
        state.labels,
        targets,
        costs,
        maximization,
        optimum,
        tuple(ranges),
        projector.peak_rows,
    )


class CompilationSession:
    """Bounded program cache owned by one reusable SolverInput plan."""

    def __init__(self, options=None):
        self.options = options or CompilationOptions()
        self.programs = {}
        self.failures = {}
        self.compile_seconds = 0.0
        self.date = None
        self.stats = Counter()
        self.events = []
        self.last_events = []
        self.warmup_reasons = Counter()

    def program(self, engine, names, maximization=True, weights=None):
        state = snapshot(engine, self.options)
        targets = tuple(dict.fromkeys(names))
        if not targets or any(s not in state.names for s in targets):
            raise CannotCompile("empty or unknown objective targets")
        costs = tuple((weights or {}).get(s, 1.0) for s in targets)
        key = (state.signature, targets, costs, maximization)
        if key in self.failures:
            raise CannotCompile(self.failures[key])
        if key not in self.programs:
            if len(self.programs) + len(self.failures) >= self.options.max_plans:
                raise CannotCompile("program cache budget reached")
            remaining = self.options.max_total_compile_seconds - self.compile_seconds
            if remaining <= 0:
                raise CannotCompile("total compilation time budget reached")
            start = perf_counter()
            try:
                program = compile_objective(
                    state,
                    targets,
                    costs,
                    maximization,
                    self.options,
                    f"P{len(self.programs) + 1}",
                    min(remaining, self.options.max_seconds_per_plan),
                )
                if (
                    sum(p.coefficient_count for p in self.programs.values())
                    + program.coefficient_count
                    > self.options.max_total_coefficients
                ):
                    raise CannotCompile("total cached coefficient budget reached")
                self.programs[key] = program
            except CannotCompile as error:
                self.failures[key] = str(error)
                raise
            finally:
                self.compile_seconds += perf_counter() - start
        else:
            self.stats["cache_hits"] += 1
        return self.programs[key], state.parameters

    def evaluate(self, engine, names, maximization=True, weights=None):
        program, params = self.program(engine, names, maximization, weights)
        result = program.evaluate(params)
        self.stats["formula_evaluations"] += 1
        self.events.append(program.name)
        return result

    def begin_day(self, date):
        self.date, self.events = date, []

    def finish_day(self, reason=None):
        self.stats["lp_days" if reason else "compiled_days"] += 1
        self.last_events.append(
            {
                "date": self.date,
                "method": "lp" if reason else "compiled",
                "formula_calls": list(self.events),
                "fallback_reason": reason,
            }
        )
        if reason:
            self.stats["discarded_formula_evaluations"] += len(self.events)

    def report(self):
        return {
            **dict(self.stats),
            "program_count": len(self.programs),
            "rejected_program_count": len(self.failures),
            "compile_seconds": self.compile_seconds,
            "cached_coefficients": sum(
                p.coefficient_count for p in self.programs.values()
            ),
            "days": list(self.last_events),
            "preparation_fallbacks": dict(self.warmup_reasons),
        }

    def formulas(self):
        prelude = (
            "Parameters are live LP bounds: measurements, NF, account capacity, and committed allocations.\n"
            "Programs include parent/path equalities and active counterflow/proportional/spill constraints.\n"
            "Daily execution follows the production priority loop; it selects programs by matrix and objective.\n"
            "Equal-priority active sets are visited iteratively; subsets are not enumerated in advance.\n"
            "On ambiguity, invalid domain, audit request, or budget limit: RESTART_DAY_WITH_LP.\n"
        )
        programs = "\n\n".join(p.text() for p in self.programs.values())
        reasons = "\n".join(
            "LP FALLBACK: " + s
            for s in dict.fromkeys([*self.failures.values(), *self.warmup_reasons])
        )
        return prelude + "\n" + programs + "\n" + reasons

    def execution_outline(self):
        # This is a faithful execution outline, not a portable standalone export
        # of graph preprocessing or state objects. formulas() gives actual bounds.
        return """# Execution outline; plan.solve() is the executable routine.
for day in dates:
    initialize_daily_measurements_accounts_and_natural_flow()
    try:
        for allocation_pass in (1, 2):
            for priority_group in production_schedule:
                while active_members_remain(priority_group):
                    apply_counterflow_caps_using_compiled_objectives()
                    increment = evaluate_compiled_common_increment_or_single_maximum()
                    commit_allocations_and_update_remaining_natural_flow(increment)
                    remove_blocked_members_using_compiled_objectives()
                    release_temporary_caps()
            if allocation_pass == 1:
                minimize_and_lock_spills_then_credit_natural_flow()
        finalize_using_compiled_objective()
    except CannotCompile:
        discard_tentative_day_and_run_original_lp_day()
    commit_final_account_and_cumulative_balances_once()
"""

    def code(self):
        helpers = """# Generated scalar objective programs. Run the full accounting workflow with plan.solve().
# Each function accepts named LP bounds. CompiledFallback means restart the day with LP.
from math import inf, isfinite

class CompiledFallback(RuntimeError):
    pass

def _compiled_interval(p, upper, lower, conditions):
    def dot(row):
        value = sum(c*x for c, x in zip(row, p, strict=True))
        if not isfinite(value): raise CompiledFallback("nonfinite formula")
        return value
    hi = min((dot(row) for row in upper), default=inf)
    lo = max((dot(row) for row in lower), default=-inf)
    tolerance = 1e-9*max(1., max(abs(x) for x in p))
    if hi < lo or any(dot(row) < -tolerance for row in conditions):
        raise CompiledFallback("domain/interval check failed")
    return lo, hi
"""
        return (
            helpers
            + "\n\n"
            + "\n\n".join(p.code() for p in self.programs.values())
            + "\n"
        )


def compiled_factory(base, session):
    class CompiledEngine(base):
        def solve_objective(self, variable_names, maximization=True, weights=None):
            return session.evaluate(self, variable_names, maximization, weights)

        def get_last_variable_reduced_cost(self, variable_name):
            return None

        def get_last_solve_constraint_evidence(self, variable_name, tolerance=1e-6):
            # No fictitious dual evidence from a previous native solve. Requests
            # for the detailed production audit use the ordinary LP day instead.
            return []

    return CompiledEngine
