from collections.abc import Iterator
from copy import deepcopy
from .models import (
    AccountingGraph, AccountingLimit, CorePropSchedule, CorePropScheduleItem,
    CoreScheduleVariable, CoreSeqSchedule, CoreSeqScheduleItem,
    InterzoneFlow,
    Trxn, TrxnGroup, TrxnPathItem, Zone, ZoneTypes
)
from .graph_manager import GraphManager


# Set up logging.
import logging
logger = logging.getLogger(__name__)



class TrxnSchedule:
    """Manage a collection of transactions."""

    def __init__(self, gm: GraphManager, txns:list[Trxn | TrxnGroup], max_daily_apportionment:float|None=None):

        self._validate(txns, gm)

        self.gm = gm
        p_trxns = self._process_input_trxns(txns)
        self.all_trxns = list(self.traverse_vars(p_trxns))
        self._max_daily_apportionment = max_daily_apportionment

        self.ordered_paths: dict[str, list[TrxnPathItem]] = {}
        for t in self.all_trxns:
            if type(t) == Trxn:
                if len(t.path) > 0:
                    self.ordered_paths[t.id] = self._get_ordered_path(t)
                else:
                    self.ordered_paths[t.id] = []

        self.lookup_flow_trxns = self._build_flow_trxns_lookup()


    @staticmethod
    def traverse_vars(vars: list[Trxn | TrxnGroup]) -> Iterator[Trxn | TrxnGroup]:
        """Recursively yields all transactions, including nested children."""
        for v in vars:
            yield v
            if isinstance(v, TrxnGroup):
                yield from TrxnSchedule.traverse_vars(v.children_trxns)


    @staticmethod
    def _validate(trxns:list[Trxn | TrxnGroup], gm:GraphManager) -> None:
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
            if isinstance(trxn, Trxn):
                for item in trxn.path:
                    try:
                        gm.get_flow_by_id(item.flow_id)
                    except ValueError:
                        raise ValueError(
                            f"Transaction {trxn.id!r} references "
                            f"unknown interzone-flow {item.flow_id!r}."
                        )


    def _process_input_trxns(self, input_txns: list['Trxn | TrxnGroup']):
        """Given the input txns list, do some neccessary checks and make needed
        modifications. Returns a seperate list, not modifying the origional
        list."""

        trxns = deepcopy(input_txns)

        self._ensure_children_after_parent(trxns)

        self._ensure_slack_trxns_exist(trxns)

        return trxns


    def _ensure_slack_trxns_exist(self, txns: list['Trxn | TrxnGroup']):
        """Adds slack transactions to ensure the problem is feasible. This will
        add an extra transaction for each interzone flow (or two if it's
        bidirectional). These slack variables represent things like unauthorized
        diversions to a user from a stream, water spilled from a reservoir or an
        import to the natural system, etc."""

        gm = self.gm

        for f in gm.graph.interzone_flows:

            flow_var_name = f'SLACK_{f.from_zone}_TO_{f.to_zone}_{f.id}'
            slackvar = Trxn(
                id=flow_var_name,
                path=[TrxnPathItem(flow_id=f.id, factor=1)],
                upper_limit=None,
                is_slack=True
            )
            txns.append(slackvar)

            if f.bidirectional:
                flow_var_name2 = f'SLACK_{f.to_zone}_TO_{f.from_zone}_{f.id}'
                slackvar2 = Trxn(
                    id=flow_var_name2,
                    path=[TrxnPathItem(flow_id=f.id, factor=-1)],
                    upper_limit=None,
                    is_slack=True
                )
                txns.append(slackvar2)


    def _ensure_children_after_parent(self, txns: list['Trxn | TrxnGroup'], parent_priority: float | None = None):
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


    def _get_ordered_path(self, trxn: Trxn) -> list[TrxnPathItem]:
        if not trxn.path:
            return []

        # Ignore loss branches for building the primary mathematical sequence
        main_path = []
        for x in trxn.path:
            flow = self.gm.get_flow_by_id(x.flow_id)
            to_z = self.gm.get_zone_by_id(flow.to_zone)
            if to_z.type != ZoneTypes.SYSTEM_GAIN_LOSS:
                main_path.append(x)

        if len(main_path) <= 1:
            return main_path

        lookup_next = {}
        for x in main_path:
            flow = self.gm.get_flow_by_id(x.flow_id)
            if x.factor >= 0:
                lookup_next[flow.from_zone] = (flow.to_zone, x)
            else:
                lookup_next[flow.to_zone] = (flow.from_zone, x)

        starts = set(lookup_next.keys())
        ends = set(v[0] for v in lookup_next.values())
        root_candidates = list(starts - ends)

        if len(root_candidates) != 1:
            return main_path

        sorted_list = []
        current_key = root_candidates[0]
        while len(sorted_list) < len(main_path):
            if current_key not in lookup_next: break
            next_zone, path_item = lookup_next[current_key]
            sorted_list.append(path_item)
            current_key = next_zone

        if len(sorted_list) != len(main_path):
            return main_path
        return sorted_list

    def get_anchor_var(self, trxn: Trxn) -> str | None:
        path = self.ordered_paths.get(trxn.id, [])
        if path:
            return f"{trxn.id}___{path[0].flow_id}"
        return None

    def _build_flow_trxns_lookup(self) -> dict[str, list[tuple[Trxn, TrxnPathItem]]]:
        lookup = {f.id: [] for f in self.gm.graph.interzone_flows}
        for t in self.all_trxns:
            if type(t) == Trxn:
                for x in t.path:
                    lookup[x.flow_id].append((t, x))
        return lookup

    def get_transaction_upper_limit(self, t: Trxn | TrxnGroup, date:str|None) -> float | None:

        upper_limit = None

        if date is None:
            raise ValueError('date not valid')

        if type(t.upper_limit) == AccountingLimit:
            upper_limit = 0
            for intv in t.upper_limit.intervals:
                if date >= intv.beg_date and date < intv.end_date:
                    upper_limit = intv.value
                    break

        elif isinstance(t.upper_limit, (int, float)):
            upper_limit = float(t.upper_limit)

        elif t.upper_limit is None:
            upper_limit = None

        else:
            raise ValueError('upper_limit must be an AccountingLimit, int, float, or None!')

        if upper_limit is None:
            upper_limit = self._max_daily_apportionment

        #log(f"TRXN UPPER LIMIT: {t.id} = {upper_limit}")
        return upper_limit

    def get_minus_vars(self, vars: list[Trxn | TrxnGroup]) -> list[Trxn]:

        def get_from(v: Trxn) -> tuple[Zone, InterzoneFlow]:
            first_item = v.path[0]

            f0 = self.gm.get_flow_by_id(first_item.flow_id)
            if first_item.factor >= 0:
                from_zone = self.gm.get_zone_by_id(f0.from_zone)
            else:
                from_zone = self.gm.get_zone_by_id(f0.to_zone)
            return from_zone, f0

        def get_to(v:Trxn) -> tuple[Zone, InterzoneFlow]:
            last_item = v.path[-1]

            fl = self.gm.get_flow_by_id(last_item.flow_id)
            if last_item.factor < 0:
                to_zone = self.gm.get_zone_by_id(fl.from_zone)
            else:
                to_zone = self.gm.get_zone_by_id(fl.to_zone)
            return to_zone, fl

        output: list[Trxn]  = []

        for v in vars:
            if type(v) == Trxn:
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

        varsByPriority: dict[float,list['Trxn | TrxnGroup']]  = {}
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
                unlimited_vars:list[Trxn] = []
                for v in pvars:
                    ub = self.get_transaction_upper_limit(v, date)
                    if ub is None:
                        if type(v) != Trxn:
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

