# Utah Water Right Apportionment Solver — compiled v2 branch

This branch is intentionally focused on the second-generation compiled-formula
solver.

The mathematical problem is still defined by the same `SolverInput` and the
same production LP construction code.  V2 then copies that LP into a
compiler-owned, parameterized intermediate representation (IR), performs
algebraic substitutions/presolve, and freezes each structurally sequential
priority objective as one of:

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
print(plan.formulas())  # semantic/compiler IR
print(plan.code())      # executable lowered Python
result = plan.solve()   # generated Python is the default runtime
```

`compile_solver_input` is also an alias for `compile_solver_input_v2` on this
branch.

## Pipeline

```text
SolverInput
    -> production LP definition
    -> parameterized compiler LP / IR
    -> one logical compiler variable per PathTrxn
    -> structural substitutions and presolve
    -> static structural priority graph
    -> frozen direct equation or reduced LP kernel
    -> runtime refresh of coefficient/bound/RHS parameters
```

The LP remains the problem definition so future complex cases can still be
expressed generally. The compiler is free to rewrite the copied LP as needed
without changing the production accounting model. In particular, the LP's
variable-per-path-leg representation is now collapsed into one logical compiler
variable per transaction, with the original leg values reconstructed from the
LP continuity equations.

## Structural preparation

Compilation no longer walks the solve period day by day and does not execute a
numerical accounting day to discover objective branches. Numeric measurements,
storage changes, natural-flow quantities, transaction limits, and committed
allocations are runtime parameters.

V2 constructs one compiler model for each **distinct structural regime**.
Time-varying constant fractional losses are matrix-coefficient parameters, so a
0%/10%/20% loss schedule reuses the same frozen structure.  The compiler's
parameter-expression system supports products and quotients of runtime scalar
parameters created by exact substitutions.  A new regime is needed only when
the actual LP sparsity/topology changes, such as external natural-flow boundary
presence changing.

Sequential priority variables are frozen as direct formulas or residual LP
kernels. Equal-priority cohorts are frozen as logical-transaction kernels after
path-leg collapse: runtime water filling introduces only one common-increment
scalar, substitutes each active member as ``dTRXN_i >= factor_i * increment``,
and reuses the same transformed cohort IR for blocked-member classification and
tiny-factor deferred solves. No temporary ``combined`` variable or path-leg
constraint is added to the production LP. Storage/counterflow ambiguity
tie-breaks remain separate structural auxiliary kernels. Reporting slack values
are not solved: they are calculated directly from the leftover measurement
residual.
Storage-to-natural residuals are likewise derived after pass one, credited to
natural flow, and the same priority schedule is executed a second time. Final
non-anchor path-leg values are reconstructed from frozen continuity equations.
None of these operations create new compiler programs at runtime.

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

### Explicit compiled day routine

Compiled V2 now owns day execution through an explicit IR. Sequential
transaction assignments, residual-row updates, natural-flow commits, derived
reporting slacks/spills, the second allocation pass, and final path-leg
reconstruction are represented as ordered execution nodes. `plan.formulas()`
starts with this executable routine before listing the frozen objective-program
definitions.

The final frozen IR is lowered once more for execution: runtime parameter names
become integer slots in contiguous NumPy arrays, and residual constraints use a
frozen integer row layout with lower/upper arrays.  The named IR is retained for
readable formulas and proofs.

`plan.code()` lowers that indexed IR into executable ordinary Python. Eligible
unique-priority assignments become direct `min(...)` / `max(...)` arithmetic
followed by integer-indexed residual updates such as `r_upper[17] -= delta`.
Equal-priority and genuinely coupled cases appear as calls to their prebuilt
frozen kernels.  The generated functions are compiled once with Python's
`compile`/`exec` during plan preparation and are the default daily execution
path.  `V2CompilationOptions(enable_generated_python=False)` retains the
indexed execution-IR interpreter as a reference/debug path.
