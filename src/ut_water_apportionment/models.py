from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from math import floor, isclose, isfinite
from .loss_models import LossDefinition


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
    measurements: 'MeasurementCollection'
    beg_date: str
    end_date: str

    # 2026-07-17: Maps flow_id -> list of daily natural flow values matching the date range
    external_natural_flows: dict[str, dict[str, float]] = field(default_factory=dict)

    def __post_init__(self):
        try:
            beg = date.fromisoformat(self.beg_date)
            end = date.fromisoformat(self.end_date)
        except ValueError as exc:
            raise ValueError(
                "Solver dates must use YYYY-MM-DD format: "
                f"{self.beg_date!r} to {self.end_date!r}"
            ) from exc

        if end < beg:
            raise ValueError(
                f"Solver end_date {self.end_date} "
                f"is before beg_date {self.beg_date}."
            )

        measurement_beg = date.fromisoformat(
            self.measurements.beg_date
        )
        measurement_end = date.fromisoformat(
            self.measurements.end_date
        )

        if beg < measurement_beg or end > measurement_end:
            raise ValueError(
                "Solver date range must be contained within the "
                "measurement date range: "
                f"solver={self.beg_date} to {self.end_date}, "
                f"measurements={self.measurements.beg_date} "
                f"to {self.measurements.end_date}."
            )

@dataclass
class SolverOutput:
    apportionments: list['SolverOutputApportionment']
    apportionments_audit: list['SolverOutputSolveGroupEvidence'] = field(
        default_factory=list
    )
    solver_backend: str | None = None

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





    def print_solve_steps(self, date: str | None = None) -> None:
        """Print one audit-table row for every variable changed by a solve.

        When ``date`` is provided, only iterations for that date are printed.
        When it is omitted, all dates are printed in chronological/sequence
        order.
        """

        def fmt_number(value: float | None) -> str:
            if value is None:
                return '-'
            return f'{value:.3f}'

        groups = [
            group
            for group in self.apportionments_audit
            if date is None or group.date == date
        ]
        groups.sort(key=lambda group: (group.date, group.sequence))

        rows: list[list[str]] = []
        for group in groups:
            for step in group.steps:
                rows.append([
                    group.date,
                    str(group.sequence),
                    step.variable_name,
                    fmt_number(step.value_before),
                    fmt_number(step.value_after),
                    fmt_number(step.value_after - step.value_before),
                    fmt_number(step.proportion_factor),
                    'Y' if group.limited_by_natural_flow else '',
                    group.reason or '-',
                ])

        if not rows:
            date_text = f' for {date}' if date is not None else ''
            print(f'No apportionment audit records were found{date_text}.')
            return

        headers = [
            'Date',
            'Seq',
            'Variable',
            'Before',
            'After',
            'Change',
            'Factor',
            'NF limit',
            'Reason',
        ]
        widths = [
            max(len(headers[index]), *(len(row[index]) for row in rows))
            for index in range(len(headers))
        ]

        def render(row: list[str]) -> str:
            cells = []
            for index, value in enumerate(row):
                if index in {1, 3, 4, 5, 6}:
                    cells.append(value.rjust(widths[index]))
                else:
                    cells.append(value.ljust(widths[index]))
            return ' | '.join(cells)

        print(render(headers))
        print('-+-'.join('-' * width for width in widths))
        for row in rows:
            print(render(row))



@dataclass
class SolverOutputApportionment:
    date: str
    interzone_flow_id: str
    txn_id: str
    value: float # A negative value indicates flow in the backward direction - but if it is zero then we need the following:
    is_forward: bool
    reason: str | None = None  # New field to identify the limiting factor


@dataclass
class SolverOutputSolveStepEvidence:
    variable_name: str
    value_before: float
    value_after: float
    proportion_factor: float | None = None

@dataclass
class SolverOutputSolveGroupEvidence:
    date: str
    sequence: int
    steps: list[SolverOutputSolveStepEvidence] = field(default_factory=list)
    reason: str | None = None
    limited_by_natural_flow: bool = False




@dataclass
class AccountingGraph:
    zones: list['Zone']
    interzone_flows: list['InterzoneFlow']


@dataclass
class Zone:
    id: str
    type: 'ZoneTypes'
    storage_meas_ids: list[str] = field(default_factory=list)


class ZoneTypes(Enum):
    STREAM = 'stream'
    STORAGE = 'storage'
    USE = 'use'
    IMPORT = 'import'
    SYSTEM_GAIN_LOSS = 'system-gain-loss'
    DEPLETION = 'depletion'


class NaturalFlowMode(Enum):
    ZERO = 'ZERO'
    CALCULATED = 'CALCULATED'
    SPECIFIED = 'SPECIFIED'


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
    loss_from_zone: LossDefinition = field(default_factory=LossDefinition)
    loss_to_zone: LossDefinition = field(default_factory=LossDefinition)

    # Natural-flow configuration. When natural_flow_mode is None, the
    # solver selects the default from the connected zone types.
    natural_flow_mode: NaturalFlowMode | None = None
    nf_measurements: list['FlowMeasurement'] = field(default_factory=list)

    # Not implemented yet - An InterzoneFlow may not neccessaraly be active for the entire run period.
    beg_date: str = '1000-01-01'
    end_date: str = '9999-12-31'

    def __post_init__(self):
        for name in ('lag_from_zone', 'lag_to_zone'):
            lag = getattr(self, name)

            if (
                isinstance(lag, bool)
                or not isinstance(lag, (int, float))
                or not isfinite(lag)
            ):
                raise ValueError(
                    f"{name} must be a finite number of days: {lag!r}"
                )

            if lag < 0:
                raise ValueError(
                    f'{name} cannot be negative: {lag}'
                )

    def set_default_natural_flow_mode(self, from_type:ZoneTypes, to_type:ZoneTypes):
        if self.natural_flow_mode is None:
            types = {from_type, to_type}
            if types == {ZoneTypes.STREAM}:
                self.natural_flow_mode = NaturalFlowMode.CALCULATED
            elif types == {ZoneTypes.STREAM, ZoneTypes.SYSTEM_GAIN_LOSS}:
                self.natural_flow_mode = NaturalFlowMode.CALCULATED
            else:
                self.natural_flow_mode = NaturalFlowMode.ZERO


@dataclass
class ZoneAccount:
    name: str
    starting_balance: float = 0



    # Don't allow incoming transactions to allow the balance to exceed
    # the ceiling:
    balance_ceiling: float | None = None

    # Don't allow outgoing delivery transactions to take the balance below
    # the floor. In many cases this will be zero, but it is possible for
    # negative balances to be allowed.
    balance_floor: float | None = None


@dataclass
class Trxn:
    id: str
    path: list['TrxnPathItem']
    upper_limit: 'float | AccountingLimit | None'
    priority: float = DEFAULT_TRXN_PRIORITY
    max_acft: float | None = None
    from_account: ZoneAccount | None = None
    to_account: ZoneAccount | None = None
    beg_date: str | None = None
    end_date: str | None = None
    is_slack: bool = False

    limit_by_remaining_account_balance: bool = False #~
    # What if we want to use the remaining_account_balance as the equal-priority proportion factor?


    def __post_init__(self):
        """Validates the data."""

        if isinstance(self.upper_limit, (int, float)) and self.upper_limit < 0:
            raise ValueError(
                f'Accounting limit cannot be negative: '
                f'{self.upper_limit} '
            )

        if self.priority < 0 or self.priority > DEFAULT_TRXN_PRIORITY:
            raise ValueError(
                f'priority must be >= 0 and <= {DEFAULT_TRXN_PRIORITY}:'
                f'{self.priority} '
            )

        if self.is_slack:
            self.priority = SLACK_TRXN_PRIORITY


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

    def __post_init__(self):
        if not isfinite(self.factor) or self.factor == 0:
            raise ValueError(
                f"Transaction path factor must be a finite, non-zero value: "
                f"{self.factor}"
            )

        if not 0 <= self.loss_before <= 1:
            raise ValueError(
                f"loss_before must be between 0 and 1: "
                f"{self.loss_before}"
            )

        if not 0 <= self.loss_after <= 1:
            raise ValueError(
                f"loss_after must be between 0 and 1: "
                f"{self.loss_after}"
            )

    def init_remaining_factor(self, rem_factor:float) -> float:
        """This function is meant to be called in sequence for all of the path
        items in a trxn, so we get the remaining factor given all preceding
        losses. """
        self.remaining_factor = rem_factor * (1-self.loss_before)
        return self.remaining_factor * (1-self.loss_after)


@dataclass
class AccountingLimit:
    intervals: list['AccountingLimitInterval']

    def __post_init__(self):
        self.intervals.sort(key=lambda x: x.beg_date)

        for previous, current in zip(
            self.intervals,
            self.intervals[1:],
        ):
            if current.beg_date < previous.end_date:
                raise ValueError(
                    "Accounting limit intervals overlap: "
                    f"{previous.beg_date} to {previous.end_date} and "
                    f"{current.beg_date} to {current.end_date}"
                )

@dataclass
class AccountingLimitInterval:
    beg_date: str
    end_date: str
    value: float

    def __post_init__(self):
        """Validates the data."""
        if self.value < 0:
            raise ValueError(
                f'Accounting limit cannot be negative: '
                f'{self.value} '
                f'({self.beg_date} to {self.end_date})'
            )

        try:
            beg = date.fromisoformat(self.beg_date)
            end = date.fromisoformat(self.end_date)
        except ValueError as exc:
            raise ValueError(
                "Accounting limit dates must use YYYY-MM-DD format: "
                f"{self.beg_date!r} to {self.end_date!r}"
            ) from exc

        if beg >= end:
            raise ValueError(
                "Accounting limit beg_date must be before end_date: "
                f"{self.beg_date} to {self.end_date}"
            )

@dataclass
class TrxnGroup:
    """A constraint that applies to a group of Transactions.
    """
    id: str
    children_trxns: list['Trxn | TrxnGroup']
    wrnum: str | None
    priority: float = DEFAULT_TRXN_PRIORITY
    upper_limit: 'float | AccountingLimit | None' = None
    max_acft: float | None = None
    comment: str | None = None
    beg_date: str | None = None
    end_date: str | None = None


    def __post_init__(self):
        """Validates the data."""
        if isinstance(self.upper_limit, (int, float)) and self.upper_limit < 0:
            raise ValueError(
                f'Accounting limit cannot be negative: '
                f'{self.upper_limit} '
            )

        if self.priority < 0 or self.priority > DEFAULT_TRXN_PRIORITY:
            raise ValueError(
                f'priority must be >= 0 and <= {DEFAULT_TRXN_PRIORITY}:'
                f'{self.priority} '
            )



@dataclass
class MeasurementSeries:
    id: str
    values: list[float | None]


@dataclass
class MeasurementCollection:
    series: list[MeasurementSeries]
    beg_date: str
    end_date: str # NOTE: this is an inclusive date. TODO: Consider changing name to avoid confusion with exclusive end_dates

    _series_by_id: dict[str, MeasurementSeries] = field(
        init=False,
        repr=False,
    )

    def __post_init__(self):
        beg = date.fromisoformat(self.beg_date)
        end = date.fromisoformat(self.end_date)

        if end < beg:
            raise ValueError(
                f"Measurement end_date {self.end_date} "
                f"is before beg_date {self.beg_date}."
            )

        expected_length = (end - beg).days + 1

        self._series_by_id = {}

        for series in self.series:
            if series.id in self._series_by_id:
                raise ValueError(
                    f"Duplicate measurement id: {series.id!r}"
                )

            if len(series.values) != expected_length:
                raise ValueError(
                    f"Measurement {series.id!r} has "
                    f"{len(series.values)} values; expected "
                    f"{expected_length} for {self.beg_date} "
                    f"through {self.end_date}."
                )

            self._series_by_id[series.id] = series

    def get(
        self,
        measurement_id: str,
        date_string: str,
        lag: float = 0,
    ) -> float | None:

        whole = floor(lag)
        fraction = lag - whole

        newer = self._get_value(
            measurement_id,
            date_string,
            whole,
        )

        if isclose(fraction, 0):
            return newer

        older = self._get_value(
            measurement_id,
            date_string,
            whole + 1,
        )

        if newer is None or older is None:
            return None

        return (
            newer * (1 - fraction)
            + older * fraction
        )

    def _get_value(
        self,
        measurement_id: str,
        date_string: str,
        lag: int = 0,
    ):
        try:
            series = self._series_by_id[str(measurement_id)]
        except KeyError:
            raise ValueError(
                f"Measurement {measurement_id!r} not recognized."
            )

        beg = date.fromisoformat(self.beg_date)
        current = date.fromisoformat(date_string)

        index = (current - beg).days - lag

        if 0 <= index < len(series.values):
            return series.values[index]

        return None

    def get_change(
        self,
        measurement_id: str,
        date_string: str,
        lag: float = 0,
    ) -> float | None:

        today = self.get(
            measurement_id,
            date_string,
            lag,
        )

        yesterday = self.get(
            measurement_id,
            date_string,
            lag + 1,
        )

        if today is None or yesterday is None:
            return None

        return today - yesterday


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


