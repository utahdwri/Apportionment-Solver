from __future__ import annotations
from dataclasses import dataclass
from datetime import date as Date
from math import isfinite
from typing import TypeAlias


@dataclass(frozen=True)
class LossCurvePoint:
    """One breakpoint on an absolute-loss curve.

    ``inflow`` is the non-negative flow immediately above the loss location.
    ``loss`` is the non-negative amount lost at that inflow, in the same units
    as ``inflow``. It is an amount, not a fraction.
    """
    inflow: float
    loss: float

    def __post_init__(self):
        if not isfinite(self.inflow) or self.inflow < 0:
            raise ValueError('LossCurvePoint.inflow must be finite and >= 0.')
        if not isfinite(self.loss) or self.loss < 0:
            raise ValueError('LossCurvePoint.loss must be finite and >= 0.')
        if self.loss > self.inflow:
            raise ValueError(
                'LossCurvePoint.loss cannot exceed its inflow. '
                f'Got inflow={self.inflow}, loss={self.loss}.'
            )


@dataclass
class LossCurve:
    """Piecewise-linear absolute loss as a function of inflow.

    The curve implicitly includes ``(0, 0)`` unless a point at zero is
    supplied. Between points it is linearly interpolated. Above the final
    point, the loss is capped at the final loss amount.
    """
    points: list[LossCurvePoint]

    def __post_init__(self):
        # Ensure the points are sorted so things work correctly.
        self.points = sorted(self.points, key=lambda p: p.inflow)

        if not self.points:
            return

        inflows = [point.inflow for point in self.points]
        if len(inflows) != len(set(inflows)):
            raise ValueError('LossCurve inflow breakpoints must be unique.')

        if self.points[0].inflow > 0:
            self.points.insert(0, LossCurvePoint(inflow=0.0, loss=0.0))
        elif self.points[0].loss != 0:
            raise ValueError('A LossCurve point at inflow=0 must have loss=0.')


    def get_loss(self, x) -> float:
        """Return the absolute loss for a non-negative inflow."""
        if not isfinite(x) or x < 0:
            raise ValueError('LossCurve inflow must be finite and >= 0.')

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



@dataclass(frozen=True)
class LossInterval:
    """A date interval over which one loss definition applies, inclusively."""
    beg_date: str
    end_date: str
    loss: LossDefinition

    def __post_init__(self):
        beg = Date.fromisoformat(self.beg_date)
        end = Date.fromisoformat(self.end_date)
        if end < beg:
            raise ValueError('LossInterval.end_date cannot precede beg_date.')
        validate_loss_definition(self.loss, allow_time_varying=False)

    def contains(self, date: str) -> bool:
        value = Date.fromisoformat(date)
        return Date.fromisoformat(self.beg_date) <= value <= Date.fromisoformat(self.end_date)

@dataclass
class TimeVaryingLoss:
    """Select a constant fraction or piecewise-linear curve by date.

    Intervals are inclusive and may not overlap. Set ``default`` explicitly if
    dates outside the listed intervals should be allowed; otherwise a missing
    date raises an error so gaps do not silently become zero loss.
    """
    intervals: list[LossInterval]
    default: LossDefinition | None = None

    def __post_init__(self):
        self.intervals = sorted(
            self.intervals,
            key=lambda interval: interval.beg_date,
        )
        if self.default is not None:
            validate_loss_definition(
                self.default,
                allow_time_varying=False,
            )

        for previous, current in zip(self.intervals, self.intervals[1:]):
            if Date.fromisoformat(current.beg_date) <= Date.fromisoformat(previous.end_date):
                raise ValueError(
                    'TimeVaryingLoss intervals may not overlap: '
                    f'{previous.beg_date}..{previous.end_date} and '
                    f'{current.beg_date}..{current.end_date}.'
                )

    def get_definition(self, date: str) -> LossDefinition:
        Date.fromisoformat(date)  # validate even when there are no intervals
        for interval in self.intervals:
            if interval.contains(date):
                return interval.loss
        if self.default is not None:
            return self.default
        raise ValueError(f'No loss definition applies on {date}.')

    def get_loss(self, inflow: float, date: str) -> float:
        return get_loss_amount(self.get_definition(date), inflow, date)


# A float retains the existing meaning: a constant loss fraction from 0 to 1.
LossDefinition: TypeAlias = float | LossCurve | TimeVaryingLoss


def validate_loss_definition(
        loss: LossDefinition,
        *,
        allow_time_varying: bool = True) -> None:
    """Validate any supported loss definition.

    ``TimeVaryingLoss`` is a top-level scheduling wrapper. Its intervals and
    default must resolve to a constant fraction or ``LossCurve`` rather than
    another ``TimeVaryingLoss``.
    """
    if isinstance(loss, TimeVaryingLoss):
        if not allow_time_varying:
            raise TypeError(
                'A TimeVaryingLoss interval/default cannot contain another '
                'TimeVaryingLoss.'
            )
        return

    if isinstance(loss, bool):
        raise TypeError(
            'A loss definition must be a numeric fraction, LossCurve, or '
            'TimeVaryingLoss; bool is not accepted.'
        )

    if isinstance(loss, (int, float)):
        if not isfinite(float(loss)) or not 0 <= float(loss) <= 1:
            raise ValueError('A constant loss fraction must be between 0 and 1.')
        return

    if not isinstance(loss, LossCurve):
        raise TypeError(
            'A loss definition must be a constant fraction, LossCurve, or '
            'TimeVaryingLoss.'
        )


def get_loss_amount(
        loss: LossDefinition,
        inflow: float,
        date: str | None = None) -> float:
    """Resolve any loss definition to an absolute loss amount."""
    if not isfinite(inflow) or inflow < 0:
        raise ValueError('Loss inflow must be finite and >= 0.')

    if isinstance(loss, TimeVaryingLoss):
        if date is None:
            raise ValueError('A date is required for TimeVaryingLoss.')
        return loss.get_loss(inflow, date)

    if isinstance(loss, LossCurve):
        return loss.get_loss(inflow)

    validate_loss_definition(loss)
    return inflow * float(loss)