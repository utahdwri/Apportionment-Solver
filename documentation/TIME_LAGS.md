# Time Lag Handling

This document describes how time lags are represented and handled by the
apportionment solver. It covers the current integer-lag implementation and the
planned approach for fractional-day lags.

The flow balance equation on a stream reach requires:

INFLOW = OUTFLOW + ΔSTORAGE

The following example illustrates how lags emerge from the ΔSTORAGE term.

```
Suppose we have 3 outflows (Q1, Q2, Q3) and a single unmeasured gain/loss term (G).

G = Q1 + Q2 + Q3 + ΔSTORAGE

Also suppose the impact of water diverted to Q1 takes 2 days to be noticed at the
bottom of the reach, Q2 takes 1 day, and Q3 and G are instantaneous. These lag
times tell us what we need to calculate ΔSTORAGE in the river reach.

ΔSTORAGE_Q1 = [Q1_t-2] - [Q1_t]
ΔSTORAGE_Q2 = [Q2_t-1] - [Q2_t]

[G_t] = [Q1_t] + [Q2_t] + [Q3_t] + ([Q1_t-2] - [Q1_t]) + ([Q2_t-1] - [Q2_t])

Which simplifies to:

[G_t] = [Q1_t-2] + [Q2_t-1] + [Q3_t]
```

The solver must be able to account for these time lags in order to faithfully
represent the physical realities of large river systems. The central goal of
the implementation is to acomidate this without requiring the main
apportionment model to solve the entire simulation period in one large linear
program.

---

## 1. Why time lags are needed

Measurements at different locations do not necessarily represent the same
water at the same time.

For example, a flow observed upstream may take one day to affect a downstream
reach. If the downstream accounting equation is being evaluated for January 5,
the corresponding upstream measurement may therefore need to come from
January 4.

The solver handles this by converting the local lag information defined on each
`InterzoneFlow` into a system-wide time offset for each flow and for each
synchronized zone.

For integer lags, this allows each day's LP to remain independent:

1. Measurements are shifted into a common accounting-time coordinate.
2. The LP is solved for that accounting day.
3. The resulting apportionments are shifted back to measurement-time dates.

Fractional-day lags require additional work because a fractional lag mixes
values from adjacent dates.

---

## 2. Local lag inputs

Each `InterzoneFlow` has two lag inputs:

```python
InterzoneFlow(
    id="A>B",
    from_zone="REACH-A",
    to_zone="REACH-B",
    lag_from_zone=0,
    lag_to_zone=1,
)
```

The fields are:

```python
lag_from_zone: float
lag_to_zone: float
```

Both values must be finite, non-negative numbers of days.

They define the time offset between the flow's measurement-time
coordinate and the time coordinate of source or destination zone.

The important relationships are:

```text
flow measurement offset = from-zone offset + lag_from_zone

flow measurement offset = to-zone offset + lag_to_zone
```

The lag inputs are therefore *local* relationships. Additional processing is
required to convert them into lags that can be used to convert measurement-time
to a global solver time.

---

## 3. Converting local lags to system-wide offsets

`DailyDataManager._set_lag_by_traversal()` traverses the accounting graph and
calculates:

```python
self._flow_lags
self._zone_lags
```

These are system-wide time offsets.

A flow offset of:

```text
3 days
```

means:

> When the solver is evaluating accounting day `D`, use the measurement for
> that flow from `D - 3 days`.

For example:

```text
solver accounting date = January 5
flow system-wide lag    = 3 days
measurement used        = January 2
```

### 3.1 Zones that synchronize time

The current implementation propagates the common time coordinate through:

```text
STREAM
STORAGE
```

zones.

These zones synchronize time because their incident quantities participate in
physical same-time accounting relationships such as flow balance, storage
change, residual-flow calculation, and natural-flow propagation.

The traversal currently does **not** propagate time through bookkeeping,
source, or sink zones such as:

```text
SYSTEM_GAIN_LOSS
USE
IMPORT
DEPLETION
```

For example, two residual flows connected to the same `SYSTEM_GAIN_LOSS` zone
do not need to have the same time coordinate merely because they share that
zone. Each residual belongs to the stream-zone balance where it is calculated.

This avoids creating lag-consistency requirements that are not neccessary for
any flow balance equation.

### 3.2 Propagation equations

Suppose a flow has already been assigned system-wide offset `L`.

At its `to_zone`:

```text
zone offset = L - lag_to_zone
```

At its `from_zone`:

```text
zone offset = L - lag_from_zone
```

When another flow connected to the same synchronized zone is encountered, its
offset is:

```text
connected flow offset
    = zone offset + that flow's local endpoint lag
```

The traversal continues until all flows connected through synchronized zones
have offsets.

### 3.3 Consistency checks

If the traversal reaches the same flow by two different physical paths, both
paths must imply the same system-wide offset.

For example, if one path implies:

```text
A>B offset = 2
```

and another physical path implies:

```text
A>B offset = 3
```

the lag definitions are inconsistent and the solver raises a `ValueError`.

This is intentional. A physically synchronized network cannot be transformed
into independent accounting-time slices if two paths require different time
coordinates for the same quantity.

### 3.4 Normalization

After traversal, the solver finds the minimum flow or synchronized-zone offset
and subtracts it from every offset:

```text
normalized offset = raw offset - minimum raw offset
```

Only relative differences matter. Adding or subtracting the same constant from
all synchronized offsets does not change the physical timing relationships.

---

## 4. Example of system-wide lag calculation

Consider:

```text
 _________
|         |------>  DIVERSION 1  [lag_from=2]
| REACH A |
|_________|------>  DIVERSION 2  [lag_from=1]
     |
     |   [lag_to=1]
     v
 _________
|         |
| REACH B |
|_________|
     |
     |   [lag_to=0]
     v
 _________
|         |
| REACH C |
|_________|

```

If the downstream `REACH-B` / `B>C` time is used as the zero-offset reference,
the resulting offsets are:

```text
REACH-B zone     0
B>C flow meas    0

A>B flow meas    1
REACH-A zone     1

A>2 flow meas    2
A>1 flow meas    3
```

Therefore, on accounting day January 5:

```text
B>C uses January 5
A>B uses January 4
A>2 uses January 3
A>1 uses January 2
```

All of those values can then participate in one January 5 accounting-time
calculation.

---

## 5. Integer-day lags

Integer lags are the simplest case because they are only date shifts.

For an integer flow lag `L`, the measurement used on accounting day `D` is:

```text
measurement date = D - L
```


### 5.1 Daily physical calculations

Observed flows are shifted before residual and natural-flow calculations are
performed.

Specified natural-flow measurements are also read using the flow's system-wide
offset.

Storage change uses the synchronized zone's system-wide offset rather than the
unshifted solver date. Thus, if a storage zone has offset 2, its storage change
for accounting day January 5 is calculated in the January 3 zone-time
coordinate.

This is important because all terms in a physical zone balance must refer to
the same time coordinate.

### 5.2 Solving the daily LP

After the physical inputs have been shifted into accounting-time space, the
solver constructs and solves one LP for that accounting day.

The principal advantage is that an integer lag does not require variables from
multiple dates in the same LP. The data are simply relabeled into a common
time coordinate before the daily model is constructed.

### 5.3 Returning results to measurement time

The current solver collects the LP results in accounting-time space and then
calls:

```python
unlag_apportionments(...)
```

For an integer lag, unlagging does **not** change the solved value. It only
changes the result date:

```text
output date = accounting date - integer lag
```

Therefore integer unlagging is exact and cannot create negative values or
numerical artifacts.

---

## 6. Fractional-day measurement interpolation

`MeasurementCollection.get()` already supports fractional lags.

For:

```text
lag = whole + fraction
```

the current measurement interpolation is:

```text
lagged_value[D]
    = (1 - fraction) * value[D - whole]
    + fraction       * value[D - whole - 1]
```

For a 1.5-day lag:

```text
lagged_value[D]
    = 0.5 * value[D - 1]
    + 0.5 * value[D - 2]
```

For a 1.8-day lag:

```text
lagged_value[D]
    = 0.2 * value[D - 1]
    + 0.8 * value[D - 2]
```

This is a forward interpolation and is well behaved: non-negative inputs
produce non-negative lagged values.

The difficulty is not lagging measurements. The difficulty is reconstructing
the individual transaction components afterward.

---

## 7. Why fractional post-solve unlagging is problematic

The repository currently contains a recurrence-based fractional
`unlag_series()` implementation.

That code mathematically inverts the fractional interpolation after the daily
LPs have been solved.

For example, with a 0.5-day lag:

```text
y[t] = 0.5*x[t] + 0.5*x[t-1]
```

solving for `x[t]` gives:

```text
x[t] = 2*y[t] - x[t-1]
```

This inverse operation does not preserve non-negativity.

A sequence of independently solved lagged transaction components can therefore
produce:

```text
negative reconstructed values
oscillation
boundary-condition artifacts
apparent direction reversals
```

even though all transaction variables in the daily LP were non-negative.

More importantly, some independently solved daily component sequences have no
exact non-negative unlagged decomposition at all.

For that reason, recurrence-based fractional unlagging should be considered a
temporary/experimental implementation rather than the intended long-term
design.

The proposed design avoids fractional inversion entirely.

---

## 8. Proposed fractional-lag formulation

The planned formulation distinguishes between two concepts:

```text
x[t] = unlagged transaction amount
       the actual component of measured flow on real-world date t

y[D] = lagged transaction impact
       the effect of x in accounting-time space
```

The unlagged variable `x[t]` becomes the authoritative transaction variable and
the value eventually reported by the solver.

The lagged impact is derived from `x`; it is never inverted afterward.

### 8.1 Measurement constraints use unlagged variables

Measured-flow decomposition should operate in real measurement time:

```text
sum(transaction x[t]) = measured_flow[t]
```

For example:

```text
senior[t] + junior[t] + slack[t] = measured diversion[t]
```

These variables remain non-negative.

Because the output is already in measurement-time space, no post-solve
unlagging is required.

### 8.2 Transaction limits use unlagged variables

Daily transaction and transaction-group limits should also apply to the
real-time variables:

```text
0 <= x_transaction[t] <= daily_limit[t]
```

A daily diversion or water-right limit normally constrains what happened on the
real-world date, not the interpolated downstream impact.

### 8.3 Lagged impacts are a forward linear transformation

For a 1.8-day lag:

```text
y[D]
    = 0.2*x[D - 1]
    + 0.8*x[D - 2]
```

The lag operator contains only non-negative coefficients.

Therefore non-negative real-time transaction amounts always create non-negative
lagged impacts.

### 8.4 Natural-flow constraints use lagged impacts

Natural-flow availability is evaluated in the common accounting-time
coordinate.

Instead of applying a natural-flow constraint directly to the raw `x`
variables, the constraint should apply to their lagged impacts.

Conceptually:

```text
sum(transaction impacts at accounting day D)
    <= available natural flow[D]
```

For a 1.8-day lag this could be written directly as:

```text
0.2*x1[D-1] + 0.8*x1[D-2]
+ 0.2*x2[D-1] + 0.8*x2[D-2]
    <= available_natural[D]
```

Explicit `y` variables are not necessarily required. The lag coefficients can
usually be substituted directly into the natural-flow constraint.

Keeping `y` as a conceptual quantity is still useful because it clearly
separates measurement-time quantities from accounting-time impacts.

### 8.5 Path continuity and losses

Where transaction path components at different locations represent the same
water at different times, continuity should also be enforced in a compatible
accounting-time coordinate.

Conceptually:

```text
lagged downstream impact
    = remaining_fraction * lagged upstream impact
```

Loss relationships should likewise operate on the appropriately timed impact
rather than forcing same-date equality between quantities whose physical
effects occur at different times.

The exact integration of lag operators with the loss-curve implementation
should be tested as the fractional-lag work proceeds.

---

## 9. Why fractional lags introduce cross-day coupling

Fractional lagging inherently causes one real-time variable to affect more than
one accounting date.

For a 0.5-day lag:

```text
impact[D]
    = 0.5*x[D]
    + 0.5*x[D-1]

impact[D+1]
    = 0.5*x[D+1]
    + 0.5*x[D]
```

Therefore today's `x[D]` affects both today's and tomorrow's accounting-time
constraints.

This means completely independent one-day LPs are not sufficient for
fractional lags if the solver must guarantee future feasibility and preserve
transaction priority.

However, this does **not** require one LP containing the entire simulation
period.

---

## 10. Proposed rolling-window solution

The proposed approach is to use a small rolling LP window.

For each current real-world day:

1. Values from already committed earlier dates are treated as constants.
2. The current day's unlagged transaction variables are optimized.
3. Enough future dates are included as provisional variables to capture all
   constraints affected by the current decision.
4. Transaction priority is applied across that window.
5. Only the current day's variables are committed.
6. The window moves forward one day and is rebuilt.

For example:

```text
solve Jan 1 through Jan 3  -> commit Jan 1
solve Jan 2 through Jan 4  -> commit Jan 2
solve Jan 3 through Jan 5  -> commit Jan 3
...
```

The required window length should be determined from the temporal support of
the lag relationships rather than from the total simulation length.

This keeps the LP size bounded even for long runs.

---

## 11. Protecting future senior transactions

A rolling window must preserve water-right priority across dates.

It would be undesirable for a junior transaction today to consume natural-flow
capacity that a senior transaction tomorrow would otherwise receive.

Therefore priority should take precedence over date within the rolling window.

Conceptually, if priority 1 is senior to priority 2:

```text
Priority 1 - today
Priority 1 - tomorrow
Priority 1 - later dates in the window

Priority 2 - today
Priority 2 - tomorrow
Priority 2 - later dates in the window
```

rather than:

```text
today's senior
today's junior
tomorrow's senior
tomorrow's junior
```

This extends the solver's existing lexicographic approach across the limited set
of dates that are temporally coupled.

A useful interpretation is:

> A junior transaction may be satisfied only to the extent that doing so does
> not reduce the amount available to any higher-priority transaction within the
> future period affected by the junior transaction.

Only the current day's transaction values are final. Future values in the
rolling window are provisional and are re-solved when those dates become
current.

---

## 12. Integer lags as a special case of the future design

The proposed formulation is compatible with integer lags.

For an integer lag of 2:

```text
impact[D] = x[D - 2]
```

There is only one coefficient, so there is no interpolation or deconvolution.

The current integer implementation can therefore remain as an efficient special
case:

```text
shift measurements -> solve one day -> shift result dates back
```

while the rolling-window machinery is used only when fractional lags create
true cross-day mixtures.

Alternatively, the future solver could eventually represent both integer and
fractional lags using the same forward lag-operator abstraction.

---

## 13. Measurement range requirements

Time lags affect the amount of measurement data required by a solve.

### Lookback

If the first requested solve date is January 5 and some physical calculation
requires a 3-day offset, measurements may be needed beginning January 2.

Storage change requires an additional preceding value because:

```text
change[t] = storage[t] - storage[t-1]
```

The required measurement lookback should eventually be calculated automatically
from:

```text
flow offsets
zone offsets
storage-change requirements
```

### Lookahead

The proposed fractional rolling-window solver may also require measurements
after the requested output period.

For example, if January 31 decisions can affect February 1 and February 2
natural-flow constraints, those future dates may need to be included internally
to protect future senior transactions.

The requested output period and the internal data period should therefore be
treated as separate concepts.

---

## 14. Current implementation status

As of the current implementation:

| Feature | Status |
|---|---|
| `lag_from_zone` / `lag_to_zone` inputs | Implemented |
| Finite/non-negative lag validation | Implemented |
| System-wide flow offsets | Implemented |
| System-wide STREAM/STORAGE zone offsets | Implemented |
| Lag consistency checks | Implemented |
| Integer measurement shifting | Implemented |
| Lagged specified natural-flow measurements | Implemented |
| Lagged storage-change measurements | Implemented |
| Fractional measurement interpolation | Implemented |
| Integer result date shifting | Implemented |
| Fractional recurrence-based unlagging | Implemented, but considered provisional |
| Fractional unlagged transaction variables in LP | Proposed |
| Natural-flow constraints on forward lagged impacts | Proposed |
| Rolling-window fractional solve | Proposed |
| Cross-date senior-priority protection | Proposed |
| Automatic measurement lookback/lookahead calculation | Proposed |

One current limitation is that `external_natural_flows` are keyed directly by
the solver date and are not currently transformed using the calculated flow
offset. This should be reviewed as the lag implementation is completed.

---

## 15. Design principles

The lag implementation should follow these principles:

1. **Reported transaction values should remain actual components of measured
   real-world flow.**

2. **Transaction variables should remain non-negative.** Reverse physical
   direction should be represented by the transaction path definition, not by
   numerical artifacts from lag inversion.

3. **Lagging should be a forward operation.** Fractional interpolation with
   non-negative weights is physically and numerically safer than reconstructing
   components by deconvolution.

4. **Physical equations must use a common time coordinate.** Flow balance,
   storage change, natural flow, continuity, and losses should compare
   quantities representing compatible times.

5. **Integer lags should remain inexpensive.** Pure date shifts do not require
   cross-day LP variables.

6. **Fractional lags should couple only the dates they actually affect.** A
   rolling window is preferable to one model covering the full simulation
   period.

7. **Priority must be protected across the lag window.** A junior transaction
   today should not reduce a senior transaction on a future date that shares
   the same lagged natural-flow capacity.

8. **Post-solve fractional inversion should be avoided.** The final output
   should already be in measurement-time space.

---

## 16. Likely implementation sequence

A practical implementation sequence is:

1. Keep the current integer-lag tests passing.
2. Add direct tests for calculated flow and storage change with non-zero zone
   offsets.
3. Formalize a reusable lag operator that returns the dates and weights
   contributing to an accounting-time value.
4. Separate unlagged transaction variables from lagged transaction impacts.
5. Apply measurement and transaction-limit constraints to unlagged variables.
6. Apply natural-flow and path-impact constraints to forward-lagged impacts.
7. Build a small fractional-lag rolling-window prototype.
8. Extend lexicographic scheduling so higher-priority future variables are
   protected before lower-priority current variables.
9. Commit only the current day's real-time variables after each rolling solve.
10. Remove recurrence-based fractional apportionment unlagging once the new
    formulation is validated.
11. Calculate required measurement lookback and lookahead automatically.

The result should preserve the main architectural objective of the solver:
long accounting periods can be processed with bounded LP size while still
representing travel-time effects and transaction priorities correctly.
