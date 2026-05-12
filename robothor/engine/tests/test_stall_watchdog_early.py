"""Tests for the early-stall guard on `_StallWatchdog`.

Covers the 2026-05-04 addition: a pre-output stall window that trips when
elapsed > threshold AND no output-bearing touches (llm_response /
stream_text / stream_tool / tool:) have been seen. Designed to catch the
"warmup completes → silence → 30-min reaper" wedge pattern on the main
heartbeat without re-introducing the mid-thought-garbage regression.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.runner import _StallWatchdog


@pytest.mark.asyncio
async def test_early_stall_trips_when_no_output_seen():
    """Setup-only touches should not block the early-stall trip."""
    wd = _StallWatchdog(
        stall_timeout=0,
        hard_timeout=0,
        early_stall_timeout=1,
        tick_seconds=0.05,
    )

    async def victim() -> None:
        for _ in range(40):
            wd.touch("session_started")
            await asyncio.sleep(0.05)

    task = asyncio.create_task(victim())
    wd.start(task)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert wd.was_stall_timeout is True
    assert "Early stall" in wd.abort_reason
    assert wd._saw_output_signal is False


@pytest.mark.asyncio
async def test_output_touch_clears_early_stall_guard():
    """An llm_response touch should disarm the early-stall guard."""
    wd = _StallWatchdog(
        stall_timeout=0,
        hard_timeout=0,
        early_stall_timeout=1,
        tick_seconds=0.05,
    )

    async def victim() -> None:
        wd.touch("session_started")
        await asyncio.sleep(0.2)
        wd.touch("llm_response:test-model")
        # Hold past the early-stall threshold; should NOT trip.
        await asyncio.sleep(1.5)

    task = asyncio.create_task(victim())
    wd.start(task)

    await task
    assert wd.was_stall_timeout is False
    assert wd._saw_output_signal is True


@pytest.mark.asyncio
async def test_disabled_when_threshold_zero():
    """early_stall_timeout=0 means no trip even with no output."""
    wd = _StallWatchdog(
        stall_timeout=0,
        hard_timeout=0,
        early_stall_timeout=0,
        tick_seconds=0.05,
    )

    async def victim() -> None:
        for _ in range(20):
            wd.touch("warmup_complete")
            await asyncio.sleep(0.05)

    task = asyncio.create_task(victim())
    wd.start(task)

    await task
    assert wd.was_stall_timeout is False


@pytest.mark.asyncio
async def test_post_progress_stall_still_works_with_early_stall_set():
    """Setting early_stall_timeout must not break the regular post-progress stall."""
    wd = _StallWatchdog(
        stall_timeout=1,
        hard_timeout=0,
        early_stall_timeout=10,
        tick_seconds=0.05,
    )

    async def victim() -> None:
        wd.touch("llm_response:test-model")  # output seen → early stall disarmed
        await asyncio.sleep(5.0)

    task = asyncio.create_task(victim())
    wd.start(task)

    with pytest.raises(asyncio.CancelledError):
        await task

    assert wd.was_stall_timeout is True
    assert "No progress" in wd.abort_reason
    assert "Early stall" not in wd.abort_reason


@pytest.mark.asyncio
async def test_tool_touch_counts_as_output():
    """tool:<name> touches should disarm the early-stall guard."""
    wd = _StallWatchdog(
        stall_timeout=0,
        hard_timeout=0,
        early_stall_timeout=1,
        tick_seconds=0.05,
    )

    async def victim() -> None:
        wd.touch("init_begin")
        await asyncio.sleep(0.2)
        wd.touch("tool:list_tasks")
        await asyncio.sleep(1.5)

    task = asyncio.create_task(victim())
    wd.start(task)

    await task
    assert wd.was_stall_timeout is False
    assert wd._saw_output_signal is True


def test_elapsed_seconds_property_available_for_diagnostic():
    """Diagnostic dump uses elapsed_seconds + idle_seconds — must be public."""
    wd = _StallWatchdog(stall_timeout=0, hard_timeout=0)
    # Properties exist and return floats, even before start() was called.
    assert isinstance(wd.elapsed_seconds, float)
    assert isinstance(wd.idle_seconds, float)
