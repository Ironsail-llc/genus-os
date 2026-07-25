"""Paired significance for eval comparisons.

Every retrieval decision so far has been made by comparing two aggregate
percentages. On 41 questions a 2-point difference is one flipped case, which is
indistinguishable from reranker noise — the reranker is a model, and borderline
cases flip between runs.

McNemar's test is the right instrument because the two arms answer the *same*
questions: only the discordant pairs carry information. That also sets a hard
floor on what any suite can prove — with all discordance one-sided you need at
least 6 disagreements to reach p < 0.05, so a stratum of 5 cases cannot produce
a significant result no matter how lopsided it looks.

Stdlib only: scipy is not a dependency and numpy is not needed for this.
"""

from __future__ import annotations

import pytest

from robothor.memory.eval_stats import mcnemar_exact, paired_report, wilson_interval


class TestMcNemar:
    def test_no_discordance_is_not_significant(self):
        """Identical arms cannot be distinguished, however many questions agree."""
        assert mcnemar_exact(0, 0) == 1.0

    def test_symmetric_discordance_is_not_significant(self):
        assert mcnemar_exact(5, 5) == pytest.approx(1.0)

    def test_lopsided_discordance_is_significant(self):
        # 9 improvements, 0 regressions
        assert mcnemar_exact(9, 0) < 0.01

    def test_six_one_sided_is_the_practical_floor(self):
        """b+c must reach 6 before a one-sided result can clear p < 0.05.

        Worth pinning: it is the arithmetic reason a 12-case suite with strata
        of 1-4 cases cannot gate anything, which is why the suite has to grow.
        """
        assert mcnemar_exact(5, 0) > 0.05
        assert mcnemar_exact(6, 0) < 0.05

    def test_direction_does_not_change_the_p_value(self):
        """Two-sided: the test says 'they differ', not 'which is better'."""
        assert mcnemar_exact(7, 1) == pytest.approx(mcnemar_exact(1, 7))

    def test_probability_stays_in_range(self):
        for b in range(0, 12):
            for c in range(0, 12):
                assert 0.0 <= mcnemar_exact(b, c) <= 1.0


class TestPairedReport:
    def _arms(self):
        before = {"q1": True, "q2": False, "q3": False, "q4": True, "q5": False}
        after = {"q1": True, "q2": True, "q3": True, "q4": False, "q5": True}
        return before, after

    def test_counts_discordant_pairs_in_both_directions(self):
        before, after = self._arms()
        r = paired_report(before, after)
        assert r["n"] == 5
        assert r["improved"] == 3  # q2, q3, q5
        assert r["regressed"] == 1  # q4
        assert r["delta"] == pytest.approx((4 - 2) / 5)

    def test_ignores_questions_missing_from_either_arm(self):
        """An arm that errored on a question must not be scored as a failure."""
        r = paired_report({"q1": True, "q2": True}, {"q1": False})
        assert r["n"] == 1, "only the question both arms answered counts"

    def test_empty_comparison_is_reported_not_crashed(self):
        r = paired_report({}, {})
        assert r["n"] == 0 and r["p"] == 1.0

    def test_per_stratum_breakdown(self):
        before = {"a1": True, "a2": True, "b1": False}
        after = {"a1": False, "a2": False, "b1": True}
        strata = {"a1": "recall", "a2": "recall", "b1": "temporal"}
        r = paired_report(before, after, strata=strata)
        assert r["per_stratum"]["recall"]["regressed"] == 2
        assert r["per_stratum"]["temporal"]["improved"] == 1


class TestWilson:
    def test_interval_brackets_the_estimate(self):
        lo, hi = wilson_interval(8, 10)
        assert lo < 0.8 < hi

    def test_small_n_is_honestly_wide(self):
        """At n=5 the interval must be wide enough to discourage reading a trend."""
        lo, hi = wilson_interval(4, 5)
        assert hi - lo > 0.4

    def test_zero_of_zero_is_the_full_range(self):
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_bounds_stay_within_zero_and_one(self):
        for k, n in [(0, 5), (5, 5), (1, 100), (99, 100)]:
            lo, hi = wilson_interval(k, n)
            assert 0.0 <= lo <= hi <= 1.0
