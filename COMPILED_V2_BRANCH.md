# Experimental branch: compiled-equations-v2

This tree contains an initial second-generation compiler alongside the existing
`compiled/` implementation.

The branch goal is:

> Keep the production LP as the authoritative problem definition, but copy each
> objective into a compiler-owned linear model that may be rewritten freely.
> Execute the resulting sequential routine with direct equations where possible
> and small transformed LP kernels where necessary. Never restart an entire day
> merely because one objective is difficult to compile.

## Public API

```python
from ut_water_apportionment import compile_solver_input_v2

plan = compile_solver_input_v2(input)
result = plan.solve()
print(plan.formulas())
print(plan.report())
```

## Implemented in this initial branch

- Separate `compiled_v2/` package; v1 remains intact.
- Compiler-owned `CompilerModel` copied from the production LP.
- Structural zero-RHS equality substitution, including path-continuity collapse.
- Fixed-variable substitution.
- One-sided reconciliation/slack elimination from equality measurements.
- Redundant lower-side removal while preserving runtime upper limits that can
  appear in spreadsheet-style formulas.
- Monotone nonobjective variables moved to harmless bounds.
- Direct scalar `MIN`/`MAX` programs after the residual problem becomes one
  variable.
- Reduced residual LP kernels for objectives that remain coupled.
- No whole-day LP fallback in v2.
- LP-vs-v2 regression tests for a simple sequential problem and the reservoir
  signed-path example.

## Current deliberate limitation

The v2 compiler currently runs its transformation passes when each objective is
encountered.  It therefore has a clean compiler IR and local-kernel execution,
but it is not yet a fully frozen ahead-of-time program.  Daily numeric bounds
are still visible to this first compiler pass.

The next major step is to replace those numeric bounds with named parameters,
compile/freeze the sequence once, and execute the same IR across all days.

See `docs/compiled-v2.md` for the planned passes.
