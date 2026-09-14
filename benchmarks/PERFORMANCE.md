# Apportionment solver performance investigation

Baseline: upstream commit `882704a86ea56bcd536aa9fd25d4374b1016a859`.

The delivered changes retain the existing daily lexicographic LP, priorities,
proportional allocation, spill pass, feasibility fallback, and default backend.
They reduce repeated Python work and avoid a narrowly defined redundant solve.
They do not implement fractional lags or active-segment allocation.

## Measured results

| Workload | Backend | Original (s) | Optimized (s) | Speedup |
| --- | --- | ---: | ---: | ---: |
| 200 rights / 20 reaches / 365 days | highspy | 29.336 | 18.431 | 1.59x |
| 2,000 rights / 100 reaches / 7 days | highspy | 67.535 | 25.487 | 2.65x |
| 200 rights / 20 reaches / 30 days, audit | highspy | 2.518 | 1.658 | 1.52x |
| 200 rights / 20 reaches / 30 days, equal priority + audit | highspy | 1.371 | 0.931 | 1.47x |
| 200 rights / 20 reaches / 7 days | highspy | 0.544 | 0.341 | 1.60x |
| 200 rights / 20 reaches / 7 days | glop | 2.588 | 2.479 | 1.04x |
| 200 rights / 20 reaches / 7 days | scipy | 5.330 | 5.256 | 1.01x |

Audit is off unless the workload says otherwise. In the year-long ablation,
the coefficient cache alone took **25.261 seconds**, compared with 29.336
seconds originally and 18.431 seconds with all retained changes. The combined
changes are the recommended configuration for the measured workloads.

For the same seven-day, 200-right input, the modified native HiGHS backend
was about **7.3x faster than GLOP and 15.4x faster than SciPy** here. If an
installation currently falls back to SciPy, installing highspy can matter
more than the code changes themselves.

These are synthetic, connected river chains with one measured diversion per
reach and multiple competing rights at each diversion. Supply varies on a
seven-day cycle. Each right's transaction path has one leg; the natural-flow
constraints propagate through the connected reaches. The proportional case
gives rights at different diversions equal priorities. The larger case is
**not** a statewide dataset or a benchmark of long transaction paths.

Times are medians of three end-to-end `solve()` calls, run sequentially in
separate processes for each configuration. Input construction, output JSON
serialization, and initial backend import are outside the timer; model building,
allocation, audit generation when enabled, and result assembly are inside it.
No profiler was enabled for the reported timings. These are descriptive timings,
not a statistical significance test. The final repetition's full output was
retained for comparison. See `results/scaling.json` for individual timings.

Runtime: Python 3.12.13, Linux x86-64, AMD EPYC 9V74 host; NumPy 2.3.5,
SciPy 1.17.0, highspy 1.15.1, OR-Tools 9.15.6755. HiGHS uses the existing
single-thread dual-simplex configuration. Absolute times depend on the machine
and input; do not extrapolate them directly to statewide runs.

## Changes retained

1. **Cache natural-flow routing coefficients within a daily calculation.**
   Availability checks and committed allocations previously walked the same
   downstream route and resolved its constant loss fractions repeatedly.
   Internal callers now reuse the coefficients. The public getter returns a
   copy, so a caller cannot corrupt the cache. Every `calculate()` call clears
   it, even when recalculating the same date with different boundaries.
   `invalidate_nf_coefficients()` is also available for future segment changes.

2. **Preserve HiGHS audit evidence only when model rows change.**
   The original wrapper copied all coefficients, bounds, incidence lists, and
   named solution dictionaries after every solve, including unaudited runs.
   The new implementation stores solution arrays with indexed name lookups.
   A row's old coefficients and bounds are copied before its first subsequent
   mutation, only if they belong to the last successful solve. Evidence is
   assembled when requested. Temporary proportional constraints remain
   auditable after they are cleared. Row insertion and coefficient changes
   also preserve the previous variable-to-row membership.

3. **Avoid redundant coefficient bookkeeping.**
   Adding a nonzero coefficient no longer searches the full row and column
   membership lists to discover whether the entry is new; its previous
   coefficient already answers that. Identical coefficient and row-bound
   assignments are skipped.

4. **Skip maximization of a transaction already fixed at its committed value.**
   This requires exact equality of lower bound, upper bound, and committed
   value. It does not classify nearly full rights using a tolerance. Audited
   runs retain the normal solve so their objective evidence and records remain
   available. Transactions that do not meet this exact fixed-value condition
   continue through the existing solver.

No new setting is required. For production runs where the step-by-step audit
is unnecessary, use the existing option:

```python
results = solve(input, solver_backend="highspy", generate_audit=False)
```

The default remains `generate_audit=True`. Install the native backend with
`python -m pip install -e '.[highs]'` when working from this checkout.

## Validation

The original suite passed on the baseline and modified code separately with
HiGHS, GLOP, and SciPy: **110 passed, 5 skipped, 7 subtests passed** for each
backend. A wrapper captured 58 successful public `solve()` calls per backend;
all captured results, including audit records, matched the corresponding
baseline exactly (174 comparisons in total).

The expanded suite reports **118 passed, 6 skipped, 7 subtests passed** in the
combined process. Five skips were already present upstream. The sixth is the
new GLOP comparison test when OR-Tools is loaded after highspy: these installed
native builds have a symbol-loading conflict. That test **passes separately**
in a fresh process, as does the complete original suite forced to GLOP. No
package-version workaround was added to the project.

New tests cover defensive cache copies, cache reuse, invalidation on the same
and next date, changing losses, saved evidence after coefficient removal and
row insertion, added variables, proportional-row cleanup, and exact-bound
shortcut behavior with and without audit generation. All seven benchmark outputs matched the corresponding baseline exactly
(150,394 apportionment records in total). An independent remaining-capacity
calculation also checked all 103,200 non-slack transaction/date values in
those optimized outputs, within 1e-6. Comparison results accompany the timings.

## What was tried but not selected

A preliminary 2,000-right, seven-day run using HiGHS primal simplex (strategy 4)
took 28.3 seconds versus 25.2 seconds for the modified dual-simplex version
(strategy 1). This was a single exploratory comparison, not a three-repeat
benchmark or proof that dual simplex always wins. It provided no reason to
change the default. The benchmark exposes `--simplex-strategy` to retest it on
other inputs.

The second allocation pass remains in place. A zero spill total alone does
not establish that the feasible allocation set is unchanged: the repository
already documents storage-delivery cases requiring that pass.

## When direct transaction allocation is safe

Your intuition about simple transactions is sound. Suppose elimination of
path-continuity equations and suitable residual slack variables yields only
resource constraints `A x <= b`, all allocation coefficients are nonnegative,
and uncommitted junior allocations can remain at their lower bounds. For a
single priority, the next allocation is the minimum of its unused right limit
and each affected resource's remaining capacity divided by its coefficient.
This needs no LP optimization.

The difficult part is establishing that reduced model. Measured flows are
represented by equalities; shared accounts, storage, directional slacks,
returns, group variables, and loss factors can create dependencies. Merely
checking that a transaction has a unique priority and no reverse path is not
an adequate certificate. Increasing one variable with all other variables
held at their current LP values can also falsely report a bottleneck, because
some of those variables are free to adjust.

A next implementation should therefore build a reduced resource model and
use direct allocation only for components that pass an explicit structural
check, with full-LP fallback for all others. Validate it against the current
LP on generated systems with shared limits, losses, storage, reversals, and
accounts. The present changes avoid that additional allocation engine and its
maintenance cost while producing a measured improvement.

## Larger systems, fractional lags, and piecewise-linear losses

Recommended next steps, in order:

- **Benchmark actual larger inputs.** The synthetic benchmark does not expose
  long transaction paths, complex nested limits, or large account ledgers.
  Retain model dimensions, number of nonzeros, solve count, simplex iterations,
  and separate model-build, optimization, and audit times for representative
  real inputs before selecting the next major refactor.
- **Separate independent accounting components.** Parallelize components only
  after accounting for all shared constraints, natural-flow dependencies,
  transactions, and accounts. Watershed boundaries alone do not prove
  independence. A common unconstrained SYSTEM_GAIN_LOSS node also does not by
  itself prove real coupling. Daily parallelization is not generally safe:
  account balances and cumulative transaction limits already carry state
  between dates.
- **Reduce long-path variables and continuity rows.** Under current constant
  fractional losses, downstream path values are fixed multiples of the anchor.
  Substitution can remove many variables and equalities while retaining a
  mapping back to path-item results. Check zero/100% losses and preserve
  auditable interpretations. This is likely more valuable for long paths than
  for the one-leg transactions benchmarked here; it remains unimplemented.
- **Reuse a daily model template where valid.** The code rebuilds its native LP
  each day. Reuse must reset allocation bounds, temporary proportional rows,
  feasibility slacks, daily measurements, account limits, cumulative limits,
  and date-dependent coefficients. Simply moving `Apportioner` construction
  outside the loop is unsafe.
- **Represent fractional lags as sparse interday coupling.** A lag of
  `k + alpha` days gives a downstream arrival contribution of
  `(1-alpha) * x[t-k] + alpha * x[t-k-1]`. It remains linear, but couples dates.
  Define initialization and end-of-period treatment, and explicitly decide
  priority ordering across dates. Use a time-expanded LP as the correctness
  reference. A rolling horizon needs boundary commitments and can differ from
  a full-horizon optimum when future constraints matter.
- **Treat piecewise-loss segments as model state.** Within an active segment,
  losses are affine. A segment change can require coefficient, intercept,
  segment-bound, and downstream-state updates together. Clear the new routing
  cache whenever that state changes. The current `get_fraction()` still
  rejects nonconstant transaction losses, so these changes do not silently
  approximate them. An active-segment method must establish how it chooses
  among feasible segments; local segment stepping is not automatically a
  globally correct solution for nonconvex loss relations.

The routing cache trades CPU for memory. A chain can require quadratically
many source-to-downstream coefficients, and the current expanded natural-flow
LP itself also grows with these dependencies. This patch does not remove that
scaling limit or the existing recursive graph traversals. It is an incremental
performance improvement, not a claim of statewide readiness.

## Reproduction

From the delivered repository root:

```bash
python -m pip install -e '.[all,dev]'
python -m unittest discover -v -s tests -t .
PYTHONPATH=src python benchmarks/benchmark_scaling.py --reaches 20 --rights 10 --days 365 --repeat 3
PYTHONPATH=src python benchmarks/benchmark_scaling.py --reaches 100 --rights 20 --days 7 --repeat 3
PYTHONPATH=src python benchmarks/benchmark_scaling.py --reaches 20 --rights 10 --days 30 --audit --proportional --repeat 3
```

Use `--backend glop` or `--backend scipy` for backend comparisons, `--output`
to save full results, and `--profile` for a diagnostic profile (not timing
comparisons). Install OR-Tools separately for GLOP. Run different native
backends in separate processes with the versions tested here.

From a Git clone containing the benchmark script, benchmark the upstream
reference without replacing your working tree (the ZIP itself has no Git history):

```bash
git worktree add ../apportionment-baseline 882704a86ea56bcd536aa9fd25d4374b1016a859
PYTHONPATH=../apportionment-baseline/src python benchmarks/benchmark_scaling.py --reaches 20 --rights 10 --days 365 --repeat 3
```

The download includes the complete modified source, tests, benchmark, this
report, recorded timings, and a patch against the stated upstream commit.
The GitHub repository has not been pushed or changed.
