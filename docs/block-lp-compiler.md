# Block-first LP / formula compiler

The block compiler is now the main experimental path for converting a `SolverInput`
into a reusable daily apportionment program.  It begins with the same linear
accounting problem used by the LP solver, but compiles each priority block as far
as possible into direct Python formulas before retaining a numerical LP fallback.

The end product is a `CompiledPlan` whose `code()` method returns the **exact
human-readable Python module that is executed**.  The generated program now
includes natural-flow initialization, priority allocation, reservoir spill/import
credit, and replay.  Kernel objects are compile-time intermediate representation
(IR); once compilation is complete, the generated Python is the runtime program.

## Basic use

```python
from ut_water_apportionment.compile import compile

plan = compile(solver_input)

# Inspect the exact generated program.
print(plan.code())

# Execute the compiled program over the SolverInput period.
result = plan.solve(check_expected_values=True)

# The same compiled structure can be reused with replacement measurement values.
result = plan.solve(measurements=updated_measurements)

print(result.compilation_report)
```

A change to graph structure, transactions, paths, priorities, limits, accounts,
or the simulation period requires recompilation.  Daily measurement values,
loss fractions, specified natural flow, boundary natural flow, cumulative state,
and account balances remain runtime inputs.

## Compiler pipeline

`compile_block()` selects the same direct/proportional/symbolic/LP pipeline for
each priority block in either pass:

```text
build_block_lp()
      |
      v
try_compile_direct_calculation()
      |
      v
try_compile_proportional_calculation()
      |
      v
try_compile_scalar_formula()
      |
      v
LPKernel fallback
```

Replay blocks are compiled only when the graph has spill/import candidates.
When replay can matter, the plan contains:

```text
PASS 1 program
REPLAY program
```

Pass 1 deliberately excludes unnecessary future reservoir counterflow.  Replay
can introduce real opposite-direction feasibility witnesses after Pass 1 has
established storage deliveries and spill/import credit.

## Structural builders

### `build_runtime_state_layout(input)`

Freezes solver structure and assigns numeric runtime slots.  Important slot
families include:

- transaction allocations and effective daily limits;
- proportional reference cfs;
- remaining group reservations;
- signed measured-flow residuals;
- forward and reverse measured-flow capacity;
- storage-account withdrawal/deposit capacity;
- endpoint delivery factors;
- specified and external-boundary natural-flow inputs;
- natural flow at zones and on flows;
- routed natural-flow coefficient slots;
- spill/replay capacity and credit state.

`new_day()` now performs **data binding**, not the complete numerical solve.  It
loads current measurements, loss factors, limits, account balances, and raw
specified/boundary NF values.  The generated Python calculates the natural-flow
network from those inputs.

### `priority_blocks(input)`

Produces stable priority cohorts.  Existing parent/child priority correction is
preserved: a child whose priority is equal to or earlier than its parent is
placed immediately after the parent.  Transactions are not filtered based on a
representative day, because a later day may activate a currently inactive limit.

### `build_block_lp(input, block, layout, replay=False)`

Builds the incremental linear problem for one priority cohort.  Unlike the old
whole-system equations, a block variable represents an **additional transaction
allocation**, not a separate variable for every path leg.

Path delivery, losses, NF routing, and account deposit factors are represented by
runtime coefficients.  This makes the block model much smaller while preserving
the same accounting constraints.

## Block variables and witnesses

A block can contain two kinds of variables.

### Target variables

These are the transactions at the priority currently being allocated.  Only
these variables are committed to runtime state.

### Witness variables

Witnesses exist only to prove feasibility.  They include:

- descendants of a group target, showing that a new reservation can eventually
  be delivered;
- descendants of an outstanding senior reservation while an intervening outside
  transaction is allocated;
- replay-only real counterflow transactions needed to prove reservoir-edge net
  flow feasibility;
- narrowly scoped replay reporting-slack witnesses where a storage outflow must
  be represented without inventing unnecessary Pass-1 counterflow.

Witness solutions are discarded after the block calculation.  They are never
mistaken for committed allocations.

## Block constraints

The current block representation can contain:

- transaction upper bounds;
- signed or directional measurement capacity rows;
- natural-flow capacity rows;
- storage-account withdrawal and deposit rows;
- group reservation equalities;
- reservoir net-flow coupling rows;
- replay-only counterflow/slack rows.

A committed target increment updates all affected runtime capacities before the
next priority block executes.

## Direct calculations

A one-target block with no required coupled optimization is compiled to a
`DirectCalculationKernel`. When its additional allocation starts at zero, its
objective is a positive constant, and its rows normalize to nonnegative
consumption bounded by remaining capacity, the generated block is a MIN formula:

```python
def _pass1_block_0(state):
    amount = min(
        state[S_REMAINING_LIMIT_A],
        state[S_REMAINING_MEASURED_FORWARD_D],
        state[S_REMAINING_NF_R],
    )
    amount = checked_nonnegative_increment(amount)
    state[S_ALLOCATED_A] += amount
    state[S_REMAINING_LIMIT_A] -= amount
    state[S_REMAINING_MEASURED_FORWARD_D] -= amount
    state[S_REMAINING_NF_R] -= amount
    return 0  # No numerical LP solve.
```

Constants and coefficient-pair signs are folded during compilation. Bounds with
the same expression appear only once. The block has no lower endpoint, row
intersection loop, or separate commit function. `checked_nonnegative_increment`
rejects non-finite amounts and amounts below `-TOL`; small negative roundoff is
clamped to zero before any update.

`_validate_direct_inputs(state)` runs once per day, after natural-flow
initialization and before any allocation. It checks the daily bounds and the
finite, declared signs of coefficients used by these formulas. Coefficient
signs are checked strictly; proven capacities allow the existing roundoff tolerance.
Signed residuals can start the day negative and become feasible after earlier
blocks; their nonnegativity is checked by the increment check at execution time.
Unlimited variable bounds may be positive infinity; constraint capacities must
be finite. Coefficients must remain unchanged throughout allocation and replay.

A runtime coefficient can still be exactly zero. Its bound is emitted as
`capacity / coefficient if coefficient > 0.0 else float('inf')`. Even a very
small positive coefficient still limits consumption. The zero case can omit
its feasibility check only when the capacity starts nonnegative and every
writer preserves it. The compiler checks all Pass 1 and replay commits for a
bound covering their total consumption. Witness coefficients must also be
nonnegative: an uncommitted counterflow witness cannot finance consumption.
Spill credit adds natural flow or clears a directional capacity.

These checks establish the capacity invariant in exact arithmetic; execution
retains the solver's floating-point tolerance. Signed net residuals and group
reservations are not assumed to be nonnegative capacities. If the compiler
cannot establish the required conditions, it reuses
`DirectCalculationKernel._interval` through the existing kernel executor.
General scalar models retain support for signed coefficients, genuine lower
bounds, and negative objectives without duplicating that interval interpreter
in the code generator.

## Analytical proportional calculations

Equal-priority blocks that contain only monotone upper-capacity coupling compile
to a `ProportionalCalculationKernel`.

For active members with proportional factors `f_i`, the next common increment is
computed from bounds such as:

```text
g <= remaining_limit_i / f_i

g <= remaining_row_capacity /
     sum(row_coefficient_i * f_i for active i)
```

The kernel commits the common increment, determines which transactions became
blocked, removes them, and continues with the remaining cohort.  Unlimited
reference members and tiny proportional shares retain the legacy allocation
conventions.

The coupled-block fast path also detects many blocked transactions directly from
exhausted residual rows, avoiding one LP classification solve per active member.

## Symbolic scalar formula compiler

Blocks that are not simple direct or monotone proportional calculations are
passed to `try_compile_scalar_formula()` before falling back to an LP.

The scalar compiler treats the block as a parametric linear program.  For a
proportional solve it introduces a common scalar objective `g`, then eliminates
witness/coupled variables until the feasible interval for `g` is expressed as
runtime formulas.

The compiler performs:

1. equality substitution where possible;
2. symbolic coefficient/RHS projection;
3. Fourier-Motzkin-style elimination using cross multiplication rather than
   specializing to one day's coefficient values;
4. canonicalization and merging of equivalent row shapes;
5. removal of structurally redundant inequalities;
6. expression-DAG construction;
7. lowering of that DAG to straight-line Python.

The projected objective only needs an upper bound. If an allocation is feasible
for `g`, the same allocation is feasible for any smaller nonnegative `g`, since
the added constraints are `x_i >= factor_i * g` with nonnegative factors. The
compiler verifies that every projected coefficient has that sign. Zero rows
still check feasibility, and unexpected runtime signs use the LP fallback.
Original LP lower bounds and reservation equalities remain part of projection.

```python
upper = min(upper_formula_1, upper_formula_2, ...)
g = max(0.0, upper)  # after feasibility and boundedness checks
```

The generated `_intersect_projected_row()` helper contains those repeated checks
once per plan. It is also used by standalone scalar programs.

Loss/routing coefficients that vary by date remain runtime slot reads inside the
already-compiled formulas.  There is **no runtime formula cache and no runtime
projection**.

Projection currently has a row budget of 5000.  If symbolic projection becomes
too large or requires unsupported assumptions, compilation retains an exact
`LPKernel` for that block rather than failing the whole plan.

## LP fallback

An unresolved block is explicit in `plan.code()`.  The generated program includes
a readable commented rendering of its variables, allocation rule, constraints,
and committed state updates, followed by the actual fallback call, for example:

```python
# Numerical LP fallback
#
# VARIABLES
#     ...
#
# CONSTRAINTS
#     ...

def _pass1_block_17(state):
    return _PASS1_FALLBACK_17.execute(state)
```

The LP object is injected into the generated module's execution namespace.  This
keeps the generated code truthful: every remaining numerical optimization is
visible as an explicit fallback call.

## One generated program

`CompiledPlan.__post_init__()` lowers all compile-time kernels into one Python
module using `compile/codegen.py`:

```text
compile-time kernel IR
        |
        v
Python source
        |
        +--> plan.code()
        |
        +--> exec(...)
                 |
                 +--> execute(state)
```

There is no separate pretty-print representation.  The source shown by
`plan.code()` is the source Python actually executes.

Generated state indexes are given readable constants, and formulas use named
intermediate expressions rather than reconstructing the original `BlockLP` at
runtime.

Generated proportional blocks share one `_allocate_proportionally()` loop.
Each block supplies its maximum-increment formula, blocker check, and commit
function. A monotone block reuses its common-increment formula with one factor
set to 1 when checking a single member; there is no separate member formula.

Identical commit functions are shared across passes. Constant coefficients are
inlined, zero update terms are omitted, and snapshot temporaries are emitted
only when an update also writes a coefficient that another update reads. Replay
calls the Pass 1 function again when the kernel type, complete LP model, and
updates are identical. It gets a separate function when its net-flow constraints
or counterflow witnesses differ.

Transaction IDs are labels, not Python local names: generated scalar allocations
use compiler-owned `amount` or `_allocation` locals. Flow and zone helper names include a
unique structural index so distinct IDs such as `D-1` and `D_1` cannot collide.
Original IDs remain unchanged in lookup keys, output, and escaped comments.

## Natural flow is part of the generated program

Natural-flow calculation previously occurred before the compiled executor.  It
is now part of the generated Python.

`new_day()` binds the raw daily inputs, including:

- measured interzone flows;
- endpoint delivery factors;
- specified NF values;
- external-boundary NF values and active flags;
- transaction/group limits;
- storage-account balances.

`execute(state)` then performs the numerical NF calculation before Pass 1.
The generated source contains:

1. endpoint loss-transform helpers;
2. NF route selection and downstream propagation;
3. calculated endpoint-loss adjustments used by system gain/loss zones;
4. routed NF coefficients used by transaction constraints;
5. specified natural-flow propagation;
6. external-boundary natural-flow handling and prior-commitment removal;
7. initialization of `remaining_nf[...]` and flow-level NF reporting values.

The compatibility path in `RuntimeStateLayout.new_day(..., natural_flow=...)`
still exists for tests/manual callers, but normal `solve_plan()` does not use a
`NaturalFlowCalculator` to initialize the compiled execution state.

## Loss-transform boundary and future piecewise losses

The generated NF code intentionally uses loss-transform helpers rather than
hard-coding NF as only a product of scalar coefficients.  Conceptually each
endpoint exposes operations like:

```python
deliver(q)
required_inflow(delivered)
```

With the currently supported fractional loss these reduce to multiplication and
division by a runtime delivery factor. The generated program defines just two
shared functions, with direct calls that identify the endpoint's factor slot:

```python
_deliver(state, S_LOSS_FROM_DELIVERY_RIVER_STO, value)
_required_inflow(state, S_LOSS_TO_DELIVERY_RIVER_USER, remaining)
```

There are no per-endpoint wrappers. Both functions read the factor from state
on each call, preserving daily loss changes, finite/sign checks, zero-flow
rounding, and the error for inverting a zero-delivery factor. `_LOSS_ENDPOINTS`
maps factor-slot indexes to flow/endpoint labels and is read only on error.

This boundary is intended for future piecewise-linear loss support.  A later
compiler can select a shared helper for each loss type, with segment logic such as:

```python
if q <= breakpoint_1:
    ...
elif q <= breakpoint_2:
    ...
else:
    ...
```

without rewriting NF routing, boundary handling, or spill-credit propagation.

Piecewise losses are **not yet supported** by the block compiler.  Supporting
piecewise incremental transaction losses will additionally require accounting
for the existing/base physical flow through a lossy reach, because incremental
loss across a breakpoint depends on the flow already present.  The current
loss-transform abstraction is intended to be the entry point for that work.

## Reservoirs, signed flow, and replay

Bidirectional reservoir accounting uses signed physical residuals together with
nonnegative directional capacities.

Pass 1 avoids the old pattern of introducing all possible reverse variables,
minimizing them, temporarily fixing them, maximizing the target, and releasing
the fixes.  Instead it structurally omits unnecessary future counterflow.

After Pass 1, generated code determines unavoidable non-natural-to-natural
residual flow, locks the corresponding replay capacity, converts the delivered
amount into downstream natural-flow credit, and then runs the precompiled replay
program.

Replay may be required even when physical spill credit is zero, because a first
pass can establish a legitimate storage delivery that allows an earlier
reservoir-inflow transaction to increase without creating an artificial flow
loop.

## Storage accounts and cumulative group limits

Storage-account balances persist through `TrxnSchedule` across days.  The block
LP only needs beginning-of-day withdrawal/deposit capacity:

```text
withdrawal capacity = balance - floor
deposit capacity    = ceiling - balance
```

A withdrawal consumes anchor allocation 1:1.  A deposit consumes ceiling
capacity using the delivered amount at the final path leg, so path losses are
respected.

Group cumulative limits are also cross-day schedule state rather than new LP
structure.  Each day's effective group upper bound is obtained from the normal
schedule limit calculation, including daily/call/cumulative restrictions.  The
final daily group allocation is committed to cumulative use at the end of the
day.  A cumulative limit may supply the group's finite effective cap even when
there is no ordinary daily upper limit.

## Daily execution

Normal execution is now approximately:

```text
for each date:
    bind raw daily inputs into state

    execute(state):
        initialize natural flow
        validate direct-formula daily inputs
        run PASS 1 formulas/kernels
        calculate and apply spill/import NF credit
        run replay formulas/kernels when applicable

    verify reservations
    construct output apportionments/slack reporting
    commit group cumulative use and account balances
```

Output reporting, lag/unlag handling, cross-day account state, and cumulative
schedule bookkeeping remain outside the generated daily formula module.

## Compilation report

`SolverOutput.compilation_report` currently reports, among other fields:

- `priority_blocks`;
- `direct_calculations`;
- `proportional_calculations`;
- `scalar_formulas`;
- `lp_kernels`;
- `maximum_formula_rows`;
- `maximum_kernel_variables`;
- `runtime_slots`;
- `execution_days`;
- `execution_lp_solves`;
- `spill_replay_flows`;
- `runtime_compilation` (currently `False`).

A plan that fully reduces to formulas should report `execution_lp_solves == 0`.

## Current support boundary

Implemented in the current block compiler:

- sequential priority allocation;
- equal-priority proportional allocation and redistribution;
- nested group reservations and feasibility witnesses;
- daily, call, and cumulative path limits;
- cumulative group limits when they provide a finite effective daily cap;
- nonnegative whole-day lags (`lag_from_zone` and `lag_to_zone` must both be
  integer-valued; values such as `1.0` are accepted);
- varying fractional endpoint losses;
- signed/reverse transaction paths where physically allowed;
- bidirectional reservoir net-flow accounting;
- storage zones and storage accounts;
- Pass-1 / spill-credit / replay reservoir behavior;
- specified natural flow;
- external-boundary natural flow and prior commitments;
- compiled NF routing and initialization;
- direct formulas;
- analytical proportional formulas;
- fully symbolic scalar projection to Python formulas;
- exact small-LP fallback when a block cannot be reduced safely.

Still deliberately unsupported:

- fractional-day lags: compilation raises `UnsupportedBlockInput` identifying
  the flow and lag field, before any daily allocation runs;
- piecewise/absolute endpoint losses and segment-dependent transaction delivery;
- formula structures that exceed configured symbolic projection safety budgets,
  except that those blocks transparently retain an `LPKernel` fallback.

Unsupported accounting rules raise `UnsupportedBlockInput` rather than silently
switching to a different solver interpretation.

Fractional interpolation/inversion remains available in the measurement and lag
utilities, but is not supported by the compiled solver. Independently unlagging
daily allocations can create negative transaction values and fail to reproduce
the original measured totals. Supporting fractional lags will require constraints
across days and consistent boundary allocations; rounding the input lags or
clipping reconstructed allocations is not a substitute.

## Validation

Useful focused tests include:

```text
tests/test_block_compile.py
tests/test_block_direct.py
tests/test_direct_min_codegen.py
tests/test_compact_codegen.py
tests/test_block_proportional.py
tests/test_block_symbolic_formula.py
tests/test_block_accounts.py
tests/test_block_group_cumulative.py
tests/test_block_reverse.py
tests/test_plan_code.py
tests/test_real_problems.py
```

The real-problem Duchesne and Uinta comparisons have been useful regression
fixtures because they exercise nested reservations, storage/replay, signed flow,
natural-flow routing, and larger symbolic formulas together.

The intended correctness rule is:

> Compilation may replace an LP with equations only when the generated program
> is algebraically equivalent for the supported runtime parameter regime.  If
> that cannot be proven within the compiler's current rules/budget, retain the
> exact LP fallback.

That principle keeps the generated program fast on common blocks while allowing
new or unusual accounting structures to remain correct before they receive a
specialized formula implementation.
