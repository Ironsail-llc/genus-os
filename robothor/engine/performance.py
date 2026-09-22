"""Comparable run measurements; parallel tool wall time is not summed."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from robothor.engine.models import RunStep, StepType


class ProgressReporter:
    """Human-readable progress from actual loop events, without a model call."""

    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.phase = "preparing"
        self.activity = "Preparing the request"

    async def status(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if kind == "iteration_start":
            self.phase, self.activity = "reasoning", "Working out the next step"
        elif kind == "tools_start":
            self.phase = "tools"
            names = [str(n) for n in event.get("tools", [])]
            self.activity = (
                "Working on the calendar request"
                if names == ["gws_calendar_add_attendees"]
                else "Running requested tools"
            )
        elif kind == "tools_done":
            self.phase, self.activity = "reviewing", "Reviewing the results"
        if self.callback is not None:
            await self.callback(event)

    def progress(self, *, elapsed_s: int, completed: int) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "elapsed_s": elapsed_s,
            "tool_calls_completed": completed,
            "text": f"{self.activity} — {elapsed_s}s elapsed; {completed} tool calls completed.",
        }


async def periodic_progress(
    session: Any,
    callback: Any,
    interval: float = 10.0,
    reporter: ProgressReporter | None = None,
) -> None:
    if callback is None:
        return
    started = time.monotonic()
    while True:
        await asyncio.sleep(interval)
        tools = sum(s.step_type == StepType.TOOL_CALL for s in session.run.steps)
        event = {
            "event": "progress",
            "run_id": session.run_id,
            **(reporter or ProgressReporter(callback)).progress(
                elapsed_s=round(time.monotonic() - started), completed=tools
            ),
        }
        try:
            await asyncio.wait_for(callback(event), timeout=5)
        except Exception as exc:
            logging.getLogger(__name__).debug("Progress delivery failed: %s", type(exc).__name__)


def record_compactions(session: Any, measurements: list[dict[str, Any]]) -> None:
    for measurement in measurements:
        session._step_counter += 1
        session.run.steps.append(
            RunStep(
                run_id=session.run_id,
                step_number=session._step_counter,
                step_type=StepType.COMPACTION,
                duration_ms=measurement["duration_ms"],
                tool_output=measurement,
            )
        )


def run_measurements(run: Any) -> dict[str, Any]:
    steps = list(run.steps)
    llm = [s for s in steps if s.step_type == StepType.LLM_CALL]
    tools = [s for s in steps if s.step_type == StepType.TOOL_CALL]
    compactions = [s for s in steps if s.step_type == StepType.COMPACTION]
    # Tool start timestamps historically denote completion in some paths.
    # Derive intervals from completion and measured duration consistently.
    intervals = []
    for step in tools:
        end = step.completed_at or step.started_at
        if end is not None:
            finish = end.timestamp() * 1000
            intervals.append((finish - (step.duration_ms or 0), finish))
    tool_wall = 0.0
    previous_end = float("-inf")
    for start, end in sorted(intervals):
        tool_wall += max(0.0, end - max(start, previous_end))
        previous_end = max(end, previous_end)
    verified = [
        s
        for s in tools
        if isinstance(s.tool_output, dict)
        and s.tool_output.get("verification") == "verified"
        and not s.tool_output.get("error")
        and not s.error_message
    ]
    first_verified = min((s.step_number for s in verified), default=None)
    started_at = run.started_at
    first_action_ms = min(
        (
            max(0, round((s.started_at - started_at).total_seconds() * 1000) - (s.duration_ms or 0))
            for s in tools
            if s.started_at and started_at
        ),
        default=None,
    )
    completion_ms = min(
        (
            max(0, round(((s.completed_at or s.started_at) - started_at).total_seconds() * 1000))
            for s in verified
            if (s.completed_at or s.started_at) and started_at
        ),
        default=None,
    )
    return {
        "run_id": run.id,
        "duration_ms": run.duration_ms,
        "model_calls": len(llm),
        "tool_calls": len(tools),
        "provider_ms": sum(s.duration_ms or 0 for s in llm),
        "tool_wall_ms": round(tool_wall),
        "compaction_ms": sum(s.duration_ms or 0 for s in compactions),
        "compactions": len(compactions),
        "input_tokens": sum(s.input_tokens or 0 for s in llm),
        "time_to_first_action_ms": first_action_ms,
        "time_to_verified_completion_ms": completion_ms,
        "post_completion_tool_calls": (
            sum(s.step_number > first_verified for s in tools)
            if first_verified is not None
            else None
        ),
    }


async def show_progress(bot: Any, chat_id: str, message_id: int | None, text: str) -> None:
    if message_id is None:
        return
    try:
        await bot.edit_message_text(
            chat_id=int(chat_id), message_id=message_id, text=text, parse_mode=None
        )
    except Exception as exc:
        logging.getLogger(__name__).debug("Progress edit failed: %s", type(exc).__name__)
