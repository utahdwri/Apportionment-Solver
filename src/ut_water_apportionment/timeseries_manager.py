from typing import Generator
from dataclasses import dataclass, field
from math import isclose, isfinite
from collections.abc import Iterator

from .models import (
    FlowComponentsTypes, InterzoneFlow, MeasurementCollection, NaturalFlowMode, Zone, ZoneTypes
)
from .graph_manager import GraphManager
from .natural_flow_calculator import CurFlowInfo, NaturalFlowCalculator


COALESCE_MISSING_FLOWS_TO_ZERO = True
COALESCE_NEGATIVE_FLOWS_TO_ZERO = True


class DailyDataManager:
    """Resolve daily physical data and delegate natural-flow accounting."""
    def __init__(
        self,
        gm: GraphManager,
        measurements: MeasurementCollection,
        natural_flow_calculator: NaturalFlowCalculator,
        external_natural_flows: dict | None = None,
    ):
        self._validate_measurement_references(gm, measurements)

        self.gm = gm
        self.measurements = measurements
        self.cur_date: str | None = None
        self.cur_flows_by_id: dict[str, CurFlowInfo] = {}
        self.cur_storage_chg_by_id: dict[str, CurFlowInfo] = {}

        self._zone_lags = {}
        self._flow_lags = {}
        self._set_lag_by_traversal()

        self.external_natural_flows = external_natural_flows or {}
        self.natural_flow_calculator = natural_flow_calculator

    # TODO - this is needed only once -- consider redesign...
    @property
    def flow_lags(self) -> dict[str, float]:
        return dict(self._flow_lags)

    @staticmethod
    def _validate_measurement_references(
        gm: GraphManager,
        measurements: MeasurementCollection,
    ) -> None:

        available_ids = {x.id for x in measurements.series}

        for flow in gm.graph.interzone_flows:

            if flow.flow_type == FlowComponentsTypes.OBSERVATION:
                for measurement in flow.flow_measurements:
                    if str(measurement.measurement_id) not in available_ids:
                        raise ValueError(
                            f"Interzone flow {flow.id!r} references missing "
                            f"measurement {measurement.measurement_id!r}."
                        )

            if flow.natural_flow_mode == NaturalFlowMode.SPECIFIED:
                for measurement in flow.nf_measurements:
                    if str(measurement.measurement_id) not in available_ids:
                        raise ValueError(
                            f"Natural flow for {flow.id!r} references missing "
                            f"measurement {measurement.measurement_id!r}."
                        )

        for zone in gm.graph.zones:
            for measurement_id in zone.storage_meas_ids:
                if str(measurement_id) not in available_ids:
                    raise ValueError(
                        f"Zone {zone.id!r} references missing storage "
                        f"measurement {measurement_id!r}."
                    )


    def set_day(self, date: str):
        self.cur_date = date
        self.cur_flows_by_id = {f.id: CurFlowInfo() for f in self.gm.graph.interzone_flows} # initializes with zero terms

        # Set Flows for this day:
        observed_values = self._get_interzone_flow_values(date)
        for f, value in observed_values.items():
            self.cur_flows_by_id[f].measured = value

        # Set storage change of zones:
        for z in self.gm.graph.zones:
            self.cur_storage_chg_by_id[z.id] = CurFlowInfo(measured=self._get_storage_change(z, date))

    def get_specified_natural_flow_values(self, date: str) -> dict[str, float]:
        """Resolve constant or measurement-based specified NF for one day."""
        values: dict[str, float] = {}
        for flow in self.gm.graph.interzone_flows:
            if flow.natural_flow_mode != NaturalFlowMode.SPECIFIED:
                continue

            value = 0.0
            lag = self._flow_lags[flow.id]
            for measurement in flow.nf_measurements:
                measured = self.measurements.get(measurement.measurement_id, date, lag)
                if measured is None:
                    if COALESCE_MISSING_FLOWS_TO_ZERO:
                        measured = 0.0
                    else:
                        raise ValueError(
                            f"Natural-flow measurement "
                            f"{measurement.measurement_id} is undefined "
                            f"on {date}."
                        )
                value += measured * measurement.adjustment_factor

            if not isfinite(value):
                raise ValueError(
                    f"Specified natural flow for {flow.id} must be finite."
                )
            values[flow.id] = value

        return values

    def get_boundary_natural_flow_values(self, date: str) -> dict[str, float]:
        values: dict[str, float] = {}
        for flow_id, daily_values in self.external_natural_flows.items():
            if flow_id not in self.cur_flows_by_id:
                raise ValueError(
                    f"Boundary natural flow references unknown flow {flow_id}."
                )
            if date in daily_values:
                value = float(daily_values[date])
                if not isfinite(value):
                    raise ValueError(
                        f"Boundary natural flow for {flow_id} on {date} must be finite."
                    )
                values[flow_id] = value
        return values

    def calc_natural_flows(self):
        """Calculate natural flow for the current day."""
        if self.cur_date is None:
            raise ValueError("set_day() must be called before calc_natural_flows().")

        self.natural_flow_calculator.calculate(
            date=self.cur_date,
            daily_flows=self.cur_flows_by_id,
            specified_values=self.get_specified_natural_flow_values(self.cur_date),
            boundary_values=self.get_boundary_natural_flow_values(self.cur_date),
        )

    def get_available_natural_at_zone(self, zone_id: str) -> float:
        """Return natural flow available for allocation at a stream zone."""
        return max(
            0.0,
            self.natural_flow_calculator.available_natural_at_zone.get(
                zone_id,
                0.0,
            ),
        )


    def _set_lag_by_traversal(self) -> None:
        """Traverse the graph to set the absolute lag (time offset) for each
        interzone-flow."""

        # The thing to populate, figure out.
        flow_lags = {}
        zone_lags = {}

        def set_flow_lag(f: InterzoneFlow, lag: float):
            if f.id in flow_lags:
                if not isclose(
                    flow_lags[f.id], lag, abs_tol=1e-9,
                ):
                    raise ValueError(f'Computed absolute lag times of {lag} '
                        f'for interzone-flow id: "{f.id}" is inconsistent '
                        f'with existing value of {flow_lags[f.id]}!')
            else:
                # Set the lag for this interzone-flow.
                flow_lags[f.id] = lag

                # Loop through the connected interzone-flows, and set their
                # lags too.
                iter_over_zone(f.to_zone, lag - f.lag_to_zone)
                iter_over_zone(f.from_zone, lag - f.lag_from_zone)

        def _zone_synchronizes_time(zone_id: str) -> bool:
            """Return true if the given zone needs """

            """ TODO
            The most general definition isn't actually:
                STREAM and STORAGE zones synchronize time.

            It is:
                Two flow endpoints must share a time frame if the solver
                places their quantities together in a constraint.

            That suggests a future LagManager could build a temporal
            constraint graph from:
                - Stream/storage physical balance relationships.
                - Residual-flow calculations.
                - Natural-flow propagation.
                - Consecutive transaction path items.
                - Any future storage-account or other cross-flow constraints.

            Then calculate connected components in that graph.
            """

            zone = self.gm.get_zone_by_id(zone_id)
            return zone.type in {
                ZoneTypes.STREAM,
                ZoneTypes.STORAGE,
            }

        def iter_over_zone(zone_id: str, lag: float):
            """Iterate through each flow to and from the given zone."""

            if not _zone_synchronizes_time(zone_id):
                return

            zone_lags[zone_id] = lag

            for flow in self.gm.get_zone_inflows(zone_id):
                set_flow_lag(flow, lag + flow.lag_to_zone)
            for flow in self.gm.get_zone_outflows(zone_id):
                set_flow_lag(flow, lag + flow.lag_from_zone)

        # Traverse the graph and assign a lag to each flow...
        for f in self.gm.graph.interzone_flows:
            if f.id not in flow_lags:
                set_flow_lag(f, 0)

        # Normalize so there are no negative zone lags.

        # Adjust reference so the downstream zone has a lag of zero.
        min_lag = min(
            list(flow_lags.values()) + list(zone_lags.values())
            ) if flow_lags else 0
        self._zone_lags = {zid: lag - min_lag for zid, lag in zone_lags.items()}
        self._flow_lags = {fid: lag - min_lag for fid, lag in flow_lags.items()}

    def _get_interzone_flow_values(self, date: str) -> dict[str, float]:
        """Given the class input, determine the total flow values for each
        interzone flow. This involves summing flow components, applying lags,
        and calculating residual flows.

        Returns a dictionary of flows values by interzone-flow-id.
        """

        total_flow_by_id: dict[str, float] = {}

        # 1. Measurements
        for f in self.gm.graph.interzone_flows:
            lag = self._flow_lags[f.id]
            total_measured = 0

            if f.flow_type == FlowComponentsTypes.OBSERVATION:
                for fm in f.flow_measurements:
                    val = self.measurements.get(fm.measurement_id, date, lag)
                    if val is None:
                        if COALESCE_MISSING_FLOWS_TO_ZERO:
                            val = 0
                        else:
                            raise ValueError(f"Measurement {fm.measurement_id} undefined on {date}")
                    total_measured += val * fm.adjustment_factor

            total_flow_by_id[f.id] = total_measured


        # 2. Calculate Residuals
        for zone_id, calcs in self._determine_residual_calc_order():
            # RESIDUAL = [MEASURED OUTFLOWS] - [MEASURED INFLOWS]
            # [UNMEASURED INFLOW] - [UNMEASURED OUTFLOW] - CALCULATED LOSSES = RESIDUAL
            #
            residual_flow = sum(total_flow_by_id[f.id] for f in self.gm.get_zone_outflows(zone_id))
            storage_chg = self._get_storage_change(self.gm.get_zone_by_id(zone_id), date)
            if storage_chg is not None:
                residual_flow += storage_chg
            residual_flow -= sum(total_flow_by_id[f.id] for f in self.gm.get_zone_inflows(zone_id))

            # Now assign the residual flow to the the interzone-flow term(s)
            for f in calcs:

                factor = 0
                if f.to_zone == zone_id:
                    factor = 1
                elif f.from_zone == zone_id:
                    factor = -1
                else:
                    raise ValueError('Unexpected error - Cannot set the direction factor...')

                if residual_flow >= 0 and f.residual_for_gains:
                    total_flow_by_id[f.id] = residual_flow * factor

                if residual_flow < 0 and f.residual_for_losses:
                    total_flow_by_id[f.id] = residual_flow * factor


        # 3. Checks
        for f in self.gm.graph.interzone_flows:
            #continue
            if total_flow_by_id[f.id] < 0 and not f.bidirectional:

                # TODO - This code may be related to the failing test - if I add the following condition then that test passes, but others fail.
                if f.flow_type != FlowComponentsTypes.OBSERVATION:
                    continue


                if COALESCE_NEGATIVE_FLOWS_TO_ZERO:
                    total_flow_by_id[f.id] = 0
                else:
                    raise ValueError(f"Net flow negative for {f.id} on {date}")

        return total_flow_by_id

    def _determine_residual_calc_order(self) -> list[tuple[str, list[InterzoneFlow]]]:
        """In order to obtain the values for Calculated Flows, we first need
        to know the order that these flows must be calculated in, since one
        calculation may depend on another. This is what this function
        provides."""

        zone_calc_order: list[str] = []
        flow_balance_calcs: dict[str, list[str]] = {}
        residual_flows_by_zone: dict[str, list[InterzoneFlow]] = {}

        def add_flow_balance_calc(zone_id: str, required_by_zone_id: str, f: InterzoneFlow):
            if zone_id not in flow_balance_calcs:
                flow_balance_calcs[zone_id] = []
                residual_flows_by_zone[zone_id] = []
            flow_balance_calcs[zone_id].append(required_by_zone_id)
            residual_flows_by_zone[zone_id].append(f)

        for f in self.gm.graph.interzone_flows:
            if f.flow_type == FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE:
                add_flow_balance_calc(f.to_zone, f.from_zone, f)
            elif f.flow_type == FlowComponentsTypes.FLOW_BALANCE_OF_SOURCE_ZONE:
                add_flow_balance_calc(f.from_zone, f.to_zone, f)

        # Find the keys that are not dependencies.
        while len(flow_balance_calcs) > 0:
            dependencies = {dep for deps in flow_balance_calcs.values() for dep in deps}
            numdel = 0
            for key in list(flow_balance_calcs.keys()):
                if key not in dependencies:
                    zone_calc_order.append(key)
                    del flow_balance_calcs[key]
                    numdel += 1

            # If no next item can be identified, raise an error instead of
            # infinite looping
            if numdel == 0:
                raise ValueError("Circular dependency in residual calculations!")

        return [(z, residual_flows_by_zone[z]) for z in zone_calc_order]

    def _get_storage_change(self, z: Zone, date: str):
        storage_chg = None
        if z.type in (ZoneTypes.STREAM, ZoneTypes.STORAGE):
            lag = self._zone_lags[z.id]
            for mid in z.storage_meas_ids:
                storage_chg = self.measurements.get_change(mid, date, lag)
                if storage_chg is not None:
                    break

            if storage_chg is None:
                if z.type == ZoneTypes.STREAM or COALESCE_MISSING_FLOWS_TO_ZERO:
                    storage_chg = 0
                else:
                    raise ValueError(f"Storage change undefined on {date}")
        else:
            storage_chg = 0
        return storage_chg



