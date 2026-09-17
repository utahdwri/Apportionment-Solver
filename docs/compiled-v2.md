# Compiled v2

V2 keeps the LP generated from `SolverInput` as the authoritative mathematical
problem definition, but compilation happens on a separate parameterized IR.

## Logical transaction variables

The production LP deliberately uses one nonnegative variable per transaction
path leg, with continuity equations tying those variables together. V2 now
collapses that representation before ordinary presolve.

For example, the production LP may contain:

```text
TRXN_2___RIVER>STO
TRXN_2___RIVER>USER
CONT_TRXN_2_0
```

If the continuity row implies equal leg magnitudes, the compiler IR becomes:

```text
TRXN_2

TRXN_2___RIVER>STO  = 1.0 * TRXN_2
TRXN_2___RIVER>USER = 1.0 * TRXN_2
```

If continuity includes a loss, the multiplier is retained. For example:

```text
TRXN_2___UPSTREAM   = 1.0 * TRXN_2
TRXN_2___DOWNSTREAM = 0.9 * TRXN_2
```

The logical variable uses the production LP anchor leg's bounds. Runtime
parameter names are also aliased to the logical transaction, for example:

```text
variable[TRXN_2].remaining_upper
```

Internally that parameter still reads the authoritative anchor-variable bound
from the production LP. Original path-leg values are reconstructed from the
logical variable whenever `Apportioner` requests them.

This transformation is derived from the LP path-leg naming and literal zero-RHS
continuity equations. It does not replace the LP as the problem definition.

## Frozen parameterized IR

Mutable variable bounds, constraint RHS values, and selected LP matrix
coefficients are represented by named runtime parameters. For example:

```text
variable[TRXN_1].remaining_upper
constraint[MEAS_RIVER>USER].remaining
constraint[NF_ZONE_RIVER].remaining_upper
coefficient[CONT_TRXN_2_0,TRXN_2___UPSTREAM]
```

`ParamExpr` can represent sums, products, and quotients of parameter-only
scalars. This is important after equality substitution: a committed transaction
bound may be multiplied by a time-varying delivery coefficient without turning
the LP decision problem nonlinear. Runtime evaluates the scalar expression
first, then solves/evaluates the ordinary linear problem.

The compiler can therefore collapse transaction path variables, substitute
other equalities, remove monotone nuisance variables, and build reduced sparse
kernels without hard-coding a preparation day's numeric measurements or loss
fractions.

## Indexed runtime layout

Named parameter expressions remain the compiler/debug representation, but they
are no longer interpreted by string key during a frozen solve.  After the IR is
final, each program lowers its live parameter names exactly once to a compact
integer layout:

```text
0 -> variable[TRXN_1].current
1 -> variable[TRXN_1].remaining_upper
2 -> constraint[MEAS_RIVER>USER].remaining
3 -> constraint[NF_ZONE_RIVER].remaining_upper
```

The corresponding runtime algebra is stored as `IndexedParamExpr` objects and
evaluated against a reusable contiguous NumPy array.  Thus a compiler expression
such as:

```text
constraint[MEAS_RIVER>USER].remaining - variable[TRXN_1].current
```

is lowered once to the equivalent of:

```python
p[2] - p[0]
```

Direct scalar programs, equal-priority kernels, reduced-LP fallbacks, guards,
and frozen path reconstruction all use this indexed execution form.  The named
`ParamExpr` trees are retained so `formulas()` can continue to show meaningful
water-accounting names and algebraic proof conditions.

Residual constraint state is similarly frozen into one `ResidualLayout` per
structural regime.  Its lower and upper sides are contiguous arrays, and each
`ResidualEffect` carries the integer row index resolved during compilation.
Parameterized residual coefficients (for example a time-varying loss factor)
are lowered into a small per-regime indexed coefficient frame that is populated
once at the start of the day. A transaction commit therefore updates residual
rows by index and evaluates any dynamic coefficient from arrays rather than
building a temporary name/value mapping.

This lowering is deliberately a boundary between readable compiler IR and
execution IR.  It is also the intended input to executable-Python generation:
`plan.code()` can emit ordinary Python against the already-frozen integer
layouts rather than re-parsing parameter names or rebuilding expression trees.
`IndexedParamExpr.python_text()` already renders its lowered expression in that
form (for example `p[2] - p[0]`).

## Execution modes

Each prepared objective becomes either:

1. **Direct scalar equation** — normally a spreadsheet-like `MIN(...)` or
   `MAX(...)`; or
2. **Frozen reduced LP kernel** — a small transformed residual problem whose
   matrix sparsity is built during preparation. Runtime refreshes only numeric
   coefficient/RHS/bound parameters.

There is intentionally no whole-day LP fallback in this branch.

### Early direct sequential compilation

Ordinary unique-priority transactions no longer enter the generic per-objective
clone/presolve pipeline just to rediscover a one-variable formula. Once per
structural regime, V2 builds a shared residual logical model, projects reporting
slacks, and classifies transaction increments that are structurally safe at
zero. It also records residual-state monotonicity proofs such as the natural-flow
lower-side invariant: an NF row starts each day with lower bound zero, and
committing nonnegative transaction increments can only make its remaining lower
side more negative.

For a unique-priority transaction whose sparse logical column is monotone, V2
therefore compiles directly to the expected residual formula:

```text
TRXN = TRXN.current + MIN(
    TRXN.remaining_upper,
    MEASUREMENT.remaining / measurement_coefficient,
    NATURAL_FLOW.remaining_upper / nf_coefficient,
    ... any other capacity rows touched by this transaction ...
)
```

The compiler never materializes the junior transaction columns in that
objective-specific program. The same logical-column coefficients drive the
explicit residual updates after the assignment. Signed/counterflow structure,
storage recourse, coupled equalities, uncertain coefficient signs, or any failed
zero-safe proof bypass this fast path and use the existing exact generic
compiler/reduced-LP machinery. `V2CompilationOptions` exposes
`enable_early_direct_sequential=False` for fallback testing and diagnostics.

## Guarded regional simplifications

Every parameter-dependent simplification now carries its actual algebraic
proof obligation. For example, removing an LP lower side is represented as:

```text
REQUIRE minimum_possible(row) >= row.lower
```

and removing an upper side is represented as:

```text
REQUIRE maximum_possible(row) <= row.upper
```

`formulas()` prints the substituted parameter expression used for that
comparison rather than an opaque message such as "side remains redundant".

Before creating a runtime guard, V2 first tries to prove the predicate over
the full declared parameter domain. If the predicate is structurally proven,
the simplification is unconditional and no runtime guard is emitted. If it is
only valid in part of parameter space, V2 freezes both:

1. the compact guarded program; and
2. a conservative guard-free alternate compiled without guarded redundancy
   removal.

Preparation validates this coverage as a compiler invariant. A frozen plan is
not allowed to contain a guarded program without a guard-free alternate for the
same structural objective. At runtime, guard failure therefore selects an
already-frozen alternate; it never triggers compilation or a whole-day LP
fallback.

## Structural preparation model

`compile_solver_input_v2(problem)` no longer traces the supplied date range by
solving accounting days. The compiler graph is derived from LP/transaction
structure. Numeric daily quantities remain parameter slots.

Preparation builds a representative LP only at actual structural transitions:

- the start of the solve period; and
- starts/ends of consecutive external-natural-flow boundary-presence runs.

Time-varying **constant fractional endpoint losses do not create a new
structural regime**. The continuity matrix position is frozen and its numeric
coefficient is supplied as a runtime parameter. Therefore a long period with
the same topology has one compiler regime even when fractional loss values
change every day.

This parameterized-coefficient representation is also the intended foundation
for piecewise-linear losses. Segment selection is not implemented here yet;
piecewise losses will require a structural segment representation (or a small
residual LP/MILP kernel), but the segment coefficients can use the same scalar
parameter-expression machinery.

Unique sequential priorities first attempt the early sparse-column compiler
described above. Only objectives that fail that structural proof enter the
generic transformed/presolve pipeline. Each allocatable equal-priority cohort is
compiled once, after path-leg collapse and residual-state rebasing, as an
``EqualPriorityProgram`` over one logical variable per transaction. At runtime
an active subset supplies only its proportion factors. The cohort solve adds one
scalar common increment ``g`` and sparse inequalities
``dTRXN_i >= factor_i * g``; it does not recreate path-leg variables,
``combined`` variables, or temporary production-LP rows. The same frozen logical
cohort IR is reused for blocked-member classification and for members deferred
because their factor is numerically tiny. Transactions that have no allocatable
LP variable are not cohort members.

Storage/counterflow ambiguity tie-breaking remains a separate structural
auxiliary operation. Reporting slacks are not solver outputs: one-direction
residual slacks are projected analytically, pure bidirectional reporting rows
are removed entirely, and final slack values are calculated from the leftover
measurement. Storage-related bidirectional rows retain transient residual-proxy
columns only where the allocation ambiguity convention needs them. These
runtime operations never extend the frozen compiler program cache.

The daily routine intentionally performs the lexicographic priority schedule
twice. After pass one, storage-to-natural reporting residuals are calculated
directly from leftover measurements, credited to natural flow, and fixed for
the reallocation pass. This conservative two-pass structure avoids specialized
spill control-flow compilation while preserving storage/natural-flow behavior.

## Explicit residual-state execution IR

V2 now separates objective compilation from execution-state progression.
`compile_solver_input_v2()` builds a `DayExecutionProgram` whose runtime nodes
own the accounting-day sequence:

```text
initialize parameterized LP + residual state

PASS 1
    assign transaction from frozen direct formula / reduced kernel
    compute transaction.increment
    update residual constraint state using collapsed-LP coefficients
    update natural-flow state

derive reporting residuals
    slack_forward = MAX(leftover_measurement, 0)
    slack_reverse = MAX(-leftover_measurement, 0)
    credit storage-to-natural residuals to natural flow

PASS 2
    rerun the same priority routine

derive final reporting slacks
reconstruct non-anchor path legs from frozen continuity equations
```

Residual effects are derived from the compiler LP *after* path-leg variables
have been collapsed to one logical transaction variable.  They are therefore
not hard-coded from `PathTrxn` path semantics.  Parameterized coefficients,
including time-varying fractional-loss coefficients, are retained in the
residual update expressions.

For example, a simple compiled routine renders as:

```text
TRXN_1 = MIN(...)
TRXN_1.increment = TRXN_1 - TRXN_1.before
constraint[MEAS_RIVER>USER].remaining -= TRXN_1.increment
constraint[NF_ZONE_RIVER].remaining_upper -= TRXN_1.increment
```

Reduced LP kernels now operate on the **final transformed residual IR**, not on
the mutable production-LP rows.  Before presolve, every logical transaction is
rebased from an absolute total `x` to a nonnegative remaining increment
`dx = x - x.current`.  Source constraint sides touched by those transactions
are bound to explicit `ResidualState` slots.  As the day program commits an
allocation, those same residual slots are decremented, so a later kernel sees
only the capacity that actually remains.

This has two important consequences:

- committed senior transaction columns can disappear completely from later
  kernels when their lexicographic value is structurally frozen; and
- reconstruction still returns the absolute production value as
  `x.current + dx`, so callers do not see the residual representation.

Storage/counterflow tie breaking is the deliberate exception to unconditional
senior elimination.  That convention temporarily locks opposing-direction
variables and can change the feasible region between priorities.  A senior
transaction involved in such a directional ambiguity is therefore retained,
when needed, only as a zero-based **residual recourse increment**.  Guarded
presolve can still eliminate it when the applicable algebra proves it
unnecessary; the conservative alternate keeps it otherwise.

The production engine remains synchronized as the source of runtime scalar
parameters such as transaction caps and parameterized loss coefficients, but
`ReducedLPProgram` freezes its own sparse rows during preparation and reads
constraint RHS values from `ResidualState`.  It does not rebuild or solve the
production LP matrix at runtime.

Equal-priority water filling is an explicit compiled logical-transaction
operation. Its common-increment solve, blocked-member classification, and
tiny-factor scalar continuation all evaluate the same frozen transformed
cohort IR against current ``ResidualState``. Only the active member set and
runtime factors change between water-filling iterations. Storage/counterflow
directional ambiguity remains an explicit auxiliary operation. Reporting
slacks and spill amounts are direct residual calculations, and final path-leg
values are reconstructed algebraically rather than by a final LP solve.


## Derived reporting slacks

Slack transactions are reporting variables. V2 does not trust an LP solution
for their final values. For a measured flow, the explicit residual state keeps
the measurement value minus committed transaction contributions. Final slack
values are therefore calculated directly:

```text
forward_slack = MAX(remaining_measurement, 0)
reverse_slack = MAX(-remaining_measurement, 0)
```

For a one-direction reporting slack, elimination of `row + slack = measured`
leaves the exact one-sided allocation constraint `row <= measured`. For a pure
bidirectional reporting pair, existentially eliminating both nonnegative slack
variables leaves no allocation constraint, so V2 removes the entire row from
the compiler model.

Storage-related bidirectional rows are the exception only for allocation
*tie-breaking*: their LP columns can remain as transient directional-residual
proxies while the historical counterflow ambiguity convention is applied.
Those transient values are never reported. After allocation, the user-facing
slack values are overwritten by the direct leftover-measurement calculation
above.


## Generated Python execution (`plan.code()`)

After the named compiler IR has stabilized and been lowered to integer parameter
and residual layouts, V2 performs one final lowering step into ordinary Python.
The same source returned by `plan.code()` is compiled during plan preparation
and is used by `plan.solve()` by default.

For a simple sequential right, the generated source has the same shape as the
spreadsheet-style formula, but uses frozen integer residual offsets directly:

```python
vb_lb, vb_ub = engine.get_variable_bounds("TRXN_1___RIVER>USER")
active = min(
    vb_ub - vb_lb,
    0.5 * (r_lower[0] + r_upper[0]),
    r_upper[2],
)
trxn_1 = vb_lb + active

r_lower[0] -= active
r_upper[0] -= active
r_lower[2] -= active
r_upper[2] -= active
```

The comments immediately above each generated assignment retain the semantic
formula (`variable[...].remaining_upper`, `constraint[...].remaining`, etc.)
so integer offsets can be related back to the readable compiler IR.  The code
also performs the required production-bound synchronization and natural-flow
commit after each direct assignment.

Not every structure should be inlined. Equal-priority cohorts retain their
compiled logical common-increment kernels because their active membership and
proportion factors are daily state. Storage/counterflow ambiguity and other
coupled residual problems call an already-frozen reduced kernel. Thus generated
Python changes execution dispatch, not the mathematical fallback hierarchy.

Code generation is conservative. A scalar assignment is inlined only when its
coefficient signs are structurally bounded away from zero, it has no runtime
guards, it does not require directional-ambiguity recourse, and its requested
value reconstructs from that one active logical variable. Anything outside
those proof conditions remains a prebuilt-kernel call.

The reference indexed interpreter can be retained with:

```python
plan = compile_solver_input_v2(
    problem,
    options=V2CompilationOptions(enable_generated_python=False),
)
```

This is used by tests to verify that generated execution and the frozen
execution IR return the same apportionments.

## Large-system benchmark

An opt-in benchmark in `tests/test_large_system_benchmark.py` compares V2 with
the direct production SciPy/HiGHS LP implementation on a deterministic large
fixture. The benchmark runs two default 10-zone / 500-transaction layouts:

* **shared priorities** — 50 equal-priority cohorts of 10 rights; and
* **unique priorities** — 500 distinct lexicographic priorities on the same
  physical network.

It measures V2 compilation separately from warmed daily execution and checks
that every reported apportionment matches the production SciPy result before
reporting performance.

Run the two large layouts independently so each process starts with a clean
allocator/native-memory state:

```bash
RUN_LARGE_SYSTEM_BENCHMARK=1 \
  python -m unittest \
  tests.test_large_system_benchmark.LargeSystemBenchmarkTests.test_v2_shared_priority_compile_and_daily_solve_vs_production_scipy -v

RUN_LARGE_SYSTEM_BENCHMARK=1 \
  python -m unittest \
  tests.test_large_system_benchmark.LargeSystemBenchmarkTests.test_v2_unique_priority_compile_and_daily_solve_vs_production_scipy -v
```

The two layouts are normally run independently so each timing starts with a
clean interpreter/native allocator state. The early sequential compiler now
keeps the unique-priority programs compact (one logical variable per directly
compiled transaction), so this separation is primarily benchmark hygiene rather
than a solver requirement.

The benchmark is skipped during normal test discovery because wall-clock
performance is hardware-sensitive. It deliberately does **not** assert that
one implementation must be faster than the other. Optional environment
variables can change the problem/repetition size:

```text
BENCHMARK_STREAM_ZONES=10
BENCHMARK_TRANSACTIONS_PER_ZONE=50
BENCHMARK_COMPILE_REPEATS=1
BENCHMARK_SOLVE_REPEATS=3
BENCHMARK_V2_SOLVE_REPEATS=3
BENCHMARK_SCIPY_SOLVE_REPEATS=1
```

The output reports production LP dimensions, SciPy LP solve count, number of
frozen V2 programs, V2 compile time, median warmed V2 daily time, SciPy daily
time, the repetition counts used for each, and both daily and
compile-plus-first-day ratios. SciPy defaults to one repeated comparison solve
because the 500-unique-priority comparison is memory-heavy; V2 defaults to
three warmed repetitions.

### Daily execution fast path

The runtime path is intentionally biased toward doing more work during
preparation so repeated accounting days stay cheap:

* Counterflow tie-breaking reuses feasible optimal solution vectors. After
  minimizing the primary sum, a component already exactly at its lower bound
  has a proven minimum and needs no scalar LP solve. Otherwise the original
  ordered scalar minimization runs and returns a fresh witness for subsequent
  components. Values merely near their bounds do not qualify; historical
  floating-point cleanup invalidates the witness before another shortcut can
  use it. Temporary component bounds and the primary-sum equality are restored
  even if a later tie-break fails.
* Equal-priority blocked-member classification reuses the maximum-sum witness.
  A member with a positive increment exceeding the allocation tolerance is
  proven able to increase and needs no recursive classification. Zero witness
  increments are unresolved, not proof of blockage: on a nonunique optimal
  face the shared capacity may have been assigned to another member. Only the
  unresolved subset is solved again. These witness values are never committed
  as allocations; the common-increment routine still determines allocation.
* `report()` includes `execution_lexicographic_bound_shortcuts` and
  `execution_classification_witness_shortcuts`. Legacy runtime counters such
  as `auxiliary_kernel_solves` and `derived_slack_values` now reset with every
  `plan.solve()` alongside the `execution_*` counters. Preparation counters
  remain unchanged.

* The indexed frozen program is lowered to executable Python once. Direct
  sequential priorities no longer dispatch through `DirectScalarProgram` at
  runtime: generated code performs the interval `min/max` calculation and
  residual-array updates directly. Source-constraint parameter slots are
  resolved to `r_lower[...]` / `r_upper[...]` offsets at generation time, so
  the hot path does not copy them through a temporary parameter array.

* The day execution IR installs its already-resolved structural regime on the
  V2 engine. Equal-priority operations use a direct `(regime, member)` index to
  reach the frozen cohort family instead of rebuilding an objective/LP
  structural fingerprint and scanning all compiled cohorts.
* Guard selection and kernel execution share one runtime-parameter evaluation;
  the selected kernel does not reread the same parameter set.
* Runtime parameters are collected only from expressions that survived into
  the final transformed IR. Source-LP parameter defaults and residual bases
  eliminated by presolve are not refreshed merely because they existed before
  compilation.
* Unique sequential priorities first use the **early direct MIN compiler**. A
  shared residual logical model proves zero-safe transaction columns once, and
  each eligible objective is compiled by inspecting only the sparse rows touched
  by that transaction. This skips objective-specific full-model cloning,
  residual rebasing, junior elimination, and guarded/conservative duplicate
  compilation.
* For structures that fail the early proof, conservative reduced kernels still
  compile the older **lower-bound scalar projection** when possible. That exact
  fallback retains the reduced LP if the canonical witness is infeasible for a
  later parameter state.
* Final path-leg reconstruction expressions are frozen during preparation.
  Daily execution evaluates those expressions directly and never rebuilds a
  `CompilerModel` or reruns path collapse just for reporting.
* A single structural-regime plan uses its sole frozen regime directly rather
  than rescanning the production LP to recompute a regime signature each day.
* Runtime `GraphManager`, transaction structure, priority groups, natural-flow
  source lookups, and lag metadata are frozen once. Per-run objects copy only
  mutable accounting/cumulative/day state instead of revalidating and
  rebuilding the entire 500+ transaction structure.
* Small reduced kernels use dense numeric matrices, while large conservative
  fallbacks remain sparse. This avoids CSR construction overhead when the
  transformed problem is only a few logical variables.
* Equal-priority variant selection explicitly tries the guarded reduced kernel
  before its conservative fallback. Program identifiers such as `V2P9` and
  `V2P10` are never used as an implicit ordering.
* When final equal-priority IR proves that every live cohort variable is a
  zero-based residual increment and every remaining capacity row consumes
  those increments monotonically, the common increment is calculated directly
  as the minimum residual-capacity ratio. No SciPy/HiGHS call is made for that
  cohort operation. The conservative LP kernel remains frozen as the fallback
  when those proof conditions do not hold.
* For equal-priority-only plans whose frozen IR does not read dynamic natural-
  flow matrix coefficients, production-LP NF coefficient writes are deferred.
  If an auxiliary production-LP solve is actually needed, the coefficients are
  materialized lazily before that solve.
* The historical second allocation pass is retained for storage systems and
  systems with a possible physical spill credit. If the compiled graph has
  neither possibility, V2 omits that provably duplicate pass.

These are execution-only optimizations: they do not change the allocation
mathematics, and the large-system benchmark still verifies all reported
apportionments against the direct SciPy production-LP implementation. On the
reference development environment after generated-Python lowering, the
shared-priority 500-transaction layout runs in roughly 0.013 seconds per day,
while the unique-priority layout is roughly 0.014 seconds per day and compiles
in only a few seconds rather than tens of seconds. The unique-priority generated
path is roughly four times faster than the indexed execution-IR interpreter in
the same environment. Both layouts are substantially faster than the direct
SciPy production solve. Exact measurements from the packaged tree are reported
by the benchmark itself; treat wall-clock numbers as illustrative rather than
test thresholds.
