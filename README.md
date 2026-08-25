# Utah Water Right Apportionment Solver

![test status](https://github.com/utahdwri/Apportionment-Solver/actions/workflows/python-app.yml/badge.svg)

[Apportionment Solver Documentation](https://github.com/utahdwri/Apportionment-Solver/blob/main/General%20Apportionment%20Solver%20Documentation.pdf)

## 1. After-the-fact distribution accounting
In Utah, water is distributed by priority in accordance with water rights. Accounting for this distribution is done after-the-fact because, in all but the most trivial cases, we do not know how much water is available to be diverted until we know how much water was actually diverted. Water users generally have a portfolio comprising many water rights and perhaps contracts for storage water. So measurements is not enough. Distribution accounting subdivides the water actually diverted into the components representing water diverted under each authorization. It colors or names the water that was moved through the system.

The primary purpose of this accounting is to assess the degree that water is being distributed in accordance with priority. These results may also provide insights into the historical actual supply that a water right has recieved; this may be much lower than the paper entitlement in river systems with variable supplies.

## 2. Solver Input
This tool performs after-the-fact accounting, calculating apportionments by water right or other authorizations for measured diversions. This accounting depends on the data described in the following subsections.
- A stream network graph defining the known flows between zones
- Measurements of diversions, imports, streamflow, and storage volumes
- Transaction schedule that defines the priority ordering and limits of the water authorizations


### 2.1. Stream Network Graph (Accounting Graph)

- The `Accounting Graph` is a network graph composed of `Zones` (or nodes) and `Interzone-Flows` (or arcs which move water between zones).
- There are different types of Zones, such as STREAM zones, USE zones, & STORAGE zones.

...

### 2.2. Measurements
- The flow along all Interzone-Flows on the Accounting Graph are know, either because they are measured or because they can be calculated from a flow balance equation (INFLOW = OUTFLOW + ΔSTORAGE).
- Storage measurements may be provided for STORAGE zones. These storage values represent end-of-day storage, meaning the change in storage for day t is ΔS(t) = S(t) - S(t-1)

### 2.3. Transaction Schedule

- Transaction: A Transaction traces a specific path from a source Zone to a destination Zone, traversing one or more Interzone-Flows. These represent authorized water rights (always originate at STREAM zones) and storage deliveries (always originate at STORAGE zones) or unauthorized movement through the Accounting Graph.

- Transaction Component: A Transaction Component refers to the portion of a Transaction traversing a single, specific Interzone-Flow. It is possible for Transaction Components to mathematically flow in the opposite direction of the physical Interzone-Flow (e.g., an exchange). Ultimately, we want to solve exactly how much water was apportioned to each Transaction Component.

- The measured, physical flow through an Interzone-Flow must equal the sum of the Transaction Components that pass through it.

- Transaction Schedule: A prioritized list of Transactions from the most senior to the most junior.

...

### 2.4. Specified Time Period

The solver runs on a daily interval for a specified time period. Sub-daily intervals are not supported, nor are intervals longer than a day.

### 2.5. A Note on Units

The solver does not prescribe what units should be used for measurements or transaction limits, but whatever is used must be consistent. For example, if you provide daily diversion measuements in cfs, you'd need to also provide storage measurements in cfs-days, storage limits in cfs-days, and transaction limits in cfs-days. Or everything in acre-feet. Or everything in whatever, so long as it's consistent across the provided input.

### 2.6. Example Input

Example:
```
from ut_water_apportionment import (
    SolverInput, AccountingGraph, Zone, ZoneTypes, InterzoneFlow, Trxn, TrxnPathItem
)

graph = AccountingGraph(
    zones=[
        Zone(id="RIVER", type=ZoneTypes.STREAM),
        Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
        Zone(id="USER", type=ZoneTypes.USE),
    ],
    interzone_flows=[
        InterzoneFlow(
            id="RIVER>USER",
            from_zone="RIVER",
            to_zone="USER",
            flow_measurements=[FlowMeasurement(measurement_id="1")]),
        InterzoneFlow(
            id="SYS>RIVER",
            from_zone="SYS",
            to_zone="RIVER",
            flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE,
            bidirectional=True
        ),
    ]
)

measurements = {
    "1": [12]
}

schedule = [
    Trxn(
        id='TRXN_1',
        priority=1,
        upper_limit=3,
        path=[TrxnPathItem(flow_id='RIVER>USER')]),
    Trxn(
        id='TRXN_2',
        priority=2,
        upper_limit=6,
        path=[TrxnPathItem(flow_id='RIVER>USER')]
    ),
    Trxn(
        id='TRXN_3',
        priority=3,
        upper_limit=12,
        path=[TrxnPathItem(flow_id='RIVER>USER')]
    ),
    Trxn(
        id='TRXN_4',
        priority=4,
        upper_limit=4,
        path=[TrxnPathItem(flow_id='RIVER>USER')]
    )
]

input = SolverInput(
    beg_date='2000-01-01',
    end_date='2000-01-01',
    accounting_graph=graph,
    measurement_beg_date='2000-01-01',
    measurement_end_date='2000-01-01',
    measurements=measurements,
    txns=schedule
)

```

## 3. Solver Output

...





## 4. The Apportionment Process - The Water Accounting Problem

Priority distribution of water rights can be complex and opaque, yet transparency is crucial for ensuring proper management of this vital public resource. In the financial world, bookkeeping rules are followed to give a name to every dollar recieved and spent and to ensure these transactions can be audited. Similarly, prudent management of water requires similar accounting. That is exactly what this solver aims to provide.

For this water accounting, we have a flow network (the Accounting Graph) and know the total flows measurements. We also know the set of authorized Transactions. The goal is to solve for the flows of each Transaction Component on an Accounting Graph given the total flow measurements. Put simply, we aim to name the water that we measured according to the individual water rights and other transactions.

To understand the system, imagine a game with two players:

1. Player 1 is the distributor (The Forward Problem): This person decides how much water goes to each transaction on the schedule in strict accordance with the rules above. After all these deliveries have been determined, the net physical flows are calculated by summing all the apportionments traversing each route. The final physical flows are then handed to Player 2.

2. Player 2 is the accountant (The Inverse Problem): This person only receives the final physical net flows, the list of transaction priorities, and the rulebook. Their job is to mathematically back-calculate the exact apportionments Player 1 made to each transaction. (This is an entirely impartial task depending only on the net flows, the transaction schedule, and the rules).


### Forward Water Distribution Rules

For forward water distribution, we imagine a Water Distributor that has perfect knowledge of the available supply and is empowered to precisely distribute that supply in accordance with the following rules. The following rules define a procedural model of how water should be distributed.

1. Well-Defined Paths: Each transaction in the schedule must have a well-defined path on the accounting graph. The path is well-defined if it starts at one source zone, ends at one destination zone, and follows only one approved route in between. (If a water right allows multiple routes, each route must be a separate transaction, or the graph must be reworked to flatten the routes into a single path).

2. Upper Limits: A transaction in the schedule may have a maximum allowed capacity (an upper limit). The Water Distributor cannot assign more water to a transaction than this limit. (This limit may represent a water right CFS limit, a duty limit based on the number of acres in use, an annual volume limit, etc.)

3. Strict Priority Apportionment: The transaction schedule is ordered by priority. The Water Distributor must start at the most senior transaction in the schedule and completely satisfy it before moving to the next. The Water Distributor must assign the maximum possible value to the transaction. If the Water Distributor assigns a value less than the upper limit, it must be for one of two reasons:

    - No remaining demand at destination: The physical delivery gauge is maxed out. If this happens, the Water Distributor must assign a value of zero to all subsequent (more junior) transactions going to the same destination.

    - No remaining water at the source: The divertible natural flow or available storage is exhausted. If this happens, the Water Distributor must assign a value of zero to all subsequent (more junior) transactions pulling from the same source.

4. Equal Priority Proportions: The schedule may contain groups of transactions that share the exact same priority order. When the Water Distributor encounters such a group, they must assign each transaction a value equal to the same proportion (e.g., 50%) of their respective maximum limits. the Water Distributor will increase this proportion equally until one of the transactions hits a bottleneck (its destination fills up, or the source runs out). The Water Distributor may then continue proportionally apportioning water to the remaining un-bottlenecked transactions in the group until they are also maximized or blocked.



### Inverse Water Accounting

Forward Water Distribution is a conceptual model rather than an operational process because, in a natural system, diversions and storage impoundments obscure the true available supply. To manage and account for these transactions in the real world, we must solve the inverse problem.

For inverse water accounting, we imagine an individual who is unaware of the decisions made by the Water Distributor, but does know the total flows that resulted from the forward distribution. The goal is to recover the unique transaction apportionments that reproduce observed physical flows while remaining consistent with the Forward Distribution Rules.

In unique cases involving transactions going in opposite directions along the same interzone flow, there may be multiple ways the Water Distributor in the "Forward Distribution" could have ended up with the net flow rate; in other words, the inverse problem may not have a unique solution. By convention, we establish the following rules to ensure uniqueness:

- If there are multiple solutions, we prefer the one that minimizes the sum of Transaction Components going either direction along an interzone flow. This preference discourages unnecessary counter-flow and favors the minimum accounting explanation consistent with observations.

It is possible that water may have been released from storage or imported into a natural stream but never delivered to a destination. These transactions are called spills and are last on the priority schedule (we'd rather allocate the water to any other possible transaction). Spilled water becomes available to water rights in priority order. Thus, accounting for spills requires a second pass.

Measurement error exist in the real world. However for after-the-fact accounting purposes the measured value is assumed to adaquately represent the actual total water delivery. Ensuring this is the case is a seperate data management problem.

### Solver Objective

The purpose of the software Solver is to do the work of Player 2. It calculates the exact apportionments for every transaction in the schedule given only the measured net flows, the transaction priority schedule, the accounting graph topology, and the system rules.

Objective:
Recover transaction allocations that satisfy all constraints.

Optimization priorities (highest to lowest):

1. Lexicographically maximize transaction allocations
   according to Transaction Schedule priority.

2. Among equivalent priority allocations,
   minimize total absolute Transaction Component flow.

subject to:
- Observed interzone flows
- Transaction limits


## 5. Other Notes


### Types of water right limits:

1. Daily diversion limit - This is generaly expressed as a constant cfs over a specified period of use, but it may also vary over time.

2. Annual diversion limit - The cummulative diversion cannot exceed this value.

3. Duty limit - Does this really need to be different from the daily diversion limit? Yes, when we have an acres measurement: if they only used some of their acres, the juniormost right still in use will recieve a lesser duty according to the actual use.

3. Annual depletion limit


### Logging

The solver uses the standard logging module for warnings and other messages. To print these messages to the console you could use something like:

```
import logging
import sys
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    force=True,
)
```

