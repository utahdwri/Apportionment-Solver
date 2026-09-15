# Utah Water Right Apportionment Solver — compiled v2 branch

This branch is intentionally focused on the second-generation compiled-formula
solver.

The mathematical problem is still defined by the same `SolverInput` and the
same production LP construction code.  V2 then copies that LP into a
compiler-owned, parameterized intermediate representation (IR), performs
algebraic substitutions/presolve, and freezes each encountered objective as one
of:

- a direct `MIN(...)` / `MAX(...)` equation; or
- a small reduced LP kernel when the residual problem remains coupled.

There is **no public whole-day LP solve mode and no v1 compiler** on this
branch.

## Public API

```python
from ut_water_apportionment import solve, compile_solver_input_v2

# Convenience API: always uses compiled v2.
result = solve(problem, check_expected_values=True)

# Retain the prepared plan so the equations/IR can be inspected and reused.
plan = compile_solver_input_v2(problem)
print(plan.formulas())
result = plan.solve()
```

`compile_solver_input` is also an alias for `compile_solver_input_v2` on this
branch.

## Pipeline

```text
SolverInput
    -> production LP definition
    -> parameterized compiler LP / IR
    -> structural substitutions and presolve
    -> frozen direct equation or reduced LP kernel
    -> runtime refresh of bounds/RHS only
```

The LP remains the problem definition so future complex cases can still be
expressed generally.  The compiler is free to rewrite the copied LP as needed
without changing the production accounting model.

## Tests

`tests/test_solver.py` is the retained production behavior suite.  Its local
`solve()` helper explicitly calls `compile_solver_input_v2(...).solve(...)`, so
all of those tests exercise the compiled-formula path.

`tests/test_compiled_v2.py` contains focused compiler/IR tests.

Run everything with:

```bash
python -m unittest discover -v -s tests -t .
```

See `docs/compiled-v2.md` for compiler details.
