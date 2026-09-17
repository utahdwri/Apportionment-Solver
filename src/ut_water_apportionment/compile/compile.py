import builtins
from copy import deepcopy
from dataclasses import dataclass, field
from pprint import pformat
from typing import Callable

from ..models import PathTrxn, SolverInput, TrxnGroup
from .kernel import LPKernel, compile_lp_kernel
from .lp import BlockLP, Constraint, Maximize, Proportional, Variable
from .state import RuntimeStateLayout, build_runtime_state_layout


@dataclass
class PriorityBlock:
    trxns: list[TrxnGroup | PathTrxn]
    priority_order: float


@dataclass
class CompiledPlan:
    state_layout: RuntimeStateLayout
    operations: list[LPKernel]

    source: str = field(init=False)
    executor: Callable = field(init=False)

    def __post_init__(self):
        self.source = self._generate_python(self.operations, self.state_layout)
        self.executor = self._compile_generated_python(self.source)


    def solve(self, measurements=None, *, check_expected_values=False):
        """Execute on the input period using a fresh runtime for each call."""
        from .runtime import solve_plan
        return solve_plan(self, measurements, check_expected_values=check_expected_values)

    def code(self):
        """The actual executable daily routine, including its block definitions."""
        return self.source


    def _generate_python(self, routine, state_layout):
        """Emit the actual daily executor and readable definitions of every LP."""
        lines = [
            '"""Generated block-LP daily routine. state is a prepared numeric array."""',
            'from ut_water_apportionment.compile.lp import (',
            '    Slot, Variable, Constraint, Maximize, Proportional, BlockLP)',
            'from ut_water_apportionment.compile.kernel import compile_lp_kernel',
            '',
            '# Runtime slot layout:',
        ]
        for slot in state_layout.slots.values():
            lines.append(f"# state[{slot.index}] = {slot.name!r}")
        lines.extend(['', 'kernels = ('])
        for operation in routine:
            lines.append('    compile_lp_kernel(' + pformat(operation.model, width=100, sort_dicts=False) + '),')
        lines.extend([')', '', 'def execute_day(state):', '    lp_solves = 0'])
        for index, operation in enumerate(routine):
            lines.append(f"    # Allocate {list(operation.model.updates)!r}")
            lines.append(f"    lp_solves += kernels[{index}].execute(state)")
        lines.extend(['    return lp_solves', ''])
        return '\n'.join(lines)


    def _compile_generated_python(self, source):
        namespace = {}
        exec(builtins.compile(source, '<block-lp-plan>', 'exec'), namespace)
        return namespace['execute_day']




def compile(input: SolverInput) -> CompiledPlan:
    """Take solver input and compile code that will calculate apportionments."""

    state_layout = build_runtime_state_layout(input)
    operations = []

    # Loop through each priority group.
    for block in priority_blocks(state_layout.input):

        # Build a LP system. It should only as complex as needed to solve the
        # apportionments for this block.
        lp_model = build_block_lp(state_layout.input, block, state_layout)

        # Convert the LP system to direct calculation formulas, iteration, or small lp
        # executions.
        operation = try_compile_direct_calculation(lp_model)

        # Fall back to proportional calculation.
        if operation is None:
            operation = try_compile_proportional_calculation(lp_model)

        # Fall back to having to solve a small lp problem.
        if operation is None:
            operation = compile_lp_kernel(lp_model)
        operations.append(operation)


    # Return the compiled set of executables.
    return CompiledPlan(state_layout, operations)




def priority_blocks(input: SolverInput) -> list[PriorityBlock]:
    """Stable priority groups; reference cfs are bound daily, never frozen here.

    Copy the transactions and preserve the existing rule that children with an
    equal/earlier priority follow their parent. Do not filter an inactive first
    day's transactions: later dates can activate them without recompilation.
    """
    groups = {}
    seen = set()

    def visit(transactions, parent_priority=None):
        for transaction in transactions:
            if transaction.id in seen:
                raise ValueError(f"Duplicate transaction ID: {transaction.id!r}")
            seen.add(transaction.id)
            if isinstance(transaction, PathTrxn) and transaction.is_slack:
                continue
            if parent_priority is not None and transaction.priority <= parent_priority:
                transaction.priority = parent_priority + 1e-5
            groups.setdefault(transaction.priority, []).append(transaction)
            if isinstance(transaction, TrxnGroup):
                visit(transaction.children_trxns, transaction.priority)

    visit(deepcopy(input.txns))
    return [PriorityBlock(groups[priority], priority) for priority in sorted(groups)]


def build_block_lp(
        input: SolverInput,
        block: PriorityBlock,
        state_layout: RuntimeStateLayout
    ) -> BlockLP:
    """Build the incremental LP used to allocate one priority block.

    The LP is intentionally smaller than ``Apportioner._build_linear_equations``:
    each variable is an *additional transaction allocation* rather than a
    variable for every path leg.  Path continuity has already been collapsed
    into the runtime flow/natural-flow coefficient slots in ``state_layout``.

    Variables
    ---------
    * Current block transactions: the increments we are actually allocating.
    * Descendants of a group target: feasibility witnesses showing that a new
      parent reservation can eventually be delivered.
    * Descendants of an outstanding senior root reservation: witnesses that
      prevent an intervening transaction from consuming capacity already
      reserved for those descendants.

    Constraints
    -----------
    * Measurement rows: transaction increments may not exceed each remaining
      measured interzone-flow capacity.
    * Natural-flow rows: stream-origin increments may not exceed remaining
      routed natural flow.
    * Reservation rows: child witness increments, less any new parent-group
      increment, must equal the group's existing remaining reservation.

    Only the current block's target variables are committed to runtime state.
    The other variables are LP witnesses and disappear after the solve.
    """

    layout = state_layout

    # ------------------------------------------------------------------
    # 1. Identify the target transactions and any auxiliary witness
    #    transactions that must exist in this block LP.
    # ------------------------------------------------------------------
    ordered_targets = [txn.id for txn in block.trxns]
    targets = set(ordered_targets)
    if not targets or len(targets) != len(ordered_targets):
        raise ValueError("A priority block must contain distinct target transactions")

    for transaction in block.trxns:
        if transaction.id not in layout.transactions:
            raise ValueError(f"Unknown block transaction: {transaction.id!r}")
        if layout.transactions[transaction.id].priority != block.priority_order:
            raise ValueError(f"Priority mismatch for {transaction.id!r}")

    included = set(targets)

    # A group target needs its descendants as feasibility witnesses.  Solving
    # the group does not allocate those descendants; it only proves that the
    # amount reserved by the group can fit through their eventual constraints.
    for name in ordered_targets:
        included.update(layout.descendants(name))

    # Keep an outstanding root reservation's subtree in later block LPs until
    # its last descendant has been processed.  This is what prevents an outside
    # transaction from using capacity that an earlier group already reserved.
    for name, transaction in layout.transactions.items():
        if not isinstance(transaction, TrxnGroup) or name in layout.parents:
            continue
        descendants = layout.descendants(name)
        last_descendant_priority = max(
            layout.transactions[child].priority for child in descendants
        )
        if transaction.priority <= block.priority_order <= last_descendant_priority:
            included.update(descendants)

    # ------------------------------------------------------------------
    # 2. Add LP variables.
    #
    #    A variable is the additional allocation for that transaction in this
    #    block solve.  Transactions from an already-processed priority can be
    #    present only as part of a reservation subtree, so freeze their new
    #    increment at zero.
    # ------------------------------------------------------------------
    variables = {}
    for name, transaction in layout.transactions.items():
        if name not in included:
            continue
        upper = 0.0 if transaction.priority < block.priority_order else layout.limits[name]
        variables[name] = Variable(lower=0.0, upper=upper)

    # Build constraints incrementally, in the same style as the legacy LP
    # builder: add a named row, then attach transaction coefficients to it.
    # Keeping these small helpers local makes the LP construction below read
    # like a list of variables and equations without introducing another public
    # builder abstraction.
    constraint_order = []
    constraint_data = {}

    def add_constraint(name, *, lower=None, upper=None):
        if name in constraint_data:
            return
        constraint_order.append(name)
        constraint_data[name] = {
            'coefficients': {},
            'lower': lower,
            'upper': upper,
        }

    def set_coefficient(constraint_name, variable_name, coefficient):
        constraint_data[constraint_name]['coefficients'][variable_name] = coefficient

    # ------------------------------------------------------------------
    # 3. Add interzone-flow measurement constraints.
    #
    #       sum(flow_coefficient[txn, flow] * increment[txn])
    #           <= remaining_measured[flow]
    #
    #    Path continuity does not need separate equations here: the coefficient
    #    slot already says how much of this flow is used by one unit allocated
    #    at the transaction anchor, including fractional losses.
    # ------------------------------------------------------------------
    for name in variables:
        transaction = layout.transactions[name]
        if not isinstance(transaction, PathTrxn):
            continue
        for item in layout.schedule.ordered_paths[name]:
            constraint_name = f"measurement[{item.flow_id!r}]"
            add_constraint(
                constraint_name,
                upper=layout.measurements[item.flow_id],
            )
            set_coefficient(
                constraint_name,
                name,
                layout.flow_coefficients[name, item.flow_id][0],
            )

    # ------------------------------------------------------------------
    # 4. Add natural-flow constraints.
    #
    #       sum(nf_coefficient[source, zone] * increment[txn])
    #           <= remaining_nf[zone]
    #
    #    Only transactions with a natural-flow source participate.  The routing
    #    coefficients are runtime slots because they can vary with daily losses
    #    and natural-flow routing.
    # ------------------------------------------------------------------
    for name in variables:
        transaction = layout.transactions[name]
        if not isinstance(transaction, PathTrxn):
            continue
        source = layout.schedule.get_nf_zone_id(transaction)
        if source is None:
            continue
        for zone in layout.natural_flow:
            constraint_name = f"natural_flow[{zone!r}]"
            add_constraint(
                constraint_name,
                upper=layout.natural_flow[zone],
            )
            set_coefficient(
                constraint_name,
                name,
                layout.nf_coefficients[source, zone][0],
            )

    # ------------------------------------------------------------------
    # 5. Add group-reservation equalities.
    #
    #       sum(child increments) - group increment
    #           = remaining_group[group]
    #
    #    For an already-created reservation the group variable is frozen at
    #    zero, so its children must still be able to account for the remaining
    #    reserved amount.  When the group itself is the target, its increment
    #    creates a new reservation that its descendant witnesses must support.
    # ------------------------------------------------------------------
    for name in variables:
        transaction = layout.transactions[name]
        if not isinstance(transaction, TrxnGroup):
            continue
        constraint_name = f"reservation[{name!r}]"
        add_constraint(
            constraint_name,
            lower=layout.groups[name],
            upper=layout.groups[name],
        )
        for child in transaction.children_trxns:
            set_coefficient(constraint_name, child.id, 1.0)
        set_coefficient(constraint_name, name, -1.0)

    constraints = [
        Constraint(
            name,
            constraint_data[name]['coefficients'],
            lower=constraint_data[name]['lower'],
            upper=constraint_data[name]['upper'],
        )
        for name in constraint_order
    ]

    # ------------------------------------------------------------------
    # 6. Define the allocation rule for the target variables.
    # ------------------------------------------------------------------
    if len(ordered_targets) == 1:
        rule = Maximize({ordered_targets[0]: 1.0})
    else:
        rule = Proportional({
            name: layout.reference_cfs[name]
            for name in ordered_targets
        })

    # ------------------------------------------------------------------
    # 7. Define what gets committed after the LP finds the target increments.
    #
    #    These are state updates, not LP constraints.  Witness variables never
    #    appear here, so their temporary LP values are discarded.
    # ------------------------------------------------------------------
    updates = {}
    for name in ordered_targets:
        transaction = layout.transactions[name]

        # Every committed allocation increases its reported allocation and
        # consumes the transaction's remaining daily/call/cumulative limit.
        effects = {
            layout.allocated[name]: 1.0,
            layout.limits[name]: -1.0,
        }

        # Allocating a child discharges that much of its parent's reservation.
        if name in layout.parents:
            effects[layout.groups[layout.parents[name]]] = -1.0

        if isinstance(transaction, TrxnGroup):
            # Allocating a group creates capacity reserved for its children.
            effects[layout.groups[name]] = 1.0
        else:
            # Allocating a path transaction consumes measured-flow and natural-
            # flow residuals using the negative counterpart of each coefficient.
            for item in layout.schedule.ordered_paths[name]:
                effects[layout.measurements[item.flow_id]] = (
                    layout.flow_coefficients[name, item.flow_id][1]
                )
            source = layout.schedule.get_nf_zone_id(transaction)
            if source is not None:
                for zone in layout.natural_flow:
                    effects[layout.natural_flow[zone]] = (
                        layout.nf_coefficients[source, zone][1]
                    )

        updates[name] = effects

    return BlockLP(variables, constraints, rule, updates)


def try_compile_direct_calculation(lp_model):
    """Reserved for the next stage; every block currently uses an LP kernel."""
    return None


def try_compile_proportional_calculation(lp_model):
    """Reserved for a future analytical water-filling implementation."""
    return None

