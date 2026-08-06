from typing import Generator

from .models import (
    AccountingGraph, FlowComponentsTypes, InterzoneFlow, Zone, ZoneTypes
)

class GraphManager:
    """Manages the static structure and traversal of the accounting graph."""
    def __init__(self, graph: AccountingGraph):
        self.graph = graph
        self.lookup_zones_by_id = {z.id: z for z in graph.zones}
        self.lookup_flows_by_id = {f.id: f for f in graph.interzone_flows}

        self.lookup_zone_outflows = {z.id: [] for z in graph.zones}
        self.lookup_zone_inflows = {z.id: [] for z in graph.zones}

        for f in graph.interzone_flows:
            self.lookup_zone_outflows[f.from_zone].append(f)
            self.lookup_zone_inflows[f.to_zone].append(f)

            # Initialize the natural_flow_mode -- this is done here because we
            # need the from and to zone types.
            if f.natural_flow_mode is None:
                from_type = self.get_zone_by_id(f.from_zone).type
                to_type = self.get_zone_by_id(f.to_zone).type
                f.set_default_natural_flow_mode(from_type, to_type)

    def get_zone_by_id(self, zone_id: str) -> Zone:
        if zone_id not in self.lookup_zones_by_id:
            raise ValueError(f'Cannot find zone id {zone_id}')
        return self.lookup_zones_by_id[zone_id]

    def get_flow_by_id(self, flow_id: str) -> InterzoneFlow:
        if flow_id not in self.lookup_flows_by_id:
            raise ValueError(f'Cannot find interzone-flow id {flow_id}')
        return self.lookup_flows_by_id[flow_id]

    def get_zone_outflows(self, zone_id: str) -> list[InterzoneFlow]:
        return self.lookup_zone_outflows.get(zone_id, [])

    def get_zone_inflows(self, zone_id: str) -> list[InterzoneFlow]:
        return self.lookup_zone_inflows.get(zone_id, [])

    def traverse_downstream(self, zone_id: str) -> Generator[InterzoneFlow, None, None]:
        """Loops through all downstream interzone-flows, only following streams."""
        stream_outflow = None
        next_zone_id = None
        for f in self.get_zone_outflows(zone_id):
            to_zone = self.get_zone_by_id(f.to_zone)
            if to_zone.type == ZoneTypes.STREAM:
                if stream_outflow is not None:
                    raise ValueError('Cannot traverse downstream: stream network diverges.')
                stream_outflow = f
                next_zone_id = to_zone.id

        if stream_outflow is not None and next_zone_id is not None:
            yield stream_outflow
            yield from self.traverse_downstream(next_zone_id)

    def get_loss_route(self, zone_id: str) -> str:
        """Finds the dynamically designated physical loss route for a given zone."""
        candidates = []
        # Check outflows (e.g., from REACH to SYSTEM)
        for f in self.get_zone_outflows(zone_id):
            if f.residual_for_losses and self.get_zone_by_id(f.to_zone).type == ZoneTypes.SYSTEM_GAIN_LOSS:
                candidates.append(f)

        # Check inflows (e.g., bidirectional from SYSTEM to REACH)
        for f in self.get_zone_inflows(zone_id):
            if f.residual_for_losses and self.get_zone_by_id(f.from_zone).type == ZoneTypes.SYSTEM_GAIN_LOSS:
                candidates.append(f)

        if len(candidates) == 1:
            return candidates[0].id
        elif len(candidates) > 1:
            raise ValueError(f"Multiple loss routes found for zone {zone_id} with residual_for_losses=True")
        else:
            raise ValueError(f"No loss route found for zone {zone_id} with residual_for_losses=True")

    def set_implied_calculated_flow_boundaries(self):
        """Previous versions of the general solver assumed that a residual
        calculation was neccessary when no flow measurements were specified.
        This function explicitly creates those calculation specifications so
        test code built using the old way will still work.

        In the future, it may be best to require a full, explicit definition
        and not depend on this function.
        """
        for z in self.graph.zones:

            # 1. Reservoirs connected to a reach with a bi-directional flow
            if z.type == ZoneTypes.STORAGE:
                for f in self.get_zone_inflows(z.id):
                    if len(f.flow_measurements) == 0:
                        f.flow_type = FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE
                        f.residual_for_gains = True
                        f.residual_for_losses = True
                    if f.flow_type == FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE:
                        f.residual_for_gains = True
                        f.residual_for_losses = True


            # 2. Stream zones connected to a gain or loss zone.
            elif z.type == ZoneTypes.STREAM:
                for f in self.get_zone_inflows(z.id):
                    from_z = self.get_zone_by_id(f.from_zone)
                    is_gain = from_z.type == ZoneTypes.SYSTEM_GAIN_LOSS
                    if is_gain and len(f.flow_measurements)==0:
                        f.flow_type = FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE
                        f.residual_for_gains = True
                        f.residual_for_losses = (f.bidirectional)
                for f in self.get_zone_outflows(z.id):
                    to_z = self.get_zone_by_id(f.to_zone)
                    is_loss = to_z.type == ZoneTypes.SYSTEM_GAIN_LOSS
                    if is_loss and len(f.flow_measurements)==0:
                        f.flow_type = FlowComponentsTypes.FLOW_BALANCE_OF_SOURCE_ZONE
                        f.residual_for_gains = (f.bidirectional)
                        f.residual_for_losses = True
