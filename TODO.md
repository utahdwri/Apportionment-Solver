
 - [ ] ...
        * Skipped items deserve an audit entry too.

- [ ] solve_for_nonpath_vars seems to be too inclusive. But when I target it to just the slack variables, many tests fail. What is going on?

- [ ] Update Trxn data model so the source-account is 'Natural Flow' for diversions from the stream,
      During the 1st pass:
        * once all the natural flow for a zone has been used up, don't try to maximize apportionments to any trxns comming from that zone's natural flow
      During the 2nd pass:
        * only attempt to maximize transactions that were previously limited by remaining natural flow.
        * once all the natural flow for a zone has been used up, don't try to maximize apportionments to any trxns comming from that zone's natural flow

      Tracking Remaining Natural Flow:
      - on startup, we need to know the natural flow avaialable to each zone.
      - after every aportionment from natural flow, we need to
        1. propigate the impact downstream
        2. report how much NF remains in each zone (put in audit log)
        3. update my set of which NF zones have been depleted
        4. (future) update the NF constraint coefs when we move from one slope to another (depending on which direction the next objective will pull)



- [ ] Implement storage-account and cumulative-volume constraints.
        * Trxn object can reference to and from storage accounts. These are referenced by an account id (str) that should be unique for the source zone and destination zone. (The source zone and destination zone are identified via the path list.)
        * Trxns can have an annual volume limit. The limit resets at a given day & month. The apportionment to a trxn must be limited to the remaining annual volume limit.
        * An account can be configured to limit outgoing trxns when the current balance drops below a specified volume.
        * An account can be configured to limit incoming trxns when the current balance hits a specified ceiling.

        - fix spelling of cummulative_limit!
        - add note to documentation. Say that the expectation is for the volume units to be consistent with the flow units (all in cfs-days or acre-feet)
        - consider adding cumulative_starting_use to zone-accounts.


- [ ] Implement non-integer time-lags - solve multi-day period with one matrix, perhaps using entire period, perhaps using rolling window.

- [ ] Implement piece-wise linear loss curves - See proposed plan in documentation.

- [ ] Keep natural-flow constraints in the spill pass and explicitly add locked spill as supply.
- [ ] Exclude TrxnGroup variables from the final minimum-component-flow objective.
- [ ] Reject malformed transaction paths instead of silently retaining input order.






3. Storage-source exhaustion is not implemented
The models define cum_acft_limit, from_account, to_account, and limit_by_remaining_account_balance, but there is no corresponding constraints or balance updates in the solver.

4. The spill second pass is conceptually too broad
Removing all natural-flow constraints does not specifically make only the identified spill available. It potentially makes every measured stream flow available, including natural flow intentionally classified as unavailable. The data manager explicitly distinguishes natural from available_natural, including external natural flow that has already been utilized upstream.

5. The final tie-break objective is not exactly the documented objective
But a group variable is constrained to equal the sum of its children. Therefore grouped transactions are counted once through their components and again through every parent group. Nested groups are counted repeatedly.

