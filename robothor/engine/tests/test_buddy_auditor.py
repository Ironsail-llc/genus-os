"""Tests for the Buddy auditor — hold-rate guardrail."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from robothor.engine import buddy_auditor
from robothor.engine.buddy_auditor import (
    HOLD_RATE_WINDOW_DAYS,
    compute_hold_rate,
    run_audit,
)


def _make_verified_task(
    task_id: str,
    held_tag: str | None = "held_7d=true",
    updated_hours_ago: int = 48,
) -> dict:
    tags = ["nightwatch", "self-improve", "email-responder", "error_rate", "verified_resolved"]
    if held_tag:
        tags.append(held_tag)
    return {
        "id": task_id,
        "status": "DONE",
        "tags": tags,
        "updated_at": datetime.now(UTC) - timedelta(hours=updated_hours_ago),
    }


class TestComputeHoldRate:
    @patch("robothor.crm.dal.list_tasks")
    def test_counts_held_true_and_false(self, mock_list):
        mock_list.return_value = [
            _make_verified_task("t1", held_tag="held_7d=true"),
            _make_verified_task("t2", held_tag="held_7d=true"),
            _make_verified_task("t3", held_tag="held_7d=false"),
            _make_verified_task("t4", held_tag=None),  # still in grace
        ]
        held_true, held_false, total = compute_hold_rate()
        assert held_true == 2
        assert held_false == 1
        assert total == 4

    @patch("robothor.crm.dal.list_tasks")
    def test_ignores_tasks_outside_window(self, mock_list):
        outside = _make_verified_task("old", held_tag="held_7d=true")
        outside["updated_at"] = datetime.now(UTC) - timedelta(days=HOLD_RATE_WINDOW_DAYS + 5)
        inside = _make_verified_task("new", held_tag="held_7d=false")
        mock_list.return_value = [outside, inside]
        held_true, held_false, total = compute_hold_rate()
        assert held_true == 0
        assert held_false == 1
        assert total == 1


class TestRunAudit:
    @patch("robothor.engine.buddy_auditor.emit_critical_notification")
    @patch("robothor.engine.buddy_auditor.pause_buddy_manifest", return_value=True)
    @patch("robothor.engine.buddy_auditor.compute_hold_rate")
    def test_pauses_when_below_threshold(self, mock_rate, mock_pause, mock_notify):
        # 1 held_true, 9 held_false → rate 0.10 < 0.30
        mock_rate.return_value = (1, 9, 10)
        outcome = run_audit()
        assert outcome.action == "paused"
        assert outcome.hold_rate == 0.10
        mock_pause.assert_called_once()
        mock_notify.assert_called_once()
        # The notification body must include the actual numbers so the
        # operator sees what happened without clicking through.
        message = mock_notify.call_args[0][0]
        assert "10%" in message
        assert "1/10" in message

    @patch("robothor.engine.buddy_auditor.emit_critical_notification")
    @patch("robothor.engine.buddy_auditor.pause_buddy_manifest")
    @patch("robothor.engine.buddy_auditor.compute_hold_rate")
    def test_ok_when_above_threshold(self, mock_rate, mock_pause, mock_notify):
        mock_rate.return_value = (8, 2, 10)  # 80% hold rate
        outcome = run_audit()
        assert outcome.action == "ok"
        assert outcome.hold_rate == 0.80
        mock_pause.assert_not_called()
        mock_notify.assert_not_called()

    @patch("robothor.engine.buddy_auditor.emit_critical_notification")
    @patch("robothor.engine.buddy_auditor.pause_buddy_manifest")
    @patch("robothor.engine.buddy_auditor.compute_hold_rate")
    def test_insufficient_samples_skips(self, mock_rate, mock_pause, mock_notify):
        # Only 3 scored (below MIN_SAMPLES=5) — don't judge yet
        mock_rate.return_value = (1, 2, 3)
        outcome = run_audit()
        assert outcome.action == "insufficient_samples"
        mock_pause.assert_not_called()
        mock_notify.assert_not_called()

    @patch("robothor.engine.buddy_auditor.emit_critical_notification")
    @patch("robothor.engine.buddy_auditor.pause_buddy_manifest", return_value=True)
    @patch("robothor.engine.buddy_auditor.compute_hold_rate")
    def test_exact_threshold_does_not_pause(self, mock_rate, mock_pause, mock_notify):
        # Rate == threshold means the loop is right at the edge — don't pause
        mock_rate.return_value = (3, 7, 10)  # 30% exactly
        outcome = run_audit()
        assert outcome.action == "ok"
        mock_pause.assert_not_called()

    @patch("robothor.engine.buddy_auditor.emit_critical_notification")
    @patch("robothor.engine.buddy_auditor.pause_buddy_manifest")
    @patch("robothor.engine.buddy_auditor.compute_hold_rate")
    def test_custom_threshold_and_min_samples(self, mock_rate, mock_pause, mock_notify):
        mock_rate.return_value = (5, 5, 10)  # 50% hold rate
        outcome = run_audit(threshold=0.60, min_samples=5)
        assert outcome.action == "paused (already)" or outcome.action == "paused"
        mock_pause.assert_called_once()


class TestPauseBuddyManifest:
    def test_pauses_active_cron_line(self, tmp_path, monkeypatch):
        # Write a minimal buddy.yaml to a temp path
        fake_path = tmp_path / "buddy.yaml"
        fake_path.write_text(
            'id: buddy\nschedule:\n  cron: "0 6-22 * * *"\n  timezone: America/New_York\n'
        )
        monkeypatch.setattr(buddy_auditor, "BUDDY_MANIFEST", fake_path)

        result = buddy_auditor.pause_buddy_manifest()
        assert result is True
        content = fake_path.read_text()
        assert "AUTO-PAUSED by buddy-auditor" in content
        assert 'cron: ""' in content

    def test_idempotent_when_already_paused(self, tmp_path, monkeypatch):
        fake_path = tmp_path / "buddy.yaml"
        fake_path.write_text(
            "id: buddy\n"
            "schedule:\n"
            "  # AUTO-PAUSED by buddy-auditor 2026-04-19T00:00:00+00:00\n"
            '  cron: ""\n'
        )
        monkeypatch.setattr(buddy_auditor, "BUDDY_MANIFEST", fake_path)

        result = buddy_auditor.pause_buddy_manifest()
        assert result is False

    def test_returns_false_when_manifest_missing(self, tmp_path, monkeypatch):
        fake_path = tmp_path / "missing.yaml"
        monkeypatch.setattr(buddy_auditor, "BUDDY_MANIFEST", fake_path)
        result = buddy_auditor.pause_buddy_manifest()
        assert result is False


class TestEmitCriticalNotification:
    @patch("robothor.crm.dal.send_notification")
    def test_sends_alert_to_main(self, mock_send):
        buddy_auditor.emit_critical_notification("hold-rate dropped")
        kwargs = mock_send.call_args.kwargs
        assert kwargs["from_agent"] == "buddy-auditor"
        assert kwargs["to_agent"] == "main"
        assert kwargs["notification_type"] == "alert"
        assert "hold-rate" in kwargs["body"]
