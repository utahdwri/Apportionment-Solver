Recommended order of changes:
- [x] Replace _determine_reason() with structured solve-step evidence.
- [x] Fix feasibility fallback and never use penalized objective values as variable values.
- [x] Make the _get_newly_maxed_vars check more efficient
- [ ] Keep natural-flow constraints in the spill pass and explicitly add locked spill as supply.
- [ ] Exclude TrxnGroup variables from the final minimum-component-flow objective.
- [ ] Make upper_limit=None semantics explicit rather than substituting 1,000.
- [ ] Reject malformed transaction paths instead of silently retaining input order.
- [ ] Implement storage-account and cumulative-volume constraints before reporting storage exhaustion as a possible reason.
- [ ] Preserve both pass-1 and pass-2 decisions rather than overwriting the transaction’s reason.

1. Missing upper limits are capped at 1,000


2. Well-defined paths are not enforced
The active path-ordering code attempts to construct a chain, but if it finds multiple roots, a broken chain, a cycle, or otherwise cannot order every item, it simply returns the original path list rather than rejecting the transaction. This should fail validation before building the LP. A transaction should have: exactly one starting zone; exactly one ending zone; connected consecutive arcs; no branch; no cycle; no repeated flow component unless explicitly supported.

3. Storage-source exhaustion is not implemented
The models define max_acft, from_account, to_account, and limit_by_remaining_account_balance, but there is no corresponding constraints or balance updates in the solver.

4. The spill second pass is conceptually too broad
Removing all natural-flow constraints does not specifically make only the identified spill available. It potentially makes every measured stream flow available, including natural flow intentionally classified as unavailable. The data manager explicitly distinguishes natural from available_natural, including external natural flow that has already been utilized upstream.

5. The final tie-break objective is not exactly the documented objective
But a group variable is constrained to equal the sum of its children. Therefore grouped transactions are counted once through their components and again through every parent group. Nested groups are counted repeatedly.

