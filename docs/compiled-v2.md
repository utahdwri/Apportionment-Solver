# Compiled equations v2 — initial branch

This package is intentionally separate from `compiled/` (v1).

## Direction

V2 keeps the ordinary LP as the authoritative mathematical definition:

```text
SolverInput
    -> production LP built by Apportioner
    -> compiler-owned LinearModel
    -> algebraic substitutions / presolve
    -> direct equation OR reduced residual LP kernel
```

The compiler-owned model is disposable and may be rewritten aggressively.  The
production LP builder does not need to be optimized for equation generation.

## What is different from v1

* No whole-day LP fallback.  Every objective is handled locally.
* Homogeneous continuity equations are substituted before solving an objective.
  This is the beginning of collapsing the LP's variable-per-path-item layout
  into logical transaction variables.
* Fixed and provably monotone nonobjective variables are moved to a bound and
  removed from the residual model.
* A scalar objective that reduces to one live variable is evaluated directly.
* Anything still coupled is solved as a transformed **reduced LP kernel**.
* The production LP remains the correctness oracle for tests.

## Deliberate initial limitations

This is an architecture branch, not yet the finished ahead-of-time compiler.
The first implementation compiles each objective when the production accounting
schedule asks for it.  Numeric LP bounds are therefore still present during
compilation.  The next major v2 step is to parameterize those bounds and freeze
the resulting IR so `compile_solver_input_v2()` produces one reusable routine
without inspecting measurement values.

Audit/dual evidence is also intentionally omitted in v2 for now.  Run the
ordinary LP solver when detailed audit evidence is required.

## Example

```python
from ut_water_apportionment import compile_solver_input_v2

plan = compile_solver_input_v2(input)
result = plan.solve()
print(plan.formulas())
print(plan.report())
```

`plan.formulas()` shows which objectives became direct calculations and which
were retained as reduced residual LP kernels, including the substitutions that
were performed before the solve.

## Next compiler passes

1. Parameterize variable/constraint bounds instead of compiling with daily
   numeric values.
2. Freeze the objective IR and remove runtime compilation.
3. Generalize structural equality substitution beyond two-variable zero-RHS
   continuity rows.
4. Represent committed senior allocations as symbolic residual RHS state.
5. Compile proportional/equal-priority loops directly into the IR.
6. Recognize derived slack/output variables and calculate them after allocation
   instead of optimizing them when that is mathematically equivalent.
7. Add exact MIN/MAX projection only for the small residual cores where it is
   clearer/faster than the reduced kernel.
