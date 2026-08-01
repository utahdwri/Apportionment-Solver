from dataclasses import dataclass, field
from enum import Enum
import numpy as np


DEFAULT_TRXN_PRIORITY = 999999999
SLACK_TRXN_PRIORITY = 1e100


'''
4/14/2026 - DJJONES
The intent of this file is to create stand-alone classes so the solver can be
used outside of the context of the container app, if neccessary. This is why
many of the classes defined here are very duplicative of classes defined in the
container app.
'''


@dataclass
class SolverInput:
    accounting_graph: 'AccountingGraph'
    txns: 'list[Trxn | TrxnGroup]'
    measurements: dict[str, list[float | None]]
    beg_date: str
    end_date: str
    measurement_beg_date: str
    measurement_end_date: str

    # 2026-07-17: Maps flow_id -> list of daily natural flow values matching the date range
    external_natural_flows: dict[str, dict[str, float]] = field(default_factory=dict)



@dataclass
class SolverOutput:
    apportionments: list['SolverOutputApportionment']

    def get_result_value(self,
                         date:str|None=None,
                         trxn_id:str|None=None,
                         flow_id:str|None=None
                         ) -> list['SolverOutputApportionment']:
        output = []
        for i in self.apportionments:
            if ((i.date == date or date is None ) and
                (i.interzone_flow_id == flow_id or flow_id is None) and
                (i.txn_id == trxn_id or trxn_id is None)):

                output.append(i)

        return output



@dataclass
class SolverOutputApportionment:
    date: str
    interzone_flow_id: str
    txn_id: str
    value: float # A negative value indicates flow in the backward direction - but if it is zero then we need the following:
    is_forward: bool
    reason: str | None = None  # New field to identify the limiting factor



@dataclass
class AccountingGraph:
    zones: list['Zone']
    interzone_flows: list['InterzoneFlow']


@dataclass
class Zone:
    id: str
    type: 'ZoneTypes'
    storage_meas_ids: list[str] = field(default_factory=list)

    loss: 'LossCurve | None' = None               #~
    loss_flow: 'InterzoneFlow | None' = None      #~ or could be defined on the InterzoneFlow?
    residual_flow: 'InterzoneFlow | None' = None  #~ or could be defined on the InterzoneFlow?


@dataclass
class LossCurvePoint: #~
    inflow: float
    loss: float

@dataclass
class LossCurve: #~
    points: list[LossCurvePoint]

    def __post_init__(self):
        # 1. Sort points by inflow to ensure np.interp works correctly
        self.points.sort(key=lambda p: p.inflow)

    def get_loss(self, x) -> float:
        """Given zone inflow (x), return the loss."""

        if len(self.points) > 0:

            # 1. Handle flows below the minimum defined breakpoint (interpolate from (0, 0))
            if x <= self.points[0].inflow:
                if self.points[0].inflow == 0:
                    return self.points[0].loss
                return self.points[0].loss / self.points[0].inflow * x

            # 2. Handle flows beyond the maximum breakpoint (ceiling)
            if x >= self.points[-1].inflow:
                return self.points[-1].loss

            # 3. Interpolate between known points for everything else
            for i in range(len(self.points) - 1):
                p1 = self.points[i]
                p2 = self.points[i + 1]

                if p1.inflow <= x <= p2.inflow:
                    dx = p2.inflow - p1.inflow
                    slope = (p2.loss - p1.loss) / dx if dx != 0 else 0
                    return p1.loss + slope * (x - p1.inflow)

        return 0

    def get_loss_when_inflow_is_residual(self, outflow: float) -> float:
        """Given zone net outflow, return the loss."""

        if len(self.points) > 0:
            # Calculate the outflow coordinate for the first breakpoint
            outflow0 = self.points[0].inflow - self.points[0].loss

            # 1. Handle flows below the minimum defined breakpoint (interpolate from 0,0)
            if outflow <= outflow0:
                # If outflow0 is 0, it means 100% loss occurred up to this point.
                # When outflow is 0, loss could mathematically be anything between 0
                # and points[0].loss, but 0 is the safest physical assumption.
                if outflow0 <= 0:
                    return 0
                return (self.points[0].loss / outflow0) * outflow

            # Calculate the outflow coordinate for the last breakpoint
            outflow_max = self.points[-1].inflow - self.points[-1].loss

            # 2. Handle flows beyond the maximum breakpoint (ceiling)
            if outflow >= outflow_max:
                return self.points[-1].loss

            # 3. Interpolate between known points in "outflow space"
            for i in range(len(self.points) - 1):
                p1 = self.points[i]
                p2 = self.points[i + 1]

                # Convert inflow breakpoints to outflow breakpoints
                outflow1 = p1.inflow - p1.loss
                outflow2 = p2.inflow - p2.loss

                if outflow1 <= outflow <= outflow2:
                    dx = outflow2 - outflow1
                    slope = (p2.loss - p1.loss) / dx if dx != 0 else 0
                    return p1.loss + slope * (outflow - outflow1)

        return 0


class ZoneTypes(Enum):
    STREAM = 'stream'
    STORAGE = 'storage'
    USE = 'use'
    IMPORT = 'import'
    SYSTEM_GAIN_LOSS = 'system-gain-loss'
    DEPLETION = 'depletion'


class FlowComponentsTypes(Enum):
    OBSERVATION = 'OBSERVATION'
    FLOW_BALANCE_OF_DESTINATION_ZONE = 'DESTINATION ZONE'
    FLOW_BALANCE_OF_SOURCE_ZONE = 'SOURCE ZONE'
    OVERLAPPING_SERVICE_AREAS = 'OVERLAPPING SERVICE AREAS'
    EMPTY = 'EMPTY'                                                            # TODO - This should not be an option long-term...
                                                                               #      - It's here to support legacy techniques that should be updated.
@dataclass
class FlowMeasurement: # This is a new class.
    measurement_id: str
    adjustment_factor: float = 1

# Old code that has been replaced:
#
#@dataclass
#class FlowComponents: # This is an old class that I want to stop using.
#    flow_type: 'FlowComponentsTypes'
#    measurement_id: str | None = None
#    loss_factor: float = 0
#    gain_factor: float = 0

@dataclass
class InterzoneFlow:
    id: str
    from_zone: str
    to_zone: str
    bidirectional: bool = False

    #
    # New stuff:
    #
    residual_for_gains: bool = False  # This replaced the gain_factor 0/1 value
    residual_for_losses: bool = False # This replaced the loss_factor 0/1 value
    flow_type: FlowComponentsTypes = FlowComponentsTypes.OBSERVATION
    flow_measurements: list[FlowMeasurement]= field(default_factory=list)

    # Not implemented yet
    lag_from_zone: float = 0
    lag_to_zone: float = 0
    loss_from_zone: float = 0 # the fraction of flow lost before the observation (0-1)
    loss_to_zone: float = 0 # the fraction of flow lost after the observation (0-1)

    # Not implemented yet
    nf_measurements: list['FlowMeasurement'] = field(default_factory=list) # gains/loss flows are natural flows
                                                                                # stream flows have a routed nf
                                                                                # if there are % losses on a stream, that should be part of the routed calc of nf.
                                                                                # diversions have zero nf.
                                                                                # imports can have a non-zero nf, but will figure out how to input that later...

    # Not implemented yet - An InterzoneFlow may not neccessaraly be active for the entire run period.
    beg_date: str = '1000-01-01'
    end_date: str = '9999-12-31'


@dataclass
class StorageAccount:
    name: str
    balance: float


@dataclass
class Trxn:
    id: str
    path: list['TrxnPathItem']
    upper_limit: 'float | AccountingLimit | None'
    priority: float = -1
    max_acft: float | None = None
    from_account: StorageAccount | None = None
    to_account: StorageAccount | None = None
    beg_date: str | None = None
    end_date: str | None = None
    lower_limit: float = 0
    is_slack: bool = False

    limit_by_remaining_account_balance: bool = False #~
    # What if we want to use the remaining_account_balance as the equal-priority proportion factor?




    def __post_init__(self):

        if self.priority < 0 or self.priority > DEFAULT_TRXN_PRIORITY:
            self.priority = DEFAULT_TRXN_PRIORITY

        if self.is_slack:
            self.priority = SLACK_TRXN_PRIORITY





    def init_losses(self, apportioner):                                        # TODO - after consolidating, the new TrxnPathItem objects don't point to the interzone-flow objects but their id. Need a way to make this function work...

        ''' 4/16/2026 - not included after consolidation
        # Add references to this Var to each traversed Arc.
        for i in self.path:
            if i.factor > 0:
                i.flow.forward_vars.append((self, i))
            if i.factor < 0:
                i.flow.backward_vars.append((self, i))'''

        # If there are any loss factors for the path, we need to calculate a
        # new factor for each path item that represents the portion remaining.
        # To do this, we will need get the ordered (chained) path.
        any_loss = False
        for path_item in self.path:
            if path_item.loss_after != 0 or path_item.loss_before != 0:
                any_loss = True
        if any_loss:
            ordered_path = self._get_ordered_path(apportioner)
            rem_factor = 1
            for path_item in ordered_path:
                rem_factor = path_item.init_remaining_factor(rem_factor)
            print(ordered_path)


    def _get_ordered_path(self, apportioner) -> 'list[TrxnPathItem]':
        """Get the sorted list of paths."""

        sorted_list: list[TrxnPathItem] = []

        # Populate a dictionary for fast lookup
        lookup_next: dict[str, tuple[Zone, TrxnPathItem]] = {}
        for x in self.path:
            to_zone = apportioner.get_flow_by_id(x.flow_id).to_zone
            from_zone = apportioner.get_flow_by_id(x.flow_id).from_zone
            if x.factor == 1:
                lookup_next[from_zone.id] = (to_zone, x)
            if x.factor == -1:
                lookup_next[to_zone.id] = (from_zone, x)

        # Now find the head of the chain - the item that ...
        starts = set([zone_id for zone_id in lookup_next])
        ends = set([lookup_next[zone_id][0].id for zone_id in lookup_next])
        root_candidates = list(starts - ends)

        if len(root_candidates) != 1:
            raise ValueError("Invalid chain: Multiple starts or a circular "
                             "loop detected.")

        # Now loop to populate the ordered output
        current_key = root_candidates[0]
        while len(sorted_list) < len(self.path):
            if current_key not in lookup_next:
                raise ValueError(f"Chain broken at value: {current_key}")

            path_item = lookup_next[current_key][1]
            sorted_list.append(path_item)

            # Move to the next
            current_key = lookup_next[current_key][0].id

        return sorted_list


    def is_spill(self, apportioner) -> bool:
        # A var has is_spill=True when it represents water under                   # TODO - get rid of is_spill!
        # the name of a user being released back to the natural
        # system, e.g. the slack variable representing reservoir
        # releases with no downstream diversion or imports with no
        # downstream diversion.

        last_item = self.path[-1]
        last_flow = apportioner.get_flow_by_id(last_item.flow_id)
        to_zone = apportioner.get_zone_by_id(last_flow.to_zone)
        if last_item.factor < 0:
            to_zone = apportioner.get_zone_by_id(last_flow.from_zone)

        first_item = self.path[0]
        first_flow = apportioner.get_flow_by_id(first_item.flow_id)
        from_zone = apportioner.get_zone_by_id(first_flow.from_zone)
        if first_item.factor < 0:
            from_zone = apportioner.get_zone_by_id(first_flow.to_zone)


        # If the flow variable goes from a non-source to a source, set
        # the spill flag.
        from_a_nonsource = ( from_zone.type != ZoneTypes.STREAM and
                            from_zone.type != ZoneTypes.SYSTEM_GAIN_LOSS )
        to_a_source = ( to_zone.type == ZoneTypes.STREAM )

        return (from_a_nonsource and to_a_source)


@dataclass
class TrxnPathItem:
    flow_id: str
    factor: float = 1                                          # previously 'direction_factor'

    # Only used for unit tests to check if the calculated value is correct
    expected_values: list[float] | None = None

    # The fraction (0-1) of the flow that is lost before the flow of this path
    # item. (If the direction-factor is 1, this means the loss occurs at the
    # from-zone.)
    loss_before: float = 0

    # The fraction (0-1) of the flow that is lost to immediately after the flow
    # of this path item. (If the direction-factor is 1, this means the loss
    # occurs at to-zone.)
    loss_after: float = 0

    # This should not be set directly
    remaining_factor: float = 1

    def init_remaining_factor(self, rem_factor:float) -> float:
        """This function is meant to be called in sequence for all of the path
        items in a trxn, so we get the remaining factor given all preceding
        losses. """
        self.remaining_factor = rem_factor * (1-self.loss_before)
        return self.remaining_factor * (1-self.loss_after)


@dataclass
class AccountingLimit:
    intervals: list['AccountingLimitInterval']

@dataclass
class AccountingLimitInterval:
    beg_date: str
    end_date: str
    value: float



@dataclass
class TrxnGroup:
    """A constraint that applies to a group of Transactions.
    """
    id: str
    children_trxns: list['Trxn | TrxnGroup']
    wrnum: str | None
    priority: float | None
    upper_limit: 'float | AccountingLimit | None'
    lower_limit: float | None = 0
    max_acft: float | None = None
    comment: str | None = None
    beg_date: str | None = None
    end_date: str | None = None




#
# Classes used for purposes internal to the solver.
#.


@dataclass
class CoreScheduleVariable:
    var: Trxn | TrxnGroup

    def __str__(self):
        return f'ScheduleVariable: {self.var.id}'


@dataclass
class CoreSeqSchedule:
    """ """
    series: list['CoreSeqScheduleItem']

    def __str__(self):

        def tab(s:str):
            return s.replace('\n', '\n...')
        return (f'SeqSchedule: ' +
                tab(''.join('\n('+str(idx+1)+'). '+str(i)
                            for idx, i in enumerate(self.series))))


@dataclass
class CorePropSchedule:
    """ """
    series: list['CorePropScheduleItem']

    def __str__(self):

        def tab(s:str):
            return s.replace('\n', '\n...')
        return (f'PropSchedule: ' +
                tab(''.join('\n(*). '+str(i)
                            for idx, i in enumerate(self.series))))


@dataclass
class CoreSeqScheduleItem:
    """A variable or sub-schedule along with the sequential priority."""
    priority: float
    item: CoreScheduleVariable | CoreSeqSchedule | CorePropSchedule

    def __str__(self):
        return f'SeqScheduleItem: priority={self.priority}, item={self.item}'


@dataclass
class CorePropScheduleItem:
    """A variable or sub-schedule along with a proportionality factor."""
    factor: float
    item: CoreScheduleVariable | CoreSeqSchedule | CorePropSchedule

    def __str__(self):
        return f'PropScheduleItem: factor={self.factor}, item={self.item}'


