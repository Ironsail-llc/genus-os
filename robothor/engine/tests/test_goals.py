"""Tests for goal-driven self-improvement primitives."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from robothor.engine.goals import (
    GoalBreach,
    GoalSpec,
    _evaluate_target,
    _get_daily_metric_history,
    compute_achievement_score,
    compute_goal_metrics,
    detect_goal_breach,
    parse_goals_from_manifest,
    suggest_corrective_actions,
)

# ─── _evaluate_target ─────────────────────────────────────────────────


class TestEvaluateTarget:
    def test_greater_than(self):
        assert _evaluate_target(0.96, ">0.95") is True
        assert _evaluate_target(0.95, ">0.95") is False
        assert _evaluate_target(0.94, ">0.95") is False

    def test_greater_or_equal(self):
        assert _evaluate_target(0.95, ">=0.95") is True
        assert _evaluate_target(0.94, ">=0.95") is False

    def test_less_than(self):
        assert _evaluate_target(0.04, "<0.05") is True
        assert _evaluate_target(0.05, "<0.05") is False

    def test_less_or_equal(self):
        assert _evaluate_target(0.05, "<=0.05") is True
        assert _evaluate_target(0.06, "<=0.05") is False

    def test_handles_integers(self):
        assert _evaluate_target(4500, "<5000") is True
        assert _evaluate_target(5001, "<5000") is False

    def test_none_fails_gracefully(self):
        # Missing metric shouldn't crash — treat as breach (False)
        assert _evaluate_target(None, ">0.95") is False


# ─── parse_goals_from_manifest ────────────────────────────────────────


class TestParseGoalsFromManifest:
    def test_parses_4_categories(self):
        manifest = {
            "goals": {
                "reach": [
                    {
                        "id": "deliver",
                        "metric": "delivery_success_rate",
                        "target": ">0.95",
                        "weight": 2.0,
                    }
                ],
                "quality": [
                    {
                        "id": "substantive",
                        "metric": "min_output_chars",
                        "target": ">500",
                        "weight": 1.5,
                    }
                ],
                "efficiency": [
                    {"id": "fast", "metric": "avg_duration_ms", "target": "<60000", "weight": 0.5}
                ],
                "correctness": [
                    {"id": "low-error", "metric": "error_rate", "target": "<0.05", "weight": 1.0}
                ],
            }
        }
        goals = parse_goals_from_manifest(manifest)
        assert len(goals) == 4
        ids = {g.id for g in goals}
        assert ids == {"deliver", "substantive", "fast", "low-error"}
        # Category is preserved
        by_id = {g.id: g for g in goals}
        assert by_id["deliver"].category == "reach"
        assert by_id["substantive"].category == "quality"
        assert by_id["fast"].category == "efficiency"
        assert by_id["low-error"].category == "correctness"

    def test_handles_missing_goals_block(self):
        assert parse_goals_from_manifest({}) == []

    def test_handles_legacy_flat_list(self):
        """Legacy manifests have a flat list — tolerate, attribute to 'correctness'."""
        manifest = {
            "goals": [
                {"id": "old", "metric": "completion_rate", "target": ">0.95", "weight": 1.0},
            ]
        }
        goals = parse_goals_from_manifest(manifest)
        assert len(goals) == 1
        assert goals[0].category == "correctness"

    def test_defaults_weight_and_window(self):
        manifest = {
            "goals": {"reach": [{"id": "x", "metric": "delivery_success_rate", "target": ">0.95"}]}
        }
        goals = parse_goals_from_manifest(manifest)
        assert goals[0].weight == 1.0
        assert goals[0].window_days == 7

    def test_skips_non_dict_entries(self, caplog):
        """A stray string/None in a goals list must not crash the sweep.
        _goal_from_dict calls .items() so any non-dict raises AttributeError,
        and sweep_all_goals has no per-manifest try/except — one typo would
        kill every agent's nightly review."""
        manifest = {
            "goals": {
                "quality": [
                    "oops — forgot the dict syntax",
                    {"id": "real", "metric": "min_output_chars", "target": ">500"},
                    None,
                ],
            },
        }
        import logging

        with caplog.at_level(logging.WARNING):
            goals = parse_goals_from_manifest(manifest)
        assert [g.id for g in goals] == ["real"]
        assert any("non-dict goal entry" in r.message for r in caplog.records)

    def test_skips_non_dict_in_legacy_flat_list(self, caplog):
        manifest = {"goals": ["oops", {"id": "ok", "metric": "error_rate", "target": "<0.05"}]}
        import logging

        with caplog.at_level(logging.WARNING):
            goals = parse_goals_from_manifest(manifest)
        assert [g.id for g in goals] == ["ok"]


# ─── compute_achievement_score ────────────────────────────────────────


class TestComputeAchievementScore:
    def test_honors_per_goal_window_days(self):
        """Regression: compute_achievement_score used to hardcode window_days=7,
        silently scoring 30/60/90-day goals against a 7-day snapshot. Goals
        must be evaluated against their declared window."""
        goals = [
            GoalSpec(
                id="fast-revert",
                category="correctness",
                metric="revert_rate",
                target="<0.05",
                weight=1.0,
                window_days=7,
            ),
            GoalSpec(
                id="slow-revert",
                category="correctness",
                metric="revert_rate",
                target="<0.05",
                weight=1.0,
                window_days=60,
            ),
        ]

        calls: list[int] = []

        def fake_compute(agent_id, window_days, tenant_id):
            calls.append(window_days)
            if window_days == 7:
                return {"revert_rate": 0.02}
            return {"revert_rate": 0.10}

        with patch("robothor.engine.goals.compute_goal_metrics", side_effect=fake_compute):
            result = compute_achievement_score("some-agent", goals)

        assert sorted(calls) == [7, 60]
        assert "fast-revert" in result["satisfied_goals"]
        assert "slow-revert" in result["breached_goals"]


# ─── compute_goal_metrics + detect_goal_breach ────────────────────────


class TestComputeAndDetect:
    """These depend on DB queries — mock the stats layer."""

    def test_compute_delegates_to_analytics(self):
        fake_stats = {
            "total_runs": 100,
            "completed": 95,
            "failed": 2,
            "timeouts": 3,
            "error_rate": 0.02,
            "avg_duration_ms": 12000.0,
            "avg_cost_usd": 0.03,
            "outcome_distribution": {"successful": 90, "partial": 5},
        }
        with patch("robothor.engine.goals.get_agent_stats", return_value=fake_stats):
            metrics = compute_goal_metrics("some-agent", window_days=7)
        # Direct passthrough
        assert metrics["error_rate"] == 0.02
        assert metrics["avg_duration_ms"] == 12000.0
        # Derived
        assert metrics["timeout_rate"] == pytest.approx(0.03)

    def test_detect_breach_persistent(self):
        """Breach for 3+ days returns a breach record."""
        goal = GoalSpec(
            id="delivery",
            category="reach",
            metric="delivery_success_rate",
            target=">0.95",
            weight=2.0,
            window_days=7,
        )
        # Simulate 4 days of metrics: all below target (0.90)
        daily_metrics = [
            {"delivery_success_rate": 0.90},
            {"delivery_success_rate": 0.88},
            {"delivery_success_rate": 0.92},
            {"delivery_success_rate": 0.85},
        ]
        with patch(
            "robothor.engine.goals._get_daily_metric_history",
            return_value=daily_metrics,
        ):
            breaches = detect_goal_breach("some-agent", [goal])
        assert len(breaches) == 1
        assert breaches[0].goal_id == "delivery"
        assert breaches[0].consecutive_days_breached == 4
        assert breaches[0].actual == pytest.approx(0.85)  # most recent
        # Window carries through so the grader can re-verify on the right slice.
        assert breaches[0].window_days == 7

    def test_detect_breach_propagates_window_days(self):
        """A 30-day-window goal must carry window_days=30 on its breach, so
        the grader re-verifies on the same slice Buddy flagged."""
        goal = GoalSpec(
            id="revert",
            category="correctness",
            metric="pr_revert_rate",
            target="<0.10",
            weight=1.0,
            window_days=30,
        )
        daily_metrics = [{"pr_revert_rate": 0.20}] * 5
        with patch(
            "robothor.engine.goals._get_daily_metric_history",
            return_value=daily_metrics,
        ):
            breaches = detect_goal_breach("some-agent", [goal])
        assert len(breaches) == 1
        assert breaches[0].window_days == 30

    def test_detect_breach_not_persistent(self):
        """Only 1 day of breach → not persistent, not reported."""
        goal = GoalSpec(
            id="delivery",
            category="reach",
            metric="delivery_success_rate",
            target=">0.95",
            weight=2.0,
            window_days=7,
        )
        daily_metrics = [
            {"delivery_success_rate": 0.96},
            {"delivery_success_rate": 0.97},
            {"delivery_success_rate": 0.85},  # only latest breach
        ]
        with patch(
            "robothor.engine.goals._get_daily_metric_history",
            return_value=daily_metrics,
        ):
            breaches = detect_goal_breach("some-agent", [goal])
        assert breaches == []

    def test_detect_breach_recovery(self):
        """Recovery after a breach resets the counter."""
        goal = GoalSpec(
            id="err",
            category="correctness",
            metric="error_rate",
            target="<0.05",
            weight=1.0,
            window_days=7,
        )
        # 3 days bad, 1 day good, then 2 days bad — only the recent 2 count
        daily_metrics = [
            {"error_rate": 0.10},
            {"error_rate": 0.12},
            {"error_rate": 0.08},
            {"error_rate": 0.03},  # recovery
            {"error_rate": 0.09},
            {"error_rate": 0.07},
        ]
        with patch(
            "robothor.engine.goals._get_daily_metric_history",
            return_value=daily_metrics,
        ):
            breaches = detect_goal_breach("some-agent", [goal])
        # Only 2 consecutive recent breaches — below the 3-day threshold
        assert breaches == []

    def test_daily_history_passes_distinct_as_of_per_day(self):
        """Regression: _get_daily_metric_history used to call the stats layer
        with identical args for every day, producing N identical snapshots
        and telling detect_goal_breach that every currently-breached goal
        had been breached for N consecutive days. Real production path: each
        iteration must pass a distinct ``as_of`` so the stats layer can
        anchor the window at that point in time."""
        seen: list[Any] = []

        def fake_stats(agent_id, days, tenant_id, as_of=None):
            seen.append(as_of)
            return {"total_runs": 0}

        with patch("robothor.engine.goals.get_agent_stats", side_effect=fake_stats):
            history = _get_daily_metric_history(
                agent_id="agent", metric="error_rate", window_days=7, lookback_days=5
            )

        assert len(history) == 5
        assert len(seen) == 5
        # Every as_of is distinct and non-None — no shared snapshot.
        assert len({str(x) for x in seen}) == 5
        assert all(x is not None for x in seen)
        # Most-recent-day is last; each earlier entry is further in the past.
        from itertools import pairwise

        for earlier, later in pairwise(seen):
            assert earlier < later

    def test_compute_goal_metrics_threads_as_of(self):
        """``as_of`` must propagate through compute_goal_metrics to get_agent_stats."""
        captured: dict[str, Any] = {}

        def fake_stats(agent_id, days, tenant_id, as_of=None):
            captured["as_of"] = as_of
            captured["days"] = days
            return {"total_runs": 0}

        import datetime as _dt

        anchor = _dt.datetime(2026, 4, 1, tzinfo=_dt.UTC)
        with patch("robothor.engine.goals.get_agent_stats", side_effect=fake_stats):
            compute_goal_metrics("agent", window_days=30, as_of=anchor)

        assert captured["as_of"] == anchor
        assert captured["days"] == 30


# ─── suggest_corrective_actions ───────────────────────────────────────


class TestCorrectiveActions:
    def test_quality_breach_maps_to_quality_templates(self):
        breach = GoalBreach(
            goal_id="substantive",
            category="quality",
            metric="min_output_chars",
            target=">500",
            actual=85.0,
            consecutive_days_breached=4,
            weight=2.0,
            window_days=7,
        )
        actions = suggest_corrective_actions(breach)
        assert any("instruction" in a.lower() for a in actions)

    def test_efficiency_breach_maps_to_efficiency_templates(self):
        # Efficiency breaches now fall through to the generic diagnosis
        # template — we no longer prescribe cap-based remedies
        # (stall_timeout_seconds, hard_budget, cost_budget_usd). The
        # playbook is observational: trim context, prune tools, check
        # iteration loops. Cost and duration are tracked, not capped.
        breach = GoalBreach(
            goal_id="iteration-loops",
            category="efficiency",
            metric="error_rate",  # any efficiency-adjacent metric
            target="<0.05",
            actual=0.20,
            consecutive_days_breached=3,
            weight=2.0,
            window_days=7,
        )
        actions = suggest_corrective_actions(breach)
        assert actions, "efficiency breach should still return the default playbook"
        joined = " ".join(actions).lower()
        # Should talk about prompt/tool/iteration diagnostics, NOT caps.
        assert "stall_timeout" not in joined
        assert "hard_budget" not in joined
        assert "cost_budget" not in joined

    def test_reach_breach_maps_to_reach_templates(self):
        breach = GoalBreach(
            goal_id="deliver",
            category="reach",
            metric="delivery_success_rate",
            target=">0.95",
            actual=0.80,
            consecutive_days_breached=5,
            weight=2.0,
            window_days=7,
        )
        actions = suggest_corrective_actions(breach)
        assert any("channel" in a.lower() or "delivery" in a.lower() for a in actions)

    def test_correctness_breach_maps_to_correctness_templates(self):
        breach = GoalBreach(
            goal_id="errs",
            category="correctness",
            metric="error_rate",
            target="<0.05",
            actual=0.18,
            consecutive_days_breached=3,
            weight=1.0,
            window_days=7,
        )
        actions = suggest_corrective_actions(breach)
        assert any(
            "error" in a.lower() or "tool" in a.lower() or "guardrail" in a.lower() for a in actions
        )
