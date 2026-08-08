import unittest

from ut_water_apportionment.lag_utils import unlag_series


class UnlagSeriesTests(unittest.TestCase):

    def test_integer_lag_only_changes_dates(self):

        dates, values = unlag_series(
            dates=[
                "2000-01-03",
                "2000-01-04",
                "2000-01-05",
            ],
            values=[
                10,
                20,
                30,
            ],
            lag=2,
        )

        self.assertEqual(
            dates,
            [
                "2000-01-01",
                "2000-01-02",
                "2000-01-03",
            ],
        )

        self.assertEqual(
            values,
            [10, 20, 30],
        )

    def test_fractional_lag_1_1_unlags_forward(self):
        """
        Actual values:

            Jan 1 = 10   <-- boundary
            Jan 2 = 2
            Jan 3 = 20
            Jan 4 = 30

        lag = 1.1:

            y Jan 3 = .9(2)  + .1(10) = 2.8
            y Jan 4 = .9(20) + .1(2)  = 18.2
            y Jan 5 = .9(30) + .1(20) = 29

        Since .9 is the larger coefficient, solve forward.
        """

        dates, values = unlag_series(
            dates=[
                "2000-01-03",
                "2000-01-04",
                "2000-01-05",
            ],
            values=[
                2.8,
                18.2,
                29.0,
            ],
            lag=1.1,
            boundary_value=10,
        )

        self.assertEqual(
            dates,
            [
                "2000-01-02",
                "2000-01-03",
                "2000-01-04",
            ],
        )

        self.assertAlmostEqual(values[0], 2)
        self.assertAlmostEqual(values[1], 20)
        self.assertAlmostEqual(values[2], 30)

    def test_fractional_lag_1_9_unlags_backward(self):
        """
        Actual values:

            Jan 1 = 10
            Jan 2 = 2
            Jan 3 = 20
            Jan 4 = 30   <-- boundary

        lag = 1.9:

            y Jan 3 = .1(2)  + .9(10) = 9.2
            y Jan 4 = .1(20) + .9(2)  = 3.8
            y Jan 5 = .1(30) + .9(20) = 21

        Since .9 is the larger coefficient, solve backward.
        """

        dates, values = unlag_series(
            dates=[
                "2000-01-03",
                "2000-01-04",
                "2000-01-05",
            ],
            values=[
                9.2,
                3.8,
                21.0,
            ],
            lag=1.9,
            boundary_value=30,
        )

        self.assertEqual(
            dates,
            [
                "2000-01-01",
                "2000-01-02",
                "2000-01-03",
            ],
        )

        self.assertAlmostEqual(values[0], 10)
        self.assertAlmostEqual(values[1], 2)
        self.assertAlmostEqual(values[2], 20)

    def test_backward_boundary_error_dies_out(self):
        """
        Use an intentionally wrong zero boundary.

        The reconstruction nearest the boundary will be wrong, but that
        error should rapidly shrink as we recurse backward because the
        error multiplier is .1/.9.
        """

        _, values = unlag_series(
            dates=[
                "2000-01-03",
                "2000-01-04",
                "2000-01-05",
            ],
            values=[
                9.2,
                3.8,
                21.0,
            ],
            lag=1.9,
            boundary_value=0,
        )

        actual = [10, 2, 20]

        errors = [
            abs(computed - expected)
            for computed, expected in zip(values, actual)
        ]

        # Moving away from the ending boundary, the error decreases.
        self.assertLess(errors[0], errors[1])
        self.assertLess(errors[1], errors[2])


if __name__ == "__main__":
    unittest.main()