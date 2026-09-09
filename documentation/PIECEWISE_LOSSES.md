# Piecewise-linear losses

Allocation-dependent piecewise-linear losses use exact mixed-integer graphs.
Equal-priority paths share each committed increment's loss using predetermined
allocation weights. Increments can cross several breakpoints; earlier input,
delivery, and loss assignments remain fixed. Native **highspy** supports this
formulation, including signed and scaled components. **SCIP is optional and is
never selected merely because members share a loss curve.** Constant-only
inputs retain the existing LP.

## Configure a curve

Install from this checkout:

```sh
python -m pip install -e '.[highs]'
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

The `highs` extra is sufficient for shared piecewise losses. `solver_backend="auto"`
prefers native HiGHS. An explicit `solver_backend="scip"` remains available with
the optional `scip` extra, but the fixed-share formulation adds no quadratic
constraints to either backend.

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
Members of the **same** cohort share losses on the current increment only;
later increments cannot change losses already assigned to any member.

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

For each allocation increment, the allocator supplies fixed anchor proportions
`alpha_i`. At a loss endpoint, normalize over the active members using that site:

```text
w_i = alpha_i / sum(alpha_j)
delta_loss_i = w_i * delta_loss
```

The aggregate loss comes from the exact piecewise graph and the selected
buildup/depletion convention. The weight is a constant during the increment,
so sharing is linear. No final-delivery ratio or continuous-variable product is
used. A single optimization may cross multiple breakpoints, including zero.
When a member stops, the next increment uses the remaining members' weights.
Previously committed physical input, delivery, and assigned loss never change.

Weights refer to the allocator's **raw anchor variables**: the first ordered
path item of each transaction. Existing proportional scheduling determines
these weights, including normalization of nested proportional schedules.
Path factors still convert raw variables to signed physical components, and
path continuity still applies. The weight itself is not multiplied by a path
factor or recalculated from the resulting delivery.

Consequently, members with differently scaled anchors or different upstream
losses need not have the same retained fraction at a shared loss point. This is
an intentional consequence of fixed allocation weights. For example, anchor
weights 2:1 and a factor of 2 on the first anchor can produce physical inputs
80:20 at the shared curve. Loss 44 is assigned as 29.333333:14.666667; remaining
flows are 50.666667:5.333333. Endpoint magnitudes remain nonnegative, so a
fixed-share assignment that would require negative delivery is infeasible.

For the curve above, measured inflow 100, input caps 60 and 30, and the first
member's delivery capped at 10, depletion first allocates input `100/9` and
`50/9`, with losses `10/9` and `5/9`. The first member then stops. The second
continues alone to input 30 and total delivery `239/9`. The first member's loss
stays `10/9`; final cumulative loss-to-delivery ratios need not match.

Mixed directions use the same predetermined, nonnegative allocation weights.
With anchor amounts 20 and 60, factors -1 and +1, and measured flow 40, aggregate
loss 30 is split 1:3: reverse loss 7.5 and forward loss 22.5. The signed records
have inflow/remaining -20/-27.5 and 60/37.5. Thus each record satisfies
`inflow - remaining == loss`, and their totals reconcile to physical input 40,
delivery 10, and loss 30. These values intentionally differ from the previous
rule based on unknown directional deliveries.

Zero aggregate delivery needs no division. If the fixed weights match input
proportions, wholly lost inputs are assigned entirely as loss. Negative
incremental losses remain permitted where the curve/convention produces them.
Residuals form the final signed component; opposing residuals cannot be used
simultaneously to manufacture attribution.

Existing transaction and account limits retain their anchor/component units.
When a stream-source anchor is after a piecewise `loss_from_zone`, natural-flow
consumption includes the inferred upstream loss. Spill reallocation credits the
actual leftover delivery, expands the natural-flow graph's bounds, and reruns
the existing second allocation pass. That pass can add new increments but
cannot rewrite the loss shares of earlier committed increments.

## Supported cases and explicit limits

| Case | Behavior |
| --- | --- |
| Continuous curves with several slopes/intercepts | Exact segment selection; capped tail supported |
| 100% marginal loss after a gauge (`loss_to_zone`) | Supported, including allocations crossing the whole segment |
| Losses at both endpoints | Composed in routing and path continuity |
| Shared/nested group limits | Linear limits; reservations checked by replaying child allocations; interleaved outside priorities at shared loss sites are explicitly unsupported |
| Equal-priority diversions at distinct loss sites | Supported by the existing proportional allocator |
| Equal-priority paths sharing the same piecewise site | Fixed allocation weights on each increment; exact MILP in HiGHS |
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

Each real path and loss component has a committed ledger. The physical model
separates this ledger, the exact current increment, and pending allocations.
Current anchor increments have equality constraints enforcing the allocator's
fixed proportions. Each site's graph determines aggregate delivery/loss; member
losses use the constant weights above. A replay with exact committed anchors
and all physical constraints intact calculates the values entered into the ledger. Spill and final residual
solves fix real components to that ledger.

Pending allocations are a **feasibility relaxation**: they retain graph,
continuity, component, and accounting constraints, but their loss shares are
free. They are not committed allocations or a promise that those particular
future assignments will be made. Each actual increment is rebuilt and solved
with the fixed-share rule. Parent reservations are additionally checked using
an isolated replay of the child schedule; preview assignments never enter the
real ledger or audit. This is a sequential allocation algorithm, not a single
MILP encoding every possible future allocation phase.

A remaining limitation is parent reservations whose children share a loss site
with outside transactions at priorities between the parent and its last child.
An independent child replay cannot establish those reservations generally. The
solver raises an explicit interleaving error rather than claiming a validated
reservation. Ordinary, nested, and equal-priority parent groups are covered by
analytical tests, including delivery-limited children and mixed counterflows.

`SolverOutput` adds three lists, empty on constant-only runs:

- `loss_allocations`: date, transaction/slack ID, flow ID, endpoint, inflow,
  remaining flow, and assigned loss. These records cover endogenous piecewise
  endpoints. Dates are unlagged to match the corresponding apportionments.
- `loss_events`: with `generate_audit=True`, records crossed breakpoints,
  before/after driver flows, the committing objective, and whether the driver
  was remaining natural flow, unallocated measured flow (`depletion`), or
  allocated measured flow (`buildup`). Negative-pool crossings include zero.
  Dates use accounting
  time, like `solve_steps`. These describe committed changes, not MIP search nodes.
- `loss_increments`: with `generate_audit=True`, the committed member increments
  for models with shared endpoints. Each `SolverOutputLossIncrement` has the
  same fields as `loss_allocations`, plus `sequence`, `driver_before`,
  `driver_after`, and `allocation_weight`. The last field is the normalized
  fixed weight at that endpoint for that increment. Sequence numbers group simultaneous members across endpoints
  and restart each accounting day. Drivers use the signed pre-loss coordinate,
  including at a post-loss gauge. Dates are unlagged like `loss_allocations`,
  so simultaneous records at differently lagged sites can have different dates.
  Summing a real member's increments at an endpoint gives its final loss
  allocation. Residuals appear only in `loss_allocations`.

MIP solutions have no LP dual certificate. Reduced costs and duals are reported
as unavailable. The existing textual audit identifies a reached transaction
upper limit when possible, otherwise reports an optimum under the combined
constraints. It does not label tight structural segment equations as water
shortages. `limited_by_natural_flow=False` on these steps means **not certified**,
not proof that natural flow was irrelevant.

## Validation and performance

See [INCREMENTAL_LOSSES_DELIVERY.md](INCREMENTAL_LOSSES_DELIVERY.md) for the current
verification and benchmark results, and `benchmarks/fixed_share_results.json`
for raw repetitions. Earlier benchmark JSON files describe historical solver
versions and different loss-sharing policies.
