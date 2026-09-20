"""Bridge shutdown cancels its recovery loop even when the lifespan raises."""

import asyncio

import bridge_service
import pytest

from robothor.autonomy import handoff_worker


async def test_bridge_owns_and_cancels_recovery_task(monkeypatch):
    started, stopped = asyncio.Event(), asyncio.Event()

    async def recovery():
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(handoff_worker, "recover_checks", recovery)
    with pytest.raises(RuntimeError, match="fixture shutdown"):
        async with bridge_service.lifespan(bridge_service.app):
            await asyncio.wait_for(started.wait(), 1)
            raise RuntimeError("fixture shutdown")
    assert stopped.is_set()
    assert bridge_service.http_client.is_closed
