import unittest
from ut_water_apportionment import (
    solve,
    AccountingGraph,
    AccountingLimit,
    AccountingLimitInterval,
    FlowMeasurement,
    FlowComponentsTypes,
    InterzoneFlow,
    MeasurementCollection,
    MeasurementSeries,
    SolverInput,
    SolverOutput,
    Trxn,
    TrxnGroup,
    TrxnPathItem,
    Zone,
    ZoneTypes
)
from ut_water_apportionment.loss_models import LossDefinition

# In case I want to see messages in the console:
import logging
import sys
logging.basicConfig(
    level=logging.WARN,
    stream=sys.stdout,
    force=True,
)


def parse_solver_input_from_dict(
        data: dict,
        results_dict: dict | None = None
        ) -> SolverInput:

    from collections import defaultdict
    from dataclasses import fields
    from enum import Enum

    # Add these imports if they aren't already imported in this module:
    from ut_water_apportionment.loss_models import (
        LossDefinition,
        LossInterval,
        LossCurvePoint,
        ResolvedLossRelation,
    )

    from ut_water_apportionment import NaturalFlowMode

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def construct(cls, **kwargs):
        """
        Construct a dataclass using only fields supported by the current
        version of that class.

        This makes this loader tolerant of fields that have been added or
        removed between solver versions.
        """
        supported = {f.name for f in fields(cls)}
        return cls(**{
            key: value
            for key, value in kwargs.items()
            if key in supported
        })


    def parse_enum(enum_type, value):
        """Accept enum instances, enum names, or enum values."""
        if value is None:
            return None

        if isinstance(value, enum_type):
            return value

        text = str(value)

        # Also tolerate strings such as "ZoneTypes.STREAM".
        prefix = enum_type.__name__ + "."
        if text.startswith(prefix):
            text = text[len(prefix):]

        try:
            return enum_type[text]
        except KeyError:
            pass

        for member in enum_type:
            if str(member.value).lower() == text.lower():
                return member

        raise ValueError(
            f"Unknown {enum_type.__name__} value {value!r}"
        )


    def parse_flow_measurement(
            value,
            default_adjustment_factor: float = 1.0
            ) -> FlowMeasurement | None:

        if value is None:
            return None

        if isinstance(value, FlowMeasurement):
            return value

        if isinstance(value, str):
            return FlowMeasurement(
                measurement_id=value,
                adjustment_factor=default_adjustment_factor,
            )

        measurement_id = (
            value.get('measurement_id')
            or value.get('node_id')
        )

        if measurement_id is None:
            return None

        return FlowMeasurement(
            measurement_id=str(measurement_id),
            adjustment_factor=float(
                value.get(
                    'adjustment_factor',
                    default_adjustment_factor
                )
            ),
        )


    def parse_loss(value) -> LossDefinition:
        """
        Parse the JSON/asdict representation of LossDefinition.

        Supports:
          - None
          - numeric constant fraction
          - {"fraction": ...}
          - {"points": [...]}
          - serialized {"segments": [...]}
          - serialized time-varying {"intervals": [...], "default": ...}
        """
        if value is None:
            return LossDefinition()

        if isinstance(value, LossDefinition):
            return value

        if isinstance(value, (int, float)):
            return LossDefinition.linear(float(value))

        if not isinstance(value, dict):
            raise TypeError(
                f"Cannot parse loss definition from {value!r}"
            )

        if 'fraction' in value:
            return LossDefinition.linear(float(value['fraction']))

        if 'points' in value:
            return LossDefinition.piecewise_linear([
                LossCurvePoint(
                    inflow=float(point['inflow']),
                    loss=float(point['loss']),
                )
                for point in value['points']
            ])

        # This is what dataclasses.asdict(LossDefinition(...)) produces.
        if value.get('segments'):
            return LossDefinition(
                segments=tuple(
                    ResolvedLossRelation(
                        segment_index=int(
                            segment.get('segment_index', index)
                        ),
                        min_driver_flow=float(
                            segment.get('min_driver_flow', 0)
                        ),
                        max_driver_flow=(
                            None
                            if segment.get('max_driver_flow') is None
                            else float(segment['max_driver_flow'])
                        ),
                        loss_slope=float(
                            segment.get('loss_slope', 0)
                        ),
                        loss_intercept=float(
                            segment.get('loss_intercept', 0)
                        ),
                    )
                    for index, segment
                    in enumerate(value['segments'])
                )
            )

        if value.get('intervals'):
            intervals = tuple(
                LossInterval(
                    beg_date=str(interval['beg_date']),
                    end_date=str(interval['end_date']),
                    loss=parse_loss(interval['loss']),
                )
                for interval in value['intervals']
            )

            default = value.get('default')

            return LossDefinition(
                intervals=intervals,
                default=(
                    None
                    if default is None
                    else parse_loss(default)
                ),
            )

        return LossDefinition()


    def parse_limit(limit_data):
        if limit_data is None:
            return None

        if isinstance(limit_data, (int, float, AccountingLimit)):
            return limit_data

        if 'intervals' not in limit_data:
            return limit_data

        return AccountingLimit(
            intervals=[
                AccountingLimitInterval(
                    beg_date=str(i['beg_date']),
                    end_date=str(i['end_date']),
                    value=float(i['value']),
                )
                for i in limit_data['intervals']
            ]
        )


    # ------------------------------------------------------------------
    # 1. Expected-value lookup
    # ------------------------------------------------------------------

    expected_lookup = {}

    if results_dict and 'apportionments' in results_dict:
        temp_lookup = defaultdict(list)

        for alloc in results_dict['apportionments']:
            flow_id = str(alloc['interzone_flow_id'])
            txn_id = str(alloc['txn_id']).replace('TRXN_', '')
            date_str = alloc['date']
            value = alloc['value']

            temp_lookup[(txn_id, flow_id)].append(
                (date_str, value)
            )

        for key, date_val_pairs in temp_lookup.items():
            expected_lookup[key] = [
                value
                for _, value in sorted(
                    date_val_pairs,
                    key=lambda x: x[0],
                )
            ]


    # ------------------------------------------------------------------
    # 2. Zones
    # ------------------------------------------------------------------

    zones = []

    for z in data['accounting_graph']['zones']:

        storage_ids = [
            str(value)
            for value in z.get('storage_meas_ids', [])
        ]

        # Legacy format.
        if not storage_ids and 'storage_contents' in z:
            storage_ids = [
                str(item['measurement_id'])
                for item in z['storage_contents']
                if item.get('measurement_id') is not None
            ]

        zones.append(
            construct(
                Zone,
                id=str(z['id']),
                type=parse_enum(ZoneTypes, z['type']),
                storage_meas_ids=storage_ids,

                # Preserves this field if the current Zone supports it.
                accounts=z.get('accounts', []),
            )
        )


    # ------------------------------------------------------------------
    # 3. Interzone flows
    # ------------------------------------------------------------------

    flows = []

    for f in data['accounting_graph']['interzone_flows']:

        # --------------------------------------------------------------
        # Current format
        # --------------------------------------------------------------

        if 'flow_type' in f:
            flow_type = parse_enum(
                FlowComponentsTypes,
                f['flow_type'],
            )
        else:
            flow_type = None

        if 'flow_measurements' in f:
            flow_measurements = [
                measurement
                for raw in f.get('flow_measurements', [])
                if (
                    measurement := parse_flow_measurement(raw)
                ) is not None
            ]

        # --------------------------------------------------------------
        # Legacy format
        # --------------------------------------------------------------
        else:
            flow_measurements = []

            positive_components = f.get(
                'pos_flow_components',
                [],
            )
            negative_components = f.get(
                'neg_flow_components',
                [],
            )

            if 'uhd_mapping' in f:
                mapping = f['uhd_mapping']
                positive_components = mapping.get(
                    'positive_flows',
                    [],
                )
                negative_components = mapping.get(
                    'negative_flows',
                    [],
                )

            calculated_type = None

            for component in positive_components:

                calculation = (
                    component.get('flow_calculation')
                    or {}
                )

                raw_type = (
                    component.get('flow_type')
                    or calculation.get('calculation_type')
                )

                if raw_type is not None:
                    text = str(raw_type)

                    if text in {
                        'FB_DEST',
                        'FLOW_BALANCE_OF_DESTINATION_ZONE',
                        'DESTINATION ZONE',
                    }:
                        calculated_type = (
                            FlowComponentsTypes
                            .FLOW_BALANCE_OF_DESTINATION_ZONE
                        )

                    elif text in {
                        'FB_SOURCE',
                        'FLOW_BALANCE_OF_SOURCE_ZONE',
                        'SOURCE ZONE',
                    }:
                        calculated_type = (
                            FlowComponentsTypes
                            .FLOW_BALANCE_OF_SOURCE_ZONE
                        )

                measurement = parse_flow_measurement(
                    component,
                    default_adjustment_factor=1.0,
                )

                if measurement is not None:
                    flow_measurements.append(measurement)

            # Negative input measurements represent reverse-direction
            # observations.
            for component in negative_components:

                measurement = parse_flow_measurement(
                    component,
                    default_adjustment_factor=-1.0,
                )

                if measurement is not None:
                    measurement.adjustment_factor = -abs(
                        measurement.adjustment_factor
                    )
                    flow_measurements.append(measurement)

            if flow_type is None:
                if calculated_type is not None:
                    flow_type = calculated_type
                elif flow_measurements:
                    flow_type = FlowComponentsTypes.OBSERVATION
                else:
                    flow_type = FlowComponentsTypes.EMPTY

        # If neither current nor legacy data supplied a type.
        if flow_type is None:
            flow_type = (
                FlowComponentsTypes.OBSERVATION
                if flow_measurements
                else FlowComponentsTypes.EMPTY
            )

        natural_flow_mode = f.get('natural_flow_mode')

        nf_measurements = [
            measurement
            for raw in f.get('nf_measurements', [])
            if (
                measurement := parse_flow_measurement(raw)
            ) is not None
        ]

        flows.append(
            construct(
                InterzoneFlow,
                id=str(f['id']),
                from_zone=str(f['from_zone']),
                to_zone=str(f['to_zone']),
                bidirectional=bool(
                    f.get('bidirectional', False)
                ),

                # Important fields that the old parser dropped:
                flow_type=flow_type,
                flow_measurements=flow_measurements,

                residual_for_gains=bool(
                    f.get('residual_for_gains', False)
                ),
                residual_for_losses=bool(
                    f.get('residual_for_losses', False)
                ),

                lag_from_zone=float(
                    f.get('lag_from_zone', 0)
                ),
                lag_to_zone=float(
                    f.get('lag_to_zone', 0)
                ),

                loss_from_zone=parse_loss(
                    f.get('loss_from_zone')
                ),
                loss_to_zone=parse_loss(
                    f.get('loss_to_zone')
                ),

                natural_flow_mode=(
                    None
                    if natural_flow_mode in (
                        None,
                        '',
                        'DEFAULT',
                    )
                    else parse_enum(
                        NaturalFlowMode,
                        natural_flow_mode,
                    )
                ),

                nf_measurements=nf_measurements,

                beg_date=str(
                    f.get('beg_date', '1000-01-01')
                ),
                end_date=str(
                    f.get('end_date', '9999-12-31')
                ),
            )
        )

    graph = AccountingGraph(
        zones=zones,
        interzone_flows=flows,
    )


    # ------------------------------------------------------------------
    # 4. Transactions
    # ------------------------------------------------------------------

    def parse_trxn(t):

        raw_id = str(
            t.get('id')
            or t.get('path_id')
        )

        t_id = (
            raw_id
            if raw_id.startswith('TRXN_')
            else f'TRXN_{raw_id}'
        )

        priority = t.get('priority_order')

        if priority is None:
            priority = t.get('priority', -1)

        # --------------------------------------------------------------
        # Transaction group
        # --------------------------------------------------------------

        if 'children_trxns' in t:

            return construct(
                TrxnGroup,
                id=t_id,
                children_trxns=[
                    parse_trxn(child)
                    for child in t['children_trxns']
                ],
                wrnum=t.get('wrnum'),
                priority=priority,
                upper_limit=parse_limit(
                    t.get('upper_limit')
                ),
                lower_limit=t.get('lower_limit', 0),
                max_acft=t.get('max_acft'),
                comment=t.get('comment'),
                beg_date=t.get('beg_date'),
                end_date=t.get('end_date'),
            )

        # --------------------------------------------------------------
        # Ordinary transaction
        # --------------------------------------------------------------

        path = []

        for p in t.get('path', []):

            flow_id = str(
                p.get('flow_id')
                or p.get('flow_name')
            )

            if t.get('path_id') is not None:
                lookup_id = str(t['path_id'])
            else:
                lookup_id = raw_id.replace(
                    'TRXN_',
                    '',
                )

            # results_dict takes precedence when supplied.
            expected = expected_lookup.get(
                (lookup_id, flow_id),
                p.get('expected_values'),
            )

            path.append(
                construct(
                    TrxnPathItem,
                    flow_id=flow_id,
                    factor=float(
                        p.get('factor', 1.0)
                    ),
                    expected_values=expected,

                    # Previously omitted:
                    loss_before=float(
                        p.get('loss_before', 0)
                    ),
                    loss_after=float(
                        p.get('loss_after', 0)
                    ),
                    remaining_factor=float(
                        p.get('remaining_factor', 1)
                    ),
                )
            )

        return construct(
            Trxn,
            id=t_id,
            path=path,
            upper_limit=parse_limit(
                t.get('upper_limit')
            ),
            priority=priority,
            lower_limit=t.get('lower_limit', 0),
            max_acft=t.get('max_acft'),
            from_account=t.get('from_account'),
            to_account=t.get('to_account'),
            beg_date=t.get('beg_date'),
            end_date=t.get('end_date'),
            is_slack=bool(
                t.get('is_slack', False)
            ),
            limit_by_remaining_account_balance=bool(
                t.get(
                    'limit_by_remaining_account_balance',
                    False,
                )
            ),
        )


    txns = [
        parse_trxn(t)
        for t in data.get(
            'transactions',
            data.get('txns', []),
        )
    ]


    # ------------------------------------------------------------------
    # 5. Measurements
    # ------------------------------------------------------------------

    raw_measurements = data['measurements']

    if isinstance(raw_measurements, MeasurementCollection):
        measurements = raw_measurements

    elif 'series' in raw_measurements:
        measurements = MeasurementCollection(
            beg_date=str(raw_measurements['beg_date']),
            end_date=str(raw_measurements['end_date']),
            series=[
                MeasurementSeries(
                    id=str(series['id']),
                    values=list(series['values']),
                )
                for series in raw_measurements['series']
            ],
        )

    # Older dictionary format.
    else:
        measurement_beg_date = data.get(
            'measurement_beg_date',
            data['beg_date'],
        )
        measurement_end_date = data.get(
            'measurement_end_date',
            data['end_date'],
        )

        measurements = MeasurementCollection(
            beg_date=str(measurement_beg_date),
            end_date=str(measurement_end_date),
            series=[
                MeasurementSeries(
                    id=str(measurement_id),
                    values=list(values),
                )
                for measurement_id, values
                in raw_measurements.items()
            ],
        )


    # ------------------------------------------------------------------
    # 6. SolverInput
    # ------------------------------------------------------------------

    return construct(
        SolverInput,
        accounting_graph=graph,
        txns=txns,
        measurements=measurements,
        beg_date=str(data['beg_date']),
        end_date=str(data['end_date']),

        # Included for compatibility with older SolverInput versions.
        measurement_beg_date=measurements.beg_date,
        measurement_end_date=measurements.end_date,

        # This was also omitted by the old parser.
        external_natural_flows={
            str(flow_id): {
                str(date): float(value)
                for date, value in values.items()
            }
            for flow_id, values
            in data.get(
                'external_natural_flows',
                {},
            ).items()
        },
    )


class HelperUtilitiesTests(unittest.TestCase):

    def test_loop_through_date_range(self):
        """Test that the function that helps loop through days works as
        expected."""

        from ut_water_apportionment.solver import (
            _loop_through_date_range
        )

        results = [d for d in _loop_through_date_range('2026-01-01',
                                                      '2026-01-05')]
        self.assertEqual(len(results), 5)
        self.assertEqual(results[0], '2026-01-01')
        self.assertEqual(results[-1], '2026-01-05')





class A_SingleReachProblems(unittest.TestCase):

    def test_simple_apportionments(self):
        """Does the measured diversion get apportioned correctly to its parts
        when the measured diversion is LESS than the sum of allowed rights?
        (I.e. the junior rights should not recieve a full apportionment.)
        """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="USER", type=ZoneTypes.USE),
                ],
                interzone_flows=[
                    InterzoneFlow(id="RIVER>USER", from_zone="RIVER", to_zone="USER",
                                  flow_measurements=[FlowMeasurement(measurement_id="1")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id='1', values=[12])
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit= 3, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[3])]),
                Trxn(id='TRXN_2', priority=2, upper_limit= 6, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[6])]),
                Trxn(id='TRXN_3', priority=3, upper_limit=12, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[3])]),
                Trxn(id='TRXN_4', priority=4, upper_limit= 4, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[0])]),
            ]
        )
        results = solve(input, check_expected_values=True)

    def test_simple_apportionments_2(self):
        """Does the measured diversion get apportioned correctly to its parts
        when the measured diversion is MORE than the sum of allowed rights?
        """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="USER", type=ZoneTypes.USE)
                ],
                interzone_flows=[
                    InterzoneFlow(id="RIVER>USER", from_zone="RIVER", to_zone="USER",
                                  flow_measurements=[FlowMeasurement(measurement_id="1")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id='1', values=[100])
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit= 3, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[ 3])]),
                Trxn(id='TRXN_2', priority=2, upper_limit= 6, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[ 6])]),
                Trxn(id='TRXN_3', priority=3, upper_limit=12, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[12])]),
                Trxn(id='TRXN_4', priority=4, upper_limit= 4, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[ 4])]),
            ]
        )
        solve(input, check_expected_values=True)

    def test_equal_priority_apportionments(self):
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                    Zone(id="C", type=ZoneTypes.USE)
                ],
                interzone_flows=[
                    InterzoneFlow(id="RIVER>A", from_zone="RIVER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="RIVER>B", from_zone="RIVER", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="B")]),
                    InterzoneFlow(id="RIVER>C", from_zone="RIVER", to_zone="C", flow_measurements=[FlowMeasurement(measurement_id="C")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id='A', values=[10]),
                MeasurementSeries(id='B', values=[5]),
                MeasurementSeries(id='C', values=[6]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=1, path=[TrxnPathItem(flow_id='RIVER>A', expected_values=[1])]), #limited by water right
                Trxn(id='TRXN_2', priority=1, upper_limit=6, path=[TrxnPathItem(flow_id='RIVER>B', expected_values=[5])]), #limited by measured diversion
                Trxn(id='TRXN_3', priority=1, upper_limit=6, path=[TrxnPathItem(flow_id='RIVER>C', expected_values=[3])]), #limited by proportion
                Trxn(id='TRXN_4', priority=1, upper_limit=6, path=[TrxnPathItem(flow_id='RIVER>C', expected_values=[3])]), #limited by proportion
            ]
        )
        solve(input, check_expected_values=True)

    def test_equal_priority_apportionments_no_diversions(self):
        """
        2/5/2025 - The program was not handeling equal-priority apportionments
        correctly when all upper-limits are zero.
        """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                    Zone(id="C", type=ZoneTypes.USE)
                ],
                interzone_flows=[
                    InterzoneFlow(id="RIVER>A", from_zone="RIVER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="RIVER>B", from_zone="RIVER", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="B")]),
                    InterzoneFlow(id="RIVER>C", from_zone="RIVER", to_zone="C", flow_measurements=[FlowMeasurement(measurement_id="C")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id='A', values=[0]),
                MeasurementSeries(id='B', values=[0]),
                MeasurementSeries(id='C', values=[0]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=0, path=[TrxnPathItem(flow_id='RIVER>A', expected_values=[0])]), #limited by water right
                Trxn(id='TRXN_2', priority=1, upper_limit=0, path=[TrxnPathItem(flow_id='RIVER>B', expected_values=[0])]), #limited by measured diversion
                Trxn(id='TRXN_3', priority=1, upper_limit=0, path=[TrxnPathItem(flow_id='RIVER>C', expected_values=[0])]), #limited by proportion
                Trxn(id='TRXN_4', priority=1, upper_limit=0, path=[TrxnPathItem(flow_id='RIVER>C', expected_values=[0])]), #limited by proportion
            ]
        )
        results = solve(input, check_expected_values=True)

    def test_reach_with_negative_storage_change(self):
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM, storage_meas_ids=["dS"]),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="RIVER>A", from_zone="RIVER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                MeasurementSeries(id='A', values=[10,10]),
                MeasurementSeries(id='dS', values=[10,0]),
            ]),
            txns=[]
        )

        results = solve(input)

        values = results.get_result_value(
            date='2000-01-02',
            flow_id='SYS>RIVER',
        )

        self.assertAlmostEqual(0, sum(x.value for x in values), delta=1e-4)

    def test_reach_with_positive_storage_change(self):
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM, storage_meas_ids=["dS"]),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="RIVER>A", from_zone="RIVER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                MeasurementSeries(id='A', values=[10,10]),
                MeasurementSeries(id='dS', values=[10,12]),
            ]),
            txns=[]
        )

        results = solve(input)

        values = results.get_result_value(
            date='2000-01-02',
            flow_id='SYS>RIVER',
        )

        self.assertAlmostEqual(12, sum(x.value for x in values), delta=1e-4)

    def test_huge_number_of_equal_priority_rights(self):
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                    Zone(id="C", type=ZoneTypes.USE)
                ],
                interzone_flows=[
                    InterzoneFlow(id="RIVER>A", from_zone="RIVER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="RIVER>B", from_zone="RIVER", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="B")]),
                    InterzoneFlow(id="RIVER>C", from_zone="RIVER", to_zone="C", flow_measurements=[FlowMeasurement(measurement_id="C")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id='A', values=[10]),
                MeasurementSeries(id='B', values=[5]),
                MeasurementSeries(id='C', values=[6]),
            ]),
            txns=[]
        )

        small_val = 0.5/47
        n = 500
        total = small_val
        for i in range(1, n):
            total += i/47

        for i in range(1, n):
            input.txns.append(
                Trxn(id='TRXN_'+str(i), priority=1, upper_limit=i/47, path=[TrxnPathItem(flow_id='RIVER>A', expected_values=[i/47/total*10])])
            )

        input.txns.append(
            Trxn(id='TRXN_'+str(n), priority=1, upper_limit=small_val, path=[TrxnPathItem(flow_id='RIVER>A', expected_values=[small_val/total*10])])
        )

        results = solve(input, check_expected_values=True)





class B_Reservoirs(unittest.TestCase):

    def reservoir_problem_input(self, stor_chg=0, stor_loss=0, Q_AB=0,
                                Q_DIV1=0, Q_BC=0, Q_DIV2=0, Q_DIV3=0,
                                Q_CD=0) -> SolverInput:
        """
        1---#-->2------> \\~~~3~~~/ ---#-->4----->5---#-->6
                #         \\_____/         #      #
                |                          |      |
                v                          v      v
                200                        400    500

        """
        return SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="REACH-C", type=ZoneTypes.STREAM),
                    Zone(id="REACH-D", type=ZoneTypes.STREAM),
                    Zone(id="STOR", type=ZoneTypes.STORAGE, storage_meas_ids=["dS"]),
                    Zone(id="DIV-1", type=ZoneTypes.USE),
                    Zone(id="DIV-2", type=ZoneTypes.USE),
                    Zone(id="DIV-3", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", flow_measurements=[FlowMeasurement(measurement_id="A>B")]),
                    InterzoneFlow(id="B>C", from_zone="REACH-B", to_zone="REACH-C", flow_measurements=[FlowMeasurement(measurement_id="B>C")]),
                    InterzoneFlow(id="C>D", from_zone="REACH-C", to_zone="REACH-D", flow_measurements=[FlowMeasurement(measurement_id="C>D")]),
                    InterzoneFlow(id="B>STOR", from_zone="REACH-B", to_zone="STOR", bidirectional=True),
                    InterzoneFlow(id="B>1", from_zone="REACH-B", to_zone="DIV-1", flow_measurements=[FlowMeasurement(measurement_id="B>1")]),
                    InterzoneFlow(id="C>2", from_zone="REACH-C", to_zone="DIV-2", flow_measurements=[FlowMeasurement(measurement_id="C>2")]),
                    InterzoneFlow(id="C>3", from_zone="REACH-C", to_zone="DIV-3", flow_measurements=[FlowMeasurement(measurement_id="C>3")]),
                    InterzoneFlow(id="Flow1", from_zone="SYS", to_zone="REACH-A", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow3", from_zone="SYS", to_zone="REACH-B", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow5", from_zone="SYS", to_zone="REACH-C", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                 MeasurementSeries(id='dS',  values=[0, stor_chg+stor_loss]),
                 MeasurementSeries(id='A>B', values=[0, Q_AB]),
                 MeasurementSeries(id='B>C', values=[0, Q_BC]),
                 MeasurementSeries(id='C>D', values=[0, Q_CD]),
                 MeasurementSeries(id='B>1', values=[0, Q_DIV1]),
                 MeasurementSeries(id='C>2', values=[0, Q_DIV2]),
                 MeasurementSeries(id='C>3', values=[0, Q_DIV3])
            ]),
            txns=[]
        )

    def test_simple_reservoir(self):
        """Check if things still work if there is a reservoir."""

        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="STORAGE", type=ZoneTypes.STORAGE, storage_meas_ids=["STORAGE"]),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                ],
                interzone_flows=[
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="RIVER>STORAGE", from_zone="RIVER", to_zone="STORAGE", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="RIVER>A", from_zone="RIVER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="RIVER>B", from_zone="RIVER", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="B")]),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                 MeasurementSeries(id='STORAGE', values=[0, 0]),
                 MeasurementSeries(id='A', values=[0, 10]),
                 MeasurementSeries(id='B', values=[0, 10])
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='RIVER>A')]),
                Trxn(id='TRXN_2', priority=1, upper_limit=4, path=[TrxnPathItem(flow_id='RIVER>B')]),
                Trxn(id='TRXN_3', priority=2, upper_limit=50, path=[TrxnPathItem(flow_id='RIVER>STORAGE')]),
                Trxn(id='TRXN_4', priority=3, upper_limit=None, path=[TrxnPathItem(flow_id='RIVER>STORAGE', factor=-1), TrxnPathItem(flow_id='RIVER>A', expected_values=[8])]),
                Trxn(id='TRXN_5', priority=3, upper_limit=None, path=[TrxnPathItem(flow_id='RIVER>STORAGE', factor=-1), TrxnPathItem(flow_id='RIVER>B', expected_values=[6])]),
            ]
        )
        solve(input, check_expected_values=True)

    def test_simple_reservoir_with_downstream_gage(self):
        """Check if things are still good even if there is a reservoir that has
        a downstream measurement that constrains apporitonments to delieveries."""

        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="UPSTREAM", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="DOWNSTREAM", type=ZoneTypes.STREAM),
                    Zone(id="STORAGE", type=ZoneTypes.STORAGE, storage_meas_ids=["STORAGE"]),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                ],
                interzone_flows=[
                    InterzoneFlow(id="SYS>UPSTREAM", from_zone="SYS", to_zone="UPSTREAM", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="SYS>DOWNSTREAM", from_zone="SYS", to_zone="DOWNSTREAM", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="UPSTREAM>DOWNSTREAM", from_zone="UPSTREAM", to_zone="DOWNSTREAM", flow_measurements=[FlowMeasurement(measurement_id="Q")]),
                    InterzoneFlow(id="UPSTREAM>STORAGE", from_zone="UPSTREAM", to_zone="STORAGE", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="DOWNSTREAM>A", from_zone="DOWNSTREAM", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="DOWNSTREAM>B", from_zone="DOWNSTREAM", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="B")]),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                 MeasurementSeries(id='STORAGE', values=[0, 0]),
                 MeasurementSeries(id='A', values=[0, 10]),
                 MeasurementSeries(id='B', values=[0, 10]),
                 MeasurementSeries(id='Q', values=[0, 2])
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='DOWNSTREAM>A')]),
                Trxn(id='TRXN_2', priority=1, upper_limit=4, path=[TrxnPathItem(flow_id='DOWNSTREAM>B')]),
                Trxn(id='TRXN_3', priority=2, upper_limit=50, path=[TrxnPathItem(flow_id='UPSTREAM>STORAGE')]),
                Trxn(id='TRXN_4', priority=3, upper_limit=None, path=[TrxnPathItem(flow_id='UPSTREAM>STORAGE', factor=-1), TrxnPathItem(flow_id='UPSTREAM>DOWNSTREAM'), TrxnPathItem(flow_id='DOWNSTREAM>A', expected_values=[1])]),
                Trxn(id='TRXN_5', priority=3, upper_limit=None, path=[TrxnPathItem(flow_id='UPSTREAM>STORAGE', factor=-1), TrxnPathItem(flow_id='UPSTREAM>DOWNSTREAM'), TrxnPathItem(flow_id='DOWNSTREAM>B', expected_values=[1])]),
            ]
        )
        results = solve(input, check_expected_values=True)

    def test_trivial(self):
        input = self.reservoir_problem_input(stor_chg=0, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=4, Q_DIV3=4, Q_CD=1)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[2])]),
            Trxn(id='TRXN_2', priority=2, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[4])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[4])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[0])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[0])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[0])])
        ])
        results = solve(input, check_expected_values=True)

    def test_storage_diversions_with_no_deliveries(self):
        input = self.reservoir_problem_input(stor_chg=2, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=4, Q_DIV3=4, Q_CD=1)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[2])]),
            Trxn(id='TRXN_2', priority=2, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[4])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[4])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[2])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[0])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[0])])
        ])
        results = solve(input, check_expected_values=True)

    def test_storage_deliveries_with_no_diversions(self):
        input = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=6, Q_DIV3=4, Q_CD=1)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[2])]),
            Trxn(id='TRXN_2', priority=2, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[4])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[4])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[0])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[0])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[2])])
        ])
        results = solve(input, check_expected_values=True)

    def test_equal_priority_deliveries(self):
        input = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=6, Q_DIV3=4, Q_CD=1)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[2])]),
            Trxn(id='TRXN_2', priority=2, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[4])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[4])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[0])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[0])]),
            Trxn(id='TRXN_6', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[2])])
        ])
        results = solve(input, check_expected_values=True)

    def test_storage_deliveries_and_diversions_and_losses(self):
        input = self.reservoir_problem_input(stor_chg=-1, stor_loss=1, Q_AB=5, Q_DIV1=2, Q_BC=7, Q_DIV2=6, Q_DIV3=4, Q_CD=1)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[2])]),
            Trxn(id='TRXN_2', priority=2, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[4])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[4])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[2])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[0])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[2])])
        ])
        results = solve(input, check_expected_values=True)

    def test_equal_priority_apportionmnets(self):
        input = self.reservoir_problem_input(stor_chg=-4, stor_loss=1, Q_AB=2, Q_DIV1=1, Q_BC=4, Q_DIV2=2+2, Q_DIV3=3+1, Q_CD=1)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[1])]),
            Trxn(id='TRXN_2', priority=1, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[2+.4])]),
            Trxn(id='TRXN_3', priority=1, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[3+.6])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[0])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[1-.6])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[2-.4])])
        ])
        results = solve(input, check_expected_values=True)

    def test_equal_priority_apportionmnets2(self):
        """Test equal priority apportionments when the storage right also has
        an equal priority.

        For this case there is 4 cfs of gain above the reservoir.
        There is another 8 cfs of gain below the reservoir.

        There are multiple solutions to this problem!!

                New Solution   Old Solution
                ------------   ------------
        TRXN_1      1               1
        TRXN_2      4               3.2
        TRXN_3      6               4.8
        TRXN_4      1               3
        TRXN_5      1               2.2     (Storage delivery)
        TRXN_6      3               3.8     (Storage delivery)

        If our convention is to prefer the solution that minimizes the absolute
        value of the positive and negative transaction components on the flow
        to the reservoir, we would take Solution A. (I think this is reasonable
        for an on-stream reservoir operator to bear the cost of ambiguity that
        results from the on-stream reservoir; they could measure reservoir
        inflows & outflow seperately if they prefer. So I think a convention
        that results in smaller reservoir inflows and deliveries is better than
        one that results in smaller NF allocations traditional diverters.)
        """
        input = self.reservoir_problem_input(stor_chg=-4, stor_loss=1, Q_AB=0, Q_DIV1=1, Q_BC=6, Q_DIV2=7, Q_DIV3=7, Q_CD=0)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[1])]),
            Trxn(id='TRXN_2', priority=1, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[4])]),
            Trxn(id='TRXN_3', priority=1, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[6])]),
            Trxn(id='TRXN_4', priority=1, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[1])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[7 - 6])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[7 - 4])])
        ])
        results = solve(input, check_expected_values=True)

    def test_change_water_that_is_not_available_at_htf_source(self):
        input = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=0, Q_DIV1=0, Q_BC=2, Q_DIV2=6, Q_DIV3=8, Q_CD=0)
        input.txns.extend([
            Trxn(id='TRXN_2', priority=2, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[4])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[6])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[0])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[2])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[0])]),
            Trxn(id='TRXN_101', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[0])])
        ])
        results = solve(input, check_expected_values=True)

    def test_change_water_that_is_available_at_htf_source(self):
        input = self.reservoir_problem_input(stor_chg=-2, stor_loss=0, Q_AB=2, Q_DIV1=0, Q_BC=4, Q_DIV2=6, Q_DIV3=8, Q_CD=0)
        input.txns.extend([
            Trxn(id='TRXN_101', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[2])]),
            Trxn(id='TRXN_2', priority=2, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[4])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[6])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[0])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[2])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[0])])
        ])
        results = solve(input, check_expected_values=True)

    def test_spill_to_natural_flow(self):
        input = self.reservoir_problem_input(stor_chg=-5, stor_loss=0, Q_AB=0, Q_DIV1=2, Q_BC=5, Q_DIV2=0, Q_DIV3=0, Q_CD=5)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[2])]),
            Trxn(id='TRXN_2', priority=2, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[0])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[0])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[0])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[0])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[0])])
        ])
        results = solve(input, check_expected_values=True)

    def test_storage_diversion_exceeds_storage_right(self):
        input = self.reservoir_problem_input(stor_chg=25, stor_loss=0, Q_AB=0, Q_DIV1=0, Q_BC=0, Q_DIV2=0, Q_DIV3=0, Q_CD=0)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[0])]),
            Trxn(id='TRXN_2', priority=2, upper_limit=4, path=[TrxnPathItem(flow_id='C>2', expected_values=[0])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=6, path=[TrxnPathItem(flow_id='C>3', expected_values=[0])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=20, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[20])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[0])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[0])])
        ])
        results = solve(input, check_expected_values=True)

    def test_presentation_example(self):
        input = self.reservoir_problem_input(stor_chg=-10, stor_loss=0, Q_AB=0, Q_DIV1=0, Q_BC=20, Q_DIV2=15, Q_DIV3=10, Q_CD=5)
        input.txns.extend([
            Trxn(id='TRXN_1', priority=1, upper_limit=2, path=[TrxnPathItem(flow_id='B>1', expected_values=[0])]),
            Trxn(id='TRXN_2', priority=2, upper_limit=5, path=[TrxnPathItem(flow_id='C>2', expected_values=[5])]),
            Trxn(id='TRXN_3', priority=3, upper_limit=2, path=[TrxnPathItem(flow_id='C>3', expected_values=[2])]),
            Trxn(id='TRXN_4', priority=4, upper_limit=100, path=[TrxnPathItem(flow_id='B>STOR', expected_values=[8])]),
            Trxn(id='TRXN_5', priority=9900, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>3', expected_values=[8])]),
            Trxn(id='TRXN_6', priority=9901, upper_limit=None, path=[TrxnPathItem(flow_id='B>STOR', factor=-1), TrxnPathItem(flow_id='B>C'), TrxnPathItem(flow_id='C>2', expected_values=[10])])
        ])
        results = solve(input, check_expected_values=True)

    def test_water_rights_cant_steal_storage_water(self):
        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="S", type=ZoneTypes.STREAM),
                    Zone(id="R", type=ZoneTypes.STORAGE, storage_meas_ids=["dS"]),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="OUT", type=ZoneTypes.STREAM),
                ],
                interzone_flows=[
                    InterzoneFlow(id="S>A", from_zone="S", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="S>R", from_zone="S", to_zone="R", bidirectional=True),
                    InterzoneFlow(id="SYS>S", from_zone="SYS", to_zone="S", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="S>OUT", from_zone="S", to_zone="OUT", flow_measurements=[FlowMeasurement(measurement_id="OUTFLOW")]),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                 MeasurementSeries(id='dS', values=[0, -5]),
                 MeasurementSeries(id='A', values=[0, 5]),
                 MeasurementSeries(id='OUTFLOW', values=[0, 0]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1900, upper_limit=5, path=[TrxnPathItem(flow_id='S>A', expected_values=[0])]),
                Trxn(id='TRXN_2', priority=1950, upper_limit=100, path=[TrxnPathItem(flow_id='S>R', expected_values=[0])]),
                Trxn(id='TRXN_3', priority=9999, upper_limit=None, path=[TrxnPathItem(flow_id='S>R', factor=-1), TrxnPathItem(flow_id='S>A', expected_values=[5])])
            ]
        )
        results = solve(input, check_expected_values=True)

    def test_water_rights_cant_steal_storage_water_v2(self):
        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="S", type=ZoneTypes.STREAM),
                    Zone(id="OUTFLOW", type=ZoneTypes.STREAM),
                    Zone(id="R", type=ZoneTypes.STORAGE, storage_meas_ids=["dS"]),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="S>A", from_zone="S", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="S>R", from_zone="S", to_zone="R", bidirectional=True),
                    InterzoneFlow(id="S>OUT", from_zone="S", to_zone="OUTFLOW", flow_measurements=[FlowMeasurement(measurement_id="OUT")]),
                    InterzoneFlow(id="SYS>S", from_zone="SYS", to_zone="S", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                 MeasurementSeries(id='dS', values=[0, -5]),
                 MeasurementSeries(id='A', values=[0, 5]),
                 MeasurementSeries(id='OUT', values=[0, 5]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1900, upper_limit=5, path=[TrxnPathItem(flow_id='S>A', expected_values=[5])]),
                Trxn(id='TRXN_2', priority=1950, upper_limit=100, path=[TrxnPathItem(flow_id='S>R', expected_values=[0])]),
                Trxn(id='TRXN_3', priority=9999, upper_limit=None, path=[TrxnPathItem(flow_id='S>R', factor=-1), TrxnPathItem(flow_id='S>A', expected_values=[0])])
            ]
        )
        results = solve(input, check_expected_values=True)


    def test_water_exchanged_btwn_resvs(self):

        """
        I'm seeing continued issues with reservoir spills. This is a
        simplification of an issue I found on Lake Fork.

        I expect the natural flow below reach-1 to be 5. (gains of 5)
        I expect the natural flow below reach-2 to be 0. (loss of 5)

        In this case, when
        maximizing the exchange from S2 to S1 the solver finds a work-around
        by maximizing the ML to BSW delivery and then spilling from BSW. In the
        6/1/2026 version, we prevented spills from the destination zone, but
        not any others.
        """"""

        Reach 1     <--------------->   Storage 1

           |
           |
           |
           v

        Reach 2     <--------------->   Storage 2

        """

        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="R1", type=ZoneTypes.STREAM),
                    Zone(id="R2", type=ZoneTypes.STREAM),
                    Zone(id="S1", type=ZoneTypes.STORAGE, storage_meas_ids=["dS1"]),
                    Zone(id="S2", type=ZoneTypes.STORAGE, storage_meas_ids=["dS2"]),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="R1>R2", from_zone="R1", to_zone="R2", flow_measurements=[FlowMeasurement(measurement_id="R1-R2")]),
                    InterzoneFlow(id="R1>S1", from_zone="R1", to_zone="S1", bidirectional=True),
                    InterzoneFlow(id="R2>S2", from_zone="R2", to_zone="S2", bidirectional=True),

                    InterzoneFlow(id="SYS>R1", from_zone="SYS", to_zone="R1", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="SYS>R2", from_zone="SYS", to_zone="R2", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                 MeasurementSeries(id='dS1', values=[0, 0]),
                 MeasurementSeries(id='dS2', values=[0, 0]),
                 MeasurementSeries(id='R1-R2', values=[0, 5]),
            ]),
            txns=[

                # Move from S1 to S2
                Trxn(id='TRXN_1', priority=1, upper_limit=1000, path=[
                    TrxnPathItem(flow_id='R2>S2', factor=-1, expected_values=[0]),
                    TrxnPathItem(flow_id='R1>R2', factor=-1, expected_values=[0]),
                    TrxnPathItem(flow_id='R1>S1', expected_values=[0])
                ]),

                # Reach to S2
                Trxn(id='TRXN_2', priority=2, upper_limit=1000, path=[
                    TrxnPathItem(flow_id='R2>S2', factor=1, expected_values=[0])
                ])
            ]
        )
        results = solve(input, check_expected_values=True)



    def test_experiment(self):
        """
        The purpose of this test was to explore a case where it was not at first clear
        what the correct answer should be.

        R1                       #  Gain to R2 = 5 cfs
        |          (5cfs)        #  NF in R2 = 10 cfs
        v                        #
        R2  <-> S  (dS=0)        # T1. Storage delivery to R3 (expect 5 cfs; now there must be 5 cfs comming in, either T3 or unauthorized.)
            --> A  (5cfs)        # T2. Diversion to A from R1 (expect 5 cfs; now T3 must be zero and there must be an unauthorized inflow to storage -- is this ok? I think Yes)
        |          (5cfs)        # T3. Diversion into storage from R1 (expect 0 cfs)
        v                        #
        R3                       #



        """
        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="R1", type=ZoneTypes.STREAM),
                    Zone(id="R2", type=ZoneTypes.STREAM),
                    Zone(id="R3", type=ZoneTypes.STREAM),
                    Zone(id="S", type=ZoneTypes.STORAGE, storage_meas_ids=["dS"]),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="R1>R2", from_zone="R1", to_zone="R2", flow_measurements=[FlowMeasurement(measurement_id="R1-R2")]),
                    InterzoneFlow(id="R2>R3", from_zone="R2", to_zone="R3", flow_measurements=[FlowMeasurement(measurement_id="R2-R3")]),
                    InterzoneFlow(id="R2>S", from_zone="R2", to_zone="S", bidirectional=True),
                    InterzoneFlow(id="R2>A", from_zone="R2", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="R2-A")]),
                    InterzoneFlow(id="SYS>R1", from_zone="SYS", to_zone="R1", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="SYS>R2", from_zone="SYS", to_zone="R2", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="SYS>R3", from_zone="SYS", to_zone="R3", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                 MeasurementSeries(id='dS', values=[0, 0]),
                 MeasurementSeries(id='R1-R2', values=[0, 5]),
                 MeasurementSeries(id='R2-R3', values=[0, 5]),
                 MeasurementSeries(id='R2-A', values=[0, 5]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=5, path=[TrxnPathItem(flow_id='R2>S', factor=-1), TrxnPathItem(flow_id='R2>R3', expected_values=[5])]),
                Trxn(id='TRXN_2', priority=2, upper_limit=5, path=[TrxnPathItem(flow_id='R1>R2', expected_values=[5]), TrxnPathItem(flow_id='R2>A', expected_values=[5])]),
                Trxn(id='TRXN_3', priority=3, upper_limit=5, path=[TrxnPathItem(flow_id='R1>R2', expected_values=[0]), TrxnPathItem(flow_id='R2>S', expected_values=[0])]),
            ]
        )
        results = solve(input, check_expected_values=True)



    def test_spill_makes_natural_flow_available_downstream(self):

        input = self.reservoir_problem_input(
            stor_chg=-5,
            stor_loss=0,
            Q_AB=0,
            Q_DIV1=0,
            Q_BC=5,
            Q_DIV2=5,
            Q_DIV3=0,
            Q_CD=0,
        )

        input.txns.extend([
            Trxn(
                id='TRXN_2',
                priority=2,
                upper_limit=4,
                path=[
                    TrxnPathItem(
                        flow_id='C>2',
                        expected_values=[4],
                    )
                ],
            )
        ])

        results = solve(input, check_expected_values=True)


class B_Imports(unittest.TestCase):

    def test_1(self):
        """ """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="S", type=ZoneTypes.STREAM),
                    Zone(id="IMP", type=ZoneTypes.IMPORT),
                    Zone(id="DIV", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="OUT", type=ZoneTypes.STREAM),
                ],
                interzone_flows=[
                    InterzoneFlow(id="IMP>S", from_zone="IMP", to_zone="S", flow_measurements=[FlowMeasurement(measurement_id="IMP")]),
                    InterzoneFlow(id="S>DIV", from_zone="S", to_zone="DIV", flow_measurements=[FlowMeasurement(measurement_id="DIV")]),
                    InterzoneFlow(id="SYS>S", from_zone="SYS", to_zone="S", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="S>OUT", from_zone="S", to_zone="OUT", flow_measurements=[FlowMeasurement(measurement_id="OUT")]),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                 MeasurementSeries(id='IMP', values=[5]),
                 MeasurementSeries(id='DIV', values=[5]),
                 MeasurementSeries(id='OUT', values=[0]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=10, upper_limit=5, path=[TrxnPathItem(flow_id='IMP>S'), TrxnPathItem(flow_id='S>DIV', expected_values=[5])]),
                Trxn(id='TRXN_2', priority=2, upper_limit=5, path=[TrxnPathItem(flow_id='S>DIV', expected_values=[0])]),
            ]
        )

        results = solve(input, check_expected_values=True)


    def test_unmeasured_import(self):
        """Imports really should be measured. But if they are not, then we
        would expect that the import transaction should be less than the
        observed gain in the reach.

        Should transaction paths be allowed to traverse a gain flow?
        (This test says yes)

        But isn't it possible that there is 8 cfs of gains and then 5 cfs of
        losses?
        (The test says we should not account in that way)

        """

        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="UPPER", type=ZoneTypes.STREAM),
                    Zone(id="LOWER", type=ZoneTypes.STREAM),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="Upper>Lower", from_zone="UPPER", to_zone="LOWER", flow_measurements=[FlowMeasurement(measurement_id="Q")]),
                    InterzoneFlow(id="Lower>A", from_zone="LOWER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="SYS>UPPER", from_zone="SYS", to_zone="UPPER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="SYS>LOWER", from_zone="SYS", to_zone="LOWER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                 MeasurementSeries(id='Q', values=[5]),
                 MeasurementSeries(id='A', values=[8]),
            ]),
            txns=[
                # TRXN_1 should be limited to the gains in the lower reach
                Trxn(id='TRXN_1', priority=1, upper_limit=10, path=[TrxnPathItem(flow_id='SYS>LOWER'), TrxnPathItem(flow_id='Lower>A', expected_values=[3])]),
                # TRXN_2 should be the remaining diversion
                Trxn(id='TRXN_2', priority=2, upper_limit=10, path=[TrxnPathItem(flow_id='Lower>A', expected_values=[5])])
            ]
        )

        results = solve(input, check_expected_values=True)




class D_PrioritySeries(unittest.TestCase):

    def test_1(self):
        """
        """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="USER", type=ZoneTypes.USE)
                ],
                interzone_flows=[
                    InterzoneFlow(id="Diversion", from_zone="RIVER", to_zone="USER", flow_measurements=[FlowMeasurement(measurement_id="1")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                 MeasurementSeries(id='1', values=[12]),
            ]),
            txns=[
                TrxnGroup(
                    id='TRXN_1',
                    wrnum=None,
                    priority=1,
                    upper_limit=7,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_2', priority=2, upper_limit=6, path=[TrxnPathItem(flow_id='Diversion', expected_values=[6])]),
                        Trxn(id='TRXN_3', priority=3, upper_limit=12, path=[TrxnPathItem(flow_id='Diversion', expected_values=[1])]),
                        Trxn(id='TRXN_4', priority=4, upper_limit=4, path=[TrxnPathItem(flow_id='Diversion', expected_values=[0])])
                    ]
                )
            ]
        )

        results = solve(input, check_expected_values=True)


    def test_2(self):
        """
        This is a case that should be solved in two iterations:

        1st: TRXN-2 gets 0.5 proportion, resulting in 3.75,
             TRXN-5 gets 0.3 proportion, resulting in 2.25
             TRXN-6 gets 0.2 proportion, resulting in 1.5

        2nd: TRXN-3 should be increased with 0.5 proportion from zero to 1.25
             TRXN-6 should be increased with 0.5 proportion from 1.5 to 2.75

        This test checks if during the 2nd iteration TRXN-6 is properly increased
        from its starting, non-zero value.
        """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE)
                ],
                interzone_flows=[
                    InterzoneFlow(id="DiversionA", from_zone="RIVER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id="DiversionB", from_zone="RIVER", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="B")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                 MeasurementSeries(id='A', values=[6]),
                 MeasurementSeries(id='B', values=[4]),
            ]),
            txns=[
                TrxnGroup(
                    id='TRXN_1',
                    wrnum=None,
                    priority=1,
                    upper_limit=10,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_2', priority=10, upper_limit=4, path=[TrxnPathItem(flow_id='DiversionA', expected_values=[3])]),
                        Trxn(id='TRXN_3', priority=20, upper_limit=6, path=[TrxnPathItem(flow_id='DiversionB', expected_values=[2])])
                    ]
                ),
                TrxnGroup(
                    id='TRXN_4',
                    wrnum=None,
                    priority=1,
                    upper_limit=10,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_5', priority=10, upper_limit=6, path=[TrxnPathItem(flow_id='DiversionA', expected_values=[3])]),
                        Trxn(id='TRXN_6', priority=10, upper_limit=4, path=[TrxnPathItem(flow_id='DiversionB', expected_values=[2])])
                    ]
                )
            ]
        )

        results = solve(input, check_expected_values=True)


    def test_3(self):
        """
        """
        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="UPPER", type=ZoneTypes.STREAM),
                    Zone(id="LOWER", type=ZoneTypes.STREAM),
                    Zone(id="RESV", type=ZoneTypes.STORAGE, storage_meas_ids=["dS"]),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                    Zone(id="C", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="UPPER>LOWER", from_zone="UPPER", to_zone="LOWER", flow_measurements=[FlowMeasurement(measurement_id="UPPER>LOWER")]),
                    InterzoneFlow(id="UPPER>RESV", from_zone="UPPER", to_zone="RESV", bidirectional=True),
                    InterzoneFlow(id=">A", from_zone="LOWER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id=">B", from_zone="LOWER", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="B")]),
                    InterzoneFlow(id=">C", from_zone="LOWER", to_zone="C", flow_measurements=[FlowMeasurement(measurement_id="C")]),
                    InterzoneFlow(id="Flow1", from_zone="SYS", to_zone="UPPER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow3", from_zone="SYS", to_zone="LOWER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                 MeasurementSeries(id='UPPER>LOWER', values=[0,2]),
                 MeasurementSeries(id='dS', values=[0,10]),
                 MeasurementSeries(id='A', values=[0,2]),
                 MeasurementSeries(id='B', values=[0,8]),
                 MeasurementSeries(id='C', values=[0,5]),
            ]),
            txns=[
                TrxnGroup(
                    id='TRXN_2',
                    wrnum=None,
                    priority=1,
                    upper_limit=4,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_20', priority=10, upper_limit=None, path=[TrxnPathItem(flow_id='>A', expected_values=[2])]),
                        Trxn(id='TRXN_21', priority=11, upper_limit=None, path=[TrxnPathItem(flow_id='UPPER>LOWER', factor=-1), TrxnPathItem(flow_id='UPPER>RESV', expected_values=[2])])
                    ]
                ),
                TrxnGroup(
                    id='TRXN_3',
                    wrnum=None,
                    priority=1,
                    upper_limit=8,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_30', priority=10, upper_limit=None, path=[TrxnPathItem(flow_id='>B', expected_values=[8])]),
                        Trxn(id='TRXN_31', priority=11, upper_limit=None, path=[TrxnPathItem(flow_id='UPPER>LOWER', factor=-1), TrxnPathItem(flow_id='UPPER>RESV', expected_values=[0])])
                    ]
                ),
                TrxnGroup(
                    id='TRXN_4',
                    wrnum=None,
                    priority=1,
                    upper_limit=9,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_40', priority=10, upper_limit=None, path=[TrxnPathItem(flow_id='>C', expected_values=[5])]),
                        Trxn(id='TRXN_41', priority=11, upper_limit=None, path=[TrxnPathItem(flow_id='UPPER>LOWER', factor=-1), TrxnPathItem(flow_id='UPPER>RESV', expected_values=[4])])
                    ]
                )
            ]
        )

        results = solve(input, check_expected_values=True)


    @unittest.skip("I don't agree with these test results any more. 7/22/2026")
    def test_4(self):
        """
        Same as previous, but now with a much smaller limit on diversions into the reservoir.
        """
        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="UPPER", type=ZoneTypes.STREAM),
                    Zone(id="LOWER", type=ZoneTypes.STREAM),
                    Zone(id="RESV", type=ZoneTypes.STORAGE, storage_meas_ids=["dS"]),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                    Zone(id="C", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="UPPER>LOWER", from_zone="UPPER", to_zone="LOWER", flow_measurements=[FlowMeasurement(measurement_id="UPPER>LOWER")]),
                    InterzoneFlow(id="UPPER>RESV", from_zone="UPPER", to_zone="RESV", bidirectional=True),
                    InterzoneFlow(id=">A", from_zone="LOWER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id=">B", from_zone="LOWER", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="B")]),
                    InterzoneFlow(id=">C", from_zone="LOWER", to_zone="C", flow_measurements=[FlowMeasurement(measurement_id="C")]),
                    InterzoneFlow(id="Flow1", from_zone="SYS", to_zone="UPPER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow3", from_zone="SYS", to_zone="LOWER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                 MeasurementSeries(id='UPPER>LOWER', values=[0,2]),
                 MeasurementSeries(id='dS', values=[0,3]),
                 MeasurementSeries(id='A', values=[0,2]),
                 MeasurementSeries(id='B', values=[0,8]),
                 MeasurementSeries(id='C', values=[0,5]),
            ]),
            txns=[
                TrxnGroup(
                    id='TRXN_2',
                    wrnum=None,
                    priority=1,
                    upper_limit=4,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_20', priority=10, upper_limit=None, path=[TrxnPathItem(flow_id='>A', expected_values=[2])]),
                        Trxn(id='TRXN_21', priority=11, upper_limit=None, path=[TrxnPathItem(flow_id='UPPER>LOWER', factor=-1), TrxnPathItem(flow_id='UPPER>RESV', expected_values=[0.22222222222222222 + 2.7777777777777777 * 2/6.5])])
                    ]
                ),
                TrxnGroup(
                    id='TRXN_3',
                    wrnum=None,
                    priority=1,
                    upper_limit=8,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_30', priority=10, upper_limit=None, path=[TrxnPathItem(flow_id='>B', expected_values=[8])]),
                        Trxn(id='TRXN_31', priority=11, upper_limit=None, path=[TrxnPathItem(flow_id='UPPER>LOWER', factor=-1), TrxnPathItem(flow_id='UPPER>RESV', expected_values=[0])])
                    ]
                ),
                TrxnGroup(
                    id='TRXN_4',
                    wrnum=None,
                    priority=1,
                    upper_limit=9,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_40', priority=10, upper_limit=None, path=[TrxnPathItem(flow_id='>C', expected_values=[5])]),
                        Trxn(id='TRXN_41', priority=11, upper_limit=None, path=[TrxnPathItem(flow_id='UPPER>LOWER', factor=-1), TrxnPathItem(flow_id='UPPER>RESV', expected_values=[0 + 2.777777 * 4.5/6.5])])
                    ]
                )
            ]
        )

        results = solve(input, check_expected_values=True)


    def test_Leahs_3_reach_problem(self):
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="Imports", type=ZoneTypes.IMPORT),
                    Zone(id="R1", type=ZoneTypes.STREAM),
                    Zone(id="R2", type=ZoneTypes.STREAM),
                    Zone(id="R3", type=ZoneTypes.STREAM),
                    Zone(id="Downstream", type=ZoneTypes.STREAM),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                    Zone(id="C", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="Imports>R1", from_zone="Imports", to_zone="R1", flow_measurements=[FlowMeasurement(measurement_id="Imports>R1")]),
                    InterzoneFlow(id="R1>R2", from_zone="R1", to_zone="R2", flow_measurements=[FlowMeasurement(measurement_id="R1>R2")]),
                    InterzoneFlow(id="R2>R3", from_zone="R2", to_zone="R3", flow_measurements=[FlowMeasurement(measurement_id="R2>R3")]),
                    InterzoneFlow(id="R3>Downstream", from_zone="R3", to_zone="Downstream", flow_measurements=[FlowMeasurement(measurement_id="R3>")]),
                    InterzoneFlow(id="R1>A", from_zone="R1", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="R1>A")]),
                    InterzoneFlow(id="R2>B", from_zone="R2", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="R2>B")]),
                    InterzoneFlow(id="R3>C", from_zone="R3", to_zone="C", flow_measurements=[FlowMeasurement(measurement_id="R3>C")]),
                    InterzoneFlow(id="Flow1", from_zone="SYS", to_zone="R1", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow3", from_zone="SYS", to_zone="R2", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow5", from_zone="SYS", to_zone="R3", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow7", from_zone="SYS", to_zone="Downstream", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                 MeasurementSeries(id="Imports>R1", values=[40]),
                 MeasurementSeries(id="R1>R2", values=[10]),
                 MeasurementSeries(id="R2>R3", values=[30]),
                 MeasurementSeries(id="R3>", values=[5]),
                 MeasurementSeries(id="R1>A", values=[50]),
                 MeasurementSeries(id="R2>B", values=[30]),
                 MeasurementSeries(id="R3>C", values=[20]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=30, path=[TrxnPathItem(flow_id='R1>A', expected_values=[20])]),
                Trxn(id='TRXN_2', priority=1, upper_limit=20, path=[TrxnPathItem(flow_id='R2>B', expected_values=[20])]),
                Trxn(id='TRXN_3', priority=1, upper_limit=40, path=[TrxnPathItem(flow_id='R3>C', expected_values=[20])]),
                Trxn(id='TRXN_11', priority=2, upper_limit=None, path=[TrxnPathItem(flow_id='Imports>R1'), TrxnPathItem(flow_id='R1>A')]),
                Trxn(id='TRXN_12', priority=3, upper_limit=None, path=[TrxnPathItem(flow_id='Imports>R1'), TrxnPathItem(flow_id='R1>R2'), TrxnPathItem(flow_id='R2>B')]),
                Trxn(id='TRXN_13', priority=4, upper_limit=None, path=[TrxnPathItem(flow_id='Imports>R1'), TrxnPathItem(flow_id='R1>R2'), TrxnPathItem(flow_id='R2>R3'), TrxnPathItem(flow_id='R3>C')])
            ]
        )

        results = solve(input, check_expected_values=True)

        print('\nRESULTS: \n')
        results.print_solve_steps()


    def test_Leahs_3_reach_problem2(self):
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="Imports", type=ZoneTypes.IMPORT),
                    Zone(id="R1", type=ZoneTypes.STREAM),
                    Zone(id="R2", type=ZoneTypes.STREAM),
                    Zone(id="R3", type=ZoneTypes.STREAM),
                    Zone(id="Downstream", type=ZoneTypes.STREAM),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE),
                    Zone(id="C", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="Imports>R1", from_zone="Imports", to_zone="R1", flow_measurements=[FlowMeasurement(measurement_id="Imports>R1")]),
                    InterzoneFlow(id="R1>R2", from_zone="R1", to_zone="R2", flow_measurements=[FlowMeasurement(measurement_id="R1>R2")]),
                    InterzoneFlow(id="R2>R3", from_zone="R2", to_zone="R3", flow_measurements=[FlowMeasurement(measurement_id="R2>R3")]),
                    InterzoneFlow(id="R3>Downstream", from_zone="R3", to_zone="Downstream", flow_measurements=[FlowMeasurement(measurement_id="R3>")]),
                    InterzoneFlow(id="R1>A", from_zone="R1", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="R1>A")]),
                    InterzoneFlow(id="R2>B", from_zone="R2", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="R2>B")]),
                    InterzoneFlow(id="R3>C", from_zone="R3", to_zone="C", flow_measurements=[FlowMeasurement(measurement_id="R3>C")]),
                    InterzoneFlow(id="Flow1", from_zone="SYS", to_zone="R1", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow3", from_zone="SYS", to_zone="R2", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow5", from_zone="SYS", to_zone="R3", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow7", from_zone="SYS", to_zone="Downstream", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                 MeasurementSeries(id="Imports>R1", values=[100]),
                 MeasurementSeries(id="R1>R2", values=[10]),
                 MeasurementSeries(id="R2>R3", values=[30]),
                 MeasurementSeries(id="R3>", values=[5]),
                 MeasurementSeries(id="R1>A", values=[50]),
                 MeasurementSeries(id="R2>B", values=[30]),
                 MeasurementSeries(id="R3>C", values=[20]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=30, path=[TrxnPathItem(flow_id='R1>A', expected_values=[0])]),
                Trxn(id='TRXN_2', priority=1, upper_limit=20, path=[TrxnPathItem(flow_id='R2>B', expected_values=[20])]),
                Trxn(id='TRXN_3', priority=1, upper_limit=40, path=[TrxnPathItem(flow_id='R3>C', expected_values=[20])]),
                Trxn(id='TRXN_11', priority=2, upper_limit=None, path=[TrxnPathItem(flow_id='Imports>R1'), TrxnPathItem(flow_id='R1>A')]),
                Trxn(id='TRXN_12', priority=3, upper_limit=None, path=[TrxnPathItem(flow_id='Imports>R1'), TrxnPathItem(flow_id='R1>R2'), TrxnPathItem(flow_id='R2>B')]),
                Trxn(id='TRXN_13', priority=4, upper_limit=None, path=[TrxnPathItem(flow_id='Imports>R1'), TrxnPathItem(flow_id='R1>R2'), TrxnPathItem(flow_id='R2>R3'), TrxnPathItem(flow_id='R3>C')])
            ]
        )

        results = solve(input, check_expected_values=True)




class E_SharedTrxnLimits(unittest.TestCase):

    def test_1(self):
        """ Can trxn-2 share the limit of trxn-1? I.e., be limited to its remaining right?
        """

        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="A", type=ZoneTypes.USE),
                    Zone(id="B", type=ZoneTypes.USE)
                ],
                interzone_flows=[
                    InterzoneFlow(id=">A", from_zone="RIVER", to_zone="A", flow_measurements=[FlowMeasurement(measurement_id="A")]),
                    InterzoneFlow(id=">B", from_zone="RIVER", to_zone="B", flow_measurements=[FlowMeasurement(measurement_id="B")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="A", values=[10]),
                MeasurementSeries(id="B", values=[10]),
            ]),
            txns=[
                TrxnGroup(
                    id='TRXN_GRP',
                    wrnum=None,
                    priority=0,
                    upper_limit=15,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='TRXN_1', priority=1, upper_limit=None, path=[TrxnPathItem(flow_id='>A', expected_values=[10])]),
                        Trxn(id='TRXN_2', priority=2, upper_limit=None, path=[TrxnPathItem(flow_id='>B', expected_values=[5])])
                    ]
                )
            ]
        )

        results = solve(input, check_expected_values=True)


    def test_dylans_example(self):
        """This is a good example to test a mix of equal-priority rights along
        with shared cfs constraints. It shows that in some cases, we need the
        shared-cfs allocations to remain flexble until after the parent-level
        allocations have been completed -- that locking in incremental child
        allocations can prevent us from getting to the optimal solution.
        """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="DIV-1", type=ZoneTypes.USE),
                    Zone(id="DIV-2", type=ZoneTypes.USE),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>1", from_zone="REACH-A", to_zone="DIV-1", flow_measurements=[FlowMeasurement(measurement_id="A>1")]),
                    InterzoneFlow(id="A>2", from_zone="REACH-A", to_zone="DIV-2", flow_measurements=[FlowMeasurement(measurement_id="A>2")]),
                    InterzoneFlow(id="SYS>REACH-A", from_zone="SYS", to_zone="REACH-A", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", flow_measurements=[FlowMeasurement(measurement_id="A>B")])
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="A>1", values=[20]),
                MeasurementSeries(id="A>2", values=[20]),
                MeasurementSeries(id="A>B", values=[0]),
            ]),
            txns=[
                Trxn(id='WR_123', priority=1, upper_limit=10, path=[TrxnPathItem(flow_id='A>1', expected_values=[8.888888889])]),
                Trxn(id='WR_234', priority=1, upper_limit=20, path=[TrxnPathItem(flow_id='A>2', expected_values=[17.77777778])]),
                TrxnGroup(
                    id='WR_345',
                    wrnum=None,
                    priority=1,
                    upper_limit=15,
                    max_acft=None,
                    children_trxns=[
                        Trxn(id='WR_345_1', priority=2, upper_limit=None, path=[TrxnPathItem(flow_id='A>1', expected_values=[11.11111111])]),
                        Trxn(id='WR_345_2', priority=2, upper_limit=None, path=[TrxnPathItem(flow_id='A>2', expected_values=[2.222222222])])
                    ]
                )
            ]
        )

        results = solve(input, check_expected_values=True)



class G_DocumentationExamples(unittest.TestCase):

    def test_summary_example(self):
        """
        """
        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="STOR", type=ZoneTypes.STORAGE, storage_meas_ids=["dS"]),
                    Zone(id="STOR-EVAP", type=ZoneTypes.USE),
                    Zone(id="DIV-1", type=ZoneTypes.USE),
                    Zone(id="DIV-2", type=ZoneTypes.USE),
                    Zone(id="REACH-C", type=ZoneTypes.STREAM),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", flow_measurements=[FlowMeasurement(measurement_id="A>B")]),
                    InterzoneFlow(id="A>STOR", from_zone="REACH-A", to_zone="STOR", bidirectional=True),
                    InterzoneFlow(id="evap-loss", from_zone="STOR", to_zone="STOR-EVAP", bidirectional=True, flow_measurements=[FlowMeasurement(measurement_id="evap-loss")]),
                    InterzoneFlow(id="B>1", from_zone="REACH-B", to_zone="DIV-1", flow_measurements=[FlowMeasurement(measurement_id="B>1")]),
                    InterzoneFlow(id="B>2", from_zone="REACH-B", to_zone="DIV-2", flow_measurements=[FlowMeasurement(measurement_id="B>2")]),
                    InterzoneFlow(id="SYS>REACH-A", from_zone="SYS", to_zone="REACH-A", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="SYS>REACH-B", from_zone="SYS", to_zone="REACH-B", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="B>C", from_zone="REACH-B", to_zone="REACH-C", flow_measurements=[FlowMeasurement(measurement_id="B>C")]),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                MeasurementSeries(id="A>B", values=[0, 90]),
                MeasurementSeries(id="dS" , values=[0, -35]),
                MeasurementSeries(id="evap-loss", values=[0, 5]),
                MeasurementSeries(id="B>1", values=[0, 50]),
                MeasurementSeries(id="B>2", values=[0, 50]),
                MeasurementSeries(id="B>C", values=[0, 0]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1880, upper_limit=40, path=[TrxnPathItem(flow_id='B>1', expected_values=[40])]),
                Trxn(id='TRXN_2', priority=1890, upper_limit=20, path=[TrxnPathItem(flow_id='B>1', expected_values=[10])]),
                Trxn(id='TRXN_3', priority=1890, upper_limit=40, path=[TrxnPathItem(flow_id='B>2', expected_values=[20])]),
                Trxn(id='TRXN_4', priority=1950, upper_limit=100, path=[TrxnPathItem(flow_id='A>STOR', expected_values=[0])]),
                Trxn(id='TRXN_5', priority=9901, upper_limit=None, path=[
                    TrxnPathItem(flow_id='A>STOR', factor=-1),
                    TrxnPathItem(flow_id='A>B'),
                    TrxnPathItem(flow_id='B>2', expected_values=[30])
                ]),
            ]
        )

        results = solve(input, check_expected_values=True)



class I_TimeLags(unittest.TestCase):

    def test_gain_calc_for_one_reach_with_integer_day_lags(self):
        """
        --[+2,0]--> DIV 1

        --[+1,0]--> DIV 2

        |
        |[0,1]
        |
        v

        --[0,0]--> DIV 3

        |
        |[0,0]  D1(+3) + D2(+2) + D3(+0)
        |
        v


        """
        input = SolverInput(
            beg_date='2000-01-03',
            end_date='2000-01-05',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="DIV-1", type=ZoneTypes.USE),
                    Zone(id="DIV-2", type=ZoneTypes.USE),
                    Zone(id="DIV-3", type=ZoneTypes.USE),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="REACH-C", type=ZoneTypes.STREAM),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>1", from_zone="REACH-A", to_zone="DIV-1", lag_from_zone=2, flow_measurements=[FlowMeasurement(measurement_id="A>1")]),
                    InterzoneFlow(id="A>2", from_zone="REACH-A", to_zone="DIV-2", lag_from_zone=1, flow_measurements=[FlowMeasurement(measurement_id="A>2")]),
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", lag_to_zone=1, flow_measurements=[FlowMeasurement(measurement_id="A>B")]),
                    InterzoneFlow(id="B>3", from_zone="REACH-B", to_zone="DIV-3", lag_from_zone=0, flow_measurements=[FlowMeasurement(measurement_id="B>3")]),
                    InterzoneFlow(id="B>C", from_zone="REACH-B", to_zone="REACH-C", lag_to_zone=0, flow_measurements=[FlowMeasurement(measurement_id="B>C")]),
                    InterzoneFlow(id="SYS>REACH-A", from_zone="SYS", to_zone="REACH-A", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="SYS>REACH-B", from_zone="SYS", to_zone="REACH-B", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-05',series=[
                MeasurementSeries(id="A>1", values=[ 0,  1,  2,  3,  4]), # takes 3 days
                MeasurementSeries(id="A>2", values=[ 0,  2,  5,  4,  5]), # takes 2 day
                MeasurementSeries(id="A>B", values=[10, 10, 10, 10, 10]), # same day impact
                MeasurementSeries(id="B>3", values=[10, 10, 10, 10, 10]), # same day impact
                MeasurementSeries(id="B>C", values=[ 0,  0,  0,  0,  0]), # same day impact
            ]),
            txns=[]
        )

        # Expected natural flow at B>C: [?, ?, 0+2+10, 1+5+10, 2+4+10]

        results = solve(input)

        for result in results.get_result_value(flow_id="B>C", date='2000-01-04'):
            print(result)
            if result.txn_id.endswith('_NF'):
                self.assertEqual(result.value, 0+2+10)
        for result in results.get_result_value(flow_id="B>C", date='2000-01-05'):
            if result.txn_id.endswith('_NF'):
                self.assertEqual(result.value, 1+5+10)


    def test_gain_calc_for_one_reach_with_fraction_day_lags(self):
        """
        Similar to previous but with non-integers
        """
        input = SolverInput(
            beg_date='2000-01-03',
            end_date='2000-01-05',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="DIV-1", type=ZoneTypes.USE),
                    Zone(id="DIV-2", type=ZoneTypes.USE),
                    Zone(id="DIV-3", type=ZoneTypes.USE),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="REACH-C", type=ZoneTypes.STREAM),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>1", from_zone="REACH-A", to_zone="DIV-1", lag_from_zone=1.8, flow_measurements=[FlowMeasurement(measurement_id="A>1")]),
                    InterzoneFlow(id="A>2", from_zone="REACH-A", to_zone="DIV-2", lag_from_zone=1.2, flow_measurements=[FlowMeasurement(measurement_id="A>2")]),
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", lag_to_zone=1, flow_measurements=[FlowMeasurement(measurement_id="A>B")]),
                    InterzoneFlow(id="B>3", from_zone="REACH-B", to_zone="DIV-3", lag_from_zone=0, flow_measurements=[FlowMeasurement(measurement_id="B>3")]),
                    InterzoneFlow(id="B>C", from_zone="REACH-B", to_zone="REACH-C", lag_to_zone=0, flow_measurements=[FlowMeasurement(measurement_id="B>C")]),
                    InterzoneFlow(id="SYS>REACH-A", from_zone="SYS", to_zone="REACH-A", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="SYS>REACH-B", from_zone="SYS", to_zone="REACH-B", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-05',series=[
                MeasurementSeries(id="A>1", values=[ 0,  1,  2,  3,  4]), # takes 2.8 days
                MeasurementSeries(id="A>2", values=[ 0,  2,  5,  4,  5]), # takes 2.2 day
                MeasurementSeries(id="A>B", values=[10, 10, 10, 10, 10]), # same day impact
                MeasurementSeries(id="B>3", values=[10, 10, 10, 10, 10]), # same day impact
                MeasurementSeries(id="B>C", values=[ 0,  0,  0,  0,  0]), # same day impact
            ]),
            txns=[]
        )

        # Expected natural flow at B>C: [?, ?, 0+2+10, 1+5+10, 2+4+10]

        results = solve(input)

        for result in results.get_result_value(flow_id="B>C", date='2000-01-04'):
            print(result)
            if result.txn_id.endswith('_NF'):
                self.assertEqual(result.value, 1*(0.2)+0*(0.8) + 2*(0.8)+0*(0.2) + 10 )

    @unittest.skip('tests not complete yet')
    def test_apportionments_with_fraction_day_lags(self):
        """
        Similar to previous but with trxn apportionments
        """
        input = SolverInput(
            beg_date='2000-01-05',
            end_date='2000-01-05',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="DIV-1", type=ZoneTypes.USE),
                    Zone(id="DIV-2", type=ZoneTypes.USE),
                    Zone(id="DIV-3", type=ZoneTypes.USE),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="REACH-C", type=ZoneTypes.STREAM),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>1", from_zone="REACH-A", to_zone="DIV-1", lag_from_zone=1.8, flow_measurements=[FlowMeasurement(measurement_id="A>1")]),
                    InterzoneFlow(id="A>2", from_zone="REACH-A", to_zone="DIV-2", lag_from_zone=1.2, flow_measurements=[FlowMeasurement(measurement_id="A>2")]),
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", lag_to_zone=1, flow_measurements=[FlowMeasurement(measurement_id="A>B")]),
                    InterzoneFlow(id="B>3", from_zone="REACH-B", to_zone="DIV-3", lag_from_zone=0, flow_measurements=[FlowMeasurement(measurement_id="B>3")]),
                    InterzoneFlow(id="B>C", from_zone="REACH-B", to_zone="REACH-C", lag_to_zone=0, flow_measurements=[FlowMeasurement(measurement_id="B>C")]),
                    InterzoneFlow(id="SYS>REACH-A", from_zone="SYS", to_zone="REACH-A", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="SYS>REACH-B", from_zone="SYS", to_zone="REACH-B", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-05',series=[
                MeasurementSeries(id="A>1", values=[ 0,  1,  2,  3,  4]), # takes 2.8 days
                MeasurementSeries(id="A>2", values=[ 0,  2,  5,  4,  5]), # takes 2.2 day
                MeasurementSeries(id="A>B", values=[10, 10, 10, 10, 10]), # same day impact
                MeasurementSeries(id="B>3", values=[10, 10, 10, 10, 10]), # same day impact
                MeasurementSeries(id="B>C", values=[ 0,  0,  0,  0,  0]), # same day impact
            ]),
            txns=[
                Trxn(id='02-1', priority=1, upper_limit=10, path=[
                    TrxnPathItem(flow_id='A>1', factor=1, expected_values=[2,  3,  4]),
                ]),
                Trxn(id='02-2', priority=2, upper_limit=10, path=[
                    TrxnPathItem(flow_id='A>2', factor=1, expected_values=[ 5,  4,  5]),
                ]),
                Trxn(id='02-3', priority=3, upper_limit=10, path=[
                    TrxnPathItem(flow_id='A>B', factor=1, expected_values=[10,10,10]),
                    TrxnPathItem(flow_id='B>3', factor=1, expected_values=[10,10,10]),
                ]),

            ]
        )
        results = solve(input, check_expected_values=False)

        results.print_solve_steps()
        results.print_apportionments(txn_id='02-1')

        self.assertAlmostEqual(1, results.get_result_value(date='2000-01-02', trxn_id='02-1', flow_id='A>1')[0].value, delta=1e-4)

    @unittest.skip('tests not complete yet')
    def test_single_transaction_across_different_fractional_lags(self):
        """
        A single physical transaction traverses two consecutive flows.

        The real-world transaction series is identical on both flows:

            Jan 1     0
            Jan 2    10
            Jan 3    20
            Jan 4    30
            Jan 5    20
            Jan 6     0

        But the two flows have different fractional time offsets:

            UP>MID  = 0.8 day
            MID>USE = 0.2 day

        Therefore their values in lagged LP-time are NOT equal.

        For example, on Jan 2:

            UP>MID:
                0.2 * 10 + 0.8 * 0 = 2

            MID>USE:
                0.8 * 10 + 0.2 * 0 = 8

        After solving and unlagging, however, both path legs should recover
        the same real-world transaction series.

        This is intended as a regression test for transaction continuity
        across path legs having different fractional lags.
        """

        actual = {
            "2000-01-01": 0.0,
            "2000-01-02": 10.0,
            "2000-01-03": 20.0,
            "2000-01-04": 30.0,
            "2000-01-05": 20.0,
            "2000-01-06": 0.0,
        }

        input = SolverInput(
            beg_date="2000-01-01",
            end_date="2000-01-06",

            accounting_graph=AccountingGraph(
                zones=[
                    Zone(
                        id="UPSTREAM",
                        type=ZoneTypes.IMPORT,
                    ),
                    Zone(
                        id="MID",
                        type=ZoneTypes.STREAM,
                    ),
                    Zone(
                        id="USE",
                        type=ZoneTypes.USE,
                    ),
                ],

                interzone_flows=[
                    #
                    # With MID as the common time reference:
                    #
                    #     UP>MID  gets absolute lag 0.8
                    #     MID>USE gets absolute lag 0.2
                    #
                    InterzoneFlow(
                        id="UP>MID",
                        from_zone="UPSTREAM",
                        to_zone="MID",
                        lag_to_zone=0,
                        flow_measurements=[
                            FlowMeasurement(
                                measurement_id="UP>MID",
                            )
                        ],
                    ),

                    InterzoneFlow(
                        id="MID>USE",
                        from_zone="MID",
                        to_zone="USE",
                        lag_from_zone=0.5,
                        flow_measurements=[
                            FlowMeasurement(
                                measurement_id="MID>USE",
                            )
                        ],
                    ),
                ],
            ),

            measurements=MeasurementCollection(
                beg_date="2000-01-01",
                end_date="2000-01-06",
                series=[
                    #
                    # These are REAL-WORLD measurements.
                    #
                    # Since there is no loss, the same physical transaction
                    # appears on both flows.
                    #
                    MeasurementSeries(
                        id="UP>MID",
                        values=[0,10,20,30,20,0],
                    ),
                    MeasurementSeries(
                        id="MID>USE",
                        values=[0,10,20,30,20,0],
                    ),
                ],
            ),

            txns=[
                Trxn(
                    id="TRXN",
                    priority=1,
                    upper_limit=None,
                    path=[
                        TrxnPathItem(
                            flow_id="UP>MID",
                        ),
                        TrxnPathItem(
                            flow_id="MID>USE",
                        ),
                    ],
                ),
            ],
        )

        results = solve(
            input,
            check_expected_values=False,
        )

        results.print_solve_steps()

        results.print_apportionments(
            txn_id="TRXN",
        )

        raise NotImplemented()


class J_Losses(unittest.TestCase):

    def test_path_delivery_losses(self):
        """
        ----> \\~~~3~~~/ ---#-->4----->
               \\_____/         #
                                |
                                v
                                400

        """
        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="STOR", type=ZoneTypes.STORAGE, storage_meas_ids=["dS"]),
                    Zone(id="DIV", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", flow_measurements=[FlowMeasurement(measurement_id="A>B")]),
                    InterzoneFlow(id="A>STOR", from_zone="REACH-A", to_zone="STOR", bidirectional=True),
                    InterzoneFlow(id="B>DIV", from_zone="REACH-B", to_zone="DIV", flow_measurements=[FlowMeasurement(measurement_id="B>DIV")]),
                    InterzoneFlow(id="Flow3", from_zone="SYS", to_zone="REACH-A", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow5", from_zone="SYS", to_zone="REACH-B", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                MeasurementSeries(id="dS",    values=[0, -10]),
                MeasurementSeries(id="A>B",   values=[0, 15]),
                MeasurementSeries(id="B>DIV", values=[0, 10]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=None, path=[
                    TrxnPathItem(flow_id='A>STOR', factor=-1, expected_values=[-10]),
                    TrxnPathItem(flow_id='A>B', loss_before=0.2, loss_after=0.2, expected_values=[8]),
                    TrxnPathItem(flow_id='B>DIV', expected_values=[6.4])
                ])
            ]
        )

        solve(input, check_expected_values=True)

    def test_interzoneflow_losses(self):
        """

        This test uses the same zones and flows as test_path_delivery_losses,
        except now the losses are applied to all variables traversing A>B,
        including natural flow.

        The loss_from_zone and loss_to_zone fractions will need to be applied
        to each traversing variable.

        The natural flow calculations will need to updated to include these
        losses in the flow balance calculation. In this example, the 20%
        loss_from_zone is an additional 3.75 outflow from zone A; this
        increases the gains to zone A from 5 to 8.75.

        As natural flow propigates downstream, these new losses need to be
        applied. In this example, the natural flow available to zone A is 8.75,
        but the amount flowing along A>B is 7, and the amount of zone A natural
        flow that augments zone B's natural flow is only 5.6.

        As each variable is solved in priority order, the variables that
        traverse A>B should have a two loss TrxnPathItems, one flowing
        along Flow4 (due to loss_from_zone), another along Flow6 (due to
        loss_to_zone). In other words, losses are routed from the 'from' or
        'to' zone to the appropriate loss zone.

        """
        input = SolverInput(
            beg_date='2000-01-02',
            end_date='2000-01-02',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="STOR", type=ZoneTypes.STORAGE, storage_meas_ids=["dS"]),
                    Zone(id="DIV", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS)
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", flow_measurements=[FlowMeasurement(measurement_id="A>B")],
                                  loss_from_zone=LossDefinition.linear(0.2),
                                  loss_to_zone=LossDefinition.linear(0.2)),
                    InterzoneFlow(id="A>STOR", from_zone="REACH-A", to_zone="STOR", bidirectional=True),
                    InterzoneFlow(id="B>DIV", from_zone="REACH-B", to_zone="DIV", flow_measurements=[FlowMeasurement(measurement_id="B>DIV")]),
                    InterzoneFlow(id="SYS>A", from_zone="SYS", to_zone="REACH-A", bidirectional=True),
                    InterzoneFlow(id="SYS>B", from_zone="SYS", to_zone="REACH-B", bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-02',series=[
                MeasurementSeries(id="dS",    values=[0, -10]),
                MeasurementSeries(id="A>B",   values=[0, 15]),
                MeasurementSeries(id="B>DIV", values=[0, 10]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=None, path=[
                                    TrxnPathItem(flow_id='B>DIV', expected_values=[3.6]) # All the natural flow avaialbe to zone B
                                ]),
                Trxn(id='TRXN_2', priority=2, upper_limit=None, path=[
                    TrxnPathItem(flow_id='A>STOR', factor=-1, expected_values=[-10]),
                    TrxnPathItem(flow_id='A>B', expected_values=[8]),
                    TrxnPathItem(flow_id='B>DIV', expected_values=[6.4])
                ])
            ]
        )

        results = solve(input, check_expected_values=True)



class K_Accounting_Graph_Details(unittest.TestCase):

    @unittest.skip('tests not complete yet')
    def test_fraction_div_loss(self):
        """Suppose we have a interzone-flow that we know looses 50% of it's
        flow. We want every apportionment traversing this interzone-flow to
        also loose 50% of its value.

        BUT LOSSES DON'T HAPPEN ALONG FLOWS, THEY HAPPEN AT ZONES!

        So suppose we have a zone, and we set up an out-flow (d)
        to have 50% flow of an in-flow (x).
        - Can we instruct the solver to do this calculation? --> One of the flows must be unknown.
        - Can we instruct the solver to calculate a 50% allocation along (d) for all paths traversing (x)?

        e.g.
        Measured ET (d)
        Unmeas Div  (x) = calc from (d = 0.5x) or (d = 0.5*inflows)
        Unmeas Loss (l) = residual

        Perhaps one way we can set this up is to say either:
        - x has a to-zone-loss of 50%, or (I think I prefer this)
        - d has a from-zone-loss of 50%.

        Expanding on this, if both x and d are measured, can we get the
        solver to calculate 50% reduced apportionments for path-components
        traversing d as compared to the same path traversing x? (Like what the depletion solver is doing right now)

        Can we extend the natural flow concept to say that the 'effective precip' or
        'non-irrigated condition' component of d is accounted for as natural flow?

        """

        raise NotImplemented

    @unittest.skip('tests not complete yet')
    def test_fraction_stream_loss(self):
        """
        Suppose a reach of our stream looses 50% of its flow. (So a delivery of
        16 cfs through the reach will only yield 8 cfs at the end.)
        How do we configure the solver to do this accounting?

        Q1, Upstream gage
        x, loss computed from fraction
        y, gain/loss computed from flow balance
        Q2, Downstream gage

        Assuming there are no diversions or other inflows & outflows to the
        zone, the equations are either:

            x = 0.5*Q1
            y = Q2 + x - Q1

        Or:
            x = 0.5*(Q1 + y)
            y = Q2 + x - Q1

        For the first case, we can represent this as:
        Q1 has a to-zone-loss of 0.5

        For the 2nd case, we can represent this as:
        Q2 has a from-zone-loss of 0.5

        """

        raise NotImplemented


    def test_upstream_natural_flows(self):
        """
        If there are existing natural flow apportionments passed in to the
        solver for an inflow, does the solver incorperate these into downstream
        natural flow calcs?
        """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="REACH-C", type=ZoneTypes.STREAM),
                    Zone(id="DIV", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", flow_measurements=[FlowMeasurement(measurement_id="A>B")]),
                    InterzoneFlow(id="B>DIV", from_zone="REACH-B", to_zone="DIV", flow_measurements=[FlowMeasurement(measurement_id="B>DIV")]),
                    InterzoneFlow(id="B>C", from_zone="REACH-B", to_zone="REACH-C", flow_measurements=[FlowMeasurement(measurement_id="B>C")]),
                    InterzoneFlow(id="Flow5", from_zone="SYS", to_zone="REACH-B", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow7", from_zone="SYS", to_zone="REACH-C", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="A>B",   values=[10]),
                MeasurementSeries(id="B>DIV", values=[20]),
                MeasurementSeries(id="B>C",   values=[ 0]),
            ]),
            txns=[],
            external_natural_flows={
                "A>B":{
                    "2000-01-01": 100
                }
            }
        )

        results = solve(input, check_expected_values=False)
        results.print_solve_steps()

        for result in results.get_result_value(flow_id="A>B"):
            if result.txn_id.endswith('_NF'):
                self.assertEqual(result.value, 100, 'Solver did not correctly use the given upstream natural flow')

        for result in results.get_result_value(flow_id="B>C"):
            if result.txn_id.endswith('_NF'):
                self.assertEqual(result.value, 110, 'Solver did not correctly calculate the downstream natural flow for the given upstream natural flow')



    def test_upstream_natural_does_not_get_allocated(self):
        """
        Does the solver know that upstream natural flow cannot be apportioned
        to water rights if it did not physically flow into the system?
        """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="REACH-C", type=ZoneTypes.STREAM),
                    Zone(id="IMPORT", type=ZoneTypes.IMPORT),
                    Zone(id="DIV", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B", flow_measurements=[FlowMeasurement(measurement_id="A>B")]),
                    InterzoneFlow(id="IMPORT>B", from_zone="IMPORT", to_zone="REACH-B", flow_measurements=[FlowMeasurement(measurement_id="IMPORT>B")]),
                    InterzoneFlow(id="B>DIV", from_zone="REACH-B", to_zone="DIV", flow_measurements=[FlowMeasurement(measurement_id="B>DIV")]),
                    InterzoneFlow(id="B>C", from_zone="REACH-B", to_zone="REACH-C", flow_measurements=[FlowMeasurement(measurement_id="B>C")]),
                    InterzoneFlow(id="Flow5", from_zone="SYS", to_zone="REACH-B", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                    InterzoneFlow(id="Flow7", from_zone="SYS", to_zone="REACH-C", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="A>B",      values=[ 10]),
                MeasurementSeries(id="IMPORT>B", values=[100]),
                MeasurementSeries(id="B>DIV",    values=[120]),
                MeasurementSeries(id="B>C",      values=[  0]),
            ]),
            txns=[
                Trxn(id='TRXN_1', priority=1, upper_limit=None, path=[
                    TrxnPathItem(flow_id='B>DIV', expected_values=[20]) # This should be limited to the remaining natural flow
                ]),
                Trxn(id='TRXN_IMP', priority=2, upper_limit=None, path=[
                    TrxnPathItem(flow_id='IMPORT>B', expected_values=[100]),
                    TrxnPathItem(flow_id='B>DIV', expected_values=[100])
                ]),
            ],
            external_natural_flows={
                "A>B":{
                    "2000-01-01": 100
                }
            }
        )

        solve(input, check_expected_values=True)



    def test_unavailable_upstream_natural_flow_is_not_reapportioned_in_spill_pass(self):
        """
        Physical streamflow that has already been apportioned upstream should
        not become available to a new downstream transaction during the spill
        pass.

        A>B carries 10 cfs, and all 10 cfs is identified as external natural
        flow. Therefore, it is physical natural flow but available_natural is
        zero. B>DIV also carries 10 cfs, so the measured flows balance exactly.
        There are no imports, storage releases, or other spills.

        The first natural-flow-limited pass must leave TRXN_1 at zero. The
        second pass must not reclassify the measured diversion as TRXN_1 merely
        because the natural-flow constraints were removed.
        """
        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-01',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="REACH-A", type=ZoneTypes.STREAM),
                    Zone(id="REACH-B", type=ZoneTypes.STREAM),
                    Zone(id="DIV", type=ZoneTypes.USE),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                ],
                interzone_flows=[
                    InterzoneFlow(id="A>B", from_zone="REACH-A", to_zone="REACH-B",
                        flow_measurements=[FlowMeasurement(measurement_id="A>B")]
                    ),
                    InterzoneFlow(id="B>DIV", from_zone="REACH-B", to_zone="DIV",
                        flow_measurements=[FlowMeasurement(measurement_id="B>DIV")]
                    ),
                    InterzoneFlow(id="Flow7", from_zone="SYS", to_zone="REACH-B",
                        flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True
                    )
                ]
            ),
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-01',series=[
                MeasurementSeries(id="A>B",      values=[10]),
                MeasurementSeries(id="B>DIV",    values=[10]),
            ]),
            txns=[
                Trxn(
                    id='TRXN_1',
                    priority=1,
                    upper_limit=10,
                    path=[
                        TrxnPathItem(
                            flow_id='B>DIV',
                            expected_values=[10]
                        )
                    ]
                )
            ],
            external_natural_flows={
                "A>B": {
                    "2000-01-01": 100
                }
            }
        )

        results = solve(input, check_expected_values=True)

        trxn_results = results.get_result_value(
            date='2000-01-01',
            trxn_id='TRXN_1',
            flow_id='B>DIV'
        )



class RealProblems(unittest.TestCase):
    """When the solver doesn't work in the wild, copy the inputs and add a
    test case here before fixing it."""

    def test_lake_fork_01(self):
        """Can this problem be solved w/o an exception?

        """
        import json
        from pathlib import Path

        filepath = Path("tests") / "test_files" / "lakefork_input.json"
        with open(filepath, 'r') as file:
            input_dict = json.load(file)
        input = parse_solver_input_from_dict(input_dict)
        solve(input)

    def test_lake_fork_02(self):
        """Can this problem be solved w/o an exception?

        """
        import json
        from pathlib import Path

        filepath = Path("tests") / "test_files" / "lakefork_input_20240328.json"
        with open(filepath, 'r') as file:
            input_dict = json.load(file)
        input = parse_solver_input_from_dict(input_dict)
        solve(input)

    def test_feron(self):
        """Can this problem be solved w/o an exception?

        """
        import json
        from pathlib import Path

        filepath = Path("tests") / "test_files" / "feron_input.json"
        with open(filepath, 'r') as file:
            input_dict = json.load(file)
        input = parse_solver_input_from_dict(input_dict)

        results = solve(input)
        results.print_solve_steps()