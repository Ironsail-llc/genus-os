"""store_memory refuses an empty write and says which argument it needed.

Before: a call without ``content`` (an agent that sent ``text``, say) stored a
blank fact and answered ``facts_stored: 1``. The agent was told the write had
succeeded, the step counted as a success, and memory gained an empty row.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.tools.dispatch import ToolContext


async def _call(args: dict[str, Any]) -> dict[str, Any]:
    from robothor.engine.tools.handlers.memory import HANDLERS

    return await HANDLERS["store_memory"](
        args, ToolContext(agent_id="example-agent", tenant_id="test-tenant")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("async_write", ["0", "1"])
async def test_missing_content_is_an_actionable_error(monkeypatch, async_write):
    monkeypatch.setenv("MEMORY_ASYNC_WRITE", async_write)
    store = AsyncMock()
    enqueue = AsyncMock()
    with (
        patch("robothor.memory.facts.extract_facts", new=AsyncMock(return_value=[])),
        patch("robothor.memory.facts.store_facts_batch", new=store),
        patch("robothor.memory.write_jobs.enqueue_write", new=enqueue),
    ):
        out = await _call({"text": "Alice prefers email.", "content_type": "preference"})

    assert "content" in out["error"]
    assert "received: content_type, text" in out["error"]
    store.assert_not_awaited()
    enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_blank_content_is_an_error(monkeypatch):
    monkeypatch.delenv("MEMORY_ASYNC_WRITE", raising=False)
    store = AsyncMock()
    with (
        patch("robothor.memory.facts.extract_facts", new=AsyncMock(return_value=[])),
        patch("robothor.memory.facts.store_facts_batch", new=store),
    ):
        out = await _call({"content": "   "})
    assert "error" in out
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_shared_implementation_refuses_blank_content_too():
    """robothor/api/mcp.py calls store_memory_content directly."""
    from robothor.engine.tools.handlers.memory import store_memory_content

    store = AsyncMock()
    with (
        patch("robothor.memory.facts.extract_facts", new=AsyncMock(return_value=[])),
        patch("robothor.memory.facts.store_facts_batch", new=store),
    ):
        out = await store_memory_content("")
    assert "error" in out
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_content_is_still_stored(monkeypatch):
    monkeypatch.delenv("MEMORY_ASYNC_WRITE", raising=False)
    with (
        patch("robothor.memory.facts.extract_facts", new=AsyncMock(return_value=[])),
        patch("robothor.memory.facts.store_facts_batch", new=AsyncMock(return_value=[41])),
    ):
        out = await _call({"content": "Alice prefers email.", "content_type": "preference"})
    assert out == {"id": 41, "facts_stored": 1}
