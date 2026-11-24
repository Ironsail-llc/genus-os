"""A saved chat exchange is indexed into conversation segments, off the turn.

``save_exchange_async`` is the one door every channel's turn persistence goes
through. Once the rows are committed it schedules segment indexing in the
background, beside the turn embedding. Indexing is best-effort: a failure is
logged and never reaches the turn.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from robothor.engine import chat_store


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def saved(monkeypatch):
    monkeypatch.setattr(chat_store, "save_exchange", lambda *a, **kw: [11, 12])
    monkeypatch.setattr(chat_store, "_embed_turns", AsyncMock())


async def test_a_saved_exchange_schedules_segment_indexing(saved, monkeypatch):
    index = AsyncMock(return_value=1)
    monkeypatch.setattr("robothor.memory.segments.index_conversation", index)

    ids = await chat_store.save_exchange_async(
        "agent:main:primary", "hello", "hi there", channel="webchat", tenant_id="test-tenant"
    )
    await _drain()

    assert ids == [11, 12]
    index.assert_awaited_once()
    args, kwargs = index.call_args
    assert args == ("agent:main:primary",)
    assert kwargs["tenant_id"] == "test-tenant"


async def test_an_indexing_failure_never_fails_the_turn(saved, monkeypatch, caplog):
    index = AsyncMock(side_effect=RuntimeError("segment store unavailable"))
    monkeypatch.setattr("robothor.memory.segments.index_conversation", index)

    with caplog.at_level(logging.WARNING):
        ids = await chat_store.save_exchange_async(
            "agent:main:primary", "hello", "hi there", tenant_id="test-tenant"
        )
        await _drain()

    assert ids == [11, 12]
    index.assert_awaited_once()
    assert any("segment" in r.getMessage().lower() for r in caplog.records)


async def test_a_failed_save_schedules_no_indexing(monkeypatch):
    def boom(*_a, **_kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(chat_store, "save_exchange", boom)
    index = AsyncMock()
    monkeypatch.setattr("robothor.memory.segments.index_conversation", index)

    assert await chat_store.save_exchange_async("agent:main:primary", "a", "b") is None
    await _drain()

    index.assert_not_awaited()
