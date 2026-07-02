"""Tests for the goal_achievement metric (self-improvement Phase 1).

The goal-judge writes ``agent_reviews`` rows with ``reviewer_type='judge'`` and
``categories.dimension='goal_achievement'``. ``goals.py`` reads them, confidence-
weights the 1-5 ratings, normalizes to 0-1, and injects the value into
``compute_goal_metrics`` so the existing weighted-average score picks it up — no
change to ``compute_achievement_score`` itself.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.goals import (
    GOAL_ACHIEVEMENT_METRIC,
    _get_goal_achievement_judgment,
    compute_goal_metrics,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.last_sql = None

    def execute(self, sql, params=None):
        self.last_sql = sql
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestGetGoalAchievementJudgment:
    def test_maps_confidence_weighted_rating_to_unit_interval(self):
        # DB returns the confidence-weighted average rating (4.2 of 5).
        with patch("robothor.crm.dal.get_connection", return_value=FakeConn([(4.2,)])):
            value = _get_goal_achievement_judgment("main", window_days=7)
        # A weighted-average rating of 4.2 maps to 0.8 on the unit interval.
        assert value == pytest.approx(0.8)

    def test_returns_none_when_no_judge_rows(self):
        with patch("robothor.crm.dal.get_connection", return_value=FakeConn([])):
            assert _get_goal_achievement_judgment("main", window_days=7) is None

    def test_returns_none_when_aggregate_is_null(self):
        # No matching rows → SUM/NULLIF yields a single (None,) row, not zero rows.
        with patch("robothor.crm.dal.get_connection", return_value=FakeConn([(None,)])):
            assert _get_goal_achievement_judgment("main", window_days=7) is None

    def test_clamps_into_unit_interval(self):
        with patch("robothor.crm.dal.get_connection", return_value=FakeConn([(5.0,)])):
            assert _get_goal_achievement_judgment("main", window_days=7) == 1.0
        with patch("robothor.crm.dal.get_connection", return_value=FakeConn([(1.0,)])):
            assert _get_goal_achievement_judgment("main", window_days=7) == 0.0

    def test_filters_to_judge_goal_achievement_rows(self):
        captured = {}

        class CapturingCursor(FakeCursor):
            def execute(self, sql, params=None):
                captured["sql"] = sql
                return self

        class CapturingConn(FakeConn):
            def cursor(self):
                return CapturingCursor(self.rows)

        with patch("robothor.crm.dal.get_connection", return_value=CapturingConn([(4.0,)])):
            _get_goal_achievement_judgment("main", window_days=7)
        assert "reviewer_type = 'judge'" in captured["sql"]
        assert "goal_achievement" in captured["sql"]


class TestComputeGoalMetricsJudge:
    def test_adds_goal_achievement_when_available(self):
        with (
            patch(
                "robothor.engine.goals.get_agent_stats",
                return_value={"total_runs": 10, "timeouts": 0},
            ),
            patch("robothor.engine.goals._get_benchmark_pass_rate", return_value=None),
            patch("robothor.engine.goals._get_session_goal_alignment_score", return_value=None),
            patch("robothor.engine.goals._get_session_goal_progress", return_value=None),
            patch("robothor.engine.goals._get_goal_achievement_judgment", return_value=0.9),
        ):
            metrics = compute_goal_metrics("main", window_days=7)
        assert metrics[GOAL_ACHIEVEMENT_METRIC] == pytest.approx(0.9)

    def test_omits_goal_achievement_when_unavailable(self):
        with (
            patch(
                "robothor.engine.goals.get_agent_stats",
                return_value={"total_runs": 10, "timeouts": 0},
            ),
            patch("robothor.engine.goals._get_benchmark_pass_rate", return_value=None),
            patch("robothor.engine.goals._get_session_goal_alignment_score", return_value=None),
            patch("robothor.engine.goals._get_session_goal_progress", return_value=None),
            patch("robothor.engine.goals._get_goal_achievement_judgment", return_value=None),
        ):
            metrics = compute_goal_metrics("main", window_days=7)
        assert GOAL_ACHIEVEMENT_METRIC not in metrics
