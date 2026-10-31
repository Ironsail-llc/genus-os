"""Setup and tool-overhead numbers from a run's recorded steps.

Steps are the ``agent_run_steps`` rows (HTTP half) or ``run.steps`` (in-process
half) as dicts. ``completed_at`` is stamped when the work ends, so an LLM call
started at ``completed_at - duration_ms``.

* ``setup_total_ms``: the ``tools_built`` warmup step, which the runner writes
  with the time from the start of setup to a built toolset.
* ``tool_ms``: the summed duration of the tool calls (the handler's own time).
* ``tool_overhead_ms``: for every gap between two LLM calls that had tool calls
  in it, the gap minus the tools' own time. That is what the loop spends around
  a tool: admission, guardrails, auditing, checkpointing, result shaping.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _ms(step: Mapping[str, Any]) -> int:
    return int(step.get("duration_ms") or 0)


def step_metrics(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    setup_total: int | None = None
    llm: list[Mapping[str, Any]] = []
    tool_ms = 0
    tool_calls = 0
    overhead: float | None = None
    pending_tool_ms = 0
    pending_tools = 0
    last_llm_end: datetime | None = None

    for step in steps:
        kind = str(step.get("step_type") or "")
        if kind == "warmup_phase" and step.get("tool_name") == "tools_built":
            setup_total = _ms(step)
        elif kind == "tool_call":
            tool_calls += 1
            tool_ms += _ms(step)
            pending_tool_ms += _ms(step)
            pending_tools += 1
        elif kind == "llm_call":
            llm.append(step)
            end = _when(step.get("completed_at"))
            if end is not None and last_llm_end is not None and pending_tools:
                start = end - timedelta(milliseconds=_ms(step))
                gap = (start - last_llm_end).total_seconds() * 1000.0
                overhead = (overhead or 0.0) + gap - pending_tool_ms
            pending_tool_ms = 0
            pending_tools = 0
            last_llm_end = end

    return {
        "setup_total_ms": setup_total,
        # No model call recorded means no steps were found, not zero tool time.
        "tool_ms": tool_ms if llm else None,
        "tool_overhead_ms": overhead,
        "tool_calls": tool_calls,
        "llm_calls": len(llm),
    }
