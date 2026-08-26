from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from math import isclose, isfinite


TOLERANCE = 1e-10


@dataclass(frozen=True)
class LossCurvePoint:
    """One point on an absolute-loss curve."""

    inflow: float
    loss: float

    def __post_init__(self) -> None:
        if not isfinite(self.inflow) or self.inflow < 0:
            raise ValueError("inflow must be finite and non-negative")
        if not isfinite(self.loss) or not 0 <= self.loss <= self.inflow:
            raise ValueError("loss must be finite and between 0 and inflow")
        if self.loss > self.inflow:
            raise ValueError(
                'LossCurvePoint.loss cannot exceed its inflow. '
                f'Got inflow={self.inflow}, loss={self.loss}.'
                )


@dataclass(frozen=True)
class ResolvedLossRelation:
    """One affine segment: loss = slope * inflow + intercept."""

    segment_index: int
    min_driver_flow: float
    max_driver_flow: float | None
    loss_slope: float
    loss_intercept: float

    @property
    def remaining_slope(self) -> float:
        return 1.0 - self.loss_slope

    def loss_at(self, inflow: float) -> float:
        return self.loss_slope * inflow + self.loss_intercept

    def remaining_at(self, inflow: float) -> float:
        return inflow - self.loss_at(inflow)

    def contains(self, inflow: float) -> bool:
        return (
            inflow >= self.min_driver_flow - TOLERANCE
            and (
                self.max_driver_flow is None
                or inflow <= self.max_driver_flow + TOLERANCE
            )
        )




@dataclass(frozen=True)
class LossInterval:
    """An inclusive date interval for one static loss definition."""

    beg_date: str
    end_date: str
    loss: "LossDefinition"

    def __post_init__(self) -> None:
        if Date.fromisoformat(self.end_date) < Date.fromisoformat(self.beg_date):
            raise ValueError("end_date cannot precede beg_date")
        if self.loss.is_time_varying:
            raise ValueError("LossInterval cannot contain a time-varying loss")

    def contains(self, value: str) -> bool:
        value_date = Date.fromisoformat(value)
        return (
            Date.fromisoformat(self.beg_date)
            <= value_date
            <= Date.fromisoformat(self.end_date)
        )


@dataclass(frozen=True)
class LossDefinition:
    """A constant, piecewise-linear, or time-varying loss."""

    segments: tuple[ResolvedLossRelation, ...] = ()
    intervals: tuple[LossInterval, ...] = ()
    default: "LossDefinition | None" = None

    def __post_init__(self) -> None:
        segments = tuple(self.segments)
        intervals = tuple(sorted(self.intervals, key=lambda x: x.beg_date))

        if segments and intervals:
            raise ValueError("a loss cannot have both segments and intervals")
        if self.default is not None and not intervals:
            raise ValueError("default is only valid for a time-varying loss")
        if self.default is not None and self.default.is_time_varying:
            raise ValueError("default cannot be time-varying")

        for previous, current in zip(intervals, intervals[1:]):
            if Date.fromisoformat(current.beg_date) <= Date.fromisoformat(
                previous.end_date
            ):
                raise ValueError("loss intervals cannot overlap")

        # LossDefinition() is the zero-loss default.
        if not segments and not intervals:
            segments = (
                ResolvedLossRelation(0, 0.0, None, 0.0, 0.0),
            )

        object.__setattr__(self, "segments", segments)
        object.__setattr__(self, "intervals", intervals)

    @classmethod
    def linear(cls, fraction: float) -> "LossDefinition":
        """Create a constant fractional loss."""

        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise TypeError("fraction must be numeric")
        fraction = float(fraction)
        if not isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("fraction must be between 0 and 1")

        return cls(
            segments=(
                ResolvedLossRelation(0, 0.0, None, fraction, 0.0),
            )
        )

    @classmethod
    def piecewise_linear(
        cls,
        points: list[LossCurvePoint],
    ) -> "LossDefinition":
        """Create a piecewise-linear absolute-loss curve.

        The curve starts at ``(0, 0)`` and absolute loss is capped above the
        final point.
        """

        points = sorted(points, key=lambda point: point.inflow)
        if not points:
            return cls()
        if len({point.inflow for point in points}) != len(points):
            raise ValueError("loss-curve inflows must be unique")
        if points[0].inflow > 0:
            points.insert(0, LossCurvePoint(0.0, 0.0))
        elif points[0].loss != 0:
            raise ValueError("loss at inflow 0 must be 0")

        segments: list[ResolvedLossRelation] = []
        for index, (first, second) in enumerate(zip(points, points[1:])):
            slope = (second.loss - first.loss) / (
                second.inflow - first.inflow
            )
            if slope > 1 + TOLERANCE:
                raise ValueError(
                    "loss slope cannot exceed 1; remaining flow must be "
                    "non-decreasing"
                )
            intercept = first.loss - slope * first.inflow
            segments.append(
                ResolvedLossRelation(
                    index,
                    first.inflow,
                    second.inflow,
                    slope,
                    intercept,
                )
            )

        last = points[-1]
        segments.append(
            ResolvedLossRelation(
                len(segments),
                last.inflow,
                None,
                0.0,
                last.loss,
            )
        )
        return cls(segments=tuple(segments))

    @classmethod
    def time_varying_piecewise_linear(
        cls,
        intervals: list[LossInterval],
        default: "LossDefinition | None" = None,
    ) -> "LossDefinition":
        """Create a loss selected by date."""

        return cls(intervals=tuple(intervals), default=default)

    @property
    def is_time_varying(self) -> bool:
        return bool(self.intervals)

    def definition_for_date(self, value: str) -> "LossDefinition":
        """Return the static definition active on ``value``."""

        Date.fromisoformat(value)
        if not self.is_time_varying:
            return self
        for interval in self.intervals:
            if interval.contains(value):
                return interval.loss
        if self.default is not None:
            return self.default
        raise ValueError(f"no loss definition applies on {value}")

    def resolve(
        self,
        driver_flow: float,
        *,
        date: str | None = None,
        movement: int = 0,
    ) -> ResolvedLossRelation:
        """Return the active affine segment.

        At a breakpoint, positive movement selects the upper segment. Zero or
        negative movement selects the lower segment.
        """

        if driver_flow < 0 and driver_flow > -1e-4:
            driver_flow = 0

        if not isfinite(driver_flow) or driver_flow < 0:
            raise ValueError("driver_flow must be finite and non-negative")

        definition = self._static(date)
        candidates = [
            segment for segment in definition.segments
            if segment.contains(driver_flow)
        ]
        if not candidates:
            raise ValueError(f"no loss segment contains flow {driver_flow}")
        if movement > 0:
            return max(candidates, key=lambda x: x.min_driver_flow)
        return min(candidates, key=lambda x: x.min_driver_flow)

    def get_loss(self, inflow: float, *, date: str | None = None) -> float:
        """Return absolute loss for a non-negative inflow."""

        return self.resolve(inflow, date=date).loss_at(inflow)

    def has_loss(self, *, date: str | None = None) -> bool:
        """Return whether this definition can produce nonzero loss."""

        if self.is_time_varying and date is None:
            definitions = [interval.loss for interval in self.intervals]
            if self.default is not None:
                definitions.append(self.default)
            return any(definition.has_loss() for definition in definitions)

        return any(
            not isclose(segment.loss_slope, 0.0, abs_tol=TOLERANCE)
            or not isclose(segment.loss_intercept, 0.0, abs_tol=TOLERANCE)
            for segment in self._static(date).segments
        )

    def transform_total_flow(
        self,
        inflow: float,
        *,
        date: str | None = None,
    ) -> float:
        """Return total flow remaining below the loss."""

        remaining = self.resolve(inflow, date=date).remaining_at(inflow)
        if remaining < -TOLERANCE:
            raise ValueError("loss exceeds inflow")
        return max(0.0, remaining)

    def transform_component_increment(
        self,
        component_increment: float,
        *,
        driver_flow: float,
        date: str | None = None,
    ) -> float:
        """Apply the active segment's marginal remaining-flow factor."""

        movement = 1 if component_increment > 0 else -1
        relation = self.resolve(
            driver_flow,
            date=date,
            movement=movement,
        )
        return component_increment * relation.remaining_slope

    def inflow_for_remaining(
        self,
        remaining_flow: float,
        *,
        date: str | None = None,
    ) -> float:
        """Invert the monotone remaining-flow relation."""

        if not isfinite(remaining_flow) or remaining_flow < 0:
            raise ValueError("remaining_flow must be finite and non-negative")

        for relation in self._static(date).segments:
            if isclose(relation.remaining_slope, 0.0, abs_tol=TOLERANCE):
                if (
                    relation.min_driver_flow == 0
                    and isclose(remaining_flow, 0.0, abs_tol=TOLERANCE)
                ):
                    return 0.0
                continue

            inflow = (
                remaining_flow + relation.loss_intercept
            ) / relation.remaining_slope
            if inflow >= -TOLERANCE and relation.contains(max(0.0, inflow)):
                return max(0.0, inflow)

        raise ValueError(
            f"remaining flow {remaining_flow} does not identify an inflow"
        )

    def resolve_endpoint_loss(
        self,
        *,
        driver_flow: float,
        date: str | None = None,
        movement: int = 0,
        loss_state=None,
    ) -> ResolvedLossRelation:
        """Return the active endpoint segment."""

        del loss_state
        return self.resolve(driver_flow, date=date, movement=movement)

    def _static(self, date: str | None) -> "LossDefinition":
        if not self.is_time_varying:
            return self
        if date is None:
            raise ValueError("date is required for a time-varying loss")
        return self.definition_for_date(date)


    def get_fraction(self, date: str) -> float:
        """Return a constant fractional loss for the current LP build."""
        relation = self.resolve_endpoint_loss(driver_flow=0.0, date=date)
        if (
            relation.min_driver_flow != 0
            or relation.max_driver_flow is not None
            or not isclose(relation.loss_intercept, 0.0, abs_tol=1e-6)
        ):
            raise NotImplementedError(
                "Transaction allocation across piecewise-linear losses requires "
                "the active-segment LP implementation."
            )
        return relation.loss_slope