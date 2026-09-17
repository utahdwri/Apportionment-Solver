import builtins
from copy import deepcopy
from dataclasses import dataclass, field
from pprint import pformat
from math import isfinite
from typing import Callable

from ..models import PathTrxn, SolverInput, TrxnGroup, ZoneTypes
from .kernel import (
    DirectCalculationKernel, LPKernel, ProportionalCalculationKernel,
    compile_direct_kernel, compile_lp_kernel, compile_proportional_kernel,
)
from .lp import BlockLP, Constraint, Maximize, Proportional, Slot, Variable
from .state import RuntimeStateLayout, build_runtime_state_layout


@dataclass
class PriorityBlock:
    trxns: list[TrxnGroup | PathTrxn]
    priority_order: float


@dataclass
class CompiledPlan:
    state_layout: RuntimeStateLayout
    operations: list[DirectCalculationKernel | ProportionalCalculationKernel | LPKernel]
    replay_operations: list[DirectCalculationKernel | ProportionalCalculationKernel | LPKernel]

    source: str = field(init=False)
    executor: Callable = field(init=False)

    def __post_init__(self):
        self.source = self._generate_python(
            self.operations, self.replay_operations, self.state_layout
        )
        self.executor, self.replay_executor = self._compile_generated_python(self.source)


    def solve(self, measurements=None, *, check_expected_values=False):
        """Execute on the input period using a fresh runtime for each call."""
        from .runtime import solve_plan
        return solve_plan(self, measurements, check_expected_values=check_expected_values)

    def code(self):
        """The actual executable daily routine, including its block definitions."""
        return self.source


    def _generate_python(self, routine, replay_routine, state_layout):
        """Emit the actual daily executor and readable definitions of every LP."""
        lines = [
            '"""Generated block-LP daily routine. state is a prepared numeric array."""',
            'from ut_water_apportionment.compile.lp import (',
            '    Slot, Variable, Constraint, Maximize, Proportional, BlockLP)',
            'from ut_water_apportionment.compile.kernel import (',
            '    compile_direct_kernel, compile_proportional_kernel, compile_lp_kernel)',
            '',
            '# Runtime slot layout:',
        ]
        for slot in state_layout.slots.values():
            lines.append(f"# state[{slot.index}] = {slot.name!r}")
        def emit_operation(operation):
            if isinstance(operation, DirectCalculationKernel):
                compiler = 'compile_direct_kernel'
            elif isinstance(operation, ProportionalCalculationKernel):
                compiler = 'compile_proportional_kernel'
            else:
                compiler = 'compile_lp_kernel'
            return (
                f'    {compiler}('
                + pformat(operation.model, width=100, sort_dicts=False)
                + '),'
            )

        lines.extend(['', 'kernels = ('])
        for operation in routine:
            lines.append(emit_operation(operation))
        lines.extend([')', '', 'replay_kernels = ('])
        for operation in replay_routine:
            lines.append(emit_operation(operation))
        lines.extend([')', '', 'def execute_day(state):', '    lp_solves = 0'])
        for index, operation in enumerate(routine):
            lines.append(f"    # Pass 1 allocate {list(operation.model.updates)!r}")
            lines.append(f"    lp_solves += kernels[{index}].execute(state)")
        lines.extend(['    return lp_solves', '', 'def execute_replay(state):', '    lp_solves = 0'])
        for index, operation in enumerate(replay_routine):
            lines.append(f"    # Replay allocate {list(operation.model.updates)!r}")
            lines.append(f"    lp_solves += replay_kernels[{index}].execute(state)")
        lines.extend(['    return lp_solves', ''])
        return '\n'.join(lines)


    def _compile_generated_python(self, source):
        namespace = {}
        exec(builtins.compile(source, '<block-lp-plan>', 'exec'), namespace)
        return namespace['execute_day'], namespace['execute_replay']




def compile(input: SolverInput) -> CompiledPlan:
    """Take solver input and compile code that will calculate apportionments."""

    state_layout = build_runtime_state_layout(input)
    operations = []
    replay_operations = []

    # Loop through each priority group. Pass 1 deliberately excludes future
    # counterflow witnesses. Replay can include them after Pass 1 has established
    # which storage deliveries/counterflows are actually needed.
    for block in priority_blocks(state_layout.input):

        lp_model = build_block_lp(
            state_layout.input, block, state_layout, replay=False
        )

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

        replay_model = build_block_lp(
            state_layout.input, block, state_layout, replay=True
        )
        replay_operation = try_compile_direct_calculation(replay_model)
        if replay_operation is None:
            replay_operation = try_compile_proportional_calculation(replay_model)
        if replay_operation is None:
            replay_operation = compile_lp_kernel(replay_model)
        replay_operations.append(replay_operation)

    return CompiledPlan(state_layout, operations, replay_operations)




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
        state_layout: RuntimeStateLayout,
        *,
        replay: bool = False,
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
    * Measurement rows: transaction increments may not exceed the remaining
      measured interzone-flow capacity in the transaction's direction.
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

    # Replay only: identify the reservoir-edge net-flow rows that need to be
    # active.  Transactions entering storage may use *real* future opposite-
    # direction transactions as feasibility witnesses. Transactions leaving
    # storage instead use a narrowly-scoped reporting-slack witness, described
    # below, so replay can represent a required storage delivery without making
    # arbitrary counterflow variables available in Pass 1.
    locked_flow_directions: dict[str, set[int]] = {}
    storage_outflow_directions: dict[str, set[int]] = {}
    if replay:
        for name in ordered_targets:
            transaction = layout.transactions[name]
            if not isinstance(transaction, PathTrxn):
                continue

            from_zone = layout.schedule.get_from_zone(transaction)
            to_zone = layout.schedule.get_to_zone(transaction)
            path = layout.schedule.ordered_paths[name]
            if not path:
                continue

            if to_zone is not None and to_zone.type not in {
                ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS
            }:
                item = path[-1]
                flow = layout.graph.get_flow_by_id(item.flow_id)
                if flow.bidirectional:
                    locked_flow_directions.setdefault(item.flow_id, set()).add(
                        1 if item.factor > 0 else -1
                    )

            if from_zone is not None and from_zone.type not in {
                ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS
            }:
                item = path[0]
                flow = layout.graph.get_flow_by_id(item.flow_id)
                if flow.bidirectional:
                    storage_outflow_directions.setdefault(item.flow_id, set()).add(
                        1 if item.factor > 0 else -1
                    )

        # For an inflow target, only real accounting transactions in the
        # opposite direction are useful witnesses.  This is the structural
        # replacement for the legacy temporary minimization of reverse/slack
        # variables: unrelated counterflow variables simply do not exist in
        # the block LP.
        for flow_id, target_directions in locked_flow_directions.items():
            for name, transaction in layout.transactions.items():
                if not isinstance(transaction, PathTrxn):
                    continue
                if transaction.priority < block.priority_order:
                    continue
                for item in layout.schedule.ordered_paths[name]:
                    if item.flow_id == flow_id:
                        direction = 1 if item.factor > 0 else -1
                        if any(direction == -target for target in target_directions):
                            included.add(name)
                        break

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

    # Replay-only counterflow reporting slack for transactions *originating* in
    # storage.  These variables are deliberately absent from Pass 1.  Their
    # runtime upper bound is zero when the measured reservoir exchange already
    # contains flow in the target direction, preventing replay from enlarging a
    # real release merely to consume downstream slack.  When no such measured
    # flow exists, the witness can balance a senior storage delivery (for
    # example a zero-net reservoir with a measured downstream release).
    replay_slack_variables: dict[tuple[str, int], str] = {}
    if replay:
        for flow_id, directions in storage_outflow_directions.items():
            for direction in directions:
                key = (flow_id, direction)
                limit = layout.replay_counterflow_slack_limits.get(key)
                if limit is None:
                    continue
                variable_name = f"__counterflow_slack__[{flow_id!r},{direction}]"
                replay_slack_variables[key] = variable_name
                variables[variable_name] = Variable(lower=0.0, upper=limit)

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
    # Pass 1 uses nonnegative gross capacity in the currently measured
    # direction and has no future counterflow witnesses. Replay uses signed net
    # residuals on target endpoint flows, with real future opposite-direction
    # transactions available as witnesses.
    # ------------------------------------------------------------------
    if not replay:
        for name in variables:
            transaction = layout.transactions[name]
            if not isinstance(transaction, PathTrxn):
                continue
            for item in layout.schedule.ordered_paths[name]:
                if item.factor > 0:
                    direction = "forward"
                    capacity = layout.measurement_forward_remaining[item.flow_id]
                else:
                    direction = "reverse"
                    if item.flow_id not in layout.measurement_reverse_remaining:
                        # A negative accounting component on a one-way physical
                        # reach is balanced by forward reporting slack. It does
                        # not consume a separate reverse gross-flow capacity.
                        continue
                    capacity = layout.measurement_reverse_remaining[item.flow_id]
                constraint_name = f"measurement_{direction}[{item.flow_id!r}]"
                add_constraint(constraint_name, upper=capacity)
                signed, negated = layout.flow_coefficients[name, item.flow_id]
                set_coefficient(
                    constraint_name, name, signed if item.factor > 0 else negated
                )
    else:
        flow_rows = {}
        for name in variables:
            if name not in layout.transactions:
                continue
            transaction = layout.transactions[name]
            if not isinstance(transaction, PathTrxn):
                continue
            for item in layout.schedule.ordered_paths[name]:
                flow = layout.graph.get_flow_by_id(item.flow_id)
                if flow.bidirectional:
                    inflow_directions = locked_flow_directions.get(item.flow_id)
                    outflow_directions = storage_outflow_directions.get(item.flow_id)
                    directions = inflow_directions or outflow_directions
                    if not directions:
                        continue
                else:
                    directions = {1}
                row = flow_rows.setdefault(item.flow_id, {
                    'coefficients': {}, 'directions': directions,
                })
                row['coefficients'][name] = layout.flow_coefficients[
                    name, item.flow_id
                ][0]

        # Add the narrowly-scoped storage-source reporting slack to its own
        # reservoir-edge row.  Its sign is opposite the target direction.
        for (flow_id, direction), variable_name in replay_slack_variables.items():
            row = flow_rows.setdefault(flow_id, {
                'coefficients': {}, 'directions': {direction},
            })
            row['coefficients'][variable_name] = float(-direction)

        for flow_id, row in flow_rows.items():
            directions = row['directions']
            bound = layout.measurement_available[flow_id]
            constraint_name = f"measurement_net[{flow_id!r}]"
            if directions == {1}:
                add_constraint(constraint_name, upper=bound)
            elif directions == {-1}:
                add_constraint(constraint_name, lower=bound)
            else:
                add_constraint(constraint_name, lower=bound, upper=bound)
            for name, coefficient in row['coefficients'].items():
                set_coefficient(constraint_name, name, coefficient)

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
        if name not in layout.transactions:
            continue
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
    # 5. Add zone-account capacity constraints.
    #
    #    Withdrawals use the transaction's anchor amount directly:
    #
    #        sum(increment[txn]) <= beginning_balance - floor
    #
    #    Deposits use the delivered amount at the end of the path:
    #
    #        sum(delivery_factor[txn] * increment[txn])
    #            <= ceiling - beginning_balance
    #
    #    The bounds are initialized once at the start of each day.  This
    #    deliberately matches the legacy LP: a deposit made today does not
    #    create additional withdrawal capacity until the following day.
    # ------------------------------------------------------------------
    for name in variables:
        if name not in layout.transactions:
            continue
        transaction = layout.transactions[name]
        if not isinstance(transaction, PathTrxn):
            continue

        if transaction.from_account is not None:
            from_zone = layout.schedule.get_from_zone(transaction)
            if from_zone is None:
                raise ValueError(f"Cannot resolve source zone for {name!r}")
            key = (from_zone.id, transaction.from_account)
            if key in layout.account_out_remaining:
                constraint_name = f"account_out[{key!r}]"
                add_constraint(
                    constraint_name,
                    upper=layout.account_out_remaining[key],
                )
                set_coefficient(constraint_name, name, 1.0)

        if transaction.to_account is not None:
            to_zone = layout.schedule.get_to_zone(transaction)
            if to_zone is None:
                raise ValueError(f"Cannot resolve destination zone for {name!r}")
            key = (to_zone.id, transaction.to_account)
            if key in layout.account_in_remaining:
                constraint_name = f"account_in[{key!r}]"
                add_constraint(
                    constraint_name,
                    upper=layout.account_in_remaining[key],
                )
                set_coefficient(
                    constraint_name,
                    name,
                    layout.to_account_coefficients[name][0],
                )

    # ------------------------------------------------------------------
    # 6. Add group-reservation equalities.
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
        if name not in layout.transactions:
            continue
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
    # 7. Define the allocation rule for the target variables.
    # ------------------------------------------------------------------
    if len(ordered_targets) == 1:
        rule = Maximize({ordered_targets[0]: 1.0})
    else:
        rule = Proportional({
            name: layout.reference_cfs[name]
            for name in ordered_targets
        })

    # ------------------------------------------------------------------
    # 8. Define what gets committed after the LP finds the target increments.
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
                signed, negated = layout.flow_coefficients[name, item.flow_id]

                # Signed physical residual: residual -= signed_flow.
                effects[layout.measurements[item.flow_id]] = negated
                effects[layout.measurement_available[item.flow_id]] = negated

                # Directional Pass-1 capacity: capacity -= abs(signed_flow).
                if item.factor > 0:
                    effects[layout.measurement_forward_remaining[item.flow_id]] = negated
                elif item.flow_id in layout.measurement_reverse_remaining:
                    effects[layout.measurement_reverse_remaining[item.flow_id]] = signed

            if transaction.from_account is not None:
                from_zone = layout.schedule.get_from_zone(transaction)
                if from_zone is None:
                    raise ValueError(f"Cannot resolve source zone for {name!r}")
                key = (from_zone.id, transaction.from_account)
                if key in layout.account_out_remaining:
                    effects[layout.account_out_remaining[key]] = -1.0

            if transaction.to_account is not None:
                to_zone = layout.schedule.get_to_zone(transaction)
                if to_zone is None:
                    raise ValueError(f"Cannot resolve destination zone for {name!r}")
                key = (to_zone.id, transaction.to_account)
                if key in layout.account_in_remaining:
                    effects[layout.account_in_remaining[key]] = (
                        layout.to_account_coefficients[name][1]
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
    """Compile an exact one-variable maximization as a direct calculation.

    ``build_block_lp`` has already collapsed path-leg continuity into runtime
    coefficients.  Therefore a block containing only its target variable is a
    scalar LP: all variable/row bounds intersect to form one feasible interval,
    and the maximizing endpoint is the exact LP solution.

    Return ``None`` whenever auxiliary reservation/counterflow witnesses or a
    proportional allocation rule make the block genuinely multi-variable.
    Those cases fall through to the later analytical/LP compilers.
    """
    if not isinstance(lp_model.rule, Maximize):
        return None
    if len(lp_model.variables) != 1:
        return None

    name = next(iter(lp_model.variables))
    if set(lp_model.rule.coefficients) != {name}:
        return None

    # The generated block builder currently emits a constant +1 objective.
    # Accept any finite, non-zero constant coefficient; a Slot-valued objective
    # could cross zero between days and is better left to the generic kernel.
    objective = lp_model.rule.coefficients[name]
    if isinstance(objective, Slot):
        return None
    objective = float(objective)
    if not isfinite(objective) or abs(objective) <= 1e-15:
        return None

    # With one declared variable every row should already be scalar, but keep
    # this check explicit so a malformed hand-built BlockLP does not silently
    # lose a coupled coefficient.
    for constraint in lp_model.constraints:
        if set(constraint.coefficients) - {name}:
            return None

    if set(lp_model.updates) - {name}:
        return None

    return compile_direct_kernel(lp_model)


def try_compile_proportional_calculation(lp_model):
    """Compile a monotone equal-priority block as analytical water filling.

    The proportional LP can be reduced exactly when all declared variables are
    target allocations and every constraint is an upper-capacity row.  With
    nonnegative runtime coefficients, increasing any allocation only consumes
    capacity, so the largest common proportional increment is the minimum of:

    * each active member's remaining variable capacity divided by its share; and
    * each shared row's remaining capacity divided by the active cohort's
      weighted use of that row.

    After committing that increment, members with zero scalar residual capacity
    are removed and the calculation repeats.  This is the same blocked-member
    water-filling schedule used by ``LPKernel.execute``, without its common-
    increment LP or per-member classification LPs.

    Reservation descendants, reservoir counterflow witnesses, lower/equality
    rows, and known negative coefficients remain genuine coupled LP cases and
    fall through.  Slot-valued coefficients are checked at runtime by the
    proportional kernel; an unexpected negative value uses its LP fallback.
    """
    if not isinstance(lp_model.rule, Proportional):
        return None

    targets = set(lp_model.rule.reference_cfs)
    if not targets or set(lp_model.variables) != targets:
        return None
    if set(lp_model.updates) - targets:
        return None

    for name in targets:
        variable = lp_model.variables[name]
        if isinstance(variable.lower, Slot):
            return None
        lower = float(variable.lower)
        if not isfinite(lower) or lower != 0.0:
            return None

    coefficient_slots = set()
    for constraint in lp_model.constraints:
        if constraint.lower is not None:
            return None
        if set(constraint.coefficients) - targets:
            return None
        for coefficient in constraint.coefficients.values():
            if isinstance(coefficient, Slot):
                coefficient_slots.add(coefficient.index)
                continue
            coefficient = float(coefficient)
            if not isfinite(coefficient) or coefficient < 0.0:
                return None

    # Runtime coefficient slots may vary by date, but they must be constant
    # during one block execution.  If committing an increment can mutate a
    # coefficient slot, later water-filling rounds would need the generic LP
    # machinery (and could even change the sign after a partial commit).
    updated_slots = {
        slot.index
        for effects in lp_model.updates.values()
        for slot in effects
    }
    if coefficient_slots & updated_slots:
        return None

    return compile_proportional_kernel(lp_model)

