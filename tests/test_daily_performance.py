"""Exact certificates that avoid repeated daily LP solves."""

from math import inf
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ut_water_apportionment import PathTrxn, V2CompilationOptions
from ut_water_apportionment.apportioner import Apportioner, TMP_LEX_OBJECTIVE
from ut_water_apportionment.compiled_test.lp_engine import LPSolver
from ut_water_apportionment.compiled_test.runtime import V2CompilationSession


def _apportioner(bounds):
    """Exercise the real LP/tie-break routine without a hydrologic fixture."""
    apportioner = Apportioner.__new__(Apportioner)
    apportioner.engine = LPSolver()
    apportioner._lexicographic_constraint_vars = set()
    apportioner.engine.add_constraint(TMP_LEX_OBJECTIVE)
    for name, (lower, upper) in bounds.items():
        apportioner.engine.add_variable(name, lb=lower, ub=upper)
    return apportioner


class DailyPerformanceTests(unittest.TestCase):
    def test_minimum_at_nonzero_and_signed_lower_bounds_needs_one_solve(self):
        bounds = {"A": (2, 10), "B": (-3, 10), "C": (0, 10)}
        apportioner = _apportioner(bounds)
        self.assertEqual(
            apportioner._lexicographic_minimum(list(bounds)),
            {"A": 2, "B": -3, "C": 0},
        )
        self.assertEqual(apportioner.engine.solve_count, 1)
        self.assertEqual(
            apportioner.engine.get_constraint_bounds(TMP_LEX_OBJECTIVE),
            (-inf, inf),
        )

    def test_positive_forced_component_skips_other_zero_components(self):
        bounds = {"A": (0, 10), "B": (0, 10), "C": (0, 10)}
        apportioner = _apportioner(bounds)
        apportioner.engine.add_constraint("forced", lb=5, ub=5)
        apportioner.engine.set_coefficient("forced", "B", 1)
        self.assertEqual(
            apportioner._lexicographic_minimum(list(bounds)),
            {"A": 0, "B": 5, "C": 0},
        )
        self.assertEqual(apportioner.engine.solve_count, 2)
        for name, original in bounds.items():
            self.assertEqual(apportioner.engine.get_variable_bounds(name), original)

    def test_nonunique_primary_witness_does_not_change_tie_break_order(self):
        apportioner = _apportioner({"A": (0, 10), "B": (0, 10)})
        apportioner.engine.add_constraint("demand", lb=1)
        for name in ("A", "B"):
            apportioner.engine.set_coefficient("demand", name, 1)
        original = apportioner._solve_auxiliary_objective
        first = True

        def solve(names, **kwargs):
            nonlocal first
            if first:
                first = False
                # Both (1, 0) and (0, 1) minimize the primary sum. Force
                # the witness that still needs a genuine A tie-break.
                return 1.0, {"A": 1.0, "B": 0.0}
            return original(names, **kwargs)

        with patch.object(apportioner, "_solve_auxiliary_objective", side_effect=solve):
            self.assertEqual(
                apportioner._lexicographic_minimum(["A", "B"]),
                {"A": 0, "B": 1},
            )
        # B's initial zero certificate is invalid after minimizing A.
        self.assertEqual(apportioner.engine.solve_count, 2)

    def test_failure_restores_temporary_bounds_and_primary_constraint(self):
        bounds = {"A": (0, 10), "B": (0, 10)}
        apportioner = _apportioner(bounds)
        with patch.object(
            apportioner,
            "_solve_auxiliary_objective",
            side_effect=[(1.0, {"A": 0.0, "B": 1.0}), ValueError("failed")],
        ):
            with self.assertRaisesRegex(ValueError, "failed"):
                apportioner._lexicographic_minimum(list(bounds))
        for name, original in bounds.items():
            self.assertEqual(apportioner.engine.get_variable_bounds(name), original)
        self.assertEqual(
            apportioner.engine.get_constraint_bounds(TMP_LEX_OBJECTIVE),
            (-inf, inf),
        )

    def test_classification_rechecks_zero_witness_on_nonunique_face(self):
        # A + B <= 1, C <= 0. One optimal witness gives all water to A,
        # but B is also able to increase. C alone is blocked. Distinct
        # columns (e.g. separate nonbinding measurement rows) prevent grouping.
        apportioner = _apportioner({name: (0, 10) for name in ("A", "B", "C")})
        variables = [PathTrxn(id=name, priority=1, path=[]) for name in ("A", "B", "C")]
        apportioner.tm = SimpleNamespace(get_anchor_var=lambda var: var.id)
        apportioner.cur_trxn_value = {var.id: 0.0 for var in variables}
        apportioner._source_nf_is_exhausted = lambda var: False
        apportioner.engine.equal_priority_column_signature = lambda name: (name,)
        calls = []

        def classify(names):
            calls.append(list(names))
            values = dict.fromkeys(names, 0.0)
            for name in names:
                if name in ("A", "B"):
                    values[name] = 1.0
                    break
            return sum(values.values()), values

        apportioner.engine.solve_equal_priority_objective = classify
        self.assertEqual(
            [var.id for var in apportioner._get_newly_maxed_vars(variables)], ["C"]
        )
        self.assertEqual(calls, [["A", "B", "C"], ["B", "C"], ["C"]])

    def test_classification_accepts_all_positive_witness_with_one_solve(self):
        apportioner = _apportioner({name: (0, 10) for name in ("A", "B", "C")})
        variables = [PathTrxn(id=name, priority=1, path=[]) for name in ("A", "B", "C")]
        apportioner.tm = SimpleNamespace(get_anchor_var=lambda var: var.id)
        apportioner.cur_trxn_value = {var.id: 0.0 for var in variables}
        apportioner._source_nf_is_exhausted = lambda var: False
        apportioner.engine.equal_priority_column_signature = lambda name: (name,)
        with patch.object(
            apportioner.engine,
            "solve_equal_priority_objective",
            create=True,
            return_value=(3.0, {"A": 1.0, "B": 1.0, "C": 1.0}),
        ) as classify:
            self.assertEqual(apportioner._get_newly_maxed_vars(variables), [])
        self.assertEqual(classify.call_count, 1)

    def test_reset_execution_counters_preserves_preparation_counters(self):
        session = V2CompilationSession(V2CompilationOptions())
        names = (
            "execution_classification_witness_shortcuts",
            "execution_lexicographic_bound_shortcuts",
            "auxiliary_kernel_solves",
            "equal_priority_scalar_auxiliary",
            "derived_slack_reconciliations",
            "derived_slack_values",
            "derived_path_reconstructions",
        )
        for name in names:
            session.stats[name] = 12
        session.stats["prepared_program_count"] = 8
        session.reset_execution_stats()
        for name in names:
            self.assertEqual(session.stats[name], 0)
        self.assertEqual(session.stats["prepared_program_count"], 8)


if __name__ == "__main__":
    unittest.main()
