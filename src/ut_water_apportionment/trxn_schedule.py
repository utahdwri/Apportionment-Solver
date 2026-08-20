from collections.abc import Iterator
from copy import deepcopy
from datetime import date as Date
from .models import (
    AccountingGraph, AccountingLimit, CorePropSchedule, CorePropScheduleItem,
    CoreScheduleVariable, CoreSeqSchedule, CoreSeqScheduleItem,
    InterzoneFlow,
    TrxnBaseClass, PathTrxn, TrxnGroup, TrxnPathItem, Zone, ZoneTypes
)
from .graph_manager import GraphManager


# Set up logging.
import logging
logger = logging.getLogger(__name__)



class TrxnSchedule:
    """Manage a collection of transactions."""

    def __init__(self, gm: GraphManager, txns:list[PathTrxn | TrxnGroup], max_daily_apportionment:float|None=None):

        self._validate(txns, gm)

        self.gm = gm
        p_trxns = self._init_process_input_trxns(txns)
        self.all_trxns = list(self.traverse_vars(p_trxns))
        self._max_daily_apportionment = max_daily_apportionment

        self.ordered_paths = self._init_build_ordered_paths()
        self._validate_account_references()

        self._natural_flow_trxns = self._init_natural_flow_trxns()             # This helps to track the trxns that depend on NF available at a given zone.

        self.lookup_flow_trxns = self._init_build_flow_trxns_lookup()

        # State that persists across daily LP instances.
        self._account_balances: dict[tuple[str, str], float] = {
            (zone.id, account.id): float(account.starting_balance)
            for zone in self.gm.graph.zones
            for account in zone.accounts
        }
        self._cumulative_used: dict[str, float] = {
            trxn.id: 0.0
            for trxn in self.all_trxns
            if type(trxn) == PathTrxn and not trxn.is_slack
        }
        self._prepared_date: str | None = None



    def _init_process_input_trxns(self, input_txns: list['PathTrxn | TrxnGroup']):
        """Given the input txns list, do some neccessary checks and make needed
        modifications. Returns a seperate list, not modifying the origional
        list."""

        trxns = deepcopy(input_txns)

        self._ensure_children_after_parent(trxns)

        self._ensure_slack_trxns_exist(trxns)

        return trxns


    def _ensure_slack_trxns_exist(self, txns: list['PathTrxn | TrxnGroup']):
        """Adds slack transactions to ensure the problem is feasible. This will
        add an extra transaction for each interzone flow (or two if it's
        bidirectional). These slack variables represent things like unauthorized
        diversions to a user from a stream, water spilled from a reservoir or an
        import to the natural system, etc."""

        gm = self.gm

        for f in gm.graph.interzone_flows:

            flow_var_name = f'SLACK_{f.from_zone}_TO_{f.to_zone}_{f.id}'
            slackvar = PathTrxn(
                id=flow_var_name,
                path=[TrxnPathItem(flow_id=f.id, factor=1)],
                upper_limit=None,
                is_slack=True
            )
            txns.append(slackvar)

            if f.bidirectional:
                flow_var_name2 = f'SLACK_{f.to_zone}_TO_{f.from_zone}_{f.id}'
                slackvar2 = PathTrxn(
                    id=flow_var_name2,
                    path=[TrxnPathItem(flow_id=f.id, factor=-1)],
                    upper_limit=None,
                    is_slack=True
                )
                txns.append(slackvar2)


    def _ensure_children_after_parent(self, txns: list['PathTrxn | TrxnGroup'], parent_priority: float | None = None):
        """Recursively shifts child priorities to ensure they solve immediately after their parent."""
        for t in txns:
            # Shift the child priority if it is less than or equal to the parent's priority
            if parent_priority is not None and t.priority is not None and t.priority <= parent_priority:
                new_priority = parent_priority + 1e-5
                logger.warning(f"WARNING: Child transaction '{t.id}' priority ({t.priority}) "
                        f"equals or precedes parent priority ({parent_priority}). "
                        f"Adjusting '{t.id}' priority to {new_priority}.")
                t.priority = new_priority


            # Recurse for nested groups using the established priority
            if type(t) == TrxnGroup:
                self._ensure_children_after_parent(t.children_trxns, t.priority)


    def _init_build_ordered_paths(self) -> dict[str, list[TrxnPathItem]]:
        """Build a collection keyed by trxn id that provides ordered, verified
        paths"""
        output = {}
        for t in self.all_trxns:
            if type(t) == PathTrxn:
                if len(t.path) > 0:
                    output[t.id] = self._get_ordered_path(t)
                else:
                    output[t.id] = []
        return output


    def _get_ordered_path(self, trxn: PathTrxn) -> list[TrxnPathItem]:
        if not trxn.path:
            return []

        next_by_zone: dict[str, tuple[str, TrxnPathItem]] = {}
        for x in trxn.path:
            flow = self.gm.get_flow_by_id(x.flow_id)
            if x.factor >= 0:
                from_zone = flow.from_zone
                to_zone = flow.to_zone
            else:
                from_zone = flow.to_zone
                to_zone = flow.from_zone

            # A transaction path should not branch.
            if from_zone in next_by_zone:
                raise ValueError(
                    f"Transaction {trxn.id!r} branches at zone "
                    f"{from_zone!r}."
                )

            next_by_zone[from_zone] = (to_zone, x)

        starts = set(next_by_zone.keys())
        ends = set(v[0] for v in next_by_zone.values())
        roots = starts - ends

        if len(roots) != 1:
            raise ValueError(
                f"Transaction {trxn.id!r} does not form one continuous path."
            )

        ordered = []
        zone = next(iter(roots))

        while zone in next_by_zone:
            zone, item = next_by_zone[zone]
            ordered.append(item)

        if len(ordered) != len(trxn.path):
            raise ValueError(
                f"Transaction {trxn.id!r} does not form one continuous path."
            )

        return ordered


    def _init_natural_flow_trxns(self) -> dict[str, list[PathTrxn]]:
        """Set up a lookup table that will make it easy to look up the set of
        trxns that originate from a given natural flow zone."""

        output: dict[str, list[PathTrxn]] = {}

        for zone in self.gm.graph.zones:                                       # TODO - consider requiring input to define account objects to assist with this...
            if zone.type == ZoneTypes.STREAM:
                output[zone.id] = []

        for trxn in self.all_trxns:
            if type(trxn) != PathTrxn:
                continue

            if trxn.is_slack:
                continue

            from_zone = self.get_from_zone(trxn)

            if from_zone and from_zone.type == ZoneTypes.STREAM:
                output[from_zone.id].append(trxn)

        return output


    def _init_build_flow_trxns_lookup(self) -> dict[str, list[tuple[PathTrxn, TrxnPathItem]]]:
        lookup = {f.id: [] for f in self.gm.graph.interzone_flows}
        for t in self.all_trxns:
            if type(t) == PathTrxn:
                for x in t.path:
                    lookup[x.flow_id].append((t, x))
        return lookup




    @staticmethod
    def traverse_vars(vars: list[PathTrxn | TrxnGroup]) -> Iterator[PathTrxn | TrxnGroup]:
        """Recursively yields all transactions, including nested children."""
        for v in vars:
            yield v
            if isinstance(v, TrxnGroup):
                yield from TrxnSchedule.traverse_vars(v.children_trxns)


    @staticmethod
    def _validate(trxns:list[PathTrxn | TrxnGroup], gm:GraphManager) -> None:
        """Raises a ValueError if:
         - trxn ids are not unique
         - interzone-flow references are not valid
        """

        all_trxns = list(TrxnSchedule.traverse_vars(trxns))

        # Transaction IDs must be unique.
        seen = set()
        for trxn in all_trxns:
            if trxn.id in seen:
                raise ValueError(f"Duplicate transaction id: {trxn.id!r}")
            seen.add(trxn.id)

        # Interzone-flow references must exist.
        for trxn in all_trxns:
            if isinstance(trxn, PathTrxn):
                for item in trxn.path:
                    try:
                        gm.get_flow_by_id(item.flow_id)
                    except ValueError:
                        raise ValueError(
                            f"Transaction {trxn.id!r} references "
                            f"unknown interzone-flow {item.flow_id!r}."
                        )


    def _validate_account_references(self) -> None:
        """Validate zone-local from_account/to_account references."""
        for trxn in self.all_trxns:
            if type(trxn) != PathTrxn or trxn.is_slack:
                continue

            if trxn.from_account is not None:
                from_zone = self.get_from_zone(trxn)
                if from_zone is None:
                    raise ValueError(
                        f"Transaction {trxn.id!r} has from_account "
                        f"{trxn.from_account!r} but no path/source zone."
                    )
                try:
                    self.gm.get_zone_account(from_zone.id, trxn.from_account)
                except ValueError as exc:
                    raise ValueError(
                        f"Transaction {trxn.id!r} references from_account "
                        f"{trxn.from_account!r}, which is not defined in its "
                        f"source zone {from_zone.id!r}."
                    ) from exc

            if trxn.to_account is not None:
                to_zone = self.get_to_zone(trxn)
                if to_zone is None:
                    raise ValueError(
                        f"Transaction {trxn.id!r} has to_account "
                        f"{trxn.to_account!r} but no path/destination zone."
                    )
                try:
                    self.gm.get_zone_account(to_zone.id, trxn.to_account)
                except ValueError as exc:
                    raise ValueError(
                        f"Transaction {trxn.id!r} references to_account "
                        f"{trxn.to_account!r}, which is not defined in its "
                        f"destination zone {to_zone.id!r}."
                    ) from exc


    def begin_day(self, date: str) -> None:
        """Prepare cross-day account/cumulative state for ``date``.

        Cumulative limits reset before the solve on their configured reset day.
        Calling this more than once for the same date is harmless.
        """
        if self._prepared_date == date:
            return

        current = Date.fromisoformat(date)
        mmdd = current.strftime('%m%d')

        for trxn in self.all_trxns:
            if type(trxn) != PathTrxn or trxn.is_slack:
                continue
            if trxn.cumulative_reset_before_MMDD is None:
                continue

            reset_mmdd = trxn.cumulative_reset_before_MMDD.replace('-', '')
            if reset_mmdd == mmdd:
                self._cumulative_used[trxn.id] = 0.0

        self._prepared_date = date


    def commit_day(self, variable_values: dict[str, float]) -> None:
        """Commit final daily allocations to cumulative and account state."""
        if self._prepared_date is None:
            raise ValueError('begin_day() must be called before commit_day().')

        for trxn in self.all_trxns:
            if type(trxn) != PathTrxn or trxn.is_slack:
                continue

            ordered_path = self.ordered_paths.get(trxn.id, [])
            if not ordered_path:
                continue

            anchor_var = self.get_anchor_var(trxn)
            if anchor_var is None:
                continue
            allocation = max(0.0, float(variable_values.get(anchor_var, 0.0)))

            if trxn.cumulative_limit is not None:
                self._cumulative_used[trxn.id] = (
                    self._cumulative_used.get(trxn.id, 0.0) + allocation
                )

            if trxn.from_account is not None:
                from_zone = self.get_from_zone(trxn)
                if from_zone is None:
                    raise ValueError(f"Cannot resolve source zone for {trxn.id!r}.")
                key = (from_zone.id, trxn.from_account)
                source_var = self.get_from_account_var(trxn)
                debit = max(0.0, float(variable_values.get(source_var, 0.0)))
                self._account_balances[key] -= debit

            if trxn.to_account is not None:
                to_zone = self.get_to_zone(trxn)
                if to_zone is None:
                    raise ValueError(f"Cannot resolve destination zone for {trxn.id!r}.")
                key = (to_zone.id, trxn.to_account)
                destination_var = self.get_to_account_var(trxn)
                credit = max(0.0, float(variable_values.get(destination_var, 0.0)))
                self._account_balances[key] += credit


    def get_account_balance(self, zone_id: str, account_id: str) -> float:
        key = (zone_id, account_id)
        if key not in self._account_balances:
            self.gm.get_zone_account(zone_id, account_id)  # raises a useful error
            raise ValueError(f"Account state was not initialized for {key!r}.")
        return self._account_balances[key]


    def get_account_outgoing_trxns(self, zone_id: str, account_id: str) -> list[PathTrxn]:
        output = []
        for trxn in self.all_trxns:
            if type(trxn) != PathTrxn or trxn.is_slack or trxn.from_account != account_id:
                continue
            from_zone = self.get_from_zone(trxn)
            if from_zone is not None and from_zone.id == zone_id:
                output.append(trxn)
        return output


    def get_account_incoming_trxns(self, zone_id: str, account_id: str) -> list[PathTrxn]:
        output = []
        for trxn in self.all_trxns:
            if type(trxn) != PathTrxn or trxn.is_slack or trxn.to_account != account_id:
                continue
            to_zone = self.get_to_zone(trxn)
            if to_zone is not None and to_zone.id == zone_id:
                output.append(trxn)
        return output


    def get_from_account_var(self, trxn: PathTrxn) -> str:
        anchor = self.get_anchor_var(trxn)
        if anchor is None:
            raise ValueError(f"Transaction {trxn.id!r} has no source path variable.")
        return anchor


    def get_to_account_var(self, trxn: PathTrxn) -> str:
        path = self.ordered_paths.get(trxn.id, [])
        if not path:
            raise ValueError(f"Transaction {trxn.id!r} has no destination path variable.")
        return f"{trxn.id}___{path[-1].flow_id}"


    def get_cumulative_used(self, trxn: TrxnBaseClass) -> float:
        return self._cumulative_used.get(trxn.id, 0.0)


    def get_cumulative_remaining(self, trxn: TrxnBaseClass) -> float | None:
        if trxn.cumulative_limit is None:
            return None
        return max(
            0.0,
            float(trxn.cumulative_limit) - self.get_cumulative_used(trxn),
        )


    def get_nf_trxn_ids_for_zone(self, zone_id:str) -> list[PathTrxn]:
        if zone_id in self._natural_flow_trxns:
            return self._natural_flow_trxns[zone_id]
        else:
            return []

    def get_nf_zone_id(self, trxn) -> str | None:
        from_zone = self.get_from_zone(trxn)
        if from_zone is not None and from_zone.id in self._natural_flow_trxns:
            return from_zone.id
        return None

    def get_from_zone(self, trxn: PathTrxn) -> Zone | None:
        ordered_path = self.ordered_paths.get(trxn.id, [])
        if not ordered_path:
            return None

        first_item = ordered_path[0]
        flow = self.gm.get_flow_by_id(first_item.flow_id)

        zone_id = (
            flow.from_zone
            if first_item.factor > 0
            else flow.to_zone
        )

        return self.gm.get_zone_by_id(zone_id)


    def get_to_zone(self, trxn: PathTrxn) -> Zone | None:
        ordered_path = self.ordered_paths.get(trxn.id, [])
        if not ordered_path:
            return None

        last_item = ordered_path[-1]
        flow = self.gm.get_flow_by_id(last_item.flow_id)

        zone_id = (
            flow.to_zone
            if last_item.factor > 0
            else flow.from_zone
        )

        return self.gm.get_zone_by_id(zone_id)


    def get_anchor_var(self, trxn: PathTrxn) -> str | None:
        path = self.ordered_paths.get(trxn.id, [])
        if path:
            return f"{trxn.id}___{path[0].flow_id}"
        return None


    def _resolve_limit_value(
        self,
        limit: float | AccountingLimit | None,
        date: str,
        *,
        use_default_when_none: bool = False,
    ) -> float | None:
        if type(limit) == AccountingLimit:
            value = 0.0
            for intv in limit.intervals:
                if date >= intv.beg_date and date < intv.end_date:
                    value = float(intv.value)
                    break
            return value

        if isinstance(limit, (int, float)) and not isinstance(limit, bool):
            return float(limit)

        if limit is None:
            return self._max_daily_apportionment if use_default_when_none else None

        raise ValueError(
            'limit must be an AccountingLimit, int, float, or None!'
        )


    def get_transaction_limit_info(
        self,
        t: PathTrxn | TrxnGroup,
        date: str | None,
    ) -> tuple[float | None, str | None]:
        """Return the effective daily limit and the field that governs it."""
        if date is None:
            raise ValueError('date not valid')

        upper_limit = self._resolve_limit_value(
            t.upper_limit,
            date,
            use_default_when_none=True,
        )
        effective = upper_limit
        source = 'UPPER_LIMIT' if upper_limit is not None else None

        call_limit = self._resolve_limit_value(t.call_limit, date)
        if call_limit is not None and (effective is None or call_limit < effective):
            effective = call_limit
            source = 'CALL_LIMIT'

        cumulative_remaining = self.get_cumulative_remaining(t)
        if (
            cumulative_remaining is not None
            and (effective is None or cumulative_remaining < effective)
        ):
            effective = cumulative_remaining
            source = 'CUMULATIVE_LIMIT'

        return effective, source


    def get_transaction_upper_limit(
        self,
        t: PathTrxn | TrxnGroup,
        date: str | None,
    ) -> float | None:
        """Return the effective daily cap after upper/call/cumulative limits."""
        return self.get_transaction_limit_info(t, date)[0]


    def get_minus_vars(self, vars: list[PathTrxn | TrxnGroup]) -> list[PathTrxn]:

        def get_from(v: PathTrxn) -> tuple[Zone, InterzoneFlow]:
            path = self.ordered_paths[v.id]
            first_item = path[0]

            f0 = self.gm.get_flow_by_id(first_item.flow_id)
            if first_item.factor >= 0:
                from_zone = self.gm.get_zone_by_id(f0.from_zone)
            else:
                from_zone = self.gm.get_zone_by_id(f0.to_zone)
            return from_zone, f0

        def get_to(v:PathTrxn) -> tuple[Zone, InterzoneFlow]:
            path = self.ordered_paths[v.id]
            last_item = path[-1]

            fl = self.gm.get_flow_by_id(last_item.flow_id)
            if last_item.factor < 0:
                to_zone = self.gm.get_zone_by_id(fl.from_zone)
            else:
                to_zone = self.gm.get_zone_by_id(fl.to_zone)
            return to_zone, fl

        output: list[PathTrxn]  = []

        for v in vars:
            if type(v) == PathTrxn:
                if len(v.path) > 0:

                    # If the variable starts at a storage zone, we need to look
                    # for slack variables flowing into that storage zone.
                    from_zone, from_flow = get_from(v)

                    if from_flow.bidirectional:
                        for trxn, path_item in self.lookup_flow_trxns[from_flow.id]:
                            ordered_trxn = self.ordered_paths.get(trxn.id, [])
                            # Use len(ordered_trxn) to ensure loss expansions don't hide the slack status
                            if trxn.is_slack and len(ordered_trxn) == 1:
                                if get_to(trxn)[0] == from_zone:
                                    output.append(trxn)

                    # If the variable ends at a storage zone, we need to look
                    # for slack variables flowing from that storage zone.
                    to_zone, to_flow = get_to(v)
                    if to_flow.bidirectional:
                        for trxn, path_item in self.lookup_flow_trxns[to_flow.id]:
                            if get_from(trxn)[0] == to_zone:
                                output.append(trxn)
        return output


    def build_schedule(self, date:str) -> CoreSeqSchedule:
        # Convert the paths dictionary to an ordered schedule list by sorting the
        # paths by priority while grouping paths with the same priority.
        vars = self.all_trxns

        output_list: list[CoreSeqScheduleItem] = []

        varsByPriority: dict[float,list['PathTrxn | TrxnGroup']]  = {}
        for v in vars:
            p = v.priority if v.priority is not None else -1
            if p not in varsByPriority:
                varsByPriority[p] = []
            varsByPriority[p].append(v)

        # Get a list of the distinct priority values sorted from smallest to
        # largest.
        priorities = sorted(varsByPriority.keys())

        # Now add the variables to the schedule, in priority order.
        for p in priorities:
            pvars = varsByPriority[p]

            # If there is only one item with this priority, it must be either a
            # variable or a sequential subseries:
            if len(pvars) == 1:
                item = CoreScheduleVariable(var=pvars[0])
                output_list.append(CoreSeqScheduleItem(priority=p, item=item))


            # Otherwise, it must be a proportional subseries:
            elif len(pvars) > 1:
                nested_sched = None

                # Look for any variables that do not have an upper limit. Increase
                # these together in equal-proportion before increasing the other
                # variables that do have a limit.
                unlimited_vars:list[PathTrxn] = []
                for v in pvars:
                    ub = self.get_transaction_upper_limit(v, date)
                    if ub is None:
                        if type(v) != PathTrxn:
                            raise NotImplementedError('Trxn Groups with no upper limit are not supported!')
                        unlimited_vars.append(v)

                if len(unlimited_vars) > 0:
                    nested_sched = CoreSeqSchedule(series=[])
                    nested_sched.series.append(CoreSeqScheduleItem(
                        priority=1,
                        item=CorePropSchedule(
                            series=[CorePropScheduleItem(
                                factor=1,
                                item=CoreScheduleVariable(var=v)
                            ) for v in unlimited_vars]
                        )
                    ))

                # Deal with variables that do have a limit.
                proportional_subseries: list[CorePropScheduleItem] = []
                cfs_sum = 0
                for v2 in pvars:
                    v2_ub = self.get_transaction_upper_limit(v2, date)
                    if v2_ub is not None:
                        cfs_sum += v2_ub
                        citem = CorePropScheduleItem(
                            factor=v2_ub,
                            item=CoreScheduleVariable(var=v2)
                        )
                        proportional_subseries.append(citem)

                # normalize the factor
                for citem in proportional_subseries:
                    if cfs_sum > 0:
                        citem.factor /= cfs_sum

                if nested_sched is None:
                    output_list.append(CoreSeqScheduleItem(
                        priority=p,
                        item=CorePropSchedule(series=proportional_subseries)
                    ))
                else:
                    nested_sched.series.append(CoreSeqScheduleItem(
                        priority=1,
                        item=CorePropSchedule(series=proportional_subseries)
                    ))
                    output_list.append(CoreSeqScheduleItem(
                        priority=p,
                        item=nested_sched
                    ))

        return CoreSeqSchedule(series=output_list)


    def get_path_item(self, trxn_id, interzone_flow_id):
        """Return the path-item object for the given trxn passing the given
        interzone flow."""

        # Strip suffixes from dynamically generated stream slack splits
        base_id = trxn_id
        if trxn_id.endswith('_NF'):
            base_id = trxn_id[:-3]
        elif trxn_id.endswith('_CPI'):
            base_id = trxn_id[:-4]

        for trxn, path_item in self.lookup_flow_trxns[interzone_flow_id]:
            if trxn.id == base_id:
                return path_item

        raise ValueError(f'path_item not found for trxn_id={trxn_id}, interzone_flow_id={interzone_flow_id}')

