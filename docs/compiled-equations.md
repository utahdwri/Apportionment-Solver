# Compiled equations with LP fallback

The alternate method uses the **current production LP rows and allocation
schedule**. It compiles small linear objective systems into reusable MIN/MAX
expressions. A day that cannot safely use those expressions is restarted using
the selected LP backend. Unsupported compilation does not mean unsupported
accounting input.

This integration starts from `main` at
`df099710e7e83077c5a07bdc27dc6888ebb795a0`. It does not import the earlier
standalone prototype or reproduce its separate accounting-graph adapter.

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

The compiler eliminates exact equalities first, then uses Fourier–Motzkin
projection. For a scalar objective, it derives upper bounds (a MIN), lower bounds
(a MAX), and inequalities defining valid daily inputs. Minimizing selects the
lower bound; maximizing selects the upper bound.

If an objective requests several variable values, knowing the optimum sum is
insufficient. The compiler also projects the range of each requested variable
while fixing that sum. Nonunique components trigger LP fallback, preserving
main's handling of otherwise ambiguous endpoint caps, spills, and finalization.

`compile_solver_input()` eagerly prepares the first day's individual transaction
objectives without running an optimizer. Additional patterns are compiled **on
first encounter during execution**, within the same budgets. This is a bounded
hybrid compiler, not exhaustive advance compilation of every possible branch.

## Inspect the formulas and code

`plan.formulas()` returns the actual symbolic expressions. For example, an
individual diversion can produce bounds of the following form:

```text
upper = MIN(
    variable[right_a___diversion].upper,
    -variable[right_b___diversion].lower
      -variable[SLACK_river_TO_farm_diversion___diversion].lower
      +constraint[MEAS_diversion].value,
    -variable[right_b___diversion].lower
      +constraint[NF_ZONE_river].upper,
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
of the group in advance.

Hundreds of equal-priority transactions are accepted, but large coupled systems
currently use LP fallback. The default variable budget counts the entire LP,
including residual and temporary variables. This version does not claim a
formula-only fast path for several hundred transactions or decompose the graph
into independently compiled blocks. Increasing budgets is optional and can be
expensive.

## Fallback and audit behavior

Fallback restarts the **whole day**, using a fresh native solver and fresh
natural-flow calculator. Tentative parent reservations, spill credits, and
allocations are discarded. Accounts and cumulative limits are committed only
once, after the final successful daily result. This also gives ambiguous
objectives the same ordinary backend solve sequence instead of freezing a
compiler-selected tie break.

Fallback reasons include:

- Variable, row, pair, coefficient-size, cache, or compilation-time budgets.
- Nonunique requested components at the objective optimum.
- Unbounded, nonfinite, or numerically uncertain compiled intervals.
- Failed projected feasibility conditions; the LP retains its usual feasibility
  relaxation/error behavior.
- A request for detailed production audit evidence.

The last case is deliberate: formulas do not currently provide native dual
values. `solve(..., method="compiled")` retains the existing
`generate_audit=True` default and consequently takes the LP path. Specify
`generate_audit=False` to use formulas, or use `plan.solve()` whose default is
False. `plan.report()` supplies execution coverage and fallback explanations;
it is not a replacement for the detailed production accounting audit.

`result.solver_backend` identifies the native/fallback backend;
`result.solve_method` identifies the selected method. `compilation_report` is
None for ordinary LP execution. For compiled execution it reports compiled days,
LP days, formula calls, discarded calls, cache hits, compilation cost, and reasons.
For a reusable plan, daily counters reset on every solve, while program count
and compilation time describe the retained cache's lifetime.

## Budgets

```python
from ut_water_apportionment import CompilationOptions, compile_solver_input

options = CompilationOptions(
    max_variables=32,
    max_rows=400,
    max_pairs=2500,
    max_fraction_bits=1024,
    max_plans=64,
    max_program_coefficients=50000,
    max_total_coefficients=250000,
    max_seconds_per_plan=1.0,
    max_total_compile_seconds=5.0,
)
plan = compile_solver_input(solver_input, options=options)
```

Budgets keep formula generation and caching bounded; they never truncate a
formula and return an approximate allocation. Failed structural programs are
remembered so identical daily models do not repeatedly incur the same failed
compilation. A day can still be more expensive than an LP day because it may
try formulas and then restart. This version establishes compatibility and an
inspection interface; performance benefits should be measured on actual inputs.

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
