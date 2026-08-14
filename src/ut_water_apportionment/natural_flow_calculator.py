from dataclasses import dataclass
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

    # # This is the portion of the natural flow that excludes upstream-boundary
    # # natural flow specified in the input data.
    # available_natural: float = 0


class NaturalFlowCalculator:
    """Calculate and propagate natural flow for one day.
    """

    def __init__(self, gm: GraphManager):
        self.gm = gm
        self.date: str | None = None
        self.flows_by_id: dict[str, CurFlowInfo] = {}
        self.natural_at_zone: dict[str, float] = {}
        self.remaining_natural_at_zone: dict[str, float] = {}
        self._calculated_outflow_by_zone: dict[str, InterzoneFlow] = {}


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

    def _prepare_calculated_routes(
        self,
        boundary_flow_ids: set[str] | None = None
    ) -> None:
        """Find the one calculated downstream stream flow for each zone."""

        boundary_flow_ids = boundary_flow_ids or set()

        self._calculated_outflow_by_zone = {}

        for flow in self.gm.graph.interzone_flows:

            # External NF boundaries are cuts in the calculated NF network.
            if flow.id in boundary_flow_ids:
                continue

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

    def _apply_flow_effects(
        self,
        flow: InterzoneFlow,
        natural: float,
        *,
        boundary: bool = False,
    ) -> None:
        """Apply a fixed flow's source and destination effects to stream zones."""
        if abs(natural) <= SOLVER_TOL:
            return

        if natural > 0:
            destination_natural = self._transform_value(flow.loss_to_zone, natural)
            self._propagate_if_stream(
                flow.to_zone,
                destination_natural
            )

            if not boundary:
                source_natural = self._inverse_value(flow.loss_from_zone, natural)
                self._propagate_if_stream(
                    flow.from_zone,
                    -source_natural
                )
            return

        # Negative values move from the declared destination to the declared
        # source. Use magnitudes while evaluating non-negative loss curves.
        magnitude = -natural
        destination_natural = self._transform_value(
            flow.loss_from_zone,
            magnitude
        )
        self._propagate_if_stream(
            flow.from_zone,
            destination_natural
        )

        if not boundary:
            source_natural = self._inverse_value(
                flow.loss_to_zone,
                magnitude
            )
            self._propagate_if_stream(
                flow.to_zone,
                -source_natural
            )


    def _propagate_if_stream(
        self,
        zone_id: str,
        delta_natural: float,
    ) -> None:

        if self.gm.get_zone_by_id(zone_id).type == ZoneTypes.STREAM:
            self.propagate(zone_id, delta_natural)


    def propagate(
        self,
        zone_id: str,
        delta_natural: float = 0.0,
    ) -> None:
        """Apply a change at ``zone`` and recursively update downstream flow.

        A negative delta reduces natural flow on the calculated downstream
        route. Calculated natural flow is limited to zero when that
        interzone-flow is not bidirectional.
        """

        if self.date is None or not self.flows_by_id:
            raise ValueError("calculate() must initialize the calculator first.")

        if self.gm.get_zone_by_id(zone_id).type != ZoneTypes.STREAM:
            return

        self.natural_at_zone[zone_id] += delta_natural

        outflow = self._calculated_outflow_by_zone.get(zone_id)
        if outflow is None:
            return

        flow_info = self.flows_by_id[outflow.id]
        old_natural = flow_info.natural

        source_natural = self.natural_at_zone[zone_id]

        if not outflow.bidirectional:
            source_natural = max(0.0, source_natural)

        new_natural = self._transform_value(
            outflow.loss_from_zone,
            source_natural
        )

        old_at_destination = self._transform_value(
            outflow.loss_to_zone,
            old_natural
        )

        new_at_destination = self._transform_value(
            outflow.loss_to_zone,
            new_natural
        )

        flow_info.natural = new_natural

        self.propagate(
            outflow.to_zone,
            new_at_destination - old_at_destination,
        )

    def calculate(
        self,
        *,
        date: str,
        daily_flows: dict[str, CurFlowInfo],
        specified_values: dict[str, float],
        boundary_values: dict[str, float],
    ) -> None:
        """
        """

        self.date = date
        self.flows_by_id = daily_flows
        self.natural_at_zone = {
            zone.id: 0.0
            for zone in self.gm.graph.zones
            if zone.type == ZoneTypes.STREAM
        }

        for flow_info in daily_flows.values():
            flow_info.natural = 0.0
            #flow_info.available_natural = 0.0


        # 1. Figure out how natural flow propages downstream and store a
        # resulting data object.
        self._prepare_calculated_routes(
            set(boundary_values)
        )



        # 2. Calculate traditional natural system gains/losses and route them.
        for flow in self.gm.graph.interzone_flows:
            if flow.id in boundary_values:
                value = boundary_values[flow.id]
                daily_flows[flow.id].natural = value
                self._apply_flow_effects(flow, value, boundary=True)
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
            #daily_flows[flow.id].available_natural = value                     # TODO - maybe don't need this...
            self._apply_flow_effects(flow, value)


        # 3. Apply specified imports, diversions, storage flows.
        for flow in self.gm.graph.interzone_flows:

            if flow.id in boundary_values:
                continue

            if flow.natural_flow_mode == NaturalFlowMode.SPECIFIED:

                value = specified_values[flow.id]
                if value < 0 and not flow.bidirectional:
                    raise ValueError(
                        f"Specified natural flow for {flow.id} cannot be negative "
                        "because the interzone-flow is not bidirectional."
                    )

                daily_flows[flow.id].natural = value
                #daily_flows[flow.id].available_natural = value                 # TODO - maybe don't need this...
                self._apply_flow_effects(flow, value)



        # Preserve the origional NF.
        # Copy to the remaining dict for future calcs.
        self.remaining_natural_at_zone = self.natural_at_zone.copy()



    def apply_external_boundary_commitments(
        self,
        boundary_values: dict[str, float],
        daily_flows: dict[str, CurFlowInfo]
    ) -> None:
        """
        Remove natural flow that was already apportioned outside the solver
        domain.

        External natural-flow boundaries are cut edges. Their upstream zones
        do not participate in remaining-natural-flow accounting.
        """

        for flow_id, specified_nf in boundary_values.items():

            flow = self.gm.get_flow_by_id(flow_id)

            measured_flow = daily_flows[flow.id].measured
            already_apportioned = specified_nf - measured_flow

            # The boundary value is expressed at the flow itself. Move the
            # adjustment through any loss at the downstream endpoint before
            # applying it to the first in-domain stream zone.
            amount_at_entry_zone = self._transform_value(
                flow.loss_to_zone,
                already_apportioned,
            )

            self.apply_committed_allocation(flow.to_zone, amount_at_entry_zone)



    def get_nf_constraint_coefficients(
        self,
        source_zone_id: str,
    ) -> dict[str, float]:
        """
        Return the natural-flow constraint coefficients for one
        unit of natural flow allocated from source_zone_id.

        Keys are stream zone ids. Values are the amount by which
        one unit allocated at the source consumes natural-flow
        availability at that zone.
        """

        if self.date is None:
            raise ValueError(
                "calculate() must be called before requesting "
                "natural-flow coefficients."
            )

        source_zone = self.gm.get_zone_by_id(source_zone_id)

        if source_zone.type != ZoneTypes.STREAM:
            raise ValueError(
                f"Natural-flow source {source_zone_id!r} "
                "is not a stream zone."
            )

        coefficients = {
            source_zone_id: 1.0
        }

        remaining_factor = 1.0
        zone_id = source_zone_id

        while zone_id in self._calculated_outflow_by_zone:
            flow = self._calculated_outflow_by_zone[zone_id]

            # Loss immediately after leaving the upstream zone.
            remaining_factor *= (
                1.0 - flow.loss_from_zone.get_fraction(self.date)
            )

            # Loss immediately before reaching the downstream zone.
            remaining_factor *= (
                1.0 - flow.loss_to_zone.get_fraction(self.date)
            )

            zone_id = flow.to_zone

            if remaining_factor <= SOLVER_TOL:
                break

            coefficients[zone_id] = remaining_factor

        return coefficients


    def apply_committed_allocation(
        self,
        source_zone_id: str,
        amount: float,
    ) -> None:
        """
        Apply a committed increase in natural-flow apportionment.
        """

        if abs(amount) <= SOLVER_TOL:
            return

        coefficients = self.get_nf_constraint_coefficients(source_zone_id)

        for zone_id, coefficient in coefficients.items():

            reduction = amount * coefficient
            self.remaining_natural_at_zone[zone_id] -= reduction

            # Normalize harmless floating-point residue, but don't hide
            # meaningful negative values caused by feasibility fallback.       # Why not?
            # I guess we want to have slightly negative remaining natural flow
            # values if that's what the feasibility fallback requires.
            if abs(self.remaining_natural_at_zone[zone_id]) <= SOLVER_TOL:
                self.remaining_natural_at_zone[zone_id] = 0.0


    def source_is_exhausted(
        self,
        source_zone_id: str,
    ) -> bool:
        """
        Is it possible to apportion any more nf water from the given zone?
        """
        #return False

        coefficients = self.get_nf_constraint_coefficients(source_zone_id)

        return any(
            coefficient > SOLVER_TOL
            and self.remaining_natural_at_zone[zone_id] <= SOLVER_TOL
            for zone_id, coefficient in coefficients.items()
        )