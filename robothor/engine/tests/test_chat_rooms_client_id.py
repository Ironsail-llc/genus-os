"""A workspace send's ``client_id``: the dashboard's outbox id, echoed back.

The dashboard reconciles the bubble it drew with the stored message by the
UUID it sent as ``client_id``. In a workspace send the engine stores it on the
user turn and ``/chat/history`` returns it as ``clientId`` on that row; it is
never trusted for anything but that echo. A malformed one is refused before
anything is admitted. An ordinary session neither stores nor returns one.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from robothor.engine.tests.test_chat_rooms_pg import _as, _run, setup  # noqa: F401
from robothor.rooms.tests.conftest import rooms_db  # noqa: F401

pytestmark = pytest.mark.integration


async def _history(s: Any, query: str) -> list[dict[str, Any]]:
    with _as(s.db, s.db.alice):
        page = (await s.client.get(f"/chat/history?{query}")).json()
    return list(page["messages"])


async def test_a_workspace_send_echoes_its_client_id_on_its_own_row(setup) -> None:  # noqa: F811
    s = setup
    s.runner.execute = AsyncMock(return_value=_run("ok"))
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    with _as(s.db, s.db.alice):
        for cid in (first, second):
            sent = await s.client.post(
                "/chat/send", json={"room_id": s.room.id, "message": "same text", "client_id": cid}
            )
            assert sent.status_code == 200

    messages = await _history(s, f"room_id={s.room.id}")
    users = [m for m in messages if m["role"] == "user"]
    assert [m["clientId"] for m in users] == [first, second]
    assert all("clientId" not in m for m in messages if m["role"] == "assistant")


@pytest.mark.parametrize("bad", ["not-a-uuid", 42, "", {"id": "x"}])
async def test_a_malformed_client_id_is_refused_and_nothing_is_admitted(setup, bad) -> None:  # noqa: F811
    s = setup
    s.runner.execute = AsyncMock(return_value=_run("never"))
    with _as(s.db, s.db.alice):
        sent = await s.client.post(
            "/chat/send", json={"room_id": s.room.id, "message": "hi", "client_id": bad}
        )
    assert sent.status_code == 400
    assert sent.json()["error"] == "invalid_client_id"
    s.runner.execute.assert_not_awaited()


async def test_an_ordinary_session_neither_stores_nor_returns_one(setup) -> None:  # noqa: F811
    s = setup
    s.runner.execute = AsyncMock(return_value=_run("ok"))
    with _as(s.db, s.db.alice):
        sent = await s.client.post(
            "/chat/send", json={"message": "main hello", "client_id": str(uuid.uuid4())}
        )
    assert sent.status_code == 200
    messages = await _history(s, "limit=50")
    assert messages and all("clientId" not in m and "client_id" not in m for m in messages)
