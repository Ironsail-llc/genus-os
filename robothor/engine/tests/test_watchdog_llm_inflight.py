"""Bounded-wait windows: the watchdog must stop killing runs that are waiting.

2026-08-27. The OpenRouter weekly cap sent the whole fleet to the local
`ollama_chat/qwen3.8:27b` tier, and ~21% of runs began failing. Almost none of
them were model failures. `watchdog.touch()` is never called while a
NON-streaming LLM call is in flight (`_call_llm`), so the last touch a cron run
records is `session_started` — and a first token that the engine itself allows
600s for (LLM_REQUEST_TIMEOUT_OLLAMA) tripped stall budgets of 120-180s. Every
failure message in `agent_runs` that day read `last activity: session_started`.

The fix is deliberately NOT a keepalive `touch()`. `_OUTPUT_TOUCH_PREFIXES`
means such a touch either disarms the early-stall guard (if named like output)
or resets the idle clock forever (if named like setup); either way a genuinely
wedged provider would then be caught only by the hard ceiling, hours later.

Instead the caller declares "this await is already bounded by someone else, and
here is that bound". Time inside the window is *attributed*, not invented: the
stall clock pauses, the early-stall clock discounts it, the hard ceiling is
untouched, and the window itself trips at budget x 1.25 — so a provider whose
own timeout is silently ignored (observed at llm_client.py:1155-1159, an 1800s
stall) is now caught EARLIER than today, with a reason that names it.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.runner import _StallWatchdog


@pytest.mark.asyncio
async def test_stall_does_not_fire_while_a_bounded_call_is_in_flight():
    """The incident: a slow first token must not read as a stall."""
    wd = _StallWatchdog(stall_timeout=1, hard_timeout=0, tick_seconds=0.05)

    async def victim() -> None:
        wd.touch("session_started")
        wd.begin_wait("llm_inflight:ollama_chat/qwen3.8:27b", 10.0)
        await asyncio.sleep(2.0)  # 2x the stall budget, inside the window
        wd.end_wait()

    task = asyncio.create_task(victim())
    wd.start(task)
    await task  # must complete, not raise CancelledError

    assert wd.was_stall_timeout is False
    assert wd.abort_reason == ""


@pytest.mark.asyncio
async def test_a_wedged_provider_dies_when_it_overruns_its_own_budget():
    """A window buys exactly the bound it declares, plus 25% - never more.

    hard_timeout is deliberately generous (and non-zero): production always
    passes the effective fleet ceiling, and a watchdog with all three budgets
    at 0 does not run at all. The point is that overrun fires FIRST, long
    before the absolute backstop would.
    """
    wd = _StallWatchdog(stall_timeout=0, hard_timeout=100, tick_seconds=0.05)

    async def victim() -> None:
        wd.begin_wait("llm_inflight:ollama_chat/qwen3.8:27b", 0.5)
        await asyncio.sleep(30)  # provider timeout silently ignored

    task = asyncio.create_task(victim())
    wd.start(task)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert wd.was_stall_timeout is True
    assert "ollama_chat/qwen3.8:27b" in wd.abort_reason
    assert "exceeded its own bound" in wd.abort_reason
    assert "0.5" in wd.abort_reason  # the ceiling it blew, named
    assert "hard timeout" not in wd.abort_reason.lower()  # not the backstop


@pytest.mark.asyncio
async def test_the_stall_clock_pauses_rather_than_resets():
    """Attributed time is discounted, not forgiven.

    0.8s idle before the window plus 0.4s after exceeds a 1s stall budget.
    A `touch()`-style reset would restart the clock and never trip.
    """
    wd = _StallWatchdog(stall_timeout=1, hard_timeout=0, tick_seconds=0.05)

    async def victim() -> None:
        wd.touch("session_started")
        await asyncio.sleep(0.8)
        wd.begin_wait("llm_inflight:m", 10.0)
        await asyncio.sleep(1.5)
        wd.end_wait()
        await asyncio.sleep(5.0)  # 0.8 + 0.4 crosses the 1s budget

    task = asyncio.create_task(victim())
    wd.start(task)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert wd.was_stall_timeout is True
    assert "No progress" in wd.abort_reason


@pytest.mark.asyncio
async def test_an_in_flight_call_does_not_disarm_the_early_stall_guard():
    """A call in flight has produced nothing, so it is not output."""
    wd = _StallWatchdog(
        stall_timeout=0, hard_timeout=0, early_stall_timeout=1, tick_seconds=0.05
    )

    async def victim() -> None:
        wd.begin_wait("llm_inflight:m", 0.4)
        await asyncio.sleep(0.4)
        wd.end_wait()
        await asyncio.sleep(30)  # non-attributed time must still trip early-stall

    task = asyncio.create_task(victim())
    wd.start(task)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert wd._saw_output_signal is False
    assert "Early stall" in wd.abort_reason


@pytest.mark.asyncio
async def test_the_hard_ceiling_is_never_suspended_by_a_window():
    """The absolute backstop must not be extendable from inside."""
    wd = _StallWatchdog(stall_timeout=0, hard_timeout=1, tick_seconds=0.05)

    async def victim() -> None:
        wd.begin_wait("llm_inflight:m", 100.0)
        await asyncio.sleep(30)

    task = asyncio.create_task(victim())
    wd.start(task)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "hard timeout" in wd.abort_reason.lower()


@pytest.mark.asyncio
async def test_the_abort_reason_names_both_the_last_progress_and_the_current_wait():
    """Retires `last activity: session_started` as the whole diagnosis."""
    wd = _StallWatchdog(stall_timeout=0, hard_timeout=1, tick_seconds=0.05)

    async def victim() -> None:
        wd.touch("session_started")
        wd.begin_wait("llm_inflight:ollama_chat/qwen3.8:27b", 100.0)
        await asyncio.sleep(30)

    task = asyncio.create_task(victim())
    wd.start(task)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "session_started" in wd.abort_reason
    assert "llm_inflight:ollama_chat/qwen3.8:27b" in wd.abort_reason


@pytest.mark.asyncio
async def test_closing_a_window_twice_is_harmless():
    """`_call_llm`'s except arm has five exits; end_wait must be idempotent."""
    wd = _StallWatchdog(stall_timeout=0, hard_timeout=0, tick_seconds=0.05)
    wd.begin_wait("llm_inflight:m", 1.0)
    wd.end_wait()
    wd.end_wait()
    assert wd.waiting_on == ""


@pytest.mark.asyncio
async def test_waiting_on_reports_the_open_window():
    """The wiring tests observe this from inside a patched acompletion."""
    wd = _StallWatchdog(stall_timeout=0, hard_timeout=0, tick_seconds=0.05)
    assert wd.waiting_on == ""
    wd.begin_wait("llm_inflight:m", 1.0)
    assert wd.waiting_on == "llm_inflight:m"
    wd.end_wait()
    assert wd.waiting_on == ""


@pytest.mark.asyncio
async def test_a_fully_disabled_watchdog_is_not_woken_by_a_wait_window():
    """Opening a window must not activate a watchdog the operator disabled.

    `_defaults.yaml` sets `stall_timeout_seconds: 0` fleet-wide, so this path
    is real. Production still gets overrun protection because the runner always
    passes a positive hard_timeout (the effective fleet ceiling).
    """
    wd = _StallWatchdog(
        stall_timeout=0, hard_timeout=0, early_stall_timeout=0, tick_seconds=0.05
    )

    async def victim() -> None:
        wd.begin_wait("llm_inflight:m", 0.1)
        await asyncio.sleep(0.6)  # far past 0.1 * 1.25
        wd.end_wait()

    task = asyncio.create_task(victim())
    wd.start(task)
    await task  # completes; nothing was watching

    assert wd.was_stall_timeout is False
