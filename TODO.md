To complete this iteration:
- reconcile database with new model.
- fully implement for interzone-flows:
    beg_date:, end_date

For later:
- Update documentation
    - Document lags
    - Document losses
    - Document specified natural flow
- Add time-lags
    - Centralize lag application in MeasurementCollection
    - make expected values check the final unlagged results

- Track storage balances, and allow paths to be limited by them.

- DailyDataManager currently does too much, particularly the nf stuff does not belong.

- [ ] Update Trxn data model so the source-account is 'Natural Flow' for diversions from the stream,
      During the 1st pass:
        * once all the natural flow for a zone has been used up, don't try to maximize apportionments to any trxns comming from that zone's natural flow
      During the 2nd pass:
        * only attempt to maximize transactions that were previously limited by remaining natural flow.
        * once all the natural flow for a zone has been used up, don't try to maximize apportionments to any trxns comming from that zone's natural flow

- [ ] Implement storage-account and cumulative-volume constraints.
        * Trxn object can reference to and from storage accounts. These are referenced by an account name (str) that should be unique for the source zone and destination zone. (The source zone and destination zone are identified via the path list.)
        * Trxns can have an annual volume limit. The limit resets at a given day & month. The apportionment to a trxn must be limited to the remaining annual volume limit.
        * An account can be configured to limit outgoing trxns when the current balance drops below a specified volume.
        * An account can be configured to limit incoming trxns when the current balance hits a specified ceiling.

- [ ] Implement piece-wise linear loss curves - See proposed plan in documentation.

- [ ] Keep natural-flow constraints in the spill pass and explicitly add locked spill as supply.
- [ ] Exclude TrxnGroup variables from the final minimum-component-flow objective.
- [ ] Reject malformed transaction paths instead of silently retaining input order.






3. Storage-source exhaustion is not implemented
The models define max_acft, from_account, to_account, and limit_by_remaining_account_balance, but there is no corresponding constraints or balance updates in the solver.

4. The spill second pass is conceptually too broad
Removing all natural-flow constraints does not specifically make only the identified spill available. It potentially makes every measured stream flow available, including natural flow intentionally classified as unavailable. The data manager explicitly distinguishes natural from available_natural, including external natural flow that has already been utilized upstream.

5. The final tie-break objective is not exactly the documented objective
But a group variable is constrained to equal the sum of its children. Therefore grouped transactions are counted once through their components and again through every parent group. Nested groups are counted repeatedly.

