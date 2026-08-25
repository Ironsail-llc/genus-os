"""An env-gated tick trace for the wall-clock enforcement stack.

Twice now (2026-08-25) a run outlived every timeout layer — 3110s against a
1200s ceiling, then 1470s+ against another — and both times the container
was gone before anyone could ask it what happened. The probes that tried to
reproduce it enforced perfectly at 30s, 600s and 1200s, so whatever wedges
the layers is state-dependent and will only ever be caught by a trace that
was already running.

``ROBOTHOR_WATCHDOG_TRACE_FILE`` names a file; when set, the watchdog
appends one line at start, each tick, each trip/cancel, and stop. Unset —
the production default — nothing changes. The write path swallows its own
errors: tracing must never be able to kill the thing it watches.
"""

from __future__ import annotations

import asyncio

import pytest

from robothor.engine.stall_watchdog import _StallWatchdog


class TestWatchdogTrace:
    @pytest.mark.asyncio
    async def test_ticks_and_breach_are_traced(self, tmp_path, monkeypatch):
        trace = tmp_path / "wd.log"
        monkeypatch.setenv("ROBOTHOR_WATCHDOG_TRACE_FILE", str(trace))

        async def victim():
            await asyncio.sleep(30)

        task = asyncio.ensure_future(victim())
        wd = _StallWatchdog(stall_timeout=0, hard_timeout=1, tick_seconds=0.2)
        wd.start(task)
        with pytest.raises(asyncio.CancelledError):
            await task
        body = trace.read_text(encoding="utf-8")
        assert "watch_start hard=1" in body
        assert "tick elapsed=" in body
        assert "CANCEL" in body

    @pytest.mark.asyncio
    async def test_unset_writes_nothing_and_changes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_WATCHDOG_TRACE_FILE", raising=False)

        async def victim():
            await asyncio.sleep(30)

        task = asyncio.ensure_future(victim())
        wd = _StallWatchdog(stall_timeout=0, hard_timeout=1, tick_seconds=0.2)
        wd.start(task)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list(tmp_path.iterdir()) == []

    def test_trip_is_traced(self, tmp_path, monkeypatch):
        trace = tmp_path / "wd.log"
        monkeypatch.setenv("ROBOTHOR_WATCHDOG_TRACE_FILE", str(trace))
        wd = _StallWatchdog(stall_timeout=0, hard_timeout=5)
        wd.trip("loop self-check")
        assert "TRIP loop self-check" in trace.read_text(encoding="utf-8")

    def test_a_broken_trace_path_never_raises(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_WATCHDOG_TRACE_FILE", "/nonexistent/dir/wd.log")
        wd = _StallWatchdog(stall_timeout=0, hard_timeout=5)
        wd.trip("must not raise")
