"""Tests for daemon.classify_reap_reason — the reaper's truthful diagnosis.

Before these changes, the reaper hardcoded
    "Reaped by watchdog: stuck in initialization (no LLM call reached)"
on every stale run, regardless of what actually happened. These tests lock in
that `classify_reap_reason` now returns a category + message that matches the
run's actual step history.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.daemon import classify_reap_reason


@pytest.fixture
def run_id() -> str:
    return "deadbeef-1111-2222-3333-abcdef123456"


def _patch_list_steps(steps):
    return patch("robothor.engine.tracking.list_steps", return_value=steps)


class TestClassifyReapReason:
    def test_no_steps_means_no_steps(self, run_id: str) -> None:
        with _patch_list_steps([]):
            category, msg = classify_reap_reason(run_id, "2026-04-23T12:00:00+00:00", None)
        assert category == "no_steps"
        assert "no steps recorded" in msg
        # And crucially: NOT the old misleading string
        assert "stuck in initialization" not in msg

    def test_daemon_restart_window_takes_precedence(self, run_id: str) -> None:
        # Run started at 10:00, daemon booted at 10:30 → run pre-dates current boot
        with _patch_list_steps([{"step_type": "llm_call"}, {"step_type": "tool_call"}]):
            category, msg = classify_reap_reason(
                run_id,
                "2026-04-23T10:00:00+00:00",
                "2026-04-23T10:30:00+00:00",
            )
        assert category == "daemon_restart"
        assert "daemon restart" in msg
        assert "2026-04-23T10:30:00+00:00" in msg

    def test_daemon_restart_does_not_apply_when_run_started_after_boot(self, run_id: str) -> None:
        # Run started at 11:00, daemon booted at 10:30 → runner crash, not restart
        with _patch_list_steps([{"step_type": "llm_response", "tool_name": None}]):
            category, msg = classify_reap_reason(
                run_id,
                "2026-04-23T11:00:00+00:00",
                "2026-04-23T10:30:00+00:00",
            )
        assert category == "post_llm_crash"
        assert "daemon restart" not in msg

    def test_post_llm_crash(self, run_id: str) -> None:
        steps = [
            {"step_type": "llm_call", "tool_name": None, "error_message": None},
            {"step_type": "llm_response", "tool_name": None, "error_message": None},
        ]
        with _patch_list_steps(steps):
            category, msg = classify_reap_reason(run_id, "2026-04-23T12:00:00+00:00", None)
        assert category == "post_llm_crash"
        assert "llm_response" in msg
        assert "total steps=2" in msg

    def test_post_tool_crash_includes_tool_name(self, run_id: str) -> None:
        steps = [
            {"step_type": "llm_call", "tool_name": None},
            {"step_type": "tool_call", "tool_name": "log_interaction", "error_message": None},
        ]
        with _patch_list_steps(steps):
            category, msg = classify_reap_reason(run_id, "2026-04-23T12:00:00+00:00", None)
        assert category == "post_tool_crash"
        assert "log_interaction" in msg

    def test_post_error_crash_includes_error_text(self, run_id: str) -> None:
        steps = [
            {"step_type": "llm_call"},
            {"step_type": "error", "tool_name": None, "error_message": "HTTPStatusError: 500"},
        ]
        with _patch_list_steps(steps):
            category, msg = classify_reap_reason(run_id, "2026-04-23T12:00:00+00:00", None)
        assert category == "post_error_crash"
        assert "HTTPStatusError" in msg

    def test_list_steps_failure_falls_back_to_no_steps(self, run_id: str) -> None:
        with patch("robothor.engine.tracking.list_steps", side_effect=RuntimeError("db down")):
            category, msg = classify_reap_reason(run_id, "2026-04-23T12:00:00+00:00", None)
        assert category == "no_steps"
        assert "no steps recorded" in msg
