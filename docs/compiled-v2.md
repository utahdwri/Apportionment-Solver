# Compiled v2

V2 keeps the LP generated from `SolverInput` as the authoritative mathematical
problem definition, but compilation happens on a separate parameterized IR.

## Frozen parameterized IR

Mutable variable bounds and constraint RHS values are represented by named
runtime parameters, for example:

```text
variable[TRXN_1___RIVER>USER].remaining_upper
constraint[MEAS_RIVER>USER].remaining
constraint[NF_ZONE_RIVER].remaining_upper
```

The compiler can therefore substitute equalities, collapse continuity
variables, remove monotone nuisance variables, and build reduced sparse kernels
without hard-coding a preparation day's numeric measurements.

## Execution modes

Each prepared objective becomes either:

1. **Direct scalar equation** — normally a spreadsheet-like `MIN(...)` or
   `MAX(...)`; or
2. **Frozen reduced LP kernel** — a small transformed residual problem whose
   sparse coefficient matrix is built during preparation. Runtime refreshes
   only numeric RHS/bound parameters.

There is intentionally no whole-day LP fallback in this branch.

## Guarded regional simplifications

Some compact direct equations require a redundancy condition that is true only
for part of parameter space. V2 may prepare a guarded compact variant and, when
preparation later encounters the same structural objective outside that region,
also prepare a conservative unguarded variant. The frozen runtime selects among
those already-prepared variants; it does not compile a new program.

## Current preparation model

`compile_solver_input_v2(problem)` traverses the supplied date range once to
discover accounting objective/control-flow signatures. Numeric bounds/RHS are
parameters rather than embedded constants. After preparation the plan is
frozen, and `plan.solve()` may only use prepared programs.

A future milestone is to derive the control-flow IR statically from the
schedule/LP structure so even objective discovery no longer needs a preparation
trace.
