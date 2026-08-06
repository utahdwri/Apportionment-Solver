from dataclasses import dataclass, field
from .models import (
    InterzoneFlow, NaturalFlowMode, ZoneTypes
)
from .graph_manager import GraphManager
from .loss_models import LossDefinition

SOLVER_TOL = 1e-6

@dataclass
class CurFlowInfo:
    """Stores the total flow, natural flow, and total apportioned flow for an interzone-flow."""

    # The measured flow should always match the input measurements.
    measured: float = 0

    # The natural flow includes any upstream-boundary natural flow specified in
    # the input data.
    natural: float = 0

    # This is the portion of the natural flow that excludes upstream-boundary
    # natural flow specified in the input data.
    available_natural: float = 0


class NaturalFlowCalculator:
    """Calculate and propagate natural flow for one day.

    ``CurFlowInfo`` is the only natural-flow state object. During
    :meth:`calculate`, this class updates the ``natural`` and
    ``available_natural`` values on the daily interzone-flow records directly.

    ``available_natural`` differs from ``natural`` only for upstream boundary
    flow. Boundary natural flow is routed downstream but is not made available
    for a second allocation inside this solver domain.
    """

    def __init__(self, gm: GraphManager):
        self.gm = gm
        self.date: str | None = None
        self.flows_by_id: dict[str, CurFlowInfo] = {}
        self.natural_at_zone: dict[str, float] = {}
        self.available_natural_at_zone: dict[str, float] = {}
        self._calculated_outflow_by_zone: dict[str, InterzoneFlow] = {}
        self._propagation_stack: set[str] = set()

    @staticmethod
    def _clamp_available(total: float, available: float) -> float:
        """Keep available flow between zero and total, preserving direction."""
        if total > 0:
            return min(max(available, 0.0), total)
        if total < 0:
            return max(min(available, 0.0), total)
        return 0.0

    def _transform_value(
        self,
        loss: LossDefinition,
        value: float,
    ) -> float:
        """Apply an endpoint loss to a signed total-flow value."""
        if abs(value) <= SOLVER_TOL:
            return 0.0
        assert self.date is not None
        sign = 1.0 if value > 0 else -1.0
        return sign * loss.transform_total_flow(abs(value), date=self.date)

    def _inverse_value(
        self,
        loss: LossDefinition,
        remaining: float,
    ) -> float:
        """Move a signed value from below an endpoint loss to above it."""
        if abs(remaining) <= SOLVER_TOL:
            return 0.0
        assert self.date is not None
        sign = 1.0 if remaining > 0 else -1.0
        return sign * loss.inflow_for_remaining(
            abs(remaining),
            date=self.date,
        )

    def _transform_pair(
        self,
        loss: LossDefinition,
        natural: float,
        available_natural: float,
    ) -> tuple[float, float]:
        """Apply a loss once to total natural flow and its available portion.

        The unavailable boundary portion is treated as the baseline component.
        This preserves an affine segment's intercept instead of applying that
        intercept independently to both accounting components.
        """
        available_natural = self._clamp_available(
            natural,
            available_natural,
        )
        unavailable = natural - available_natural

        natural_after = self._transform_value(loss, natural)
        unavailable_after = self._transform_value(loss, unavailable)
        available_after = natural_after - unavailable_after
        available_after = self._clamp_available(
            natural_after,
            available_after,
        )
        return natural_after, available_after

    def _inverse_pair(
        self,
        loss: LossDefinition,
        natural: float,
        available_natural: float,
    ) -> tuple[float, float]:
        """Move natural flow and its available portion above an endpoint loss."""
        available_natural = self._clamp_available(
            natural,
            available_natural,
        )
        unavailable = natural - available_natural

        natural_before = self._inverse_value(loss, natural)
        unavailable_before = self._inverse_value(loss, unavailable)
        available_before = natural_before - unavailable_before
        available_before = self._clamp_available(
            natural_before,
            available_before,
        )
        return natural_before, available_before

    def _calculated_losses_at_zone(self, zone_id: str) -> float:
        """Return physical endpoint losses occurring at a zone."""
        total_loss = 0.0

        for flow in self.gm.get_zone_inflows(zone_id):
            measured = self.flows_by_id[flow.id].measured
            loss = flow.loss_to_zone
            if measured >= 0:
                remaining = self._transform_value(loss, measured)
                total_loss += measured - remaining
            else:
                before = self._inverse_value(loss, -measured)
                total_loss += before + measured

        for flow in self.gm.get_zone_outflows(zone_id):
            measured = self.flows_by_id[flow.id].measured
            loss = flow.loss_from_zone
            if measured >= 0:
                before = self._inverse_value(loss, measured)
                total_loss += before - measured
            else:
                remaining = self._transform_value(loss, -measured)
                total_loss += -measured - remaining

        return total_loss

    def _calculate_system_gain_loss_flow(self, flow: InterzoneFlow) -> float:
        from_type = self.gm.get_zone_by_id(flow.from_zone).type
        to_type = self.gm.get_zone_by_id(flow.to_zone).type
        measured = self.flows_by_id[flow.id].measured

        if (
            from_type == ZoneTypes.SYSTEM_GAIN_LOSS
            and to_type == ZoneTypes.STREAM
        ):
            value = measured + self._calculated_losses_at_zone(flow.to_zone)
        elif (
            from_type == ZoneTypes.STREAM
            and to_type == ZoneTypes.SYSTEM_GAIN_LOSS
        ):
            value = measured - self._calculated_losses_at_zone(flow.from_zone)
        else:
            raise ValueError(
                f"CALCULATED mode is not defined for flow {flow.id} between "
                f"{from_type.value} and {to_type.value}."
            )

        if not flow.bidirectional:
            value = max(0.0, value)
        return value

    def _prepare_calculated_routes(self) -> None:
        """Find the one calculated downstream stream flow for each zone."""
        self._calculated_outflow_by_zone = {}

        for flow in self.gm.graph.interzone_flows:
            if flow.natural_flow_mode != NaturalFlowMode.CALCULATED:
                continue
            if not (
                self.gm.get_zone_by_id(flow.from_zone).type == ZoneTypes.STREAM
                and self.gm.get_zone_by_id(flow.to_zone).type == ZoneTypes.STREAM
            ):
                continue

            existing = self._calculated_outflow_by_zone.get(flow.from_zone)
            if existing is not None:
                raise ValueError(
                    f"Natural flow at zone {flow.from_zone} is underdetermined "
                    f"because both {existing.id} and {flow.id} use "
                    "CALCULATED mode."
                )
            self._calculated_outflow_by_zone[flow.from_zone] = flow

        # A recursive propagation method cannot resolve a calculated cycle.
        for first_zone in self._calculated_outflow_by_zone:
            visited: set[str] = set()
            zone = first_zone
            while zone in self._calculated_outflow_by_zone:
                if zone in visited:
                    raise ValueError(
                        "Calculated natural-flow routes contain a cycle; "
                        "specify at least one flow in the cycle explicitly."
                    )
                visited.add(zone)
                zone = self._calculated_outflow_by_zone[zone].to_zone

    def _propigate_if_stream(
        self,
        zone: str,
        delta_natural: float = 0.0,
        delta_available_natural: float = 0.0,
    ) -> None:
        if self.gm.get_zone_by_id(zone).type == ZoneTypes.STREAM:
            self.propigate(
                zone,
                delta_natural,
                delta_available_natural,
            )

    def _apply_flow_effects(
        self,
        flow: InterzoneFlow,
        natural: float,
        available_natural: float,
        *,
        boundary: bool = False,
    ) -> None:
        """Apply a fixed flow's source and destination effects to stream zones."""
        if abs(natural) <= SOLVER_TOL:
            return

        if natural > 0:
            destination_natural, destination_available = self._transform_pair(
                flow.loss_to_zone,
                natural,
                available_natural,
            )
            self._propigate_if_stream(
                flow.to_zone,
                destination_natural,
                destination_available,
            )

            if not boundary:
                source_natural, source_available = self._inverse_pair(
                    flow.loss_from_zone,
                    natural,
                    available_natural,
                )
                self._propigate_if_stream(
                    flow.from_zone,
                    -source_natural,
                    -source_available,
                )
            return

        # Negative values move from the declared destination to the declared
        # source. Use magnitudes while evaluating non-negative loss curves.
        magnitude = -natural
        available_magnitude = -available_natural
        destination_natural, destination_available = self._transform_pair(
            flow.loss_from_zone,
            magnitude,
            available_magnitude,
        )
        self._propigate_if_stream(
            flow.from_zone,
            destination_natural,
            destination_available,
        )

        if not boundary:
            source_natural, source_available = self._inverse_pair(
                flow.loss_to_zone,
                magnitude,
                available_magnitude,
            )
            self._propigate_if_stream(
                flow.to_zone,
                -source_natural,
                -source_available,
            )

    def propigate(
        self,
        zone: str,
        delta_natural: float | None = None,
        delta_available_natural: float | None = None,
    ) -> None:
        """Apply a change at ``zone`` and recursively update downstream flow.

        A negative delta reduces natural flow on the calculated downstream
        route. Calculated natural flow is limited to zero when that
        interzone-flow is not bidirectional.
        """
        if self.date is None or not self.flows_by_id:
            raise ValueError("calculate() must initialize the calculator first.")
        if self.gm.get_zone_by_id(zone).type != ZoneTypes.STREAM:
            return

        delta_natural = 0.0 if delta_natural is None else delta_natural
        delta_available_natural = (
            0.0
            if delta_available_natural is None
            else delta_available_natural
        )

        self.natural_at_zone[zone] += delta_natural
        self.available_natural_at_zone[zone] += delta_available_natural

        outflow = self._calculated_outflow_by_zone.get(zone)
        if outflow is None:
            return
        if zone in self._propagation_stack:
            raise ValueError(
                f"Calculated natural-flow propagation encountered a cycle at {zone}."
            )

        flow_info = self.flows_by_id[outflow.id]
        old_natural = flow_info.natural
        old_available = flow_info.available_natural

        source_natural = self.natural_at_zone[zone]
        source_available = self.available_natural_at_zone[zone]
        if not outflow.bidirectional:
            source_natural = max(0.0, source_natural)
        source_available = self._clamp_available(
            source_natural,
            source_available,
        )

        new_natural, new_available = self._transform_pair(
            outflow.loss_from_zone,
            source_natural,
            source_available,
        )
        if not outflow.bidirectional:
            new_natural = max(0.0, new_natural)
            new_available = max(0.0, min(new_natural, new_available))

        old_destination = self._transform_pair(
            outflow.loss_to_zone,
            old_natural,
            old_available,
        )
        new_destination = self._transform_pair(
            outflow.loss_to_zone,
            new_natural,
            new_available,
        )

        flow_info.natural = new_natural
        flow_info.available_natural = new_available

        self._propagation_stack.add(zone)
        try:
            self.propigate(
                outflow.to_zone,
                new_destination[0] - old_destination[0],
                new_destination[1] - old_destination[1],
            )
        finally:
            self._propagation_stack.remove(zone)

    def calculate(
        self,
        *,
        date: str,
        daily_flows: dict[str, CurFlowInfo],
        specified_values: dict[str, float],
        boundary_values: dict[str, float],
        loss_state=None,
    ) -> None:
        """Initialize fixed natural flows and propagate their effects.

        The method intentionally follows the earlier natural-flow approach:

        1. Reset daily natural-flow values.
        2. Apply boundary natural flow.
        3. Calculate local system gains/losses.
        4. Apply explicitly specified natural-flow components.
        5. Recursively propagate each change along calculated stream routes.
        """
        del loss_state  # Reserved for future active loss-segment integration.

        self.date = date
        self.flows_by_id = daily_flows
        self.natural_at_zone = {
            zone.id: 0.0
            for zone in self.gm.graph.zones
            if zone.type == ZoneTypes.STREAM
        }
        self.available_natural_at_zone = {
            zone_id: 0.0 for zone_id in self.natural_at_zone
        }
        self._propagation_stack = set()

        for flow_info in daily_flows.values():
            flow_info.natural = 0.0
            flow_info.available_natural = 0.0

        self._prepare_calculated_routes()

        # Boundary natural flow was calculated outside this problem domain.
        # Route it downstream, but do not make it available again.
        for flow_id, value in boundary_values.items():
            flow = self.gm.get_flow_by_id(flow_id)
            if flow.natural_flow_mode == NaturalFlowMode.SPECIFIED:
                raise ValueError(
                    f"Flow {flow.id} cannot define both specified natural "
                    "flow and external boundary natural flow."
                )
            if value < 0 and not flow.bidirectional:
                raise ValueError(
                    f"Boundary natural flow for {flow.id} is negative but "
                    "the interzone-flow is not bidirectional."
                )

            daily_flows[flow.id].natural = value
            daily_flows[flow.id].available_natural = 0.0
            self._apply_flow_effects(flow, value, 0.0, boundary=True)

        # Calculate traditional natural system gains/losses and route them.
        for flow in self.gm.graph.interzone_flows:
            if flow.id in boundary_values:
                continue
            if flow.natural_flow_mode != NaturalFlowMode.CALCULATED:
                continue

            from_type = self.gm.get_zone_by_id(flow.from_zone).type
            to_type = self.gm.get_zone_by_id(flow.to_zone).type
            if from_type == ZoneTypes.STREAM and to_type == ZoneTypes.STREAM:
                continue
            if {from_type, to_type} != {
                ZoneTypes.STREAM,
                ZoneTypes.SYSTEM_GAIN_LOSS,
            }:
                raise ValueError(
                    f"CALCULATED natural flow is not supported for {flow.id}; "
                    "configure ZERO or SPECIFIED instead."
                )

            value = self._calculate_system_gain_loss_flow(flow)
            daily_flows[flow.id].natural = value
            daily_flows[flow.id].available_natural = value
            self._apply_flow_effects(flow, value, value)

        # Apply specified imports, diversions, storage flows, and stream flows.
        for flow in self.gm.graph.interzone_flows:
            if flow.id in boundary_values:
                continue
            mode = flow.natural_flow_mode
            if mode == NaturalFlowMode.ZERO or mode == NaturalFlowMode.CALCULATED:
                continue

            value = specified_values[flow.id]
            if value < 0 and not flow.bidirectional:
                raise ValueError(
                    f"Specified natural flow for {flow.id} cannot be negative "
                    "because the interzone-flow is not bidirectional."
                )

            daily_flows[flow.id].natural = value
            daily_flows[flow.id].available_natural = value
            self._apply_flow_effects(flow, value, value)

