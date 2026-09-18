# Apportionment Solver: correctness and generated-code review

Reviewed September 18, 2026, using the attached `src(6).zip`. The linked GitHub branch could not be fetched, so these findings refer to the attachment, not a verified current Git commit. No project implementation files were changed.

Archive SHA-256: `10067387cbfd36ce35c1b17c9bfd4e7dbbea32cc0ed02734d269b2a2930abd03`.

The block-first architecture is a useful foundation: path variables collapse to anchor increments, senior allocations become residual state, and a small `BlockLP` separates accounting rules from direct/proportional/symbolic execution. The main correctness problems I found are in accounting-model construction, data preparation, reporting, and generated identifiers. An LP fallback cannot repair an incorrectly constructed accounting model.

Your simplification ideas are largely sound. Separate daily validation, specialize upper-cap calculations, share equivalent pass/replay blocks, and emit the proportional scheduling loop once. Preserve the distinction between validating daily inputs and checking feasibility after earlier allocations have changed the residual state.

## Evidence and scope

- Existing command: `PYTHONPATH=src python -m unittest discover -s tests -v`.
- Existing result: **101 tests run; 94 passed and 7 skipped**, in approximately 18.7 seconds here.
- Runtime: Python 3.12.14, NumPy 2.3.5, SciPy 1.17.0. These timings are observations from this environment, not a benchmark comparison with your machine.
- Inspected the compiler, source emitter, symbolic projector, numerical kernels, runtime binding/reporting, schedule, graph, measurement, loss, and lag handling.
- Executed small examples for the findings below and inspected generated source and block models.
- `test_review_regressions.py` supplies ten proposed tests. **They intentionally fail on this snapshot.** Their assertions describe the expected corrections; they are not an implementation patch. The run reported 9 assertion failures and 6 errors because several methods contain subtests.
- The Duchesne and Uinta tests in `tests/test_real_problems.py` call `solve()` but do not assert expected allocations or accounting invariants. They establish that execution completes, not that the results are correct.

## Correctness findings

### 1. P1 — Replay counterflow witnesses can bypass their parent-group limits

Location: `src/ut_water_apportionment/compile/compile.py`, lines 260–270, with reservation construction later in `build_block_lp()`.

Replay adds an opposite-direction path transaction using `included.add(name)`. It does not also include the transaction's ancestor groups and the constraints needed to make that child a valid witness. The earlier reservation-subtree loop only includes root groups whose priority interval covers the current block; it does not cover a future group needed by a newly added counterflow witness.

Reproduction:

- River-to-reservoir net measurement: 0.
- Downstream diversion measurement: 10.
- Priority 1: `FILL`, river to reservoir, limit 10.
- Priority 2: group `P`, limit **0**.
- Priority 3: `RELEASE`, child of `P`, limit 10, reservoir to river to diversion.

The first replay block contains `FILL` and `RELEASE`, but omits `P`. It proves a 10-unit fill feasible using a 10-unit release witness. The later group and release blocks correctly allocate zero to the release. Final output nevertheless reports `FILL = 10`, a reverse reservoir slack of −10, and an unauthorized downstream diversion of 10.

This violates the stated rule that replay inflows are justified by feasible real counterflow transactions. A child with a zero parent cap is not such a witness.

**Recommendation:** Build the full constraint closure for every witness: ancestor groups, applicable group limits/reservations, and any additional descendants needed by those equations. Also ensure that later blocks preserve any future counterflow feasibility on which an already committed allocation depends. Merely adding a child to an LP is insufficient.

Regression: `test_replay_counterflow_witness_respects_its_parent_cap`.

### 2. P1 — Fractional unlagging can create negative allocations and break measurement reconciliation

Locations: `compile/runtime.py:74`; `lag_utils.py:238–261` and the inverse recurrence in `unlag_series()`.

The solver allocates nonnegative quantities independently in the lagged daily frame, then independently inverse-interpolates each output component with a default zero boundary. That inverse operation does not preserve nonnegativity, daily limits, or consistency with the actual measurement boundary values.

Reproduction:

- Original diversion measurements: 10 on January 1, 2, and 3.
- Solve January 2–3; diversion lag is 0.5 day.
- Transaction limits: 5 on January 2 and 0 on January 3.

The returned forward-path transaction values are **10 and −10**. The signed sums of all returned diversion components are **20 and 0**, whereas the original measurements are **10 and 10**. Changing `is_forward` to match the reconstructed sign does not make a negative allocation legal for that transaction path.

**Recommendation:** Make measurement-time allocation feasibility and boundary allocations part of the time-coupled model. A period model or another constrained reconstruction can enforce the required relationships. Do not fix this by clipping negative outputs, because that generally breaks measurement totals. Until this is implemented, fractional allocation support should be explicitly limited or rejected when it cannot preserve those invariants. The two incomplete fractional-allocation tests are currently skipped, despite the compiler documentation describing fractional lags as supported.

Regressions: `test_fractional_unlag_preserves_transaction_direction`, `test_fractional_unlag_reconciles_to_original_measurements`.

### 3. P1 — Natural-flow consumption omits the first endpoint loss

Locations: `compile/state.py:219–243`; `compile/compile.py:424–434` and the corresponding NF state updates.

The anchor variable is the allocation on the first path leg. Its flow coefficient starts at `item.factor`; endpoint delivery factors are applied only between successive legs. Natural-flow constraints then charge the anchor allocation directly against source NF, with coefficient 1 at the source. That misses the conversion from first-leg gauge flow back to the amount removed from the source zone.

Reproduction: specify 4 units of NF entering a river zone, a diversion gauge measurement of 10, a 50% `loss_from_zone` on that diversion, and a transaction limit of 10. The solver allocates **4 at the diversion gauge**. That requires **8 at the source**, although only 4 units of source NF are available. The maximum gauge allocation supported by that NF is **2**.

**Recommendation:** Give each transaction explicit anchor-to-source and anchor-to-destination conversions, in addition to the existing per-flow conversions. Multiply routed NF consumption by the source withdrawal conversion, using the appropriate endpoint for reverse paths. Use the same conversion for both LP coefficients and state updates. Review account endpoint semantics at the same boundary; the existing account code deliberately uses path-leg variables, so changing those units needs an explicit decision rather than an incidental refactor.

Regression: `test_source_endpoint_loss_is_charged_to_natural_flow`.

### 4. P1 — Distinct graph IDs can produce the same generated function and silently change allocations

Location: `compile/codegen.py:674–702`; `_identifier()`.

Slot constants have collision handling, but flow/zone helper names do not. For example, `D-1` and `D_1` both generate `_deliver_D_1_from`. The later function definition replaces the earlier definition, so the wrong flow's loss factor is used. Zone names have the same problem for NF routing and propagation helpers.

In a reproduced two-diversion/storage example, renaming an unrelated flow from `D_1` to `D_OTHER` changed the senior natural-water diversion from **8 to 10** and the storage delivery at that diversion from **2 to 0**, with no numerical input change.

**Recommendation:** Allocate a unique compiler-owned symbol for every graph object. Use an index or another collision-free suffix. Keep the original ID in a safely formatted comment or label; do not use normalized display text as symbol identity.

Regression: `test_renaming_a_flow_does_not_change_allocations`.

### 5. P2 — Ordinary transaction IDs can break generated Python

Locations: `compile/codegen.py:267–271`, the assignment in `emit_direct()`, and the corresponding local in `emit_scalar_formula()`.

`_identifier()` does not protect against Python keywords, the `state` parameter, imported functions, constants, or emitter temporaries. Confirmed examples:

| Transaction ID | Result |
| --- | --- |
| `class` | Compilation raises `SyntaxError` |
| `state` | The allocation overwrites the state-array parameter; execution fails |
| `min`, `isfinite`, `TOL` | Execution raises `UnboundLocalError` |

**Recommendation:** Use a generated local such as `_allocation_0`, with the transaction ID only in a comment. This is both simpler and safer than trying to enumerate all reserved names.

Regression: `test_transaction_ids_do_not_become_unsafe_python_locals`.

### 6. P2 — Negative observations are normalized after dependent residuals are calculated

Location: `timeseries_manager.py:282–346`.

With the enabled `COALESCE_NEGATIVE_FLOWS_TO_ZERO` policy, a negative one-way measurement contributes to residual-flow calculations before it is replaced with zero. Dependent flow balances and NF then refer to a different set of physical values from the values exposed to allocation.

Reproduction: two river diversions measure −5 and 8. The first becomes zero, but the computed system gain remains **3**, not **8**. The transaction on the second diversion receives only **3**, although the normalized data support 8.

**Recommendation:** Validate and normalize observed values before calculating any dependent residuals, then calculate residuals in the precomputed dependency order. This is a concrete reason to introduce the separate daily-input preparation stage you suggested.

Regression: `test_negative_observations_are_normalized_before_residuals`.

### 7. P2 — Bidirectional stream slack reports natural flow twice, with cancelling signs

Location: `compile/runtime.py:61–68`.

Both forward and reverse slack transactions receive the complete signed `flow_natural` value. With a bidirectional stream-to-stream flow carrying 10 units of natural water and no scheduled transactions, output contains:

| Component | Value | `is_forward` |
| --- | ---: | --- |
| Forward slack NF | 10 | True |
| Forward slack CPI | 0 | False |
| Reverse slack NF | −10 | True |
| Reverse slack CPI | 10 | False |

The overall signed physical total is still 10, but reported NF sums to zero and CPI to 10. Some direction flags also contradict the value signs.

**Recommendation:** Form a single signed NF/CPI decomposition for each flow, then assign components to the appropriate reporting direction. Keep the valid case where a signed CPI residual offsets NF that exceeds measured flow; the fix is to avoid duplicating the decomposition across both slack directions.

Regression: `test_bidirectional_stream_slack_does_not_cancel_natural_flow`.

### 8. P2 — Public solve options are silently ignored

Location: `solver.py:5–18`.

`solve()` accepts `max_daily_apportionment` and `compilation_options`, but neither is passed to the compiler. Calling `solve(problem, max_daily_apportionment=3)` for an otherwise unlimited transaction with a 10-unit diversion allocates **10**.

**Recommendation:** Thread supported options into compilation and schedule construction. Until an option is implemented, reject a non-default argument explicitly rather than silently accepting it.

Regression: `test_public_max_daily_apportionment_is_honored`.

### 9. P2 — A transaction path entering a cycle never finishes validation

Location: `trxn_schedule.py`, `_get_ordered_path()`, particularly its `while zone in next_by_zone` loop.

The path `A → B → C → B` has one root and no branching source, so it passes the preliminary checks. Traversal then alternates B and C indefinitely while appending path items; the final path-length check is never reached. A separate timed probe confirmed nontermination. This probe is not included in the portable regression file.

**Recommendation:** Track visited zones or consumed path items during traversal and raise a contextual `ValueError` on repetition. Complete this structural validation before generating code.

### 10. P2 — Generated errors are not instances of the public exception class

Location: `compile/codegen.py`, `PythonPlanEmitter.build()`.

The generated module defines a fresh `BlockLPError` class. Numerical fallbacks use `compile.kernel.BlockLPError`, which is also the class exported from `ut_water_apportionment.compile`. Therefore a caller catching the public `BlockLPError` catches LP failures but misses generated-calculation failures with the same class name. Confirmed with an unbounded direct reverse transaction.

**Recommendation:** Import or inject the shared exception classes into the generated module. There should be one error contract across all execution paths.

Regression: `test_generated_errors_use_the_public_exception_class`.

## Answers to the simplification questions

### Can daily values be checked up front or in a separate function?

**Yes.** Distinguish checks by what can change:

| Stage | Checks and calculations |
| --- | --- |
| Compile time | References, path continuity/cycles, priority/group structure, supported loss definitions, symbol uniqueness, constant signs, residual dependency order |
| Start of each solve | Measurement shape/date coverage, replacement-measurement references, raw value policy; optionally validate all raw series in one pass |
| Start of each day | Resolve losses, specified/boundary NF, normalized observations, residual flows, active limits, cumulative remaining limits, beginning account capacities |
| After NF initialization | Validate the derived daily coefficients and their sign/finite-value contract once |
| During allocation | Check conditions depending on changed residual state or active proportions: feasibility, exhausted reservations, division by a possibly zero pivot, boundedness, numerical tolerance |

Most repeated finite-value and coefficient-sign checks can leave individual block bodies because those coefficients do not change during a day. A one-time check before the entire period is insufficient for cumulative balances and daily derived values. Likewise, a projection normalization guard can depend on the active proportional cohort and must remain where that cohort is known.

Do not remove zero-coefficient handling merely because a slot is marked nonnegative: `Slot.sign == 1` currently means **greater than or equal to zero**, not strictly positive. A zero coefficient contributes no cap, but its row can still expose infeasibility. Treat positive infinity as an allowed unbounded transaction cap where the API permits it; reject NaN.

### Is `_lower` really needed?

**For the common generated code, substantially less than the implementation suggests.** There are three distinct levels:

1. **Keep lower/equality constraints in `BlockLP`.** Reservations and signed replay constraints need that representation. Removing it from the accounting IR would lose information.
2. **Specialize direct blocks to upper caps when their structure proves it.** Every direct block in both real fixtures qualifies after normalizing coefficient signs: 175 of 175 in Duchesne and 86 of 86 in Uinta, counting both passes. A negative coefficient with a lower row can still become an ordinary upper cap after multiplication by −1. Keep the general interval path only for blocks that truly require it, including supported hand-built LPs.
3. **The symbolic scalar projector can use an upper-only result by construction.** It maximizes a nonnegative scalar `g` subject to `x_i >= factor_i * g`, where the factors are nonnegative. If some `g` is feasible, every smaller nonnegative `g` is feasible using the same witness `x`. Thus a feasible projected domain is `[0, g_max]`; original lower constraints become feasibility conditions on the witnesses, not a positive minimum for `g`.

The symbolic implementation supports that proof: easy-target substitution introduces only nonnegative scalar-factor coefficients; equalities contain no easy-target coefficients; equality substitution and inequality projection preserve nonnegative scalar-factor coefficients. I instrumented all **31 symbolic programs** generated for the two real fixtures: **9,386 final rows and 54,477 scalar-factor coefficient expressions**, with **zero negative or unknown-sign expressions**.

That supports replacing the generic projected `_lower` / negative-coefficient branch with an explicit compile-time invariant and an upper-cap evaluator. Retain zero-row feasibility checks and a check that the resulting upper endpoint is nonnegative within tolerance. If future compiler rules violate this invariant, reject that specialization or use the generic fallback.

After validation and constant folding, a simple direct block should resemble this conceptual code:

```python
def _block_0(state):
    amount = min(
        state[LIMIT_A],
        state[DIVERSION_REMAINING],
        state[NF_REMAINING],
    )
    amount = checked_nonnegative_increment(amount)
    state[ALLOCATED_A] += amount
    state[LIMIT_A] -= amount
    state[DIVERSION_REMAINING] -= amount
    state[NF_REMAINING] -= amount
```

This illustrates a lossless, unit-coefficient case. Actual blocks must still update all required reporting/accounting slots, and dynamic coefficients need zero-aware cap evaluation.

### Can replay reuse `pass1_block` functions?

**Some can; replacing the complete replay program with pass 1 is incorrect.** The function names are incidental. The relevant distinction is between directional gross-flow capacity in pass 1 and signed net-flow capacity with additional witnesses in replay.

I tested a zero-net river/reservoir connection with a 10-unit downstream diversion, a senior fill transaction, and a later valid release transaction. The current distinct replay models allocate fill 10 and release 10. Substituting the existing pass-1 operations for all replay operations allocates **zero to both**. Pass 1 sees zero gross capacity in either reservoir direction and cannot prove the balanced exchange.

Recommended approach:

- Compile only one program when there are no replay candidates. Currently replay operations are still built and emitted even when `execute()` never calls them.
- Normalize each block to its actual capacity expressions, witnesses, objective, and updates; intern equivalent normalized models and share the emitted function.
- Reuse an ordinary block when its residual resources have the same meaning in both phases. Different slot names may hide equal resources, but do not assume directional and signed residuals are interchangeable after counterflow or spill locking.
- Retain separate specialized blocks for actual signed-flow/witness differences.
- Do not skip replay solely because `_apply_spill_credits()` returns zero: a zero-net reservoir exchange can still need replay.

A small helper such as `compile_block(model)` should also replace the duplicated direct → proportional → symbolic → LP selection logic inside `compile()`.

## Other ways to make the compiler and output smaller

### Fold constants and omit structurally zero terms before emission

`Slot.constant_value` already exists, but `PythonPlanEmitter.scalar()` still emits a state read for every slot. Direct and proportional code therefore repeatedly reads and branches on coefficients already known to be 0 or 1. Honor constants consistently, discard zero update terms, simplify multiplication by ±1, and omit structurally unreachable NF terms. A zero row still needs a feasibility check if its bound is not proven nonnegative.

`build_block_lp()` currently adds NF rows for every stream zone for every NF-sourced variable, including unreachable zones. Use structural reachability to avoid constructing this dense intermediate representation in the first place.

### Canonicalize coefficients by identity, not display name

`formula.py:scalar_expr()` maps a negative coefficient alias back to its source slot index but retains the negative slot's display name. `SlotExpr` includes `name` in dataclass equality/hash. Consequently, `c + (-c)` does not simplify to zero for a dynamic coefficient pair whose two names differ, despite both expressions reading the same source index.

I confirmed the result remains an `AddExpr` containing `SlotExpr(index=0, name='c')` and `SlotExpr(index=0, name='negative_c')`. Make the display name non-comparing metadata, or canonicalize both aliases to the same source symbol. This should reduce unnecessary projection complexity; I have not measured its performance benefit.

More broadly, a coefficient can be represented as a canonical value/reference plus a multiplier. Then negative coefficients need not consume separate state slots at all. Preserve the physical alias relationship explicitly rather than maintaining duplicate values by convention.

### Emit one proportional scheduling loop

The active-cohort / tiny-share / increment / blocker-removal loop is reproduced across `LPKernel`, `ProportionalCalculationKernel`, `ScalarFormulaKernel`, and emitted block functions. Keep one authoritative scheduling algorithm and vary the common-increment, blocker, and commit operations. A shared helper can itself be included in `plan.code()` if the goal is for that source to show the full executable program.

This also reduces the chance that kernel and emitted behavior diverge. For example, `_commit_updates()` validates/clamps increments, while `emit_commit()` emits raw updates without that common validation contract. A single validated increment boundary is easier to reason about.

The existing deferral of proportional shares below `1e-6` is a policy choice carried across implementations, not an exact statement of proportional allocation for arbitrarily small shares. Keep it documented if retained.

### Emit direct updates when no coefficient can be overwritten

`emit_commit()` calculates `_change_N` temporaries for every destination slot, then writes all slots in a second phase. That snapshot approach is necessary only when a coefficient read could be affected by another update. For ordinary generated blocks, coefficient slots are separate from mutable capacity/allocation slots.

Check that dependency condition at compile time. When read-coefficient slots and written slots are disjoint, emit `state[slot] += coefficient * amount` directly; otherwise keep the snapshot form. For cohorts, combine terms per destination and avoid repeated computation of each member's increment.

### Compile static topology once

`DailyDataManager._determine_residual_calc_order()` recomputes a structural dependency order every day. Cache it with the graph/lag metadata. NF routing code also emits similar graph-dispatch logic separately for many source zones. Share route-selection/propagation logic or emit sparse precomputed routes where topology and boundary configuration permit it.

### Separate inspection from unnecessary source expansion

Showing the exact executed source is useful. It does not require copying every common algorithm into every block. Emit shared helper definitions plus compact specialized block bodies. Keep numerical fallbacks explicit. The current `plan.code()` needs its injected fallback namespace when fallbacks are present, so it is not generally a standalone exported module; either document that distinction or provide a deliberate export mechanism.

## Size and runtime observations

Both real fixtures cover June 1–July 1, 2025: 31 execution days.

| Observation | Duchesne | Uinta |
| --- | ---: | ---: |
| Priority blocks | 159 | 70 |
| Generated source lines | 119,068 | 194,926 |
| Generated UTF-8 bytes | 7,777,644 | 9,004,751 |
| Top-level generated functions | 1,247 | 457 |
| Lines inside replay block/helper functions | 55,730 | 95,651 |
| Direct blocks qualifying for upper-cap specialization | 175 / 175 | 86 / 86 |
| Scalar-formula blocks, both passes | 10 | 21 |
| Maximum intermediate formula rows | 157 | 4,096 |
| Explicit LP-kernel blocks | 0 | 0 |
| Actual numerical LP solves during execution | 31 | 0 |
| Observed compile seconds | 2.394 | 4.869 |
| Observed solve seconds | 3.136 | 5.245 |

Replay block/helper functions account for roughly 47% and 49% of these modules. This is an opportunity for deduplication, not evidence that the same fraction can be removed without examining equivalence. Also, `lp_kernels == 0` does not mean a run is LP-free: formula runtime guards can still call their numerical fallback, as Duchesne demonstrates.

## Suggested order of work

1. Add the targeted regressions and output invariants. Fix replay witness closure, source-loss NF conversion, generated identifiers, and fractional-time correctness first.
2. Establish one daily preparation/validation contract; normalize measurements before residual calculation. Preserve residual feasibility checks during allocation.
3. Add an explicit upper-cap specialization, including the scalar projection invariant, and honor constant/sign metadata throughout emission.
4. Canonicalize symbolic aliases; fold zero terms and omit unreachable NF rows before projection.
5. Share the proportional loop and equivalent pass/replay operations; do not build dead replay programs.
6. Cache structural residual/NF work and only then remeasure compilation size and solve time.

For validation, compare specialized arithmetic with `LPKernel` on the **same correctly constructed block**, including zero delivery coefficients, inactive limits, and exhausted capacities. Separately test accounting invariants that a shared bad model could violate: original-date measurement reconciliation, transaction direction, parent/cumulative limits, account balances, and source NF consumption. The real fixtures need expected results or these independent invariants in addition to smoke execution.

The top-level README also needs reconciliation with the current API: it still uses `Trxn`, `compile_solver_input()`, `plan.formulas()`, and `plan.report()`, which are not the current exported interface in this snapshot. The `new_day()` docstring and compiler documentation mention an obsolete compatibility argument. Piecewise losses are explicitly unsupported by the current block compiler; their rejection is a documented boundary, not a newly identified correctness bug.

## Running the proposed regressions

After copying `test_review_regressions.py` into the project's `tests` directory and installing the project:

```bash
python -m unittest tests.test_review_regressions -v
```

Expect failures against the reviewed attachment. The file is intended as a starting set of regression cases to turn green while fixing the findings, not as a replacement for the existing suite.
