"""Slack's ``/clear`` empties the stored history too, not only the cached one.

Slack conversations persist their exchanges through the chat store, and the
store is what a restarted engine restores sessions from. A ``/clear`` that
emptied only the in-memory session would hand the cleared history back after
the next restart — the operator would have been told "Session cleared." and the
conversation would remember anyway.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine.slack import SlackBot

TENANT = "test-tenant"


class _Store:
    def __init__(self) -> None:
        self.cleared: list[dict[str, Any]] = []

    async def clear_session_async(self, session_key: str, tenant_id: str = "") -> None:
        self.cleared.append({"session_key": session_key, "tenant_id": tenant_id})


@pytest.fixture
def store(monkeypatch) -> _Store:
    from robothor.engine import chat_store

    fake = _Store()
    monkeypatch.setattr(chat_store, "clear_session_async", fake.clear_session_async)
    return fake


@pytest.fixture
def session(monkeypatch) -> Any:
    from robothor.engine import chat
    from robothor.engine.chat_history import ChatHistory

    held = SimpleNamespace(history=ChatHistory([{"role": "user", "content": "hi"}]))
    monkeypatch.setattr(chat, "get_shared_session", lambda key: held)
    return held


def _bot() -> SlackBot:
    return SlackBot(SimpleNamespace(), SimpleNamespace(tenant_id=TENANT))


@pytest.mark.asyncio
async def test_clear_empties_the_stored_history_so_a_restart_does_not_restore_it(store, session):
    await _bot()._clear_session("C123")
    assert list(session.history) == []
    assert store.cleared == [{"session_key": "agent:main:slack:C123", "tenant_id": TENANT}]


@pytest.mark.asyncio
async def test_clear_waits_for_a_run_in_flight_on_the_same_conversation(store, session):
    """Otherwise the run finishing after the clear writes its turn back into
    the history the operator just emptied."""
    from robothor.engine.channels import inbound

    release = asyncio.Event()

    async def _hold() -> None:
        async with inbound.conversation_turn("agent:main:slack:C123"):
            await release.wait()

    holder = asyncio.create_task(_hold())
    await asyncio.sleep(0)
    clearing = asyncio.create_task(_bot()._clear_session("C123"))
    for _ in range(50):
        await asyncio.sleep(0)
    assert store.cleared == [], "cleared while a run on the conversation was in flight"

    release.set()
    await asyncio.wait_for(asyncio.gather(holder, clearing), 2.0)
    assert len(store.cleared) == 1


def test_the_clear_command_goes_through_the_clearing_method():
    from robothor.engine.tests.astcheck import called_names, function_def

    start = function_def("robothor.engine.slack", "start")
    assert "_clear_session" in called_names(start)
