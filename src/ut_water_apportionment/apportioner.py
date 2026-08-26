'''

Update documentation
Add time-lags
Specified natural flows
Track storage balances, and allow paths to be limited by them.
Better audit tracing of results
Is there anything left in the gemini file that could be useful to me? Any constraints?

'''


from typing import Generator
from math import isclose
from .models import (
    CorePropSchedule,
    CoreScheduleVariable, CoreSeqSchedule,
    FlowComponentsTypes,
    SolverOutputApportionment,
    SolveStepResult,
    SolveStepVariableResult,
    PathTrxn, TrxnGroup, ZoneTypes
)
from .graph_manager import GraphManager
from .timeseries_manager import DailyDataManager
from .trxn_schedule import TrxnSchedule
from .natural_flow_calculator import NaturalFlowCalculator
from .lp_solver import (
    LPSolverError,
    LPSolverFactory,
    LPSolverProtocol,
    resolve_solver_backend,
)


# --- Configuration Constants ---
SLACK_TRXN_PRIORITY = 1e100                 # TODO - remove duplicate constant in models.py
SOLVER_TOL = 1e-6
SHOW_LOG = False

PREFIX_MEASURE = 'MEAS_'
PREFIX_NF_ZONE = 'NF_ZONE_'
PREFIX_PARENT = 'PARENT_'
PREFIX_CONT = 'CONT_'
PREFIX_ACCOUNT_OUT = 'ACCOUNT_OUT_'
PREFIX_ACCOUNT_IN = 'ACCOUNT_IN_'

# Set up logging.
import logging
logger = logging.getLogger(__name__)


class Apportioner:
    """Orchestrates solving the equations."""

    def __init__(
        self,
        gm: GraphManager,
        tm: TrxnSchedule,
        dm: DailyDataManager,
        nfc: NaturalFlowCalculator,
        lp_solver_factory: LPSolverFactory | None = None,
        generate_audit: bool = False,
    ):
        self.gm = gm
        self.tm = tm
        self.dm = dm
        self.nfc = nfc
        self.generate_audit = generate_audit

        self._lp_solver_factory = (
            lp_solver_factory
            if lp_solver_factory is not None
            else resolve_solver_backend().factory
        )
        self.cur_trxn_value: dict[str, float] = {}
        self.apportionments_audit: list[SolveStepResult] = []
        self._audit_sequence = 0
        self.all_trxns = tm.all_trxns
        self.engine = self._build_linear_equations()

        self.feasibility_slacks:list[str] = []


    def _append_audit_record(
            self,
            variables: list[SolveStepVariableResult],
            reason: str,
            limited_by_natural_flow: bool = False,
            date: str | None = None,
            ):
        """A single place for appending an solve-step result."""
        if not self.generate_audit:
            return #None

        audit_date = date or self.dm.cur_date
        if audit_date is None:
            raise ValueError("Daily data date has not been set.")

        self._audit_sequence += 1

        audit_record = SolveStepResult(
            date=audit_date,
            sequence=self._audit_sequence,
            variables=variables,
            reason=reason,
            limited_by_natural_flow=limited_by_natural_flow,
            remaining_natural_flow=self.nfc.remaining_natural_at_zone.copy(),
        )

        self.apportionments_audit.append(audit_record)
        #return audit_record


    def _build_linear_equations(self) -> LPSolverProtocol:
        """Add the variables and constraints to the linear solver engine using a variable-per-path-item approach."""
        engine = self._lp_solver_factory(tolerance=SOLVER_TOL)

        # Add Variables
        for trxn in self.all_trxns:
            if type(trxn) == PathTrxn:
                for x in trxn.path:
                    var_name = f"{trxn.id}___{x.flow_id}"
                    engine.add_variable(name=var_name, lb=0, ub=None)
            elif type(trxn) == TrxnGroup:
                engine.add_variable(name=trxn.id, lb=0, ub=None)

        # Add Interzone Flow Measurements
        for f in self.gm.graph.interzone_flows:
            con_name = PREFIX_MEASURE + f.id
            engine.add_constraint(name=con_name, lb=None, ub=None)

        # Add Natural Flow Constraints (Streams)
        for z in self.gm.graph.zones:
            if z.type == ZoneTypes.STREAM:
                con_name = PREFIX_NF_ZONE + z.id
                engine.add_constraint(name=con_name, lb=None, ub=None)

        # Tie variables to constraints
        for trxn in self.all_trxns:
            if type(trxn) == PathTrxn:
                # 1. Measurement Constraints
                for x in trxn.path:
                    var_name = f"{trxn.id}___{x.flow_id}"
                    con_name = PREFIX_MEASURE + x.flow_id
                    engine.set_coefficient(con_name, var_name, x.factor)

                ordered_path = self.tm.ordered_paths.get(trxn.id, [])

                # 2. Path Continuity Constraints (Mass Balance pushing downstream)
                for i in range(len(ordered_path) - 1):
                    leg1 = ordered_path[i]
                    leg2 = ordered_path[i+1]
                    v1 = f"{trxn.id}___{leg1.flow_id}"
                    v2 = f"{trxn.id}___{leg2.flow_id}"
                    con_name = f"{PREFIX_CONT}{trxn.id}_{i}"

                    engine.add_constraint(name=con_name, lb=0, ub=0)

                    f1 = self.gm.get_flow_by_id(leg1.flow_id)
                    f2 = self.gm.get_flow_by_id(leg2.flow_id)

                    # Select the endpoint losses encountered in the actual
                    # transaction direction. Only constant fractional losses
                    # are supported in transaction continuity at this stage.
                    l1_exit = (
                        f1.loss_to_zone
                        if leg1.factor > 0
                        else f1.loss_from_zone
                    )
                    l2_enter = (
                        f2.loss_from_zone
                        if leg2.factor > 0
                        else f2.loss_to_zone
                    )
                    date = self.dm.cur_date
                    if date is None:
                        raise ValueError("Daily data date has not been set.")

                    rem_factor = (
                        (1.0 - l1_exit.get_fraction(date))
                        * (1.0 - leg1.loss_after)
                        * (1.0 - l2_enter.get_fraction(date))
                        * (1.0 - leg2.loss_before)
                    )

                    engine.set_coefficient(con_name, v2, 1.0)
                    engine.set_coefficient(con_name, v1, -rem_factor)


        # 3. Natural Flow Constraints (only tied to anchor variables)
        ''' -- this code was replaced with what follows.
        for trxn in self.all_trxns:
            if type(trxn) == Trxn:
                ordered_path = self.tm.ordered_paths.get(trxn.id, [])
                if not trxn.is_slack and len(ordered_path) > 0:
                    first_item = ordered_path[0]
                    anchor_var = f"{trxn.id}___{first_item.flow_id}"

                    first_flow = self.gm.get_flow_by_id(first_item.flow_id)
                    from_zone = self.gm.get_zone_by_id(first_flow.from_zone)
                    if first_item.factor < 0:
                        from_zone = self.gm.get_zone_by_id(first_flow.to_zone)

                    if from_zone.type == ZoneTypes.STREAM:
                        engine.set_coefficient(PREFIX_NF_ZONE + from_zone.id, anchor_var, 1)
                        for f in self.gm.traverse_downstream(from_zone.id):
                            engine.set_coefficient(PREFIX_NF_ZONE + f.to_zone, anchor_var, 1)
        '''


        # Zone-account limits. These are shared constraints: every transaction
        # that references the same account competes for the same available
        # outgoing/incoming capacity.
        for zone in self.gm.graph.zones:
            for account in zone.accounts:
                balance = self.tm.get_account_balance(zone.id, account.id)

                if account.balance_floor is not None:
                    con_name = f"{PREFIX_ACCOUNT_OUT}{zone.id}___{account.id}"
                    available_out = max(0.0, balance - account.balance_floor)
                    engine.add_constraint(name=con_name, lb=0, ub=available_out)
                    for trxn in self.tm.get_account_outgoing_trxns(zone.id, account.id):
                        engine.set_coefficient(
                            con_name,
                            self.tm.get_from_account_var(trxn),
                            1.0,
                        )

                if account.balance_ceiling is not None:
                    con_name = f"{PREFIX_ACCOUNT_IN}{zone.id}___{account.id}"
                    available_in = max(0.0, account.balance_ceiling - balance)
                    engine.add_constraint(name=con_name, lb=0, ub=available_in)
                    for trxn in self.tm.get_account_incoming_trxns(zone.id, account.id):
                        engine.set_coefficient(
                            con_name,
                            self.tm.get_to_account_var(trxn),
                            1.0,
                        )


        # Group Limits
        for trxn in self.all_trxns:
            if type(trxn) == TrxnGroup:
                con_name = PREFIX_PARENT + trxn.id
                engine.add_constraint(name=con_name, lb=0, ub=0)
                engine.set_coefficient(con_name, trxn.id, 1)

                for v2 in trxn.children_trxns:
                    if type(v2) == PathTrxn:
                        anchor_var = self.tm.get_anchor_var(v2)
                        if anchor_var:
                            engine.set_coefficient(con_name, anchor_var, -1)
                    elif type(v2) == TrxnGroup:
                        engine.set_coefficient(con_name, v2.id, -1)

        return engine

    def update_daily_bounds(self):
        """Updates the limits on variables and measurements for the current day."""
        # Update Variable Limits
        for trxn in self.all_trxns:
            upper_limit = self.tm.get_transaction_upper_limit(trxn, self.dm.cur_date)

            if type(trxn) == PathTrxn:
                if trxn.is_slack:
                    upper_limit = None

                anchor_var = self.tm.get_anchor_var(trxn)
                if anchor_var:
                    # Apply upper_limit strictly to the anchor variable
                    self.engine.update_variable_bounds(anchor_var, lb=0, ub=upper_limit)

                # Loop through all paths (including mathematically derived loss branches)
                for path_item in trxn.path:
                    var_name = f"{trxn.id}___{path_item.flow_id}"
                    if var_name != anchor_var:
                        self.engine.update_variable_bounds(var_name, lb=0, ub=None)

            elif type(trxn) == TrxnGroup:
                self.engine.update_variable_bounds(trxn.id, lb=0, ub=upper_limit)

        # Update Measurement Constraints
        for f in self.gm.graph.interzone_flows:

            # Remove the measurement constraint if the type is UNCONSTRAINED.
            if f.flow_type == FlowComponentsTypes.UNCONSTRAINED:
                self.engine.update_constraint_ub(name=PREFIX_MEASURE + f.id, ub=None)
                continue

            # Otherwise, set the meas constraint to the measured flow value.
            flow = self.dm.cur_flows_by_id[f.id].measured
            if flow is not None:
                self.engine.update_constraint_lb(name=PREFIX_MEASURE + f.id, lb=flow)
                self.engine.update_constraint_ub(name=PREFIX_MEASURE + f.id, ub=flow)

    def apply_nf_mass_balance_constraints(self, date:str):
        """Set natural-flow limits and variable coefs for the current day."""

        # 1. Calculate the natural flow.
        self.nfc.calculate(
            date=date,
            daily_flows=self.dm.cur_flows_by_id,
            specified_values=self.dm.get_specified_natural_flow_values(date),
            boundary_values=self.dm.get_boundary_natural_flow_values(date),
        )

        self._append_audit_record(
            variables=[],
            reason='NF Calculations',
            date=date,
        )


        # 1-B.
        # Accounting boundaries affect only what remains available
        # to this solver.
        self.nfc.apply_external_boundary_commitments(
            daily_flows=self.dm.cur_flows_by_id,
            boundary_values=self.dm.get_boundary_natural_flow_values(date)
        )

        self._append_audit_record(
            variables=[],
            reason='NF Boundary Adjustments',
            date=date,
        )


        # 2. Complete the NF Constraints (these constraints were created in
        #    init but we didn't have all the info to complete them)
        for z in self.gm.graph.zones:
            if z.type == ZoneTypes.STREAM:

                # A. Update the coefs for all variables that are constrained.
                coefficients = self.nfc.get_nf_constraint_coefficients(z.id)
                for trxn in self.tm.get_nf_trxn_ids_for_zone(z.id):
                    anchor_var = self.tm.get_anchor_var(trxn)
                    if anchor_var is None:
                        continue
                    for zone_id, coefficient in coefficients.items():
                        self.engine.set_coefficient(
                            PREFIX_NF_ZONE + zone_id,
                            anchor_var,
                            coefficient,
                        )

                # B. Set the bounds to fix NF to the actual value.
                con_name = PREFIX_NF_ZONE + z.id
                nf_available = max(0, self.nfc.remaining_natural_at_zone[z.id])

                self.engine.update_constraint_lb(name=con_name, lb=0)
                self.engine.update_constraint_ub(
                    name=con_name, ub=nf_available
                )

    def remove_nf_mass_balance_constraints(self):

        for z in self.gm.graph.zones:
            if z.type == ZoneTypes.STREAM:
                self.engine.update_constraint_lb(name=PREFIX_NF_ZONE + z.id, lb=0)
                self.engine.update_constraint_ub(name=PREFIX_NF_ZONE + z.id, ub=None)

    def calculate_spills(self) -> float:
        """Find all the variables that flow to a stream, and add a constraint
        to prevent them from increasing. """

        total_spill = 0

        natural_zones = {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}

        previous_remaining_nf = self.nfc.remaining_natural_at_zone.copy()

        # Find all the spill vars...
        spill_vars:list[PathTrxn] = []
        for t in self.all_trxns:
            if type(t) == PathTrxn and t.is_slack:
                if len(t.path) == 1:
                    path_item = t.path[0]
                    flow = self.gm.get_flow_by_id(path_item.flow_id)
                    from_zone = self.gm.get_zone_by_id(flow.from_zone)
                    to_zone = self.gm.get_zone_by_id(flow.to_zone)

                    if path_item.factor < 0:
                        from_zone, to_zone = to_zone, from_zone

                    if from_zone.type not in natural_zones and to_zone.type in natural_zones:
                        spill_vars.append(t)

        # Minimize the spill vars.
        anchor_spill_vars:list[str] = [self.tm.get_anchor_var(t) for t in spill_vars if self.tm.get_anchor_var(t)] # type: ignore
        if anchor_spill_vars:

            obj_value, solved_values = self._with_feasibility_fallback(
                'solve for lock spills',
                lambda: self.engine.solve_objective(
                    anchor_spill_vars,
                    maximization=False
                )
            )

            # Lock the spill vars to their calculated value.
            for var_name, value in solved_values.items():
                self.engine.update_variable_bounds(var_name, lb=value, ub=value)
                self.cur_trxn_value[var_name] = value


            # Increase the NF.
            for var_name, value in solved_values.items():
                # 1. Get the to-zone for the spill variable, and
                spill_trxn = next(
                    (
                        t for t in spill_vars
                        if self.tm.get_anchor_var(t) == var_name
                    ),
                    None,
                )

                if spill_trxn is None:
                    raise ValueError(
                        f"Could not find spill transaction for variable {var_name!r}."
                    )

                path_item = spill_trxn.path[0]
                flow = self.gm.get_flow_by_id(path_item.flow_id)

                # Determine the receiving zone in the actual transaction direction,
                # along with the endpoint loss encountered before the spill reaches it.
                if path_item.factor > 0:
                    to_zone_id = flow.to_zone
                    endpoint_loss = flow.loss_to_zone
                else:
                    to_zone_id = flow.from_zone
                    endpoint_loss = flow.loss_from_zone

                # 2. Apply a NF credit to that to-zone
                credit_at_zone = endpoint_loss.transform_total_flow(
                    value,
                    date=self.dm.cur_date,
                )
                self.nfc.apply_committed_allocation(to_zone_id, -credit_at_zone)

                # 3. Add it to the output
                total_spill += credit_at_zone


            # Update the NF constraint upper bounds.
            # This needs to take into account the routing of spills downstream.
            # We can do this by comparing the current NF to that from the
            # latest audit record. The increase in each zone is what needs to
            # be added to the NF constraint.
            for zone_id, prev_nf in previous_remaining_nf.items():
                new_nf = self.nfc.remaining_natural_at_zone[zone_id]
                delta = new_nf - prev_nf

                # 3. Increase the NF constraint.
                con_name = PREFIX_NF_ZONE + zone_id
                lb, ub = self.engine.get_constraint_bounds(con_name)
                self.engine.update_constraint_ub(
                    name=con_name,
                    ub=ub+delta
                )


            # Add to audit log
            self._append_audit_record(
                variables=[
                    SolveStepVariableResult(
                        variable_name=varid,
                        value_before=0,
                        value_after=value,
                    )
                    for varid, value in solved_values.items()
                ],
                reason='Spills',
            )


        return total_spill


    def calculate_apportionments(self,
                                 schedule: CoreSeqSchedule,
                                 start_priority=None,
                                 stop_priority=None):
        """Solves the variables in the order defined by the schedule."""


        for x in schedule.series:
            priority = x.priority
            item = x.item

            if start_priority is not None and start_priority > priority: continue
            if stop_priority is not None and stop_priority < priority: continue
            if priority >= SLACK_TRXN_PRIORITY: return

            logger.debug(f"\nPriority: {priority}")

            if type(item) is CorePropSchedule:
                self.maximize_series(item)
            elif type(item) is CoreSeqSchedule:
                self.calculate_apportionments(
                    item,
                    start_priority,
                    stop_priority
                )
            elif type(item) is CoreScheduleVariable:
                if self._source_nf_is_exhausted(item.var):
                    continue

                self._maximize_var(item.var)

            logger.info(f"Completed iteration for priority: {priority}")

    def solve_for_nonpath_vars(self):
        # NOTE: I get away with not bothering to seperate the non-path variables
        #       from the path variables in this code because the path variables
        #       have already been maximized and updated so they can not be less
        #       than their max values.
        target_variables = []
        for trxn in self.all_trxns:
            if type(trxn) == PathTrxn:
                for x in trxn.path:
                    target_variables.append(f"{trxn.id}___{x.flow_id}")
            elif type(trxn) == TrxnGroup:
                target_variables.append(trxn.id)

        _, variable_values = self._with_feasibility_fallback(
            'solve for nonpath vars',
            lambda: self.engine.solve_objective(
                target_variables,
                maximization=False
            )
        )

        for var_name, solved_value in variable_values.items():
            self.cur_trxn_value[var_name] = solved_value
            logger.debug(f' - maxed {var_name} to {solved_value}')

    def _minimize_minus_vars(self, vars: list[PathTrxn | TrxnGroup]) -> dict[str, float]:
        origional_ub: dict[str, float] = {}
        minus_vars = self.tm.get_minus_vars(vars)

        if not minus_vars:
            return origional_ub

        var_names = []
        for minus_var in minus_vars:
            anchor = self.tm.get_anchor_var(minus_var)
            if anchor and anchor not in origional_ub:
                _, ub = self.engine.get_variable_bounds(anchor)
                origional_ub[anchor] = ub
                var_names.append(anchor)

        if var_names:
            # Minimize ALL dump slacks simultaneously
            _, solved_values = self._with_feasibility_fallback(
                'solve for minimizing minus vars',
                lambda: self.engine.solve_objective(
                    var_names,
                    maximization=False
                )
            )

            # Lock their upper bounds to the newly found minimums
            for v_name in var_names:
                min_val = solved_values[v_name]

                # Clean up floating point noise to prevent ABNORMAL status
                if abs(min_val) < SOLVER_TOL:
                    min_val = 0.0

                self.engine.update_variable_bounds(v_name, ub=min_val)

        return origional_ub

    def _reset_minus_vars(self, origional_ub: dict[str, float]):
        for minus_var, ub in origional_ub.items():
            self.engine.update_variable_bounds(minus_var, ub=ub)

    def _maximize_var(self, var: PathTrxn | TrxnGroup):
        """My earlier version of this function used _minimize_minus_vars and
        _reset_minus_vars to prevent apportionments from forcing a reservoir
        spill to increase the divertible natural flow."""
        if type(var) == PathTrxn:
            target_var = self.tm.get_anchor_var(var)
        else:
            target_var = var.id

        if not target_var:
            return

        origional_ub = self._minimize_minus_vars([var])
        value_before = self.cur_trxn_value.get(target_var, 0.0)
        new_value = self._with_feasibility_fallback(
            'solve to maximize var ' + var.id,
            lambda: self.engine.maximize_and_update_variable(
                target_var
            )
        )
        self.cur_trxn_value[target_var] = new_value

        # Update the remaining NF
        delta = new_value - value_before
        self._apply_natural_flow_change(var, delta)

        # Add to audit history
        if self.generate_audit:
            solve_step, audit_context = self._capture_solve_step_data(
                var=var,
                target_var=target_var,
                value_before=value_before,
                value_after=new_value
            )
            self._record_audit_iteration(
                steps=[solve_step],
                contexts=[audit_context],
                limiting_txn_ids=[var.id],
                is_proportional=False
            )

        logger.debug(f' - maxed {target_var} to {new_value}')
        self._reset_minus_vars(origional_ub)

    def maximize_series(self, series: CorePropSchedule | CoreSeqSchedule):
        """Maximize the given series of variables (either a sequential or
        proportional series) until all the variables in the series are
        maximized.
        Tiny factor variables are caught, warned, and executed
        sequentially immediately following the equal-priority series.
        """
        maxed_vs: list[PathTrxn | TrxnGroup] = []
        deferred_vars: list[PathTrxn | TrxnGroup] = []  # Track variables with tiny proportion factors

        vars_list, factors = self._get_next_iter(series, maxed_vs)
        circular_loop_strikes = 0
        while vars_list:
            var_names = []
            vars_by_name = {}
            for v in vars_list:
                if type(v) == PathTrxn:
                    var_name = self.tm.get_anchor_var(v)
                else:
                    var_name = v.id

                if var_name:
                    var_names.append(var_name)
                    vars_by_name[var_name] = v

            proportion_factors = {var_names[i]: factors[i] for i in range(len(var_names))}

            has_tiny_factor = False
            for var_name, f in list(proportion_factors.items()):
                if 0 < f < 0.000001:
                    logger.warning(f"WARNING: Proportion factor for Variable {var_name} ({f:.8f}) is too small. "
                        f"Moving to sequential execution immediately after this series.")
                    # Match string back to var obj
                    var_obj = vars_by_name.get(var_name)
                    if var_obj and var_obj not in deferred_vars:
                        deferred_vars.append(var_obj)
                        maxed_vs.append(var_obj)  # Mark as "maxed" to drop from current prop iterations
                    has_tiny_factor = True

            # If any tiny factors were filtered out, re-evaluate the next iteration parameters
            if has_tiny_factor:
                vars_list, factors = self._get_next_iter(series, maxed_vs)
                continue

            factors_sum = sum(proportion_factors.values())

            # Deal with the edge case where every proportion_factor is zero.
            if factors_sum == 0:
                for var_name in proportion_factors:
                    self.engine.update_variable_bounds(var_name, lb=0)
                    self.cur_trxn_value[var_name] = 0
                    logger.debug(f' - initialized {var_name} to 0')
                break

            ## minimize all minus vars
            origional_ub = self._minimize_minus_vars(vars_list)
            values_before = {
                var_name: self.cur_trxn_value.get(var_name, 0.0)
                for var_name in var_names
            }

            # Solve
            var_values = self._with_feasibility_fallback(
                'solve to maximize series',
                lambda: self.engine.maximize_group_by_proportions(
                    var_names,
                    proportion_factors
                )
            )
            member_steps = []
            audit_contexts = []

            # Update the variables. Set only the lb for now, since we might
            # be able to increase this variable further in a future iteration.
            for var_name, var_value in var_values.items():

                # Prevent floating-point noise from creating negative lower bounds
                # Use abs() to prevent zeroing out valid negative flows!
                if abs(var_value) < SOLVER_TOL:
                    var_value = 0.0

                self.engine.update_variable_bounds(var_name, lb=var_value)
                self.cur_trxn_value[var_name] = var_value

                var_obj = vars_by_name.get(var_name)
                if var_obj:

                    if self.generate_audit:
                        solve_step, audit_context = self._capture_solve_step_data(
                            var=var_obj,
                            target_var=var_name,
                            value_before=values_before[var_name],
                            value_after=var_value,
                            proportion_factor=proportion_factors[var_name]
                        )
                        member_steps.append(solve_step)
                        audit_contexts.append(audit_context)

                    delta = var_value - values_before[var_name]

                    self._apply_natural_flow_change(var_obj, delta)


            # Identify every member that can no longer increase. This uses
            # batched objectives so one solve can classify many variables.
            newly_maxed = self._get_newly_maxed_vars(vars_list)

            limiting_txn_ids = [v.id for v in newly_maxed]

            audit_record = self._record_audit_iteration(
                steps=member_steps,
                contexts=audit_contexts,
                limiting_txn_ids=limiting_txn_ids,
                is_proportional=True
            )
            for var_name, var_obj in vars_by_name.items():
                if var_name in var_values:
                    logger.debug(
                        f' - maxed {var_name} to {self.cur_trxn_value[var_name]} '
                    )

            # Get a list of the variables that are now maximized.
            maxed_vs.extend(newly_maxed)

            # This function will check to see if the series can further be
            # maximized, possibly by dropping a constrained variable (in a
            # proportional series) or by replacing a constrained variable by the
            # next in line (for a sequential series). It returns the info needed
            # to continue on into another loop iteration.
            old_cnt = len(vars_list)
            vars_list, factors = self._get_next_iter(series, maxed_vs)
            new_cnt = len(vars_list)
            if new_cnt >= old_cnt:
                circular_loop_strikes += 1
                if circular_loop_strikes >= 3:
                    raise RuntimeError('Circular loop detected! Problem identifying any equal-priority variables to drop!')

            ## reset all minus vars
            self._reset_minus_vars(origional_ub)

        # 2. Process the tiny-factor variables sequentially right after the group closes
        for var_obj in deferred_vars:
            logger.warning(f"Processing deferred tiny-factor variable sequentially: {var_obj.id}")
            self._maximize_var(var_obj)


    def _get_newly_maxed_vars(self, vars: list[PathTrxn | TrxnGroup]):
        """Return the variables that cannot be increased further.

        The active variables have already been fixed at their current values by
        lower bounds. Maximizing all unresolved variables together can therefore
        identify multiple non-maxed variables in one solve. If none of the
        unresolved variables increases, every remaining variable is maxed.
        """


        maxed_ids: set[str] = set()
        remaining: dict[str, PathTrxn | TrxnGroup] = {}
        current_values: dict[str, float] = {}

        for var in vars:
            if self._source_nf_is_exhausted(var): # If there is no more nf then no need to spend a solver run
                maxed_ids.add(var.id)
                continue
            if type(var) == PathTrxn:
                target_var = self.tm.get_anchor_var(var)
            else:
                target_var = var.id

            if not target_var:
                maxed_ids.add(var.id)
                continue

            current_value = self.cur_trxn_value[target_var]
            _, upper_bound = self.engine.get_variable_bounds(target_var)

            if (
                upper_bound != float('inf')
                and isclose(current_value, upper_bound, abs_tol=SOLVER_TOL)
            ):
                maxed_ids.add(var.id)
                continue

            remaining[target_var] = var
            current_values[target_var] = current_value

        while remaining:
            target_vars = list(remaining.keys())
            _, solved_values = self._with_feasibility_fallback(
                'check newly maxed vars',
                lambda: self.engine.solve_objective(
                    target_vars,
                    maximization=True
                )
            )

            increasable_vars = [
                target_var
                for target_var in target_vars
                if solved_values[target_var]
                    > current_values[target_var] + SOLVER_TOL
            ]

            if not increasable_vars:
                maxed_ids.update(var.id for var in remaining.values())
                break

            # A variable that increased in this feasible solution is known not
            # to be maxed. Remove all such variables and test the unresolved
            # remainder together in the next batch.
            for target_var in increasable_vars:
                del remaining[target_var]

        return [var for var in vars if var.id in maxed_ids]

    def _get_next_iter(self, schedule: CorePropSchedule | CoreSeqSchedule, maxed_vars: list[PathTrxn | TrxnGroup]):
        """Returns two lists for the next iteration.
        If there are no remaining variables to maximize, returns two empty
        lists.
        """
        var_names: list[PathTrxn | TrxnGroup] = []
        factors: list[float] = []

        # If it is a sequential series, return the params for the next item.
        if type(schedule) is CoreSeqSchedule:
            for x in schedule.series:
                item = x.item
                if type(item) == CoreSeqSchedule or type(item) == CorePropSchedule:
                    var_names, factors = self._get_next_iter(item, maxed_vars)
                elif type(item) is CoreScheduleVariable:
                    if (
                        item.var not in maxed_vars
                        and not self._source_nf_is_exhausted(item.var)
                    ):
                        var_names.append(item.var)
                        factors.append(1)
                if len(var_names)>0:
                    break

        # If it is a proportional series, return the list of paths and a list
        # of each's proportion. If the proportional series has any sub-series,
        # this will involve identifying which variable(s) from the subseries
        # need to be considered and their factor(s).
        elif type(schedule) is CorePropSchedule:
            for x in schedule.series:
                item, factor = x.item, x.factor

                if type(item) == CoreSeqSchedule or type(item) == CorePropSchedule:
                    svar_names, sfactors = self._get_next_iter(item, maxed_vars)
                    var_names.extend(svar_names)
                    sum_sfactors = sum(sfactors)
                    if sfactors:
                        x = 0
                        if sum_sfactors > 0:
                            x = factor / sum_sfactors
                        factors.extend([f * x for f in sfactors])

                elif type(item) is CoreScheduleVariable:
                    if (
                        item.var not in maxed_vars
                        and not self._source_nf_is_exhausted(item.var)
                    ):
                        var_names.append(item.var)
                        factors.append(factor)

        return var_names, factors

    def get_variables(self, date: str) -> list[SolverOutputApportionment]:
        """Formats the solved values into the output structure."""
        vars_output: list[SolverOutputApportionment] = []

        for v in self.all_trxns:

            # Skip non-transaction objects (like TrxnGroups)
            if type(v) != PathTrxn:
                continue

            # Check if this variable is a special single-hop stream-to-stream slack variable
            is_stream_slack = False

            natural_set = set([ZoneTypes.STREAM])

            if v.is_slack and len(v.path) == 1:

                flowobj = self.gm.get_flow_by_id(v.path[0].flow_id)

                # If this variable represents a slack variable for a stream to stream
                # connection,
                from_zone_type = self.gm.get_zone_by_id(flowobj.from_zone).type
                to_zone_type = self.gm.get_zone_by_id(flowobj.to_zone).type

                if from_zone_type == ZoneTypes.STREAM and to_zone_type == ZoneTypes.STREAM:
                    is_stream_slack = True

            if is_stream_slack:
                flowobj = self.gm.get_flow_by_id(v.path[0].flow_id)


                # Get the natural flow.
                natural_flow = self.dm.cur_flows_by_id[flowobj.id].natural
                var_name = f"{v.id}___{v.path[0].flow_id}"
                var_value = self.cur_trxn_value.get(var_name, 0.0)

                if natural_flow is not None and var_value is not None:
                    vars_output.append(SolverOutputApportionment(
                        date=date,
                        interzone_flow_id=v.path[0].flow_id,
                        txn_id=v.id + '_NF',
                        value=natural_flow * v.path[0].factor,
                        is_forward=True
                    ))
                    vars_output.append(SolverOutputApportionment(
                        date=date,
                        interzone_flow_id=v.path[0].flow_id,
                        txn_id=v.id + '_CPI',
                        value=(var_value - natural_flow) * v.path[0].factor,
                        is_forward=(var_value > natural_flow),
                        reason=''
                    ))
            else:
                for a in v.path:
                    var_name = f"{v.id}___{a.flow_id}"
                    var_value = self.cur_trxn_value.get(var_name, 0.0)

                    if var_value is None:
                        raise ValueError(f'value for {var_name} is None')

                    vars_output.append(SolverOutputApportionment(
                        date=date,
                        interzone_flow_id=a.flow_id,
                        txn_id=v.id,
                        value=var_value * a.factor,
                        is_forward=a.factor > 0,
                        reason=''
                    ))

        return vars_output

    def _capture_solve_step_data(
            self,
            var: PathTrxn | TrxnGroup,
            target_var: str,
            value_before: float,
            value_after: float,
            proportion_factor: float | None = None
            ) -> tuple[SolveStepVariableResult, dict]:
        """Capture the small amount of transient solve evidence needed later.

        Constraint details are used only to produce the iteration reason and
        natural-flow flag. They are deliberately not retained in SolverOutput.
        """
        evidence_variable_names = [target_var]

        if type(var) == TrxnGroup:
            def add_descendant_variables(group: TrxnGroup):
                for child in group.children_trxns:
                    if type(child) == PathTrxn:
                        anchor_var = self.tm.get_anchor_var(child)
                        if anchor_var:
                            evidence_variable_names.append(anchor_var)
                    elif type(child) == TrxnGroup:
                        add_descendant_variables(child)

            add_descendant_variables(var)

        constraints = []
        seen_constraints = set()
        for evidence_variable_name in evidence_variable_names:
            for constraint in self.engine.get_last_solve_constraint_evidence(
                    evidence_variable_name, SOLVER_TOL):
                constraint_name = constraint['constraint_name']
                if constraint_name in seen_constraints:
                    continue
                seen_constraints.add(constraint_name)
                constraints.append(dict(constraint))

        upper_limit, limit_source = self.tm.get_transaction_limit_info(
            var, self.dm.cur_date
        )
        context = {
            'txn_id': var.id,
            'var': var,
            'upper_limit_reached': (
                upper_limit is not None
                and isclose(value_after, upper_limit, abs_tol=SOLVER_TOL)
            ),
            'limit_source': limit_source,
            'constraints': constraints,
        }

        return (
            SolveStepVariableResult(
                variable_name=target_var,
                value_before=value_before,
                value_after=value_after,
                proportion_factor=proportion_factor,
            ),
            context,
        )

    def _record_audit_iteration(
            self,
            steps: list[SolveStepVariableResult],
            contexts: list[dict],
            limiting_txn_ids: list[str],
            is_proportional: bool
            ):
        if self.dm.cur_date is None:
            raise ValueError("Daily data date has not been set.")

        if not self.generate_audit:
            return

        reason, limited_by_natural_flow = self._summarize_iteration_limit(
            contexts,
            limiting_txn_ids,
            is_proportional,
        )

        self._append_audit_record(
            variables=steps,
            reason=reason,
            limited_by_natural_flow=limited_by_natural_flow,
        )


    def _summarize_iteration_limit(
            self,
            contexts: list[dict],
            limiting_txn_ids: list[str],
            is_proportional: bool
            ) -> tuple[str, bool]:
        if not contexts:
            return "No variables were recorded for this solve", False

        limiting_ids = set(limiting_txn_ids)
        limiting_contexts = [
            context
            for context in contexts
            if context['txn_id'] in limiting_ids
        ]
        if not limiting_contexts:
            limiting_contexts = contexts

        reasons = []

        reached_limits: dict[str, list[str]] = {}
        for context in limiting_contexts:
            if not context['upper_limit_reached']:
                continue
            source = context.get('limit_source') or 'UPPER_LIMIT'
            reached_limits.setdefault(source, []).append(context['txn_id'])

        limit_labels = {
            'UPPER_LIMIT': 'upper limit',
            'CALL_LIMIT': 'call limit',
            'CUMULATIVE_LIMIT': 'cumulative limit',
        }
        for source, txn_ids in reached_limits.items():
            label = limit_labels.get(source, 'transaction limit')
            if len(txn_ids) == 1:
                reasons.append(f"Reached {label} for '{txn_ids[0]}'")
            else:
                reasons.append(
                    f"Reached {label}s for "
                    + ", ".join(f"'{txn_id}'" for txn_id in txn_ids)
                )

        selected_blockers: dict[str, dict] = {}
        for context in limiting_contexts:
            var_obj = context['var']
            own_group_constraint = (
                PREFIX_PARENT + context['txn_id']
                if type(var_obj) == TrxnGroup
                else None
            )

            eligible = [
                constraint
                for constraint in context['constraints']
                if constraint['blocks_direct_increase']
                and self._get_constraint_type(constraint['constraint_name'])
                    not in {'PATH_CONTINUITY', 'PROPORTIONAL_ALLOCATION'}
                and constraint['constraint_name'] != own_group_constraint
            ]

            parent_group_blockers = [
                constraint
                for constraint in eligible
                if self._get_constraint_type(constraint['constraint_name'])
                    == 'GROUP_LIMIT'
            ]
            if parent_group_blockers:
                eligible = parent_group_blockers

            dual_supported = [
                constraint
                for constraint in eligible
                if constraint['dual_value'] is not None
                and abs(constraint['dual_value']) > SOLVER_TOL
            ]
            blockers = dual_supported if dual_supported else eligible

            for constraint in blockers:
                selected_blockers[constraint['constraint_name']] = constraint

        descriptions = [
            self._constraint_reason_text(constraint_name)
            for constraint_name in selected_blockers
        ]
        if descriptions:
            reasons.append("Reached " + "; ".join(descriptions))

        limited_by_natural_flow = any(
            constraint_name.startswith(PREFIX_NF_ZONE)
            for constraint_name in selected_blockers
        )

        if reasons:
            return ". ".join(reasons), limited_by_natural_flow

        if is_proportional:
            return (
                "Equal-priority proportional increment reached its maximum "
                "feasible value; no limiting constraint was identified",
                False,
            )

        return "No limiting constraint was identified", False

    def _constraint_reason_text(self, constraint_name: str) -> str:
        constraint_type = self._get_constraint_type(constraint_name)
        if constraint_type == 'MEASUREMENT':
            return f"measured flow constraint '{constraint_name[len(PREFIX_MEASURE):]}'"
        if constraint_type == 'NATURAL_FLOW':
            return f"natural flow availability at '{constraint_name[len(PREFIX_NF_ZONE):]}'"
        if constraint_type == 'GROUP_LIMIT':
            return f"transaction group limit '{constraint_name[len(PREFIX_PARENT):]}'"
        if constraint_type == 'ACCOUNT_FLOOR':
            return f"account floor '{constraint_name[len(PREFIX_ACCOUNT_OUT):]}'"
        if constraint_type == 'ACCOUNT_CEILING':
            return f"account ceiling '{constraint_name[len(PREFIX_ACCOUNT_IN):]}'"
        if constraint_type == 'SPILL_LOCK':
            return "locked minimum spill"
        if constraint_type == 'FEASIBILITY':
            return f"feasibility adjustment '{constraint_name}'"
        return f"constraint '{constraint_name}'"

    def _get_constraint_type(self, constraint_name: str) -> str:
        if constraint_name.startswith(PREFIX_MEASURE):
            return 'MEASUREMENT'
        if constraint_name.startswith(PREFIX_NF_ZONE):
            return 'NATURAL_FLOW'
        if constraint_name.startswith(PREFIX_PARENT):
            return 'GROUP_LIMIT'
        if constraint_name.startswith(PREFIX_ACCOUNT_OUT):
            return 'ACCOUNT_FLOOR'
        if constraint_name.startswith(PREFIX_ACCOUNT_IN):
            return 'ACCOUNT_CEILING'
        if constraint_name.startswith(PREFIX_CONT):
            return 'PATH_CONTINUITY'
        if constraint_name.startswith('combined_'):
            return 'PROPORTIONAL_ALLOCATION'
        if constraint_name == 'lock-slacks':
            return 'SPILL_LOCK'
        if constraint_name.startswith('FEAS_'):
            return 'FEASIBILITY'
        return 'OTHER'



    def _apply_natural_flow_change(
        self,
        var: PathTrxn | TrxnGroup,
        delta: float,
    ):
        if type(var) != PathTrxn:
            return

        source_zone_id = self.tm.get_nf_zone_id(var)

        if source_zone_id is None:
            return

        self.nfc.apply_committed_allocation(
            source_zone_id,
            delta,
        )


    def _source_nf_is_exhausted(
        self,
        var: PathTrxn | TrxnGroup,
    ) -> bool:

        if type(var) == PathTrxn:
            from_zone_id = self.tm.get_nf_zone_id(var)
            if from_zone_id is None:
                return False

            return self.nfc.source_is_exhausted(from_zone_id)

        if type(var) == TrxnGroup:
            children = list(
                self.tm.traverse_vars(var.children_trxns)
            )

            source_children = [
                child
                for child in children
                if type(child) == PathTrxn
            ]

            return (
                bool(source_children)
                and all(
                    self._source_nf_is_exhausted(child)
                    for child in source_children
                )
            )

        return False


    def feasibility_fallback(self) -> float:
        """
        The intended fallback hierarchy is:
        1. minimize total constraint violation;
        2. among solutions with minimum violation, maximize the current transaction.
        """
        FEASIBILITY_FALLBACK_TOL = 10 * SOLVER_TOL

        self.feasibility_slacks = self.add_feasibility_vars(self.feasibility_slacks)

        # A previous fallback may already have fixed FEAS_SUM. Release that lock
        # before finding the minimum feasible violation under the current bounds.
        self.engine.update_variable_bounds( 'FEAS_SUM', lb=0, ub=float('inf') )

        _, solved_values = self.engine.solve_objective(
            ['FEAS_SUM'],
            maximization=False
        )
        minimum_feasibility = solved_values['FEAS_SUM']

        # Lock the minimum violation before returning to the requested
        # transaction objective. This makes the fallback lexicographic:
        # first minimize constraint violations, then optimize apportionment.
        self.engine.update_variable_bounds(
            'FEAS_SUM',
            lb=0,
            ub=minimum_feasibility+ FEASIBILITY_FALLBACK_TOL
        )

        return minimum_feasibility


    def add_feasibility_vars(self, feasibility_slacks) -> list[str]:
        """Update the constraints to include true slack variables that will
        ensure the system is feasible, even if we have floating point rounding
        issues that can occur from the equal priority scalling stuff.

        The side effect of adding these is that we have to minimize them before
        solving for variables.
        """

        engine = self.engine

        def add_feasibility_slack(con_name, coef):
            var_name = 'FEAS_SLACK_'+con_name+'_'+str(coef)
            if var_name not in feasibility_slacks:
                engine.add_variable(var_name, lb=0, ub=None)
                engine.set_coefficient(con_name, var_name, coef)
                engine.set_coefficient('FEAS_TOTAL', var_name, 1)
                feasibility_slacks.append(var_name)

        if not engine.has_variable('FEAS_SUM'):
            engine.add_variable('FEAS_SUM', lb=0, ub=None)
            engine.add_constraint('FEAS_TOTAL', lb=0, ub=0)
            engine.set_coefficient('FEAS_TOTAL', 'FEAS_SUM', -1)

        # Add vars to each constraint:
        for con_name in engine.get_constraint_names():
            if con_name == 'FEAS_TOTAL':
                continue
            add_feasibility_slack(con_name, 1)
            add_feasibility_slack(con_name, -1)

        return feasibility_slacks



    def _with_feasibility_fallback(
        self,
        operation: str,
        solve_fn,
    ):
        """Runs the given function. If a LPSolverError exception is
        raised, then employs the feasibility fallback and try running the
        given function again."""
        try:
            return solve_fn()

        except LPSolverError:
            logger.warning(
                "%s failed. Adding feasibility slacks...",
                operation
            )

            minimum_feasibility = self.feasibility_fallback()

            logger.warning(
                "%s required feasibility adjustment of %.12g",
                operation,
                minimum_feasibility
            )

            return solve_fn()