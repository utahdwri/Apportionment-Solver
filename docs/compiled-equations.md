# Compiled equations with LP fallback

The alternate method uses the **current production LP rows and allocation
schedule**. It compiles small linear objective systems into reusable MIN/MAX
expressions. A day that cannot safely use those expressions is restarted using
the selected LP backend. Unsupported compilation does not mean unsupported
accounting input.

This integration starts from `main` at
`df099710e7e83077c5a07bdc27dc6888ebb795a0`. It does not import the earlier
standalone prototype or reproduce its separate accounting-graph adapter.

## Residual constraint state

Senior allocations are not carried forward as live decision variables. Once a
transaction is committed, its contribution is absorbed into each affected
constraint bound. Junior formulas therefore consume parameters such as
`constraint[MEAS_diversion].remaining` and
`constraint[NF_ZONE_RIVER].remaining_upper`. Algebraically, for
`a1*x1 + a2*x2 <= b`, after `x1` is committed the compiled state is
`a2*x2 <= b - a1*x1`. Signed coefficients work naturally: a negative
coefficient can increase the residual capacity when that variable is committed.

`compile_solver_input()` traces the production schedule once over the supplied
date range, compiling every objective pattern it encounters, then freezes the
program cache. `plan.solve()` performs no symbolic compilation.


## Usage

```python
from ut_water_apportionment import solve, compile_solver_input

# Existing behavior is unchanged.
ordinary = solve(solver_input)

# One-shot alternate method, returning the normal complete SolverOutput.
result = solve(solver_input, method="compiled", generate_audit=False)
print(result.compilation_report)

# Reusable plan with an inspection interface.
plan = compile_solver_input(solver_input, solver_backend="auto")
print(plan.formulas())
result = plan.solve()  # Defaults to generate_audit=False on the plan interface.

# Execution may discover additional dated/active-set programs. Inspect again
# afterward to see every program compiled during this run.
print(plan.formulas())
print(plan.code())
print(plan.execution_outline())
print(plan.report())

# Reuse formulas with replacement measurements; input graph and limits stay fixed.
result = plan.solve(measurements=other_measurement_collection)
```

`solver_backend` still chooses `highspy`, `glop`, `scipy`, or `auto`. It selects
both the native model wrapper and the fallback solver. There is no additional
mandatory dependency. Install the project normally, for example
`python -m pip install -e '.[highs,dev]'`.

The reusable plan owns a copy of SolverInput. Each execution starts accounts and
cumulative usage afresh and reuses the formula cache. Create another plan when
changing the graph, transaction schedule, limits, or other non-measurement input.
The plan is stateful and should not be executed concurrently from multiple threads.

## What is actually compiled

For each objective, the compiler reads the selected backend's current variable
bounds, constraint bounds, and coefficients. This includes production's:

- Measurement equalities and path continuity, with the losses supported by main.
- Natural-flow limits and updates, including specified boundaries.
- Parent/child sum equations and the parent's own scheduled objective.
- Cumulative and account limits supplied as the day's bounds.
- Temporary endpoint counterflow caps and proportional-increment constraints.
- Minimum-spill locks and the resulting increased natural-flow upper bounds.

Bounds are symbolic parameters. Changing a measurement or committed allocation
changes an input to the formula. Changing coefficients, active constraint sides,
or objective weights selects a different program.

Compilation is objective-specific. Before Fourier–Motzkin projection, the
compiler performs a sparse presolve around the current priority objective:

- Exact non-objective equalities are substituted first.
- Already committed senior allocations are treated as live bound parameters and
  moved to the right-hand side when their minimum feasible value is optimal for
  the current objective.
- Same-direction junior variables that can only tighten the current objective are
  likewise fixed at the harmless bound (normally zero) instead of being carried
  through projection.
- Redundant rows implied by live variable bounds are replaced by guarded runtime
  conditions, so changed daily bounds can safely trigger LP fallback rather than
  reusing an invalid simplification.
- Variables with genuinely competing/mixed signs remain live and are projected
  exactly. Signed variables and negative path/counterflow coefficients are
  therefore supported; sign is used to decide what can be removed, not as a
  restriction on the model.

After this reduction, Fourier–Motzkin projection derives upper bounds (a MIN),
lower bounds (a MAX), and inequalities defining valid daily inputs. Minimizing
selects the lower bound; maximizing selects the upper bound. The exact projector
uses positive cross-multiplication for equality substitution and inequality
pairing, so it avoids most Fraction divisions. Rows that do not contain the
variable being eliminated are carried forward in canonical form instead of being
renormalized every round, and row hashes are cached for exact duplicate removal.
Scalar objectives that presolve to zero or one active variable bypass
Fourier–Motzkin entirely and are isolated directly from the remaining rows.

If an objective requests several variable values, knowing the optimum sum is
insufficient. The compiler also projects the range of each requested variable
while fixing that sum. Nonunique components trigger LP fallback, preserving
main's handling of otherwise ambiguous endpoint caps, spills, and finalization.

`compile_solver_input()` eagerly prepares only the first day's most-senior
ordinary transaction objective without running an optimizer. Later priorities are
compiled **on first encounter during execution**, after senior commitments have
been applied. That timing is intentional: it lets each junior objective compile
against a much smaller reduced system rather than the untouched full-period model.
This is a bounded hybrid compiler, not exhaustive advance compilation of every
possible branch.

## Inspect the formulas and code

`plan.formulas()` returns the actual symbolic expressions. For example, an
individual diversion can produce bounds of the following form:

```text
upper = MIN(
    variable[right_a___diversion].upper,
    -variable[right_b___diversion].lower
      -variable[SLACK_river_TO_farm_diversion___diversion].lower
      +constraint[MEAS_diversion].remaining,
    -variable[right_b___diversion].lower
      +constraint[NF_ZONE_river].remaining_upper,
)
```

These are live LP bounds: `.lower` often represents a previous commitment, and
`.value` represents an equality's right-hand side. Generated output retains
additional feasibility conditions rather than hiding them in simplified prose.

`plan.code()` exports executable Python functions for the cached objective
programs. Each function takes a dictionary of named bounds and returns an
objective value and requested variable values. It raises `CompiledFallback` if
its conditions cannot certify the result. It does **not** export the complete
graph/data/account workflow; `plan.solve()` executes that workflow.

`plan.execution_outline()` separately describes the daily orchestration. Program
IDs in `plan.report()['days'][...]['formula_calls']` identify the actual formulas
visited on each day. Calls made before an LP restart are recorded as discarded
formula evaluations; they are not counted as a successfully compiled day.

Run `python examples/compiled_equations.py` from an installed checkout for a
complete equal-priority example and generated files. The two rights have limits
4 and 8. For measured flows 9, 6, and 12, their allocations are respectively
(3, 6), (2, 4), and (4, 8).

## Equal priorities and large inputs

The existing production loop determines active members, applies proportional
increments, drops blocked members, and repeats. The compiler sees the common
increment as another scalar LP objective. It **does not enumerate all subsets**
of the group in advance. Members with identical LP constraint columns share one
residual representation during common-increment compilation. During the later
"which members are maxed?" check, members with identical columns also share one
representative objective after members already at their individual upper bounds
have been removed. This is exact for positive headroom: increasing either member
by the same epsilon has the same effect on every LP constraint.

The variable budget now applies to the **reduced objective**, not the entire LP.
A source model with hundreds of variables can therefore compile a senior or junior
priority objective when equality substitution and sign-aware bound elimination
leave only a small coupled core. Equal-priority groups can still leave a coupled residual core when many
members or signed counterflows must move together. If exact projection exceeds
the soft symbolic limits, the compiler retains that already-presolved residual
problem as a **reduced LP kernel**. Runtime solves only that small objective,
updates residual state, and then continues the compiled accounting sequence.

`plan.report()["program_reductions"]` records source-variable count, active
variable count, equality eliminations, and bound eliminations for each cached
program. This makes the reduction visible when tuning large models. Cached
program coefficient counts are computed once when a program is created and the
session maintains the running total, avoiding quadratic recounting as the plan
cache grows.

## Fallback and audit behavior

Compiled execution now has three levels:

1. A direct or projected symbolic MIN/MAX program.
2. A cached **reduced LP kernel** when the objective-specific presolved core is
   still too expensive to project symbolically. The kernel contains only the
   variables and rows that survived equality substitution, residual-state
   updates, and sign-aware bound elimination. After the kernel is solved, the
   accounting loop continues; the day is **not** restarted.
3. A whole-day LP restart only when the reduced kernel itself cannot be built or
   evaluated safely, a previously unseen frozen-plan branch is encountered, the
   global cache budgets are exceeded, or detailed audit evidence is requested.

`plan.report()` distinguishes these paths. A day whose formulas and reduced LP
kernels all succeed has `method == "compiled+reduced_lp"` and increments
`hybrid_days`; `lp_days` counts only true whole-day restarts. The report also
separates `symbolic_program_count`, `reduced_lp_program_count`,
`formula_evaluations`, and `reduced_lp_evaluations`.

A reduced LP kernel is still exact with respect to the presolved numeric LP; it
does not approximate or truncate a symbolic expression. Its matrix structure is
cached during preparation. At runtime only the current residual RHS parameters
are populated and SciPy/HiGHS solves that local kernel.

Whole-day restart reasons still include unsupported/unseen frozen-plan branches,
nonfinite or infeasible runtime state, global cache/coefficient budgets, and a
request for detailed production audit evidence. Audit remains a deliberate native
LP path because formulas and reduced kernels do not currently provide the native
dual/evidence objects used by the production audit.

`result.solver_backend` identifies the requested production/fallback backend;
`result.solve_method` identifies the selected method. `compilation_report` is
None for ordinary LP execution. For compiled execution, reusable-plan counters
reset on every solve while program count and compilation time describe the
retained cache's lifetime.

## Budgets

```python
from ut_water_apportionment import CompilationOptions, compile_solver_input

options = CompilationOptions(
    max_variables=512,
    max_rows=5000,
    max_pairs=100000,
    max_fraction_bits=4096,
    max_plans=512,
    max_program_coefficients=1000000,
    max_total_coefficients=5000000,
    max_kernel_variables=5000,
    max_kernel_rows=100000,
    max_symbolic_variables_before_kernel=20,
    max_symbolic_rows_before_kernel=750,
    max_symbolic_pairs_before_kernel=5000,
    max_seconds_per_plan=120.0,
    max_total_compile_seconds=600.0,
    max_symbolic_seconds_before_kernel=0.05,
)
plan = compile_solver_input(solver_input, options=options)
```

Budgets keep formula generation and caching bounded; they never truncate a
formula and return an approximate allocation. `max_variables` and `max_rows` are
hard objective-specific limits. The `max_symbolic_*_before_kernel` settings are
softer preferences: crossing one stops exact projection and stores a reduced LP
kernel instead. Increase them when inspectable formulas are more important than
preparation time; decrease them when quick hybrid preparation is preferred.
Coefficient budgets count nonzero symbolic terms rather than dense zero padding.
Failed structural programs are remembered so identical daily models do not
repeatedly incur the same failed compilation.

## Code organization

- `compiled/projection.py`: bounded, exact symbolic elimination and numerical
  MIN/MAX evaluation; no water-accounting policy.
- `compiled/runtime.py`: native LP snapshots, structural cache keys, component
  uniqueness checks, and text/Python exports.
- `compiled/__init__.py`: SolverInput preparation and reusable plan API.
- `solver.py`: method selection and daily restart; both methods share the same
  `_solve_day` accounting sequence.

The compiled tests compare complete outputs against production fixtures and
exercise signed support, equal-priority iteration, ambiguity, restart after
partial progress, accounts/cumulative use, a 300-member group, audit fallback,
and optimizer-free repeated execution on supported small inputs.
