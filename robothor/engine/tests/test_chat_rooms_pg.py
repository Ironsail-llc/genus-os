"""Chat on a room's session against real Postgres: the rooms data layer, the
chat store and the routes together.

D1 + D2 end to end: Alice speaks in a room, the engine restarts (nothing in
RAM), Bob opens the room and sees Alice's turn attributed, then continues the
conversation and the agent hears both of them by name. The room's activity
mark follows the newest saved message. A stranger is refused.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.auth.deps import AuthContext
from robothor.engine.chat import _sessions, init_chat, router
from robothor.engine.models import AgentRun, RunStatus, TriggerType
from robothor.rooms import dal
from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401

pytestmark = pytest.mark.integration


def _run(text: str) -> AgentRun:
    return AgentRun(status=RunStatus.COMPLETED, output_text=text, trigger_type=TriggerType.WEBCHAT)


@pytest.fixture
async def setup(rooms_db, engine_config, isolated_plan_claims, monkeypatch):  # noqa: F811
    from fastapi import FastAPI

    names = {rooms_db.alice: "Alice", rooms_db.bob: "Bob", rooms_db.carol: "Carol"}
    monkeypatch.setattr("robothor.engine.chat_store.get_connection", committed_connection)
    monkeypatch.setattr("robothor.engine.chat_store._embed_turns", AsyncMock())
    published: list[Any] = []
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: published.append(a))
    monkeypatch.setattr(
        "robothor.engine.chat._resolve_webchat_identity",
        lambda auth: SimpleNamespace(display_name=names.get(auth.user_id, "")),
    )
    runner = MagicMock()
    runner.config = engine_config
    _sessions.clear()
    app = FastAPI()
    with patch("robothor.engine.chat.load_all_sessions", return_value={}):
        init_chat(runner, engine_config)
    app.include_router(router)
    room = dal.create_room(
        rooms_db.tenant, kind="agent", title="Pricing", agent_id="main", created_by=rooms_db.alice
    )
    dal.add_member(rooms_db.tenant, room.id, rooms_db.bob, added_by=rooms_db.alice)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(
            db=rooms_db, room=room, runner=runner, client=client, published=published
        )
    _sessions.clear()


def _as(db: Any, user: str) -> Any:
    auth = AuthContext(user_id=user, tenant_id=db.tenant, role="member", typ="user")
    return patch("robothor.engine.chat._auth_context", return_value=auth)


async def test_a_conversation_two_people_share_survives_a_restart(setup) -> None:
    s = setup
    s.runner.execute = AsyncMock(return_value=_run("Forty is below cost."))
    with _as(s.db, s.db.alice):
        sent = await s.client.post(
            "/chat/send", json={"room_id": s.room.id, "message": "price it at 40"}
        )
    assert sent.status_code == 200

    _sessions.clear()  # a restart: the room's session is not in RAM
    with _as(s.db, s.db.bob):
        page = (await s.client.get(f"/chat/history?room_id={s.room.id}")).json()

    question, answer = page["messages"]
    assert question["authorUserId"] == s.db.alice
    assert question["authorName"] == "Alice"
    assert question["content"] == "price it at 40"
    assert "authorUserId" not in answer
    assert "sessionKey" not in page

    bumped = dal.get_room(s.db.tenant, s.room.id)
    assert bumped.last_message_id == answer["id"]
    assert bumped.last_activity_at is not None
    assert s.published[-1][:2] == ("agent", "room.message")

    s.runner.execute = AsyncMock(return_value=_run("45 then."))
    with _as(s.db, s.db.bob):
        await s.client.post("/chat/send", json={"room_id": s.room.id, "message": "and 45?"})
    kwargs = s.runner.execute.await_args.kwargs
    assert kwargs["message"] == "[Bob] and 45?"
    assert [m["content"] for m in kwargs["conversation_history"]] == [
        "[Alice] price it at 40",
        "Forty is below cost.",
    ]


async def test_a_stranger_cannot_read_or_write_the_room(setup) -> None:
    s = setup
    s.runner.execute = AsyncMock(return_value=_run("never"))
    with _as(s.db, s.db.carol):
        read = await s.client.get(f"/chat/history?room_id={s.room.id}")
        sent = await s.client.post("/chat/send", json={"room_id": s.room.id, "message": "hi"})
    with _as(s.db, str(uuid.uuid4())):
        other = await s.client.get(f"/chat/history?room_id={s.room.id}")

    assert read.status_code == sent.status_code == other.status_code == 404
    s.runner.execute.assert_not_awaited()
