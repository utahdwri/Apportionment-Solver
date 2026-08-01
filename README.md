# Apportionment-Solver
Calculate after-the-fact water right apportionments based on flow measurements and water rights linked to the so-called WR Network

![test status](https://github.com/utahdwri/Apportionment-Solver/actions/workflows/python-app.yml/badge.svg)

[Apportionment Solver Documentation](https://github.com/utahdwri/Apportionment-Solver/blob/main/General%20Apportionment%20Solver%20Documentation.pdf)



# The Water Accounting Problem

Priority distribution of water rights can be complex and opaque, yet transparency is crucial for ensuring proper management of this vital public resource. In the financial world, bookkeeping rules are followed to give a name to every dollar recieved and spent and to ensure these transactions can be audited. Similarly, prudent management of water requires similar accounting. That is what this solver aims to provide.

For this water accounting, we have a flow network (the Accounting Graph) and know the total flows measurements. We also know the set of authorized Transactions. The goal is to solve for the flows of each Transaction Component on an Accounting Graph given the total flow measurements. Put simply, we aim to name the water that we measured according to the individual water rights (and other transactions).


# Definitions

- Accounting Graph: A network graph composed of Zones and Interzone-Flows (which move water between zones).

- Zone: A node on the Accounting Graph. There are STREAM zones, USE zones, & STORAGE zones.

- Interzone Flow: an arc on the Accounting Graph with a measured flow. The measured, physical flow through an Interzone-Flow must equal the sum of the Transaction Components that pass through it.

- Transaction: A Transaction traces a specific path from a source Zone to a destination Zone, traversing one or more Interzone-Flows. These represent authorized water rights (always originate at STREAM zones) and storage deliveries (always originate at STORAGE zones) or unauthorized movement through the Accounting Graph.

- Transaction Component: A Transaction Component refers to the portion of a Transaction traversing a single, specific Interzone-Flow. It is possible for Transaction Components to mathematically flow in the opposite direction of the physical Interzone-Flow (e.g., an exchange). Ultimately, we want to solve exactly how much water was apportioned to each Transaction Component.

- Transaction Schedule: A prioritized list of Transactions from the most senior to the most junior.



# The Distribution and Accounting Game

To understand the system, imagine a game with two players:

1. Player 1 is the distributor (The Forward Problem): This person decides how much water goes to each transaction on the schedule in strict accordance with the rules above. After all these deliveries have been determined, the net physical flows are calculated by summing all the apportionments traversing each route. The final physical flows are then handed to Player 2.

2. Player 2 is the accountant (The Inverse Problem): This person only receives the final physical net flows, the list of transaction priorities, and the rulebook. Their job is to mathematically back-calculate the exact apportionments Player 1 made to each transaction. (This is an entirely impartial task depending only on the net flows, the transaction schedule, and the rules).


# Forward Water Distribution Rules

For forward water distribution, we imagine a Water Distributor that has perfect knowledge of the available supply and is empowered to precisely distribute that supply in accordance with the following rules. The following rules define a procedural model of how water should be distributed.

1. Well-Defined Paths: Each transaction in the schedule must have a well-defined path on the accounting graph. The path is well-defined if it starts at one source zone, ends at one destination zone, and follows only one approved route in between. (If a water right allows multiple routes, each route must be a separate transaction, or the graph must be reworked to flatten the routes into a single path).

2. Upper Limits: A transaction in the schedule may have a maximum allowed capacity (an upper limit). The Water Distributor cannot assign more water to a transaction than this limit. (This limit may represent a water right CFS limit, a duty limit based on the number of acres in use, an annual volume limit, etc.)

3. Strict Priority Apportionment: The transaction schedule is ordered by priority. The Water Distributor must start at the most senior transaction in the schedule and completely satisfy it before moving to the next. The Water Distributor must assign the maximum possible value to the transaction. If the Water Distributor assigns a value less than the upper limit, it must be for one of two reasons:

    - No remaining demand at destination: The physical delivery gauge is maxed out. If this happens, the Water Distributor must assign a value of zero to all subsequent (more junior) transactions going to the same destination.

    - No remaining water at the source: The divertible natural flow or available storage is exhausted. If this happens, the Water Distributor must assign a value of zero to all subsequent (more junior) transactions pulling from the same source.

4. Equal Priority Proportions: The schedule may contain groups of transactions that share the exact same priority order. When the Water Distributor encounters such a group, they must assign each transaction a value equal to the same proportion (e.g., 50%) of their respective maximum limits. the Water Distributor will increase this proportion equally until one of the transactions hits a bottleneck (its destination fills up, or the source runs out). The Water Distributor may then continue proportionally apportioning water to the remaining un-bottlenecked transactions in the group until they are also maximized or blocked.



# Inverse Water Accounting

Forward Water Distribution is a conceptual model rather than an operational process because, in a natural system, diversions and storage impoundments obscure the true available supply. To manage and account for these transactions in the real world, we must solve the inverse problem.

For inverse water accounting, we imagine an individual who is unaware of the decisions made by the Water Distributor, but does know the total flows that resulted from the forward distribution. The goal is to recover the unique transaction apportionments that reproduce observed physical flows while remaining consistent with the Forward Distribution Rules.

In unique cases involving transactions going in opposite directions along the same interzone flow, there may be multiple ways the Water Distributor in the "Forward Distribution" could have ended up with the net flow rate; in other words, the inverse problem may not have a unique solution. By convention, we establish the following rules to ensure uniqueness:

- If there are multiple solutions, we prefer the one that minimizes the sum of Transaction Components going either direction along an interzone flow. This preference discourages unnecessary counter-flow and favors the minimum accounting explanation consistent with observations.

It is possible that water may have been released from storage or imported into a natural stream but never delivered to a destination. These transactions are called spills and are last on the priority schedule (we'd rather allocate the water to any other possible transaction). Spilled water becomes available to water rights in priority order. Thus, accounting for spills requires a second pass.

Measurement error exist in the real world. However for after-the-fact accounting purposes the measured value is assumed to adaquately represent the actual total water delivery. Ensuring this is the case is a seperate data management problem.

## Purpose of the Solver

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
