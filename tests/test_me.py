import unittest
from ut_water_apportionment import (
    compile, CompileOptions,
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
    PathTrxn,
    TrxnGroup,
    TrxnPathItem,
    Zone,
    ZoneAccount,
    ZoneTypes
)
from ut_water_apportionment.loss_models import LossDefinition



class A_Simple(unittest.TestCase):

    def test_1(self):

        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-03',
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
            measurements=MeasurementCollection(beg_date='2000-01-01', end_date='2000-01-03',series=[
                MeasurementSeries(id='1', values=[12, 3, 10])
            ]),
            txns=[
                PathTrxn(id='TRXN_1', priority=1, upper_limit= 3, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[3,0,0])]),
                PathTrxn(id='TRXN_2', priority=2, upper_limit= 6, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[6,0,0])]),
                #PathTrxn(id='TRXN_3', priority=3, upper_limit=12, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[3,0,0])]),
                #PathTrxn(id='TRXN_4', priority=4, upper_limit= 4, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[0,0,0])]),
            ]
        )


        plan = compile(input, options=CompileOptions(compile_to_formulas=False))
        print(plan)


        print('2)')
        result = plan.solve()

        print('DONE')

        #print('1)')
        #results = solve(input, check_expected_values=True)



    def test_resrevoir(self):

        input = SolverInput(
            beg_date='2000-01-01',
            end_date='2000-01-03',
            accounting_graph=AccountingGraph(
                zones=[
                    Zone(id="RIVER", type=ZoneTypes.STREAM),
                    Zone(id="STO", type=ZoneTypes.STORAGE, storage_meas_ids=['STO']),
                    Zone(id="SYS", type=ZoneTypes.SYSTEM_GAIN_LOSS),
                    Zone(id="USER", type=ZoneTypes.USE),
                ],
                interzone_flows=[
                    InterzoneFlow(id="RIVER>STO", from_zone="RIVER", to_zone="STO", bidirectional=True,
                                  flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE),
                    InterzoneFlow(id="RIVER>USER", from_zone="RIVER", to_zone="USER",
                                  flow_measurements=[FlowMeasurement(measurement_id="1")]),
                    InterzoneFlow(id="SYS>RIVER", from_zone="SYS", to_zone="RIVER", flow_type=FlowComponentsTypes.FLOW_BALANCE_OF_DESTINATION_ZONE, bidirectional=True),
                ]
            ),
            measurements=MeasurementCollection(beg_date='1999-12-31', end_date='2000-01-03',series=[
                MeasurementSeries(id='1', values=[12, 12, 3, 10]),
                MeasurementSeries(id='STO', values=[1200, 1200, 1200, 1200])
            ]),
            txns=[
                PathTrxn(id='TRXN_1', priority=1, upper_limit= 3, path=[TrxnPathItem(flow_id='RIVER>USER')]),
                PathTrxn(id='TRXN_2', priority=2, upper_limit= 6, path=[TrxnPathItem(flow_id='RIVER>STO', factor=-1),
                                                                        TrxnPathItem(flow_id='RIVER>USER')]),
                #PathTrxn(id='TRXN_3', priority=3, upper_limit=12, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[3,0,0])]),
                #PathTrxn(id='TRXN_4', priority=4, upper_limit= 4, path=[TrxnPathItem(flow_id='RIVER>USER', expected_values=[0,0,0])]),
            ]
        )


        plan = compile(input)
        print(plan.code())       # Actual symbolic MIN/MAX expressions.


        print('2)')
        result = plan.solve()

        print('DONE')

        #print('1)')
        #results = solve(input, check_expected_values=True)