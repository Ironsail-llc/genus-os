"""Tests for the new benchmark_pass_rate goal metric (added 2026-05-06).

Verifies:
- parse_goals_from_manifest accepts a goal targeting benchmark_pass_rate
- _get_benchmark_pass_rate reads from the benchmark_results table
- compute_goal_metrics surfaces the value when present and omits when absent
- detect_goal_breach treats None measurement days as neutral (no false-trip)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.goals import (
    GoalSpec,
    _get_benchmark_pass_rate,
    compute_goal_metrics,
    detect_goal_breach,
    parse_goals_from_manifest,
)


class TestParseGoalsManifest:
    def test_passes_its_job_goal_parses(self):
        manifest = {
            "goals": {
                "quality": [
                    {
                        "id": "passes-its-job",
                        "metric": "benchmark_pass_rate",
                        "target": ">=0.85",
                        "weight": 5.0,
                        "window_days": 7,
                    }
                ]
            }
        }
        goals = parse_goals_from_manifest(manifest)
        assert len(goals) == 1
        assert goals[0].metric == "benchmark_pass_rate"
        assert goals[0].target == ">=0.85"
        assert goals[0].weight == 5.0


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


class TestGetBenchmarkPassRate:
    def test_returns_true_pass_rate_from_passed_over_total(self):
        """The metric is passed/total_cases — a real pass rate, not the
        partial-credit `pass_rate` aggregate column (which only needs 0.70
        per task to count as a pass)."""
        with patch(
            "robothor.crm.dal.get_connection",
            return_value=FakeConn([(46, 50)]),
        ):
            value = _get_benchmark_pass_rate("email-classifier", window_days=7)
        assert value == pytest.approx(0.92)

    def test_returns_none_when_no_row(self):
        with patch(
            "robothor.crm.dal.get_connection",
            return_value=FakeConn([]),
        ):
            value = _get_benchmark_pass_rate("never-graded", window_days=7)
        assert value is None

    def test_returns_none_when_total_cases_zero(self):
        """A row with zero cases is not a 0% pass rate — it's no data."""
        with patch(
            "robothor.crm.dal.get_connection",
            return_value=FakeConn([(0, 0)]),
        ):
            value = _get_benchmark_pass_rate("empty-suite", window_days=7)
        assert value is None


class TestComputeGoalMetrics:
    def test_adds_benchmark_pass_rate_when_available(self):
        with (
            patch(
                "robothor.engine.goals.get_agent_stats",
                return_value={"total_runs": 10, "timeouts": 0},
            ),
            patch(
                "robothor.engine.goals._get_benchmark_pass_rate",
                return_value=0.88,
            ),
        ):
            metrics = compute_goal_metrics("email-classifier", window_days=7)
        assert metrics["benchmark_pass_rate"] == pytest.approx(0.88)
        assert metrics["total_runs"] == 10

    def test_omits_benchmark_pass_rate_when_unavailable(self):
        """Don't pollute the dict with None — detect_goal_breach treats absence as neutral."""
        with (
            patch(
                "robothor.engine.goals.get_agent_stats",
                return_value={"total_runs": 10, "timeouts": 0},
            ),
            patch(
                "robothor.engine.goals._get_benchmark_pass_rate",
                return_value=None,
            ),
        ):
            metrics = compute_goal_metrics("never-graded", window_days=7)
        assert "benchmark_pass_rate" not in metrics


class TestDetectGoalBreachNoneNeutral:
    def test_pre_measurement_does_not_trigger_breach(self):
        """Before any benchmark has run, the goal should not flag a breach."""
        goal = GoalSpec(
            id="passes-its-job",
            category="quality",
            metric="benchmark_pass_rate",
            target=">=0.85",
            weight=5.0,
            window_days=7,
        )
        # All days have no benchmark_pass_rate measurement.
        empty_history = [{"total_runs": 5} for _ in range(14)]
        with patch(
            "robothor.engine.goals._get_daily_metric_history",
            return_value=empty_history,
        ):
            breaches = detect_goal_breach("never-graded", [goal])
        assert breaches == []

    def test_persistent_low_score_triggers_breach(self):
        """If three+ consecutive days score below target, breach fires."""
        goal = GoalSpec(
            id="passes-its-job",
            category="quality",
            metric="benchmark_pass_rate",
            target=">=0.85",
            weight=5.0,
            window_days=7,
        )
        history = [{"benchmark_pass_rate": 0.90} for _ in range(11)] + [
            {"benchmark_pass_rate": 0.50},
            {"benchmark_pass_rate": 0.40},
            {"benchmark_pass_rate": 0.30},
        ]
        with patch(
            "robothor.engine.goals._get_daily_metric_history",
            return_value=history,
        ):
            breaches = detect_goal_breach("regressing-agent", [goal])
        assert len(breaches) == 1
        assert breaches[0].metric == "benchmark_pass_rate"
        assert breaches[0].consecutive_days_breached >= 3
