import unittest

from ut_water_apportionment import (
    AccountingGraph,
    FlowMeasurement,
    InterzoneFlow,
    MeasurementCollection,
    MeasurementSeries,
    SolverInput,
    Trxn,
    TrxnPathItem,
    Zone,
    ZoneTypes,
    solve,
)


class PeriodLPTests(unittest.TestCase):

    def test_future_day_limit_constrains_earlier_fractional_lag_allocation(self):
        """A later downstream measurement must constrain an earlier allocation.

        The two path legs use different fractional time offsets:

            UP>MID  = 0.8 day
            MID>USE = 0.2 day

        The transaction must be continuous in aligned/accounting time.

        With raw measured flows:

                         Jan 1   Jan 2   Jan 3
            UP>MID          0      10       0
            MID>USE         0      10       3

        Jan 2 aligned continuity is:

            0.2*a2 + 0.8*a1
                = 0.8*b2 + 0.2*b1

        Since a1=b1=0:

            b2 = 0.25*a2

        Jan 3 aligned continuity is:

            0.2*a3 + 0.8*a2
                = 0.8*b3 + 0.2*b2

        Since a3=0 and b3 <= 3:

            0.8*a2 <= 0.8*3 + 0.2*(0.25*a2)
            0.75*a2 <= 2.4
            a2 <= 3.2

        A one-day-at-a-time LP cannot know this while solving Jan 2.  The
        period-wide LP should therefore allocate exactly 3.2 upstream on
        Jan 2, 0.8 downstream on Jan 2, and 3.0 downstream on Jan 3.
        """

        input = SolverInput(
            beg_date="2000-01-02",
            end_date="2000-01-03",
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
                    InterzoneFlow(
                        id="UP>MID",
                        from_zone="UPSTREAM",
                        to_zone="MID",
                        lag_to_zone=0.8,
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
                        lag_from_zone=0.2,
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
                end_date="2000-01-03",
                series=[
                    MeasurementSeries(
                        id="UP>MID",
                        values=[0, 10, 0],
                    ),
                    MeasurementSeries(
                        id="MID>USE",
                        values=[0, 10, 3],
                    ),
                ],
            ),
            txns=[
                Trxn(
                    id="TRXN",
                    priority=1,
                    upper_limit=100,
                    path=[
                        TrxnPathItem(
                            flow_id="UP>MID",
                        ),
                        TrxnPathItem(
                            flow_id="MID>USE",
                        ),
                    ],
                )
            ],
        )

        results = solve(
            input,
            check_expected_values=False,
        )

        def value(
            date: str,
            flow_id: str,
        ) -> float:
            rows = results.get_result_value(
                date=date,
                trxn_id="TRXN",
                flow_id=flow_id,
            )
            self.assertEqual(
                len(rows),
                1,
                msg=(
                    f"Expected one TRXN result for "
                    f"{flow_id} on {date}; "
                    f"found {len(rows)}."
                ),
            )
            return rows[0].value

        self.assertAlmostEqual(
            value("2000-01-02", "UP>MID"),
            3.2,
            delta=1e-5,
        )
        self.assertAlmostEqual(
            value("2000-01-02", "MID>USE"),
            0.8,
            delta=1e-5,
        )
        self.assertAlmostEqual(
            value("2000-01-03", "UP>MID"),
            0.0,
            delta=1e-5,
        )
        self.assertAlmostEqual(
            value("2000-01-03", "MID>USE"),
            3.0,
            delta=1e-5,
        )

        # Final transaction components are in real-world measurement time.
        # The full decomposition of each observed flow must equal the raw
        # measurement on each day.
        for flow_id, expected in {
            "UP>MID": {
                "2000-01-01": 0.0,
                "2000-01-02": 10.0,
                "2000-01-03": 0.0,
            },
            "MID>USE": {
                "2000-01-01": 0.0,
                "2000-01-02": 10.0,
                "2000-01-03": 3.0,
            },
        }.items():
            for date, measured in expected.items():
                apportioned = sum(
                    row.value
                    for row in results.get_result_value(
                        date=date,
                        flow_id=flow_id,
                    )
                )
                self.assertAlmostEqual(
                    apportioned,
                    measured,
                    delta=1e-5,
                    msg=(
                        f"Apportionments on {flow_id} "
                        f"do not sum to the raw measurement "
                        f"on {date}."
                    ),
                )


if __name__ == "__main__":
    unittest.main()
