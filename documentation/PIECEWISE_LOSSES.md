# Piecewise-linear losses

Allocation-dependent piecewise-linear losses support signed/scaled transaction
components, two priority attribution conventions, delivery-weighted sharing by
equal-priority cohorts, and downstream natural-flow availability. Native
**highspy** handles the mixed-integer graphs. The optional **SCIP** backend handles
the nonlinear sharing constraints when several equal-priority paths cross one
loss site. Constant-only inputs retain the existing LP.

This is a correctness-first implementation using mixed-integer segment selection.
It is materially slower than the constant-loss LP. It is not a demonstration of
statewide capacity; measurements on a representative basin remain necessary.

## Configure a curve

Install from this checkout:

```sh
python -m pip install -e '.[scip]'
```

Points specify **inflow and absolute loss**, in the same flow units as the input:

```python
from ut_water_apportionment import (
    FlowMeasurement, InterzoneFlow, LossCurvePoint, LossDefinition, solve,
)

loss = LossDefinition.piecewise_linear([
    LossCurvePoint(inflow=0, loss=0),
    LossCurvePoint(inflow=20, loss=20),
    LossCurvePoint(inflow=60, loss=40),
    LossCurvePoint(inflow=100, loss=44),
])
reach = InterzoneFlow(
    id="A>B", from_zone="A", to_zone="B",
    flow_measurements=[FlowMeasurement("A>B")],
    loss_to_zone=loss,
)
# Include reach in your SolverInput.accounting_graph, then:
# solver_input.loss_attribution_method = "depletion"  # or "buildup"
# result = solve(solver_input, generate_audit=True)
```

The `scip` extra includes both highspy and PySCIPOpt. If no equal-priority paths
share a curve, the smaller `highs` extra suffices. `solver_backend="auto"` selects
SCIP when shared cohorts occur during the requested dates. An explicit backend
choice is respected and produces an installation/capability error if necessary.

Loss is interpolated between points and capped at the last absolute loss above
the last point. An omitted origin is inserted automatically. A 100 cfs inflow in
this example delivers 56 cfs; above 100 cfs the absolute loss stays at 44 cfs.

The curve must be continuous, start at (0, 0), and satisfy `0 <= loss(Q) <= Q`.
Delivered flow `R(Q) = Q - loss(Q)` must be non-decreasing. Absolute loss itself
may decrease over a finite segment. Raw `ResolvedLossRelation` definitions are
validated against these conditions too.

`LossDefinition.time_varying_piecewise_linear([LossInterval(...)], default=...)`
selects a curve by accounting date, including transitions between piecewise
curves and constant fractions. `LossInterval` is exported by the package.

## Accounting rules

There are two distinct reference flows:

1. **Natural-flow availability:** withdrawing `d` from remaining natural flow
   `Q` reduces downstream availability by `R(Q) - R(Q-d)`. Both endpoint losses
   are composed when present. Remaining-flow balance equations couple the
   stream zones, so an allocation can cross several segments in one solve.
   Removing 90 from 100 in the example reduces downstream availability by 56,
   not by 90 times the slope at 100.
2. **Transaction paths:** process cohorts in ascending numeric transaction
   priority. `SolverInput.loss_attribution_method` selects their reference pool:

   | Setting | Initial pool | Next pool for signed cohort component `x` | Cohort delivery at `loss_to_zone` |
   | --- | --- | --- | --- |
   | `"depletion"` (default) | Measured flow `M` | `Q - x` | `R(Q) - R(Q-x)` |
   | `"buildup"` | Zero | `Q + x` | `R(Q+x) - R(Q)` |

   At `loss_from_zone` the gauge is after the loss: use differences of the
   inverse remaining-flow curve to infer upstream components. Residual/slack
   flow is processed last. The setting applies to path attribution at modeled
   piecewise flows, including their other endpoint; it does not change the
   natural-flow availability rule or constant-only models.

Physical measured flow must remain nonnegative, but counterfactual priority
pools may be negative. Extend loss as `Lplus(Q) = L(max(0, Q))` and signed
remaining flow as `R(Q) = Q - Lplus(Q)`. Only the loss driver is clipped. Signed
components and signed remaining flow are retained. Thus a reverse senior under
`buildup` incurs zero incremental loss while its cohort leaves the pool below
zero; later forward flow first brings the pool back to zero.

A path factor contributes `factor * path_variable` in physical flow units.
Negative factors represent reverse components; nonunit magnitudes are supported.
A junior cohort cannot change attribution to unchanged senior components.
Members of the **same** cohort can change one another's shares.

For a measured inflow of 100 through the example curve with `depletion`:

| Component | Inflow | Delivered | Assigned loss |
| --- | ---: | ---: | ---: |
| Senior transaction | 60 | 46 | 14 |
| Junior transaction | 30 | 10 | 20 |
| Unallocated slack | 10 | 0 | 10 |
| Total | 100 | 56 | 44 |

These assignments telescope to the site's physical total loss. An individual
assigned loss can be negative on a decreasing absolute-loss segment even though
the site's total loss is non-negative. Priority here means the leaf transaction's
numeric `priority`; group constraints retain the existing scheduling behavior.

With `buildup`, those same inflows deliver 20, 27, and 9 respectively, with
assigned losses 40, 3, and 1. Both settings reconcile to total delivery 56 and
loss 44. With measured flow 40, a reverse senior of -20 and forward junior of
+60 receive losses 0 and 30 under `buildup`, or -10 and 40 under `depletion`.

### Equal-priority sharing

Equal-priority members sharing an endpoint form one cohort in the loss model.
The existing proportional priority allocator maximizes them jointly, subject to
all physical, shared-limit, path, and loss-sharing constraints. Compute the
cohort's aggregate incremental loss `T` from the selected convention and require

```text
loss_i * sum(delivery_j) = T * delivery_i
```

Here `delivery_i` is the **nonnegative physical magnitude leaving this loss
endpoint in member i's transaction direction**: downstream magnitude for a
forward member, upstream magnitude for a reverse member. It is not the signed
net flow, entitlement, or delivery at a more distant destination. These weights
are optimized variables. This rule is enforced inside each solve and can change
the optimum; it is not a postprocessing allocation of losses.

For a mixed cohort with measured components -20 and +60 at measured flow 40,
aggregate loss is 30. Reverse delivery is 20 and forward delivery is 40, so the
assigned losses are 10 and 20. Loss records use the declared physical axis:
the reverse record has inflow -20, remaining -30, and loss +10. Consequently
`inflow - remaining == loss` and all records still sum to physical totals.

If every member delivers zero, proportional weights are undefined. The model
uses each component's conservation equation without dividing by zero: a wholly
lost forward component retains its input as its assigned loss. Empty members
receive zero. Residuals form a final signed component, and opposing residuals
cannot be used simultaneously to manufacture attribution.

Existing transaction and account limits retain their anchor/component units.
When a stream-source anchor is after a piecewise `loss_from_zone`, natural-flow
consumption includes the inferred upstream loss. Spill reallocation credits the
actual leftover delivery, expands the natural-flow graph's bounds, and reruns
the existing second allocation pass.

## Supported cases and explicit limits

| Case | Behavior |
| --- | --- |
| Continuous curves with several slopes/intercepts | Exact segment selection; capped tail supported |
| 100% marginal loss after a gauge (`loss_to_zone`) | Supported, including allocations crossing the whole segment |
| Losses at both endpoints | Composed in routing and path continuity |
| Shared group limits | Retain the existing accounting constraints |
| Equal-priority diversions at distinct loss sites | Supported by the existing proportional allocator |
| Equal-priority paths sharing the same piecewise site | Joint optimization and delivery-weighted loss sharing; requires SCIP |
| Reverse/bidirectional or scaled path flow at a piecewise site | Supported with finite nonzero factors and nonnegative physical measurements |
| Counterflow with no inferable finite gross-flow bound | Rejected; provide finite transaction/shared limits |
| `loss_from_zone` with any 100% marginal-loss segment | Rejected: the inverse attribution is not unique |
| Unconstrained or missing physical flow at a piecewise site | Rejected: a finite, non-negative driver is required |
| Integer day lags | Supported using the existing lag alignment |
| Fractional lags in an endogenous piecewise model | Rejected; require a coupled model across time |
| Fixed exogenous piecewise curves without crossing transactions | Keep the existing LP backend compatibility |
| Endogenous piecewise curves with GLOP or SciPy | Requires native highspy or SCIP |

Calculated natural-flow routing retains the existing acyclic, single-outflow
rules; this change does not add bifurcating natural-flow routing.

## Formulation and audit

For each reachable segment `[lo, hi]`, a binary selector `z` and a distance `w`
satisfy `0 <= w <= (hi-lo)z`. The selectors sum to one, and the graph enforces
`Q = sum(lo*z + w)` and `R = sum(R(lo)*z + slope_R*w)`. This represents the
graph itself, including nonconvex shapes. It does not use a convex relaxation
or rely on the starting segment leading to the global optimum.

Bounds come from measured pools and the phase's no-withdrawal supply. For signed
paths, a linear relaxation uses the daily transaction/shared caps and continuity
constraints to bound gross counterflow before constructing the graphs; the net
physical measurement alone is not used as a gross-flow cap. Single
reachable segments use an affine equality without binaries. HiGHS uses zero
relative MIP gap and an absolute gap of `1e-8`. Every final curve equality is
independently checked against the evaluator to absolute tolerance `1e-5`.
Existing feasibility fallback can still relax accounting constraints; it cannot
relax curve geometry or segment-selection constraints.

Delivery-weighted sharing adds quadratic equalities, making general shared
cohorts nonconvex mixed-integer nonlinear problems. SCIP solves those models
globally within numerical tolerances (see the [SCIP problem classes](https://www.scipopt.org/doc/html/WHATPROBLEMS.php)
and [PySCIPOpt expression documentation](https://pyscipopt.readthedocs.io/en/stable/tutorials/expressions.html)).
This implementation rebuilds the SCIP model for each objective, requests zero
optimality gap, and checks final graph values and divided loss shares to absolute
tolerance `1e-5`. Non-optimal terminations are reported as errors; they do not
trigger accounting relaxation. Proven infeasibility retains the existing
accounting fallback, with curve and sharing constraints protected from relaxation.

Committed cohort anchors are reconstructed together in a separate path model.
Tentative junior allocations used to establish feasibility are not recorded as
committed water. Existing reservoir counterflow minimization is retained away
from modeled loss endpoints; supporting real juniors remain free at those sites.

`SolverOutput` adds two lists, empty on constant-only runs:

- `loss_allocations`: date, transaction/slack ID, flow ID, endpoint, inflow,
  remaining flow, and assigned loss. These records cover endogenous piecewise
  endpoints. Dates are unlagged to match the corresponding apportionments.
- `loss_events`: with `generate_audit=True`, records crossed breakpoints,
  before/after driver flows, the committing objective, and whether the driver
  was remaining natural flow, unallocated measured flow (`depletion`), or
  allocated measured flow (`buildup`). Negative-pool crossings include zero.
  Dates use accounting
  time, like `solve_steps`. These describe committed changes, not MIP search nodes.

MIP solutions have no LP dual certificate. Reduced costs and duals are reported
as unavailable. The existing textual audit identifies a reached transaction
upper limit when possible, otherwise reports an optimum under the combined
constraints. It does not label tight structural segment equations as water
shortages. `limited_by_natural_flow=False` on these steps means **not certified**,
not proof that natural flow was irrelevant.

## Measured performance

### Signed attribution and joint cohorts

The current implementation was measured on a shared three-leg import-to-use
path. Linux, Python 3.12.13, highspy 1.15.1, PySCIPOpt 6.2.1; medians of three
sequential runs, seven days per run, audit disabled. The first repetition
includes backend import; input construction and serialization are excluded.

| Rights | Piecewise sites | Priority and convention | Backend | Median seconds |
| ---: | ---: | --- | --- | ---: |
| 25 | 0 | Distinct priorities, constant losses | highspy | 0.049 |
| 25 | 1 | Distinct priorities, depletion | highspy | 1.608 |
| 25 | 1 | Distinct priorities, buildup | highspy | 2.141 |
| 25 | 1 | Joint equal-priority cohort, depletion | SCIP | 1.040 |
| 25 | 1 | Joint equal-priority cohort, buildup | SCIP | 1.103 |
| 100 | 1 | Joint equal-priority cohort, depletion | SCIP | 3.353 |

These are different accounting problems, not equivalent-problem speedups.
Combining equal priorities reduces the number of priority-pool graphs and
objectives, but adds nonlinear sharing constraints. It performed well on this
regular example; overlapping cohorts, binding delivery caps, and counterflows
can make the nonlinear search substantially harder. Statewide capacity has not
been established.

Against the previous piecewise commit `f026fbe`, a constant-loss 200-right,
30-day run produced **8,940 identical apportionments**, and an existing
piecewise 25-right, seven-day run produced **581 identical apportionments**.
All output fields matched exactly in both comparisons. Constant-loss medians
were 1.566 seconds before and 1.480 after; no new constant-loss speedup is claimed.
The raw repetitions and comparison metadata are in
`benchmarks/signed_cohort_results.json`.

```sh
python -m benchmarks.benchmark_piecewise --workload path --reaches 2 --rights 25 --sites 1 --days 7 --proportional
python -m benchmarks.benchmark_piecewise --workload path --reaches 2 --rights 25 --sites 1 --days 7 --proportional --loss-attribution-method buildup
python -m benchmarks.benchmark_piecewise --workload path --reaches 2 --rights 100 --sites 1 --days 7 --proportional
```

### Original piecewise implementation

Linux, Python 3.12.13, highspy 1.15.1, one HiGHS thread. Medians of three
sequential end-to-end `solve()` calls, without audit. The piecewise benchmark
includes initial backend import in the first repetition; input construction and
JSON serialization are excluded. These are descriptive timings, not a statistical
significance test. Adding nonzero curves changes the physical problem.

| Synthetic workload, seven days | Piecewise sites | Median seconds |
| --- | ---: | ---: |
| 200 diversion rights, 20 reaches, distinct priorities | 0 | 0.358 |
| Same network, distinct priorities | 1 | 1.908 |
| Same network, distinct priorities | 5 | 3.834 |
| Same network, proportional priority cohorts | 1 | 0.599 |
| 25 rights sharing a three-leg import-to-use path | 0 | 0.054 |
| Same shared path, distinct priorities | 1 | 1.686 |

Profiling the first implementation exposed unnecessary reconstruction of paths
that never crossed a piecewise site. Restricting reconstruction to affected paths
reduced the one-site routing case from 3.234 to 1.908 seconds (41%), and its
proportional variant from 2.369 to 0.599 seconds (75%). Exact segment selection
was retained. The shared-path case is more expensive because each transaction
adds priority-pool graphs at the shared site.

The 200-right, 30-day constant-loss regression produced **8,940 identical
apportionments** to the previous optimized commit `f51d32b`; all existing output
fields matched. Medians were 1.679 seconds before and 1.486 seconds after. This
comparison establishes compatibility; the timing difference is not claimed as
an additional constant-loss optimization.

Raw repetitions are in `benchmarks/piecewise_results.json`, the initial profile
comparison in `benchmarks/piecewise_initial_results.json`, and the constant
regression in `benchmarks/piecewise_constant_regression.json`. Reproduce with:

```sh
python -m benchmarks.benchmark_piecewise --sites 1 --days 7 --repeat 3
python -m benchmarks.benchmark_piecewise --sites 5 --days 7 --repeat 3
python -m benchmarks.benchmark_piecewise --sites 1 --days 7 --proportional
python -m benchmarks.benchmark_piecewise --workload path --reaches 2 --rights 25 --sites 1 --days 7
python -m pytest -q
```

Validation covers exact increments, senior/junior attribution, both endpoints,
post-loss anchors, shared limits, proportional diversions, capped tails,
decreasing loss, dates, lags, spill and external-boundary credits, and explicit
unsupported-case errors.
A seeded 25-curve test compares allocation against an independent enumeration
of the curve segments under binding downstream demand and subsequent spill
credits. The original suite also passes separately on HiGHS, GLOP, and SciPy
(110 passed, five existing skips, seven subtests each).

The complete expanded suite reports **175 passed, six skipped, seven subtests
passed**. Five skips are unchanged upstream tests. The sixth is the GLOP
comparison in a process that already loaded highspy; the installed native builds
have a symbol-loading conflict. The original GLOP suite passes in its own process.

The 33 new analytical cases exercise both conventions, signed pool crossings,
reverse multi-leg paths, nonunit factors, mixed-direction cohorts, zero delivery,
input-order independence, upstream loss inversion, both endpoints, shared caps,
date-dependent backend selection, and audit consistency. A binding-delivery
example has an independently derived quadratic optimum, demonstrating that
sharing affects optimization itself. These cases also assert that accounting
feasibility relaxation was not used. CI installs the SCIP extra and runs pytest
so both the original tests and these cases are collected.

For larger systems, constant-fraction inputs remain the fastest supported option.
Use exact piecewise curves where their flow dependence matters, and benchmark
the actual path overlap before expanding their use. A certified continuous-LP
fast path for suitable curve shapes would be a subsequent optimization; this
implementation provides an exact reference against which to validate it.
