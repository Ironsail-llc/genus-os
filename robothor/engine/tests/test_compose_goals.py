"""Tests for compose_goals — the unified read path.

`compose_goals(agent_id, manifest, tenant_id)` returns the merged GoalSpec
list that the buddy + auto-researcher + warmup all read from. It pulls
metric_targets from the unified session_goal task when present, falls
back to the manifest's `goals:` block when no task exists, and prepends
synthetic GoalSpecs for `session_goal_alignment_score` and
`session_goal_progress` when the operator has set an objective.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from robothor.engine.goals import GoalSpec, compose_goals


def _row(
    *, tags: list[str], objective: str = "", meta: dict[str, Any] | None = None, task_id: str = "t1"
) -> dict[str, Any]:
    return {
        "id": task_id,
        "objective": objective,
        "tags": tags,
        "status": "TODO",
        "session_goal_meta": meta
        or {
            "objective": objective,
            "success_criteria": [],
            "metric_targets": [],
            "evidence": [],
            "completion_note": "",
            "alignment_target": ">=0.7",
        },
    }


@patch("robothor.engine.goals._load_active_goal_for_agent")
def test_no_task_falls_back_to_manifest_goals(mock_load):
    mock_load.return_value = None
    manifest = {
        "id": "main",
        "goals": {
            "quality": [
                {
                    "id": "passes-its-job",
                    "metric": "benchmark_pass_rate",
                    "target": ">=0.85",
                    "weight": 5.0,
                }
            ]
        },
    }

    specs = compose_goals(agent_id="main", manifest=manifest, tenant_id="default")
    ids = [s.id for s in specs]
    assert "passes-its-job" in ids


@patch("robothor.engine.goals._load_active_goal_for_agent")
def test_task_metric_targets_replace_manifest_when_present(mock_load):
    # Task has metric_targets; manifest has different goals. Task wins as source of truth.
    mock_load.return_value = _row(
        tags=["session_goal", "agent:main", "thread"],
        objective="Ship Q3 redesign",
        meta={
            "objective": "Ship Q3 redesign",
            "success_criteria": ["criterion 1"],
            "metric_targets": [
                {
                    "id": "ship-it",
                    "category": "reach",
                    "metric": "delivery_success_rate",
                    "target": ">=0.95",
                    "weight": 5.0,
                    "window_days": 7,
                    "extras": {},
                }
            ],
            "evidence": [],
            "completion_note": "",
            "alignment_target": ">=0.7",
        },
    )

    manifest = {
        "id": "main",
        "goals": {
            "quality": [
                {"id": "passes-its-job", "metric": "benchmark_pass_rate", "target": ">=0.85"}
            ]
        },
    }

    specs = compose_goals(agent_id="main", manifest=manifest, tenant_id="default")
    ids = [s.id for s in specs]
    assert "ship-it" in ids
    # Manifest's `passes-its-job` is shadowed when the task's metric_targets exist.
    assert "passes-its-job" not in ids


@patch("robothor.engine.goals._load_active_goal_for_agent")
def test_synthetic_alignment_goal_present_when_objective_set(mock_load):
    mock_load.return_value = _row(
        tags=["session_goal", "agent:main", "thread"],
        objective="Make the morning briefing useful",
        meta={
            "objective": "Make the morning briefing useful",
            "success_criteria": ["c1"],
            "metric_targets": [],
            "evidence": [],
            "completion_note": "",
            "alignment_target": ">=0.7",
        },
    )
    manifest = {"id": "main", "goals": {}}

    specs = compose_goals(agent_id="main", manifest=manifest, tenant_id="default")
    ids = [s.id for s in specs]
    assert "session-goal-alignment" in ids
    alignment = next(s for s in specs if s.id == "session-goal-alignment")
    assert alignment.metric == "session_goal_alignment_score"
    assert alignment.target == ">=0.7"
    assert alignment.weight >= 5.0  # high weight so the loop targets it


@patch("robothor.engine.goals._load_active_goal_for_agent")
def test_synthetic_progress_goal_present_when_criteria_exist(mock_load):
    mock_load.return_value = _row(
        tags=["session_goal", "agent:main", "thread"],
        objective="Ship the merge",
        meta={
            "objective": "Ship the merge",
            "success_criteria": ["c1", "c2"],
            "metric_targets": [],
            "evidence": [],
            "completion_note": "",
            "alignment_target": ">=0.7",
        },
    )
    manifest = {"id": "main", "goals": {}}

    specs = compose_goals(agent_id="main", manifest=manifest, tenant_id="default")
    ids = [s.id for s in specs]
    assert "session-goal-progress" in ids


@patch("robothor.engine.goals._load_active_goal_for_agent")
def test_no_objective_means_no_synthetic_goals(mock_load):
    # Task exists but objective was never edited (legacy or freshly-seeded
    # placeholder). Synthetic alignment goal should not be added.
    mock_load.return_value = _row(
        tags=["session_goal", "agent:main", "thread"],
        objective="",
        meta={
            "objective": "",
            "success_criteria": [],
            "metric_targets": [],
            "evidence": [],
            "completion_note": "",
            "alignment_target": ">=0.7",
        },
    )
    manifest = {"id": "main", "goals": {}}

    specs = compose_goals(agent_id="main", manifest=manifest, tenant_id="default")
    ids = [s.id for s in specs]
    assert "session-goal-alignment" not in ids
    assert "session-goal-progress" not in ids


@patch("robothor.engine.goals._load_active_goal_for_agent")
def test_synthetic_alignment_uses_task_alignment_target(mock_load):
    mock_load.return_value = _row(
        tags=["session_goal", "agent:main", "thread"],
        objective="x",
        meta={
            "objective": "x",
            "success_criteria": [],
            "metric_targets": [],
            "evidence": [],
            "completion_note": "",
            "alignment_target": ">=0.85",  # tighter than default
        },
    )
    manifest = {"id": "main", "goals": {}}

    specs = compose_goals(agent_id="main", manifest=manifest, tenant_id="default")
    alignment = next(s for s in specs if s.id == "session-goal-alignment")
    assert alignment.target == ">=0.85"


def test_returns_goalspec_dataclasses():
    """Sanity: callers (buddy_critic, etc.) expect GoalSpec dataclass instances."""
    with patch("robothor.engine.goals._load_active_goal_for_agent", return_value=None):
        manifest = {
            "id": "main",
            "goals": {"quality": [{"id": "g", "metric": "m", "target": ">=0.5"}]},
        }
        specs = compose_goals(agent_id="main", manifest=manifest, tenant_id="default")
        assert all(isinstance(s, GoalSpec) for s in specs)
