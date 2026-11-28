"""Metrics for the answer arm, on examples small enough to compute by hand."""

from __future__ import annotations

import pytest

from robothor.memory import eval_stats as st


class TestWilsonReuse:
    def test_eight_of_ten(self):
        lo, hi = st.wilson_interval(8, 10)
        assert lo == pytest.approx(0.4902, abs=1e-4)
        assert hi == pytest.approx(0.9433, abs=1e-4)


class TestPairedDifference:
    def test_two_regressions_in_ten(self):
        """b=0 improved, c=2 regressed, n=10.

        diff = (b - c)/n = -0.2
        var  = ((b + c) - (b - c)^2 / n) / n^2 = (2 - 0.4) / 100 = 0.016
        95%  = -0.2 +- 1.96 * 0.126491 = [-0.44792, 0.04792]
        """
        before = {f"q{i}": True for i in range(10)}
        after = {**before, "q0": False, "q1": False}
        diff, lo, hi = st.paired_difference_ci(before, after)
        assert diff == pytest.approx(-0.2)
        assert lo == pytest.approx(-0.44792, abs=1e-4)
        assert hi == pytest.approx(0.04792, abs=1e-4)

    def test_identical_arms_have_a_zero_width_interval(self):
        same = {"a": True, "b": False, "c": True}
        assert st.paired_difference_ci(same, same) == (0.0, 0.0, 0.0)

    def test_only_shared_questions_count(self):
        diff, _, _ = st.paired_difference_ci({"a": True, "x": True}, {"a": False, "y": False})
        assert diff == pytest.approx(-1.0)

    def test_no_shared_questions(self):
        assert st.paired_difference_ci({}, {}) == (0.0, 0.0, 0.0)


class TestNonInferiority:
    def test_two_regressions_in_ten_is_not_non_inferior(self):
        before = {f"q{i}": True for i in range(10)}
        after = {**before, "q0": False, "q1": False}
        out = st.non_inferiority(before, after, margin=0.05)
        assert out["non_inferior"] is False
        assert out["margin"] == 0.05
        assert out["lower"] == pytest.approx(-0.44792, abs=1e-4)

    def test_identical_arms_are_non_inferior(self):
        same = {f"q{i}": i % 2 == 0 for i in range(40)}
        assert st.non_inferiority(same, same)["non_inferior"] is True

    def test_lower_bound_must_clear_minus_margin(self):
        """One regression in 400: diff -0.0025, se = sqrt(1 - 1/400)/400 = 0.0024969,
        lower = -0.0025 - 1.96*0.0024969 = -0.0073939 > -0.05 → non-inferior."""
        before = {f"q{i}": True for i in range(400)}
        after = {**before, "q0": False}
        out = st.non_inferiority(before, after)
        assert out["lower"] == pytest.approx(-0.0073939, abs=1e-5)
        assert out["non_inferior"] is True


class TestMinimumDetectableEffect:
    def test_n100_at_half(self):
        """(1.959964 + 0.841621) * sqrt(2 * 0.25 / 100) = 2.801585 * 0.0707107 = 0.198101."""
        assert st.minimum_detectable_effect(100) == pytest.approx(0.198101, abs=1e-4)

    def test_shrinks_with_n(self):
        assert st.minimum_detectable_effect(400) < st.minimum_detectable_effect(100)

    def test_skewed_accuracy_needs_a_smaller_effect(self):
        assert st.minimum_detectable_effect(100, p=0.9) < st.minimum_detectable_effect(100)

    def test_zero_n_is_one(self):
        assert st.minimum_detectable_effect(0) == 1.0


class TestSpreadAndPercentiles:
    def test_run_spread(self):
        out = st.run_spread([0.5, 0.7, 0.6])
        assert out["min"] == 0.5 and out["max"] == 0.7
        assert out["range"] == pytest.approx(0.2)
        assert out["stdev"] == pytest.approx(0.1)
        assert out["runs"] == 3

    def test_single_run_has_zero_spread(self):
        out = st.run_spread([0.4])
        assert out["range"] == 0.0 and out["stdev"] == 0.0

    def test_percentile_nearest_rank(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        assert st.percentile(values, 50) == 50.0
        assert st.percentile(values, 95) == 100.0
        assert st.percentile([], 50) == 0.0

    def test_promotion_verdict_is_reused_unchanged(self):
        before = {f"q{i}": False for i in range(10)}
        after = {f"q{i}": True for i in range(10)}
        report = st.paired_report(before, after)
        ok, _ = st.promotion_verdict(report)
        assert ok
