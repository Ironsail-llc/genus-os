"""Tests for the classify_run_failure tool handler.

This tool is the operator-visible counterpart to daemon.classify_reap_reason.
It must return the same category for the same underlying run data so the
reaper and the self-diagnosis tool never disagree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from robothor.engine.tools.handlers.observability import HANDLERS


@pytest.fixture
def classify_handler():
    return HANDLERS["classify_run_failure"]


@pytest.mark.asyncio
async def test_missing_run_id(classify_handler) -> None:
    result = await classify_handler({}, None)
    assert "error" in result


@pytest.mark.asyncio
async def test_run_not_found(classify_handler) -> None:
    with patch("robothor.engine.tracking.get_run", return_value=None):
        result = await classify_handler({"run_id": "missing"}, None)
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_post_tool_crash_classification(classify_handler) -> None:
    started = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    run = {
        "id": "run-1",
        "agent_id": "buddy",
        "status": "timeout",
        "error_message": "Reaped by watchdog: runner crashed after tool log_interaction",
        "started_at": started,
        "completed_at": None,
        "model_used": "sonnet-4.6",
        "input_tokens": 10_000,
        "output_tokens": 500,
    }
    steps = [
        {"step_type": "llm_call", "tool_name": None, "error_message": None},
        {
            "step_type": "tool_call",
            "tool_name": "log_interaction",
            "error_message": "HTTPStatusError: 500",
        },
    ]
    with (
        patch("robothor.engine.tracking.get_run", return_value=run),
        patch("robothor.engine.tracking.list_steps", return_value=steps),
    ):
        result = await classify_handler({"run_id": "run-1"}, None)

    assert result["agent_id"] == "buddy"
    assert result["status"] == "timeout"
    assert result["category"] == "post_tool_crash"
    assert result["llm_was_called"] is True
    assert result["total_llm_calls"] == 1
    assert result["total_steps"] == 2
    assert result["last_step_type"] == "tool_call"
    assert result["last_step_tool"] == "log_interaction"
    assert result["tokens_used"] == 10_500


@pytest.mark.asyncio
async def test_daemon_restart_classification(classify_handler, monkeypatch) -> None:
    monkeypatch.setenv("ROBOTHOR_DAEMON_START_TS", "2026-04-22T18:27:32+00:00")
    started = datetime(2026, 4, 22, 18, 15, 0, tzinfo=UTC)
    run = {
        "id": "run-z",
        "agent_id": "calendar-monitor",
        "status": "timeout",
        "error_message": "Run cancelled externally",
        "started_at": started,
        "completed_at": None,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    with (
        patch("robothor.engine.tracking.get_run", return_value=run),
        patch("robothor.engine.tracking.list_steps", return_value=[]),
    ):
        result = await classify_handler({"run_id": "run-z"}, None)

    assert result["category"] == "daemon_restart"
    assert result["daemon_restart_in_window"] is True
    assert result["daemon_started_at"] == "2026-04-22T18:27:32+00:00"


@pytest.mark.asyncio
async def test_no_steps_classification(classify_handler, monkeypatch) -> None:
    # No ROBOTHOR_DAEMON_START_TS env, no steps → no_steps category
    monkeypatch.delenv("ROBOTHOR_DAEMON_START_TS", raising=False)
    started = datetime(2026, 4, 23, 6, 13, 0, tzinfo=UTC)
    run = {
        "id": "run-a",
        "agent_id": "auto-agent",
        "status": "timeout",
        "error_message": "Reaped",
        "started_at": started,
        "completed_at": None,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    with (
        patch("robothor.engine.tracking.get_run", return_value=run),
        patch("robothor.engine.tracking.list_steps", return_value=[]),
    ):
        result = await classify_handler({"run_id": "run-a"}, None)

    assert result["category"] == "no_steps"
    assert result["llm_was_called"] is False
    assert result["total_llm_calls"] == 0
    assert result["daemon_restart_in_window"] is False
