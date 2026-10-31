"""A run's recorded steps become setup and tool-overhead numbers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bench.turn.steps import step_metrics

BASE = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


def _at(ms: int) -> datetime:
    return BASE + timedelta(milliseconds=ms)


def _steps() -> list[dict]:
    # Steps record completed_at at the end of the work, so an LLM call that
    # took 100 ms and finished at 300 started at 200.
    return [
        {
            "step_type": "warmup_phase",
            "tool_name": "system_prompt_build",
            "duration_ms": 40,
            "completed_at": _at(40),
        },
        {
            "step_type": "warmup_phase",
            "tool_name": "tools_built",
            "duration_ms": 180,
            "completed_at": _at(180),
        },
        {"step_type": "llm_call", "duration_ms": 100, "completed_at": _at(300)},
        {"step_type": "tool_call", "tool_name": "noop", "duration_ms": 15, "completed_at": _at(330)},
        {"step_type": "tool_call", "tool_name": "noop", "duration_ms": 5, "completed_at": _at(340)},
        # Next LLM call started at 450 - 60 = 390: a 90 ms gap after the
        # first call, 20 ms of which the tools spent.
        {"step_type": "llm_call", "duration_ms": 60, "completed_at": _at(450)},
    ]


def test_setup_total_comes_from_the_tools_built_step() -> None:
    assert step_metrics(_steps())["setup_total_ms"] == 180


def test_tool_time_and_overhead() -> None:
    metrics = step_metrics(_steps())
    assert metrics["tool_ms"] == 20
    assert metrics["tool_overhead_ms"] == pytest.approx(70.0)
    assert metrics["tool_calls"] == 2
    assert metrics["llm_calls"] == 2


def test_timestamps_as_iso_strings_work_too() -> None:
    steps = [{**s, "completed_at": s["completed_at"].isoformat()} for s in _steps()]
    assert step_metrics(steps)["tool_overhead_ms"] == pytest.approx(70.0)


def test_a_turn_without_tools_has_no_tool_overhead() -> None:
    steps = [{"step_type": "llm_call", "duration_ms": 100, "completed_at": _at(100)}]
    metrics = step_metrics(steps)
    assert metrics["tool_overhead_ms"] is None
    assert metrics["tool_ms"] == 0
    assert metrics["setup_total_ms"] is None
