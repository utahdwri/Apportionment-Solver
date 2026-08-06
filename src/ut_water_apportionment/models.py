from dataclasses import dataclass, field
from enum import Enum
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
    priority: float = -1
    max_acft: float | None = None
    from_account: ZoneAccount | None = None
    to_account: ZoneAccount | None = None
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
    lower_limit: float = 0
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


