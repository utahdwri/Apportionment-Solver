from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, timedelta
from math import floor, isclose, isfinite

from .models import SolverOutputApportionment


SeriesKey = tuple[str, str]


def unlag_series(
    dates: list[str],
    values: list[float],
    lag: float,
    *,
    boundary_value: float = 0.0,
    tolerance: float = 1e-12,
) -> tuple[list[str], list[float]]:
    """Undo the lag/interpolation applied by MeasurementCollection.get().

    For lag = whole + fraction, the forward lag operation is:

        y[t] =
            (1 - fraction) * x[t - whole]
            + fraction * x[t - whole - 1]

    For fractional lags, one boundary value is required.

    The recurrence is evaluated in the numerically stable direction:
      * fraction <= 0.5: solve forward
      * fraction > 0.5: solve backward

    This ensures the recurrence always divides by the larger interpolation
    coefficient.

    Returns values dated in the original measurement-time coordinate.
    """

    if len(dates) != len(values):
        raise ValueError("dates and values must have the same length.")

    if not dates:
        return [], []

    if not isfinite(lag) or lag < 0:
        raise ValueError(
            f"lag must be a finite, non-negative number: {lag}"
        )

    if not isfinite(boundary_value):
        raise ValueError(
            f"boundary_value must be finite: {boundary_value}"
        )

    if any(not isfinite(value) for value in values):
        raise ValueError("All values to unlag must be finite.")

    parsed_dates = [date.fromisoformat(value) for value in dates]

    # Fractional inversion requires one equation for each consecutive day.
    for previous, current in zip(
        parsed_dates,
        parsed_dates[1:],
    ):
        if current - previous != timedelta(days=1):
            raise ValueError(
                "Fractional unlagging requires consecutive daily values: "
                f"{previous} to {current}."
            )

    # Avoid tiny floating-point fractions around integer lag values.
    nearest_integer = round(lag)

    if isclose(
        lag,
        nearest_integer,
        abs_tol=tolerance,
    ):
        whole = int(nearest_integer)
        fraction = 0.0
    else:
        whole = floor(lag)
        fraction = lag - whole

    #
    # Integer lag: only the dates need to move.
    #
    if fraction == 0:
        output_dates = [
            value - timedelta(days=whole)
            for value in parsed_dates
        ]

        return (
            [value.isoformat() for value in output_dates],
            list(values),
        )

    newer_weight = 1.0 - fraction
    older_weight = fraction

    reconstructed = [0.0] * len(values)

    #
    # The equation is:
    #
    # y[t] = newer_weight * x[t]
    #      + older_weight * x[t - 1]
    #
    # after removing the whole-day portion of the lag.
    #

    if newer_weight >= older_weight:
        #
        # Stable forward recurrence.
        #
        # Boundary condition:
        #
        #     x[first_date - 1] = boundary_value
        #
        previous_value = boundary_value

        for index, lagged_value in enumerate(values):

            current_value = (
                lagged_value
                - older_weight * previous_value
            ) / newer_weight

            reconstructed[index] = current_value
            previous_value = current_value

        output_dates = [
            value - timedelta(days=whole)
            for value in parsed_dates
        ]

    else:
        #
        # Stable backward recurrence.
        #
        # Boundary condition:
        #
        #     x[last_date] = boundary_value
        #
        # where last_date is after removing the whole-day lag.
        #
        next_value = boundary_value

        for index in range(len(values) - 1, -1, -1):

            previous_value = (
                values[index]
                - newer_weight * next_value
            ) / older_weight

            reconstructed[index] = previous_value
            next_value = previous_value

        output_dates = [
            value - timedelta(days=whole + 1)
            for value in parsed_dates
        ]

    return (
        [value.isoformat() for value in output_dates],
        reconstructed,
    )


def unlag_apportionments(
    apportionments: list[SolverOutputApportionment],
    flow_lags: Mapping[str, float],
    *,
    boundary_values: Mapping[SeriesKey, float] | None = None,
    default_boundary_value: float = 0.0,
) -> list[SolverOutputApportionment]:
    """Convert solved apportionments from LP-time to measurement-time.

    Each transaction component on each flow is independently unlagged.

    boundary_values may optionally contain values keyed by:

        (interzone_flow_id, txn_id)

    Any series without an explicit value uses default_boundary_value.
    """

    boundary_values = boundary_values or {}

    # Use indices so the original result ordering is preserved.
    indices_by_series: dict[SeriesKey, list[int]] = defaultdict(list)

    for index, apportionment in enumerate(apportionments):
        key = (
            apportionment.interzone_flow_id,
            apportionment.txn_id,
        )
        indices_by_series[key].append(index)

    output = list(apportionments)

    for key, indices in indices_by_series.items():

        flow_id, txn_id = key

        if flow_id not in flow_lags:
            raise ValueError(
                f"No lag was calculated for flow {flow_id!r}."
            )

        # Recurrence must run chronologically even if the original
        # result list is stored in some other order.
        indices = sorted(
            indices,
            key=lambda index: apportionments[index].date,
        )

        rows = [
            apportionments[index]
            for index in indices
        ]

        boundary_value = boundary_values.get(
            key,
            default_boundary_value,
        )

        unlagged_dates, unlagged_values = unlag_series(
            dates=[row.date for row in rows],
            values=[row.value for row in rows],
            lag=flow_lags[flow_id],
            boundary_value=boundary_value,
        )

        for index, row, new_date, new_value in zip(
            indices,
            rows,
            unlagged_dates,
            unlagged_values,
        ):
            # A nonzero reconstructed value determines its direction.
            # Preserve the original direction flag for exact zero.
            is_forward = row.is_forward

            if not isclose(new_value, 0.0, abs_tol=1e-12):
                is_forward = new_value > 0

            output[index] = replace(
                row,
                date=new_date,
                value=new_value,
                is_forward=is_forward,
            )

    return output