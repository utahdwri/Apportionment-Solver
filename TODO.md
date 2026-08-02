Recommended order of changes:
- [x] Replace _determine_reason() with structured solve-step evidence.
    *
- [x] Fix feasibility fallback and never use penalized objective values as variable values.
- [ ] Keep natural-flow constraints in the spill pass and explicitly add locked spill as supply.
- [ ] Exclude TrxnGroup variables from the final minimum-component-flow objective.
- [ ] Make upper_limit=None semantics explicit rather than substituting 1,000.
- [ ] Reject malformed transaction paths instead of silently retaining input order.
- [ ] Implement storage-account and cumulative-volume constraints before reporting storage exhaustion as a possible reason.
- [ ] Preserve both pass-1 and pass-2 decisions rather than overwriting the transaction’s reason.