"""Tests for the Buddy grader — closed-loop verification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from robothor.engine.buddy_grader import (
    extract_escalation_level,
    hold_check_7d,
    parse_baseline_from_body,
    verify_resolved_task,
)


def _body_with_baseline(
    metric: str = "error_rate",
    target: str = "<0.05",
    baseline: float | None = 0.12,
    window_days: int = 7,
) -> str:
    return (
        f"Some task body.\n\n"
        f'<!-- buddy-baseline: {{"metric": "{metric}", "target": "{target}", '
        f'"baseline": {baseline}, "window_days": {window_days}}} -->\n'
    )


def _body_with_legacy_baseline(
    metric: str = "error_rate",
    target: str = "<0.05",
    baseline: float | None = 0.12,
) -> str:
    """A task body in the pre-window_days format, for back-compat tests."""
    return (
        f"Legacy body.\n\n"
        f'<!-- buddy-baseline: {{"metric": "{metric}", "target": "{target}", '
        f'"baseline": {baseline}, "window_days_hint": null}} -->\n'
    )


def _make_task(
    task_id: str = "task-1",
    agent_id: str = "email-responder",
    metric: str = "error_rate",
    target: str = "<0.05",
    baseline: float = 0.12,
    status: str = "DONE",
    resolved_hours_ago: int = 50,
    extra_tags: list[str] | None = None,
    requires_human: bool = False,
) -> dict:
    tags = ["nightwatch", "self-improve", agent_id, metric]
    if extra_tags:
        tags.extend(extra_tags)
    return {
        "id": task_id,
        "status": status,
        "body": _body_with_baseline(metric, target, baseline),
        "tags": tags,
        "resolved_at": datetime.now(UTC) - timedelta(hours=resolved_hours_ago),
        "updated_at": datetime.now(UTC) - timedelta(hours=resolved_hours_ago),
        "requires_human": requires_human,
    }


class TestParseBaseline:
    def test_extracts_json_blob(self):
        body = _body_with_baseline()
        parsed = parse_baseline_from_body(body)
        assert parsed["metric"] == "error_rate"
        assert parsed["target"] == "<0.05"
        assert parsed["baseline"] == 0.12
        assert parsed["window_days"] == 7

    def test_extracts_nondefault_window_days(self):
        body = _body_with_baseline(window_days=30)
        parsed = parse_baseline_from_body(body)
        assert parsed["window_days"] == 30

    def test_legacy_body_without_window_days(self):
        """In-flight tasks opened before the window_days field was added
        still parse cleanly — the grader falls back to the default window."""
        body = _body_with_legacy_baseline()
        parsed = parse_baseline_from_body(body)
        assert parsed["metric"] == "error_rate"
        assert "window_days" not in parsed

    def test_returns_none_when_missing(self):
        assert parse_baseline_from_body("no marker here") is None
        assert parse_baseline_from_body("") is None

    def test_returns_none_on_bad_json(self):
        body = "<!-- buddy-baseline: {not valid json} -->"
        assert parse_baseline_from_body(body) is None


class TestExtractEscalationLevel:
    def test_no_escalation_tag(self):
        assert extract_escalation_level(["self-improve", "nightwatch"]) == 0

    def test_single(self):
        assert extract_escalation_level(["escalation:1"]) == 1

    def test_multiple_takes_max(self):
        assert extract_escalation_level(["escalation:1", "escalation:2"]) == 2

    def test_empty(self):
        assert extract_escalation_level([]) == 0
        assert extract_escalation_level(None) == 0


class TestVerifyResolvedTask:
    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_metric_held_marks_verified_resolved(self, mock_metrics, mock_update, _mock_journal):
        # Current error_rate 0.02 satisfies target "<0.05"
        mock_metrics.return_value = {"error_rate": 0.02}
        task = _make_task()
        outcome = verify_resolved_task(task)
        assert outcome is not None
        assert outcome.satisfied is True
        assert "verified_resolved" in outcome.new_tags_added
        assert outcome.escalation_level == 0
        assert outcome.requires_human is False
        # update_task called once, keeping status=DONE
        kwargs = mock_update.call_args.kwargs
        assert "verified_resolved" in kwargs["tags"]
        assert kwargs.get("status") is None  # DONE stays DONE

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_metric_still_breached_bumps_escalation(self, mock_metrics, mock_update, _mock_journal):
        mock_metrics.return_value = {"error_rate": 0.09}  # still >0.05
        task = _make_task()
        outcome = verify_resolved_task(task)
        assert outcome is not None
        assert outcome.satisfied is False
        assert outcome.escalation_level == 1
        assert "verify_failed" in outcome.new_tags_added
        assert "escalation:1" in outcome.new_tags_added
        # Task re-opened to IN_PROGRESS
        kwargs = mock_update.call_args.kwargs
        assert kwargs["status"] == "IN_PROGRESS"

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_escalation_2_routes_to_auto_researcher(self, mock_metrics, mock_update, _mock_journal):
        mock_metrics.return_value = {"error_rate": 0.09}
        task = _make_task(extra_tags=["escalation:1"])
        outcome = verify_resolved_task(task)
        assert outcome.escalation_level == 2
        kwargs = mock_update.call_args.kwargs
        assert kwargs["assigned_to_agent"] == "auto-researcher"
        assert "escalation:2" in kwargs["tags"]
        assert "escalation:1" not in kwargs["tags"]  # collapsed to max
        assert kwargs.get("requires_human") is None  # not yet at 3

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_escalation_3_sets_requires_human(self, mock_metrics, mock_update, _mock_journal):
        mock_metrics.return_value = {"error_rate": 0.09}
        task = _make_task(extra_tags=["escalation:2"])
        outcome = verify_resolved_task(task)
        assert outcome.escalation_level == 3
        assert outcome.requires_human is True
        kwargs = mock_update.call_args.kwargs
        assert kwargs["requires_human"] is True

    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_too_recent_returns_none(self, mock_metrics, mock_update):
        task = _make_task(resolved_hours_ago=10)  # less than 48h
        outcome = verify_resolved_task(task)
        assert outcome is None
        mock_update.assert_not_called()

    @patch("robothor.crm.dal.update_task")
    def test_already_verified_returns_none(self, mock_update):
        task = _make_task(extra_tags=["verified_resolved"])
        outcome = verify_resolved_task(task)
        assert outcome is None
        mock_update.assert_not_called()

    @patch("robothor.crm.dal.update_task")
    def test_non_done_status_returns_none(self, mock_update):
        task = _make_task(status="IN_PROGRESS")
        outcome = verify_resolved_task(task)
        assert outcome is None
        mock_update.assert_not_called()

    @patch("robothor.crm.dal.update_task")
    def test_missing_baseline_marker_returns_none(self, mock_update):
        task = _make_task()
        task["body"] = "No marker in this body."
        outcome = verify_resolved_task(task)
        assert outcome is None
        mock_update.assert_not_called()

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_metric_computation_failure_still_evaluates(
        self, mock_metrics, mock_update, _mock_journal
    ):
        """If compute_goal_metrics errors, current_val is None → treated as breach."""
        mock_metrics.side_effect = RuntimeError("db down")
        task = _make_task()
        outcome = verify_resolved_task(task)
        assert outcome is not None
        assert outcome.satisfied is False
        assert outcome.current is None

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_uses_baseline_window_days_on_reverify(self, mock_metrics, mock_update, _mock_journal):
        """A 30-day-window goal must be re-verified on its own 30-day slice,
        not the grader's hardcoded 7. Otherwise long-window goals swing on
        noise and the loop escalates fixes that actually stuck."""
        mock_metrics.return_value = {"error_rate": 0.02}
        task = _make_task()
        task["body"] = _body_with_baseline(window_days=30)
        verify_resolved_task(task)
        # The grader should pass window_days=30 through to compute_goal_metrics.
        call_kwargs = mock_metrics.call_args.kwargs
        assert call_kwargs["window_days"] == 30

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_legacy_body_falls_back_to_default_window(
        self, mock_metrics, mock_update, _mock_journal
    ):
        """A task body in the legacy `window_days_hint: null` format must
        still verify cleanly — defaulting to 7, the old hardcoded value."""
        mock_metrics.return_value = {"error_rate": 0.02}
        task = _make_task()
        task["body"] = _body_with_legacy_baseline()
        outcome = verify_resolved_task(task)
        assert outcome is not None
        call_kwargs = mock_metrics.call_args.kwargs
        assert call_kwargs["window_days"] == 7

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_satisfied_run_stamps_verified_at_tag(self, mock_metrics, mock_update, _mock_journal):
        """The grader stamps `verified_at:<iso>` alongside `verified_resolved`
        so the 7-day hold check has a fixed anchor independent of whatever
        else touches the task."""
        mock_metrics.return_value = {"error_rate": 0.02}
        task = _make_task()
        outcome = verify_resolved_task(task)
        assert outcome is not None
        assert outcome.satisfied is True
        kwargs = mock_update.call_args.kwargs
        verified_at_tags = [t for t in kwargs["tags"] if t.startswith("verified_at:")]
        assert len(verified_at_tags) == 1
        # The tag round-trips to a parseable datetime.
        from datetime import datetime as _dt

        _dt.fromisoformat(verified_at_tags[0].split(":", 1)[1])


class TestHoldCheck7d:
    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_held_tags_true_when_metric_still_satisfies(
        self, mock_metrics, mock_update, _mock_journal
    ):
        mock_metrics.return_value = {"error_rate": 0.02}
        task = _make_task(extra_tags=["verified_resolved"])
        task["updated_at"] = datetime.now(UTC) - timedelta(days=8)
        outcome = hold_check_7d(task)
        assert outcome is not None
        assert outcome.satisfied is True
        assert "held_7d=true" in outcome.new_tags_added

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_held_tags_false_when_regressed(self, mock_metrics, mock_update, _mock_journal):
        mock_metrics.return_value = {"error_rate": 0.08}
        task = _make_task(extra_tags=["verified_resolved"])
        task["updated_at"] = datetime.now(UTC) - timedelta(days=8)
        outcome = hold_check_7d(task)
        assert outcome.satisfied is False
        assert "held_7d=false" in outcome.new_tags_added

    @patch("robothor.crm.dal.update_task")
    def test_too_recent_returns_none(self, mock_update):
        task = _make_task(extra_tags=["verified_resolved"])
        task["updated_at"] = datetime.now(UTC) - timedelta(days=3)
        assert hold_check_7d(task) is None

    @patch("robothor.crm.dal.update_task")
    def test_not_verified_returns_none(self, mock_update):
        task = _make_task()  # no verified_resolved tag
        task["updated_at"] = datetime.now(UTC) - timedelta(days=10)
        assert hold_check_7d(task) is None

    @patch("robothor.crm.dal.update_task")
    def test_already_hold_checked_returns_none(self, mock_update):
        task = _make_task(extra_tags=["verified_resolved", "held_7d=true"])
        task["updated_at"] = datetime.now(UTC) - timedelta(days=10)
        assert hold_check_7d(task) is None

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_uses_baseline_window_days_on_hold_check(
        self, mock_metrics, mock_update, _mock_journal
    ):
        """Hold-check must use the goal's own window, not the grader default."""
        mock_metrics.return_value = {"pr_revert_rate": 0.02}
        task = _make_task(
            metric="pr_revert_rate",
            target="<0.05",
            baseline=0.10,
            extra_tags=["verified_resolved"],
        )
        task["body"] = _body_with_baseline(
            metric="pr_revert_rate", target="<0.05", baseline=0.10, window_days=60
        )
        task["updated_at"] = datetime.now(UTC) - timedelta(days=8)
        hold_check_7d(task)
        call_kwargs = mock_metrics.call_args.kwargs
        assert call_kwargs["window_days"] == 60

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_prefers_verified_at_tag_over_updated_at(
        self, mock_metrics, mock_update, _mock_journal
    ):
        """When verified_at:<iso> is present, the hold check anchors on it,
        not on task.updated_at (which drifts)."""
        mock_metrics.return_value = {"error_rate": 0.02}
        # updated_at is only 2 days ago (would normally skip), but verified_at
        # is 10 days ago — so the hold check should fire.
        verified_anchor = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        task = _make_task(extra_tags=["verified_resolved", f"verified_at:{verified_anchor}"])
        task["updated_at"] = datetime.now(UTC) - timedelta(days=2)
        outcome = hold_check_7d(task)
        assert outcome is not None
        assert outcome.satisfied is True


class TestDryRunMode:
    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_dryrun_skips_update_but_returns_outcome(
        self, mock_metrics, mock_update, _mock_journal, monkeypatch
    ):
        """ROBOTHOR_BUDDY_GRADER_DRYRUN=1 → compute verdict, write nothing."""
        monkeypatch.setenv("ROBOTHOR_BUDDY_GRADER_DRYRUN", "1")
        mock_metrics.return_value = {"error_rate": 0.09}
        task = _make_task()
        outcome = verify_resolved_task(task)
        assert outcome is not None
        assert outcome.satisfied is False
        assert outcome.escalation_level == 1
        # No task update applied in dry-run.
        mock_update.assert_not_called()

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_dryrun_applies_when_flag_absent(
        self, mock_metrics, mock_update, _mock_journal, monkeypatch
    ):
        """Without the env flag, normal write path runs."""
        monkeypatch.delenv("ROBOTHOR_BUDDY_GRADER_DRYRUN", raising=False)
        mock_metrics.return_value = {"error_rate": 0.02}
        task = _make_task()
        outcome = verify_resolved_task(task)
        assert outcome is not None
        mock_update.assert_called_once()


class TestAgentIdResolution:
    """The grader infers agent_id from the task tags. Make sure ambiguous
    tag sets don't cause it to pick a metric name as the agent."""

    @patch("robothor.engine.buddy_grader._journal")
    @patch("robothor.crm.dal.update_task")
    @patch("robothor.engine.buddy_grader.compute_goal_metrics")
    def test_picks_agent_tag_not_metric_tag(self, mock_metrics, mock_update, _mock_journal):
        mock_metrics.return_value = {"tool_success_rate": 0.99}
        # Tags in unusual order to verify the metric-name filter catches tool_success_rate
        task = {
            "id": "t1",
            "status": "DONE",
            "body": _body_with_baseline(metric="tool_success_rate", target=">0.95", baseline=0.82),
            "tags": [
                "tool_success_rate",  # metric name
                "crm-enrichment",  # the real agent
                "self-improve",
                "nightwatch",
            ],
            "resolved_at": datetime.now(UTC) - timedelta(hours=50),
            "updated_at": datetime.now(UTC) - timedelta(hours=50),
            "requires_human": False,
        }
        outcome = verify_resolved_task(task)
        assert outcome is not None
        assert outcome.agent_id == "crm-enrichment"
