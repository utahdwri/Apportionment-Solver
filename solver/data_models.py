from dataclasses import dataclass
from enum import Enum

'''
@dataclass
class AccountingNetwork:
    """A collection of information needed to define the accounting problem."""
    zones: list['Zone']
    interzone_flows: list['InterzoneFlow']
    variables: list['ApportionmentSolverVar']


@dataclass
class Zone:
    id: int
    name: str
    type: str


@dataclass
class InterzoneFlow:
    id: int
    from_zone_id: int
    from_zone_name: str
    to_zone_id: int
    to_zone_name: str
    uhd_mapping: 'InterzoneFlowMapping'


@dataclass
class InterzoneFlowMapping:
    positive_flows: list['FlowBoundary']
    negative_flows: list['FlowBoundary']
    on_stream_reservoir_node_id: int


@dataclass
class FlowBoundary:
    measurement_id: int
    flowline_id: int
    lat: float
    lon: float
    dist: float
    name: str
    id: int

    '''

class ZoneTypes(Enum):
    STREAM = 'stream'
    STORAGE = 'storage'
    USE = 'use'
    IMPORT = 'import'
    SYSTEM_GAIN_LOSS = 'system-gain-loss'


@dataclass
class ApportionmentSolverZone:
    """A Node in the apportionment Graph."""
    name: str
    is_source: bool
    type: ZoneTypes
    storage_chg: float = 0
    
    # If this is set, it indicates the node is an on-stream storage node. The 
    # value indicates the name of the stream reach.
    storage_on_reach: str | None = None
    
    def __post_init__(self):
        # Create variables.
        self.inflows:list['ApportionmentSolverArc'] = []
        self.outflows:list['ApportionmentSolverArc'] = []
        self.net_reach_gains:float = 0

    def is_storage_node(self):
        """Return whether or not this node is a storage node."""
        return self.storage_on_reach is not None

    def all_tributary_source_zones(self) -> list['ApportionmentSolverZone']:
        """Return a list including this source zone and all upsteram source zones. 
        Only applicable if this zone is a source zone. 
        """
        tributary_source_zones: list['ApportionmentSolverZone'] = []

        def get_next_upstream_source_zones(
                target_zones: list['ApportionmentSolverZone']
            ) -> list[ApportionmentSolverZone]:
            upstream_source_zones: list[ApportionmentSolverZone] = []
            for target_zone in target_zones:
                for x in target_zone.inflows:
                    if x.from_zone.is_source:
                        upstream_source_zones.append(x.from_zone)
            return upstream_source_zones
        
        if self.is_source:
            next_source_zones:list[ApportionmentSolverZone] = [self]
            while len(next_source_zones) > 0:
                for z in next_source_zones:
                    tributary_source_zones.append(z)
                next_source_zones = get_next_upstream_source_zones(next_source_zones)

        return tributary_source_zones

    def get_gains_losses(self) -> tuple[float, float]:
        gains = 0
        losses = 0

        for flow in self.inflows:
            if flow.flow is None:
                raise ValueError(f'Interzone flow {flow.name} has no value!')
            if flow.from_zone.type == ZoneTypes.SYSTEM_GAIN_LOSS:
                gains += flow.flow
            if flow.to_zone.type == ZoneTypes.SYSTEM_GAIN_LOSS:
                losses += flow.flow

        return gains, losses

@dataclass
class ApportionmentSolverArc:
    """An Arc in the apportionment Graph."""
    name: str
    from_zone: ApportionmentSolverZone
    to_zone: ApportionmentSolverZone
    flow: float | None # (Only arcs related to GAINS, LOSSES, or STORAGE nodes can have a None flow specified initially.)

    def __post_init__(self):
        # Create another variable
        self.forward_vars: list[ApportionmentSolverVar] = []
        self.backward_vars: list[ApportionmentSolverVar]  = []

        # Link the related nodes to this arc.
        self.from_zone.outflows.append(self)
        self.to_zone.inflows.append(self)


@dataclass
class ApportionmentSolverVarPathItem:
    """"""
    arc: ApportionmentSolverArc
    factor: float


@dataclass
class ApportionmentSolverVar:
    """A variable/transaction in the apportionment Graph. 
    These are what we aim to solve for!"""
    name: str
    path_id: int | None
    priority: float | None
    lb: float | None
    ub: float | None
    arc_path: list[ApportionmentSolverVarPathItem]
    value: float | None = 0
    series: str | None = None
    child_series: str | None = None
    expected_value: float | None = None
    other_limited_vars:'ApportionmentSolverVarGroup | None' = None
    is_spill:bool = False # A var has is_spill=True when it represents water under 
                     # the name of a user being released back to the natural 
                     # system, e.g. the slack variable representing reservoir 
                     # releases with no downstream diversion or imports with no 
                     # downstream diversion.



    def __post_init__(self):

        # Add references to this Var to each traversed Arc.
        for i in self.arc_path:
            if i.factor > 0:
                i.arc.forward_vars.append(self)
            if i.factor < 0:
                i.arc.backward_vars.append(self)

        # The following is only applicable to transaction variables,
        # not group-limit varaibles.
        if len(self.arc_path) > 0:

            # Add a pointer to the source zone.
            first_item = self.arc_path[0]
            from_zone = first_item.arc.from_zone
            if first_item.factor < 0:
                from_zone = first_item.arc.to_zone
            self.from_zone: ApportionmentSolverZone = from_zone

            # Add a pointer to the destination zone.
            last_item = self.arc_path[-1]
            to_zone = last_item.arc.to_zone
            if last_item.factor < 0:
                to_zone = last_item.arc.from_zone
            self.to_zone: ApportionmentSolverZone = to_zone

            # 
            self.minus_vars = []
            if self.to_zone.type == ZoneTypes.STORAGE:
                if last_item.factor > 0:
                    for x in last_item.arc.backward_vars:
                        if x.is_spill:
                            self.minus_vars.append(x.name)

@dataclass
class ScheduleVariable:
    var_name: str

@dataclass
class SequentialSchedule:
    """ """
    series: list['SequentialScheduleItem']

@dataclass
class ProportionalSchedule:
    """ """
    series: list['ProportionalScheduleItem']

@dataclass
class SequentialScheduleItem:
    """ """
    priority: float
    item: ScheduleVariable | SequentialSchedule | ProportionalSchedule

@dataclass
class ProportionalScheduleItem:
    """ """
    factor: float
    item: ScheduleVariable | SequentialSchedule | ProportionalSchedule





@dataclass
class ApportionmentSolverVarGroup:
    """
    """
    members: list[ApportionmentSolverVar]