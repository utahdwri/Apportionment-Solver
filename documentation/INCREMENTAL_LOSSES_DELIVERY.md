# Fixed allocation weights for incremental losses

This update replaces delivery-dependent loss sharing with fixed allocation
weights on each committed increment. It applies to shared forward, reverse,
and scaled paths. A complete source checkout and a patch against `loss-curve`
commit `c3ad4e7a93a97cbddff19a9274faaa9435559091` are included in the delivery.

## Use

```sh
python -m pip install -e '.[highs]'
```

```python
result = solve(solver_input, generate_audit=True)
assert result.solver_backend == "highspy"
for increment in result.loss_increments:
    print(increment.txn_id, increment.sequence,
          increment.allocation_weight, increment.loss)
```

Both `loss_attribution_method="depletion"` (default) and `"buildup"` remain
available. No additional setting enables fixed sharing: it replaces the old
shared-cohort rule. SCIP remains an explicitly selectable optional backend;
shared losses no longer trigger it automatically or add quadratic constraints.

## Accounting change

For each increment, normalize the allocator's raw-anchor proportions over the
active members at each loss site. Assign `member_loss = weight * total_loss`.
The aggregate loss comes from the exact piecewise curve. Physical endpoint
conservation and path factors remain in the model. The weights are not
recomputed from resulting deliveries or multiplied by a path factor.

An increment may cross several breakpoints. After it is committed, its input,
delivery, and loss entries are immutable. Later increments use newly normalized
weights if members have stopped. Final cumulative loss-to-delivery ratios need
not match. Differently scaled paths or upstream losses can also produce
different retained fractions within an increment under this explicit rule.

For the documented curve and mixed anchor components -20/+60 at measured flow
40, loss 30 now splits 7.5/22.5 in the predetermined 1:3 ratio. This intentionally
changes the old directional-delivery weighting result.

The solver replays exact anchor increments with the full physical constraints
before committing them. Pending allocations form a feasibility relaxation,
not committed shares; parent reservations receive an additional child-schedule
replay. All final real components are fixed to the ledger before residual
reconciliation. See PIECEWISE_LOSSES.md for the formulation and audit details.

## Validation

- Full suite: **199 passed, 7 skipped, 7 subtests passed**. Six skips are existing
  upstream skipped cases; one is the installed GLOP/highspy native-library
  symbol conflict when both are loaded in one process.
- Analytical coverage includes member dropouts, crossings of multiple curve
  breakpoints, inverse endpoints, mixed directions, negative accounting pools,
  differently scaled anchors, different upstream curves, and nested/equal
  parent reservations. Tests reject accounting feasibility relaxation in these
  analytical scenarios.
- A test blocks SCIP imports while solving shared losses with the default
  backend. Another explicitly uses SCIP while forbidding quadratic rows.
- All pre-existing output fields match the baseline exactly on the constant-loss
  200-right/30-day case (**8,940 apportionments**) and the distinct-priority
  piecewise case (**581 apportionments**). The added increment audit is empty
  in both cases.
- The graph's fixed-positive-driver interval bug was also corrected: disjoint
  segments no longer create negative-width intervals at a singleton bound.

## Measured runtime

Seven-day synthetic shared paths, medians of three sequential runs without
an audit. Baseline: the fetched cumulative-sharing branch, automatically using
SCIP. Updated: fixed-share increments, automatically using HiGHS. Python
3.12.14, highspy 1.15.1, PySCIPOpt 6.2.1. Input construction and serialization
are excluded. First repetition includes backend import.

| Rights | Convention | Baseline seconds | Updated seconds | Runtime reduction |
| ---: | --- | ---: | ---: | ---: |
| 25 | depletion | 0.885 | 0.623 | 29.6% |
| 25 | buildup | 1.054 | 0.645 | 38.8% |
| 100 | depletion | 3.204 | 2.591 | 19.1% |

These are descriptive comparisons of two accounting policies on the same
synthetic inputs. They do not establish statewide capacity or statistical
significance. Raw repetitions and source hashes are in
`benchmarks/fixed_share_results.json`; compatibility checks are in
`benchmarks/fixed_share_compatibility.json`. Reproduce the timings from each
checkout with:

```sh
python -m benchmarks.benchmark_piecewise --workload path --reaches 2 --rights 25 --sites 1 --days 7 --repeat 3 --proportional
python -m benchmarks.benchmark_piecewise --workload path --reaches 2 --rights 25 --sites 1 --days 7 --repeat 3 --proportional --loss-attribution-method buildup
python -m benchmarks.benchmark_piecewise --workload path --reaches 2 --rights 100 --sites 1 --days 7 --repeat 3 --proportional
```

## Remaining limitation

A parent reservation is explicitly rejected when outside transactions share a
loss site at priorities between the parent and its last child. An independent
child replay does not generally validate that interleaving. Ordinary, nested,
and equal-priority parent cases are supported and tested; general interleaved
reservations still need a joint model of future increments. The earlier draft's
mixed-direction parent-reservation test now passes under fixed sharing.

Existing limitations for fractional-day lags, ambiguous inverse curves with
100% marginal loss, and unbounded gross counterflow remain documented in
PIECEWISE_LOSSES.md.
