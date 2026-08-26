"""The live checklist never rendered, and nothing said so.

Both `_retry_on_flood` call sites in the todo_updated handler passed a
pre-built coroutine where the function's own docstring requires a zero-arg
factory ("Must be a factory (not a pre-built coroutine) since you can't await
twice"). That raises TypeError before any HTTP request is made — and the whole
block is wrapped in `except Exception: logger.debug(...)`, so the feature was
dead and the only evidence was a DEBUG line nobody reads.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.telegram import TelegramBot

TODOS = [{"content": "do a thing", "status": "completed"}]


def _bot() -> TelegramBot:
    b = TelegramBot.__new__(TelegramBot)
    b.bot = MagicMock()
    b.bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
    b.bot.edit_message_text = AsyncMock(return_value=MagicMock(message_id=77))
    return b


@pytest.mark.asyncio
async def test_the_first_checklist_is_actually_sent():
    b = _bot()

    msg_id = await b._send_or_edit_checklist(chat_id="123", todos=TODOS, message_id=None)

    assert b.bot.send_message.await_count == 1, "the checklist send never executed"
    assert msg_id == 77


@pytest.mark.asyncio
async def test_a_subsequent_checklist_edits_in_place():
    b = _bot()

    msg_id = await b._send_or_edit_checklist(chat_id="123", todos=TODOS, message_id=77)

    assert b.bot.edit_message_text.await_count == 1
    assert b.bot.send_message.await_count == 0
    assert msg_id == 77


@pytest.mark.asyncio
async def test_retry_on_flood_rejects_a_prebuilt_coroutine_loudly():
    """The misuse that caused this must not be a generic TypeError again."""
    b = _bot()

    async def work():
        return 1

    coro = work()
    try:
        with pytest.raises(TypeError, match="factory"):
            await b._retry_on_flood(coro)
    finally:
        coro.close()


@pytest.mark.asyncio
async def test_retry_on_flood_still_accepts_a_factory():
    b = _bot()
    calls = []

    async def work():
        calls.append(1)
        return "ok"

    assert await b._retry_on_flood(lambda: work()) == "ok"
    assert calls == [1]
