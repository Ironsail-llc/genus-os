"""Pending profile lookup remains visible without repeating or delaying its work."""

import asyncio
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robothor.engine.runtime import profile_lookup, profile_progress
from robothor.engine.runtime.contracts import ExecutionContext, RunRequest


@pytest.mark.parametrize("delivery_fails", [False, True])
async def test_pending_lookup_reports_progress_and_stops_when_read_finishes(
    monkeypatch, delivery_fails
):
    release, finished = Event(), Event()
    two_updates = asyncio.Event()
    events = []
    profile = SimpleNamespace(difficulty_class="simple")

    def read(*args):
        try:
            assert release.wait(3), "fixture thread must be released"
            return profile, ""
        finally:
            finished.set()

    async def status(event):
        events.append(event)
        if len(events) == 2:
            two_updates.set()
        if delivery_fails:
            raise OSError("synthetic delivery loss")

    loader = Mock(side_effect=read)
    monkeypatch.setattr("robothor.engine.runner.load_agent_config_or_reason", loader)
    monkeypatch.setattr(profile_progress, "INTERVAL_SECONDS", 0.01)
    request = RunRequest(
        ExecutionContext("tenant", "operator", "request"),
        "main",
        "Do the work",
        {"on_status": status},
    )
    task = asyncio.create_task(
        profile_lookup.lookup(request, "fixture", datetime.now(UTC) - timedelta(seconds=2))
    )
    try:
        await asyncio.wait_for(two_updates.wait(), 1)
        assert not task.done()
        assert all(
            event["phase"] == "preparing" and event["request_id"] == "request" for event in events
        )
        assert all(
            event["elapsed_s"] >= 2 and event["tool_calls_completed"] == 0 for event in events
        )
        release.set()
        assert await asyncio.wait_for(task, 1) == (profile, "")
        count = len(events)
        await asyncio.sleep(0.03)
        assert len(events) == count
        loader.assert_called_once_with("main", "fixture")
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.gather(task, return_exceptions=True)
