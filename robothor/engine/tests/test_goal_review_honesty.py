"""The nightly goal review must never invent a grade it did not measure.

Four defects are pinned here:

1. ``run_nightly_auto_review`` wrote ``rating=achievement["rating"] or 3`` —
   turning "we measured nothing" into a mid-range pass. On 2026-08-21 that
   fabricated 20 of 20 auto-review rows as a 3/5.
2. Coverage was invisible. Unmeasured goals are dropped from ``total_weight``,
   so an agent satisfying 2 of its 7 goals scored a perfect 5/5 and was
   indistinguishable from an agent measured end to end.
3. No minimum coverage was required before a rating was emitted.
4. The synthetic ``session-goal-*`` specs — injected at runtime, present in no
   manifest — were counted as manifest breaches, driving self-improve tasks
   against contracts the agent never declared.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from robothor.engine.goals import (
    MIN_MEASUREMENT_COVERAGE,
    SESSION_GOAL_ALIGNMENT_ID,
    SESSION_GOAL_PROGRESS_ID,
    UNMEASURED_REASON,
    GoalSpec,
    compose_goals,
    compute_achievement_score,
    detect_goal_breach,
    run_nightly_auto_review,
    session_goals_enforced,
)


def _spec(goal_id: str, metric: str, target: str = ">=0.5", weight: float = 1.0) -> GoalSpec:
    return GoalSpec(
        id=goal_id,
        category="quality",
        metric=metric,
        target=target,
        weight=weight,
        window_days=7,
    )


def _session_goal_row(objective: str, criteria: list[str]) -> dict[str, Any]:
    return {
        "id": "t1",
        "objective": objective,
        "tags": ["session_goal", "agent:worker"],
        "status": "TODO",
        "session_goal_meta": {
            "objective": objective,
            "success_criteria": criteria,
            "metric_targets": [],
            "evidence": [],
            "completion_note": "",
            "alignment_target": ">=0.7",
        },
    }


# ─── 1. No fabricated neutral grade ───────────────────────────────────


class TestNoFabricatedRating:
    def test_unmeasured_agent_persists_null_rating_not_three(self):
        """The whole point: rating None must reach the DAL as None."""
        captured: dict[str, Any] = {}

        def fake_register(agent_id, rating, categories, feedback, action_items, **kwargs):
            captured["rating"] = rating
            captured["categories"] = categories
            captured["feedback"] = feedback
            return "review-1"

        with (
            patch("robothor.engine.goals.compose_goals", return_value=[_spec("g1", "m1")]),
            patch("robothor.engine.goals.compute_goal_metrics", return_value={"total_runs": 5}),
            patch("robothor.engine.goals.detect_goal_breach", return_value=[]),
            patch("robothor.engine.goals.register_review", side_effect=fake_register),
        ):
            results = run_nightly_auto_review([{"id": "never-graded"}])

        assert captured["rating"] is None, "an unmeasured agent must not be graded 3/5"
        assert captured["categories"]["measured"] is False
        assert captured["categories"]["rating_reason"] == UNMEASURED_REASON
        assert results[0]["rating"] is None
        assert results[0]["measured"] is False

    def test_measured_agent_still_gets_its_rating(self):
        """Regression guard: honesty must not blank out real grades."""
        captured: dict[str, Any] = {}

        def fake_register(agent_id, rating, categories, feedback, action_items, **kwargs):
            captured["rating"] = rating
            captured["categories"] = categories
            return "review-1"

        with (
            patch("robothor.engine.goals.compose_goals", return_value=[_spec("g1", "m1")]),
            patch("robothor.engine.goals.compute_goal_metrics", return_value={"m1": 0.9}),
            patch("robothor.engine.goals.detect_goal_breach", return_value=[]),
            patch("robothor.engine.goals.register_review", side_effect=fake_register),
        ):
            results = run_nightly_auto_review([{"id": "graded"}])

        assert captured["rating"] == 5
        assert captured["categories"]["measured"] is True
        assert captured["categories"]["rating_reason"] is None
        assert results[0]["rating"] == 5


# ─── 2. Coverage is reported, never silently excluded ─────────────────


class TestCoverageReporting:
    def test_achievement_reports_measured_and_total_counts(self):
        goals = [_spec("a", "m_a"), _spec("b", "m_b"), _spec("c", "m_c")]

        with patch(
            "robothor.engine.goals.compute_goal_metrics",
            return_value={"m_a": 0.9, "m_b": 0.9},
        ):
            result = compute_achievement_score("partial", goals)

        assert result["total_goals"] == 3
        assert result["measured_goals"] == ["a", "b"]
        assert result["unmeasured_goals"] == ["c"]
        assert result["coverage"] == round(2.0 / 3.0, 4)

    def test_coverage_is_weighted_not_counted(self):
        """A weight-5 unmeasured goal must dent coverage more than a weight-1."""
        goals = [_spec("heavy", "m_heavy", weight=5.0), _spec("light", "m_light", weight=1.0)]

        with patch("robothor.engine.goals.compute_goal_metrics", return_value={"m_light": 0.9}):
            result = compute_achievement_score("skewed", goals)

        assert result["coverage"] == round(1.0 / 6.0, 4)
        assert result["measured_weight"] == 1.0
        assert result["total_weight"] == 6.0

    def test_review_feedback_states_n_of_m_unmeasured(self):
        captured: dict[str, Any] = {}

        def fake_register(agent_id, rating, categories, feedback, action_items, **kwargs):
            captured["feedback"] = feedback
            return "review-1"

        goals = [_spec(f"g{i}", f"m{i}") for i in range(7)]
        with (
            patch("robothor.engine.goals.compose_goals", return_value=goals),
            patch(
                "robothor.engine.goals.compute_goal_metrics",
                return_value={"m0": 0.9, "m1": 0.9},
            ),
            patch("robothor.engine.goals.detect_goal_breach", return_value=[]),
            patch("robothor.engine.goals.register_review", side_effect=fake_register),
        ):
            run_nightly_auto_review([{"id": "auto-researcher"}])

        assert "2 of 7 goals measured" in captured["feedback"]
        assert "5 unmeasured" in captured["feedback"]


# ─── 3. Minimum coverage gate ─────────────────────────────────────────


class TestCoverageGate:
    def test_perfect_score_on_two_of_seven_goals_emits_no_rating(self):
        """The auto-researcher case: 5/5 on 29% of its contract."""
        goals = [_spec(f"g{i}", f"m{i}") for i in range(7)]

        with patch(
            "robothor.engine.goals.compute_goal_metrics",
            return_value={"m0": 0.9, "m1": 0.9},
        ):
            result = compute_achievement_score("auto-researcher", goals)

        assert result["rating"] is None
        assert result["score"] is None
        assert result["rating_reason"] == UNMEASURED_REASON
        # Nothing is hidden — the partial number is still reported.
        assert result["partial_score"] == 1.0
        assert result["coverage"] < MIN_MEASUREMENT_COVERAGE

    def test_coverage_at_threshold_still_rates(self):
        goals = [_spec("a", "m_a"), _spec("b", "m_b")]

        with patch("robothor.engine.goals.compute_goal_metrics", return_value={"m_a": 0.9}):
            result = compute_achievement_score("half", goals)

        assert result["coverage"] == 0.5
        assert result["rating"] == 5
        assert result["score"] == 1.0
        assert result["rating_reason"] is None

    def test_min_coverage_is_overridable(self):
        goals = [_spec(f"g{i}", f"m{i}") for i in range(4)]

        with patch("robothor.engine.goals.compute_goal_metrics", return_value={"m0": 0.9}):
            strict = compute_achievement_score("x", goals, min_coverage=0.5)
            lax = compute_achievement_score("x", goals, min_coverage=0.1)

        assert strict["rating"] is None
        assert lax["rating"] == 5


# ─── 4. Synthetic session goals ───────────────────────────────────────


class TestSyntheticGoals:
    def test_compose_marks_session_goals_synthetic(self):
        row = _session_goal_row("Ship the thing", ["c1"])
        manifest = {
            "id": "worker",
            "goals": {"quality": [{"id": "declared", "metric": "m", "target": ">=0.5"}]},
        }

        with patch("robothor.engine.goals._load_active_goal_for_agent", return_value=row):
            specs = compose_goals(agent_id="worker", manifest=manifest, tenant_id="default")

        by_id = {s.id: s for s in specs}
        assert by_id[SESSION_GOAL_ALIGNMENT_ID].synthetic is True
        assert by_id[SESSION_GOAL_PROGRESS_ID].synthetic is True
        assert by_id["declared"].synthetic is False

    def test_breach_detection_skips_synthetic_by_default(self):
        synthetic = GoalSpec(
            id=SESSION_GOAL_ALIGNMENT_ID,
            category="quality",
            metric="session_goal_alignment_score",
            target=">=0.7",
            weight=5.0,
            window_days=7,
            synthetic=True,
        )
        declared = _spec("declared", "error_rate", target="<0.05")

        history = [{"session_goal_alignment_score": 0.1, "error_rate": 0.5} for _ in range(14)]
        with patch("robothor.engine.goals._get_daily_metric_history", return_value=history):
            breaches = detect_goal_breach("worker", [synthetic, declared])

        assert [b.goal_id for b in breaches] == ["declared"]

    def test_breach_detection_includes_synthetic_when_opted_in(self):
        synthetic = GoalSpec(
            id=SESSION_GOAL_ALIGNMENT_ID,
            category="quality",
            metric="session_goal_alignment_score",
            target=">=0.7",
            weight=5.0,
            window_days=7,
            synthetic=True,
        )

        history = [{"session_goal_alignment_score": 0.1} for _ in range(14)]
        with patch("robothor.engine.goals._get_daily_metric_history", return_value=history):
            breaches = detect_goal_breach("worker", [synthetic], include_synthetic=True)

        assert [b.goal_id for b in breaches] == [SESSION_GOAL_ALIGNMENT_ID]

    def test_review_separates_manifest_breaches_from_synthetic_ones(self):
        """`categories.breached` mixed both, so every consumer read a
        runtime-injected goal as a declared contract the agent had failed."""
        synthetic = GoalSpec(
            id=SESSION_GOAL_ALIGNMENT_ID,
            category="quality",
            metric="session_goal_alignment_score",
            target=">=0.7",
            weight=5.0,
            window_days=7,
            synthetic=True,
        )
        declared = _spec("declared", "m_a", target=">=0.9")
        captured: dict[str, Any] = {}

        def fake_register(agent_id, rating, categories, feedback, action_items, **kwargs):
            captured["categories"] = categories
            captured["feedback"] = feedback
            return "review-1"

        with (
            patch(
                "robothor.engine.goals.compose_goals",
                return_value=[synthetic, declared],
            ),
            patch(
                "robothor.engine.goals.compute_goal_metrics",
                return_value={"session_goal_alignment_score": 0.1, "m_a": 0.1},
            ),
            patch("robothor.engine.goals.detect_goal_breach", return_value=[]),
            patch("robothor.engine.goals.register_review", side_effect=fake_register),
        ):
            run_nightly_auto_review([{"id": "worker"}])

        assert captured["categories"]["breached_manifest"] == ["declared"]
        assert captured["categories"]["breached_synthetic"] == [SESSION_GOAL_ALIGNMENT_ID]
        assert "not manifest contracts" in captured["feedback"]
        assert "not enforced" in captured["feedback"]

    def test_session_goals_enforced_reads_manifest_opt_in(self):
        assert session_goals_enforced({"id": "a"}) is False
        assert session_goals_enforced({"id": "a", "session_goals": {}}) is False
        assert session_goals_enforced({"id": "a", "session_goals": {"enforce": True}}) is True
        assert session_goals_enforced(None) is False

    def test_nightly_review_passes_opt_in_to_breach_detection(self):
        seen: dict[str, Any] = {}

        def fake_detect(agent_id, goals, tenant_id=None, include_synthetic=False):
            seen[agent_id] = include_synthetic
            return []

        with (
            patch("robothor.engine.goals.compose_goals", return_value=[_spec("g1", "m1")]),
            patch("robothor.engine.goals.compute_goal_metrics", return_value={"m1": 0.9}),
            patch("robothor.engine.goals.detect_goal_breach", side_effect=fake_detect),
            patch("robothor.engine.goals.register_review", return_value="r"),
        ):
            run_nightly_auto_review(
                [
                    {"id": "opted-out"},
                    {"id": "opted-in", "session_goals": {"enforce": True}},
                ]
            )

        assert seen == {"opted-out": False, "opted-in": True}

    def test_achievement_lists_synthetic_goal_ids(self):
        synthetic = GoalSpec(
            id=SESSION_GOAL_ALIGNMENT_ID,
            category="quality",
            metric="session_goal_alignment_score",
            target=">=0.7",
            weight=5.0,
            window_days=7,
            synthetic=True,
        )
        declared = _spec("declared", "m_a")

        with patch(
            "robothor.engine.goals.compute_goal_metrics",
            return_value={"session_goal_alignment_score": 0.9, "m_a": 0.9},
        ):
            result = compute_achievement_score("worker", [synthetic, declared])

        assert result["synthetic_goals"] == [SESSION_GOAL_ALIGNMENT_ID]
        by_id = {g["id"]: g for g in result["per_goal"]}
        assert by_id[SESSION_GOAL_ALIGNMENT_ID]["synthetic"] is True
        assert by_id["declared"]["synthetic"] is False
