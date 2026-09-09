# Signed and shared piecewise losses

Historical delivery notes for commit `6b5f39b`. The current implementation shares
losses separately for each committed increment; see
[INCREMENTAL_LOSSES_DELIVERY.md](INCREMENTAL_LOSSES_DELIVERY.md) for the update.
The results and patch names below describe the earlier download.

This checkout includes selectable priority attribution and joint equal-priority
loss sharing, built on the earlier piecewise implementation at `f026fbe`.

## Install and run

From the included `apportionment-solver` directory:

```sh
python -m pip install -e '.[scip]'
```

Configure your existing input:

```python
solver_input.loss_attribution_method = "depletion"  # default; start at measured flow
# solver_input.loss_attribution_method = "buildup"  # start at zero
result = solve(solver_input)  # automatic backend selection
```

Signed intermediate pools use zero loss below zero. Negative and scaled path
factors are supported while the physical measured flow remains nonnegative.
Equal-priority members sharing a loss endpoint are optimized jointly, and their
aggregate incremental loss is shared in proportion to actual delivery at that
endpoint in each member's transaction direction. Those delivery weights are
enforced during optimization. SCIP is required for general shared cohorts;
the `scip` extra installs it together with highspy.

## Results

- 175 tests passed; six skipped; seven subtests passed. Skips consist of five
  pre-existing upstream skips and one installed GLOP/highspy native-library
  conflict in the same process.
- All output fields match the previous implementation on the 8,940-apportionment
  constant-loss regression and the 581-apportionment default piecewise regression.
- Synthetic joint cohorts over seven days: 25 rights took 1.040 seconds with
  depletion and 1.103 seconds with buildup; 100 rights took 3.353 seconds with
  depletion. These are medians of three runs without auditing, not statewide
  capacity measurements.

See [PIECEWISE_LOSSES.md](PIECEWISE_LOSSES.md) for formulas, examples, limitations,
test details, and reproducible benchmark commands. Raw new timings are in
`benchmarks/signed_cohort_results.json`.

Fractional day lags still require a coupled model across time. A post-loss gauge
cannot uniquely invert a curve with a 100% marginal-loss segment. Signed
counterflow also needs finite bounds inferable from transaction/shared limits.

## Apply to an existing checkout

The download contains a complete source checkout and two alternative patches:

- `patches/signed-losses-since-f026fbe.patch`: this update only; apply on the
  previous piecewise implementation.
- `patches/all-changes-since-882704a.patch`: all local performance and piecewise
  changes since the original upstream base.

Choose the patch matching your base; do not apply both. From your repository root,
replace `PATH_TO_PATCH` with the chosen patch's location:

```sh
git apply --check PATH_TO_PATCH
git apply PATH_TO_PATCH
python -m pip install -e '.[scip,dev]'
python -m pytest -q
```

The package manifest identifies the exact revisions. This work is committed
locally and has not been pushed to GitHub.
