import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.models import RunStep, StepType
from robothor.engine.performance import periodic_progress, run_measurements
from robothor.engine.routine_request import TOOL, attach_draft_reference


def test_parallel_tool_time_is_counted_once_and_errors_not_verified():
    start = datetime.now(UTC)
    steps = [
        RunStep(
            step_number=i,
            step_type=StepType.TOOL_CALL,
            started_at=start + timedelta(seconds=2),
            duration_ms=1000,
            tool_output={"verification": "verified"} if i == 2 else {},
        )
        for i in (1, 2, 3)
    ]
    run = SimpleNamespace(steps=steps, started_at=start, duration_ms=3000, id="fixture")
    result = run_measurements(run)
    assert result["tool_wall_ms"] == 1000
    assert result["post_completion_tool_calls"] == 1
    assert result["time_to_first_action_ms"] == 1000
    assert result["time_to_verified_completion_ms"] == 2000


async def test_progress_runs_while_provider_is_waiting_and_cancels():
    callback = AsyncMock()
    session = SimpleNamespace(run_id="fixture", run=SimpleNamespace(steps=[]))
    task = asyncio.create_task(periodic_progress(session, callback, interval=0.005))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert callback.await_count >= 1
    assert callback.call_args.args[0]["event"] == "progress"


def test_draft_marker_attached_from_tool_evidence():
    session = SimpleNamespace(
        run=SimpleNamespace(
            steps=[
                RunStep(tool_name=TOOL, tool_output={"status": "draft", "operation_id": "fixture"})
            ]
        )
    )
    assert attach_draft_reference(session, "Review this draft.").endswith(
        "Calendar operation: fixture"
    )
    text = attach_draft_reference(session, "Review this draft.")
    assert attach_draft_reference(session, text) == text


async def test_progress_names_phase_and_elapsed_time_without_tool_arguments():
    from robothor.engine.performance import ProgressReporter

    callback = AsyncMock()
    reporter = ProgressReporter(callback)
    await reporter.status(
        {
            "event": "tools_start",
            "tools": ["gws_calendar_add_attendees"],
            "arguments": {"secret": "never display"},
        }
    )
    event = reporter.progress(elapsed_s=45, completed=2)
    assert event["phase"] == "tools"
    assert "45s" in event["text"]
    assert "calendar" in event["text"].lower()
    assert "never display" not in str(event)
