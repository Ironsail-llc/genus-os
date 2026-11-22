# ruff: noqa: F811 - pytest fixtures are imported and requested by name
"""A workspace turn outlives its requester's stream.

In a shared workspace other members wait on the answer, so the sender closing
the tab or going back to their main conversation (the client disconnecting)
does not cancel the turn: it finishes, its answer is saved once with its
author, the room hears of it, and the room's lock is released as usual. An
explicit stop (``/chat/abort``) still cancels it. The personal main
conversation is unchanged: a disconnect there cancels the turn.

Fixtures (fake rooms, a mock runner, the ASGI client) come from
``test_chat_rooms.py``.
"""

from __future__ import annotations

import asyncio
import gc
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import chat_rooms
from robothor.engine.tests.test_chat_rooms import (  # noqa: F401 - fixtures by name
    BOB,
    TENANT,
    _as,
    _auth,
    _run,
    _send,
    client,
    fake,
    mock_runner,
)


class Gate:
    """A runner that waits to be let go, and says whether it was cancelled."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def __call__(self, **kw: Any) -> Any:
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return _run("the whole answer")


async def _leave(sending: asyncio.Task[Any]) -> None:
    """The client goes away mid-answer."""
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending


async def _until(predicate: Any, seconds: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), seconds)


async def test_a_workspace_turn_finishes_after_its_sender_leaves(client, fake, mock_runner):
    room = fake.add()
    fake.share(room, BOB)
    gate = Gate()
    mock_runner.execute = AsyncMock(side_effect=gate.__call__)

    sending = asyncio.create_task(_send(client, _auth(BOB), room_id=room.id, message="how?"))
    await asyncio.wait_for(gate.entered.wait(), 2)
    await _leave(sending)
    gc.collect()  # the detached turn is held, not collected

    gate.release.set()
    await _until(lambda: fake.store.get((TENANT, room.session_key)))

    assert not gate.cancelled
    rows = fake.store[(TENANT, room.session_key)]
    assert [r["role"] for r in rows] == ["user", "assistant"]  # saved once
    assert rows[0]["content"] == "how?" and rows[0]["author_user_id"] == BOB
    assert rows[1]["content"] == "the whole answer"
    await _until(lambda: any(a[1] == "room.message" for a, _ in fake.published))
    await _until(lambda: chat_rooms.busy(chat_rooms.ChatTarget(room.session_key, room)) is None)


async def test_a_workspace_turn_that_fails_after_its_sender_left_frees_the_room(
    client, fake, mock_runner
):
    room = fake.add()
    fake.share(room, BOB)
    gate = Gate()

    async def fail(**kw: Any) -> Any:
        await gate(**kw)
        raise RuntimeError("the model provider is down")

    mock_runner.execute = AsyncMock(side_effect=fail)
    sending = asyncio.create_task(_send(client, _auth(BOB), room_id=room.id))
    await asyncio.wait_for(gate.entered.wait(), 2)
    await _leave(sending)

    with (
        patch.object(chat_rooms.logger, "error", MagicMock()),
        patch("robothor.engine.chat.logger") as chat_logger,
    ):
        gate.release.set()
        target = chat_rooms.ChatTarget(room.session_key, room)
        await _until(lambda: chat_rooms.busy(target) is None)
        await _until(lambda: chat_logger.error.called)


async def test_an_explicit_stop_still_cancels_a_workspace_turn(client, fake, mock_runner):
    from robothor.engine.runtime.current import active_context

    room = fake.add()
    fake.share(room, BOB)
    gate = Gate()

    async def run(**kw: Any) -> Any:
        from types import SimpleNamespace

        active_context.set(SimpleNamespace(tenant_id=TENANT, principal_id=BOB, request_id="r"))
        return await gate(**kw)

    mock_runner.execute = AsyncMock(side_effect=run)
    sending = asyncio.create_task(_send(client, _auth(BOB), room_id=room.id))
    await asyncio.wait_for(gate.entered.wait(), 2)
    with _as(_auth(BOB)), patch("robothor.engine.runtime.controls.issue_request", MagicMock()):
        await client.post("/chat/abort", json={"room_id": room.id})
    await asyncio.wait_for(sending, 2)

    assert gate.cancelled


async def test_the_main_conversation_turn_still_stops_when_its_sender_leaves(
    client, fake, mock_runner
):
    gate = Gate()
    mock_runner.execute = AsyncMock(side_effect=gate.__call__)

    sending = asyncio.create_task(_send(client, _auth(BOB), message="hello"))
    await asyncio.wait_for(gate.entered.wait(), 2)
    await _leave(sending)

    await _until(lambda: gate.cancelled)
