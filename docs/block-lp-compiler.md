# Block-first LP compiler

This is the first executable implementation of the `compile/compile.py`
sketch. Every priority block currently runs an LP kernel. Direct formulas and
analytical proportional loops are deliberately deferred. This establishes the
accounting contract for later optimizations; it is not a performance release.

## Apply and use

Apply `block_lp_kernels.patch` from the root of the supplied `project(2).zip`
package (the directory containing `src` and `tests`):

```sh
git apply --check block_lp_kernels.patch
git apply block_lp_kernels.patch
```

The existing public `solve` and `compile_solver_input_v2` APIs are unchanged.
Opt into the new implementation explicitly:

```python
from ut_water_apportionment.compile import compile

plan = compile(solver_input)
result = plan.solve(check_expected_values=True)

# Reuse the structure with another MeasurementCollection for the same period.
result = plan.solve(measurements=updated_measurements)

print(plan.code())
print(result.compilation_report)
```

Each `solve()` starts with fresh daily and cumulative accounting state. The
plan owns a copy of the input. Changes to graph structure, transactions, limits,
or the simulation period require a new plan; measurement values can be replaced
without recompiling. The existing dependencies, including SciPy, are sufficient.

## Three building functions

* `build_runtime_state_layout(input)` snapshots the structure and assigns numeric
  slots for allocations, remaining transaction limits, reference cfs, remaining
  group reservations, measured flows, natural flows, and routing coefficients.
  It does not read or specialize to a representative day's measurements.
* `priority_blocks(input)` groups transactions in priority order. Children at
  or before their parent's priority are moved immediately after it, using the
  existing schedule convention. Proportional reference cfs bind to each day's
  effective limits, including call and cumulative limits.
* `build_block_lp(input, block, layout)` emits the existing `BlockLP`
  representation from `compile/lp.py`. One variable represents a transaction's
  increment; fractional path losses become runtime coefficient slots rather
  than additional path-leg variables.

For manual inspection:

```python
from ut_water_apportionment.compile import (
    build_runtime_state_layout, priority_blocks, build_block_lp,
)

layout = build_runtime_state_layout(solver_input)
models = [build_block_lp(layout.input, block, layout)
          for block in priority_blocks(layout.input)]
```

## Reservations and state updates

An ordinary block contains its targets. Group allocations need additional
descendant variables to demonstrate that their reserved amount can be delivered.
Outstanding senior reservations also remain represented while intervening
outside transactions are allocated. Consequently, a large group subtree can
still produce a large kernel.

Only target increments are committed. Auxiliary descendant solutions are
feasibility witnesses, not allocations. A group allocation creates a reservation;
its child allocations discharge that reservation. Physical flow and natural-flow
capacities are consumed when path transactions are committed. This avoids
premature child allocation and counting a reservation twice.

Updates resolve all coefficient slots before changing any state, then aggregate
changes. Each subsequent kernel sees the updated residual capacities. Committed
transactions are represented by their accounting effects, rather than being
reoptimized as free variables.

## Execution and transparency

`plan.code()` returns the actual executed daily Python source, including each
`BlockLP` definition and the sequence of kernel calls. Its `execute_day(state)`
expects a prepared numeric state array; the package supplies daily data binding,
natural-flow calculation, kernel implementation, and output reporting. It is
not a standalone replacement for those dependencies.

Singleton blocks maximize their target. Equal-priority blocks use proportional
increments, commit the shared increment, and solve scalar LPs to identify blocked
members before redistributing. Unlimited members share equally before finite
members, following the existing solver convention. Very small proportional
factors are deferred, also following the existing solver. LP matrices are bound
and solved at runtime, so this baseline can still be slow.

The report includes kernel count, maximum kernel variable count, slot count,
and actual LP solve count. Later formula compilers can replace kernels while
retaining the same slot and update contract.

## Initial support boundary

Implemented: forward paths on non-bidirectional allocated flows; sequential and
equal-priority allocation; nested group reservations; daily, call, and cumulative
path limits; daily proportional references; lags; varying fractional losses;
and existing natural-flow preparation, including specified and external values.

Not yet implemented: storage and storage accounts, account transfers, reverse
or bidirectional transaction allocation, spill/import credit replay,
unconstrained physical flows, piecewise/absolute losses, cumulative group
limits, and groups without finite daily limits. These raise
`UnsupportedBlockInput` rather than silently switching accounting rules or
falling back to the old full-system solver. Bidirectional natural balancing
flows are permitted when they are not transaction paths.

In particular, the Duchesne and Uinta cases that require storage cannot yet use
this backend. Opposing-flow elimination will need an explicit implementation
when reverse transaction allocation is added.

## Validation

Run the focused contracts with:

```sh
PYTHONPATH=src python -m unittest tests.test_block_compile -v
```

The 18 tests cover daily state reuse, priority correction, sequential and
proportional allocation, blocked-member redistribution, unlimited references,
nested and competing reservations, cumulative resets, runtime loss factors,
specified natural flow, executable source replay, empty transaction lists,
and explicit rejection of unsupported inputs. Supported fixtures compare
outputs with the existing solver. Existing solver tests remain unchanged.
