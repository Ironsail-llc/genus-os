"""What the engine REALLY answers a workspace's chat and period requests,
recorded for the dashboard's contract test.

Real Postgres, the real chat routes and the real periods route; only the LLM
(the runner's ``execute``) and the room's background run are stand-ins. The
recording is ``app/src/lib/rooms/__tests__/contract/engine-replies.json``;
``check_golden`` fails here when a reply changes, and
``app/src/lib/rooms/__tests__/engine-contract.test.ts`` feeds it to the
dashboard's chat routes and parsers.

What the dashboard relies on, each asserted here too: a room send streams
``event: room`` ``{roomId}`` first; history echoes ``roomId`` and carries
``authorUserId`` / ``authorName`` on user turns and never a session key; a
busy room is 409 ``{error: "room_busy", answering}``; no access is 404 and a
viewer's send 403, as JSON.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
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

#: The dashboard's outbox id for the recorded send (``client_id``).
CLIENT_ID = "5d5d5d5d-0000-4000-8000-000000000005"


def _run(text: str) -> AgentRun:
    return AgentRun(status=RunStatus.COMPLETED, output_text=text, trigger_type=TriggerType.WEBCHAT)


@pytest.fixture
async def engine(rooms_db, engine_config, isolated_plan_claims, monkeypatch):  # noqa: F811
    from fastapi import FastAPI

    from robothor.engine import rooms_api
    from robothor.rooms import periods

    db = rooms_db
    db.vera = "0f0f0f0f-1111-4222-8333-444444444444"
    names = {db.alice: "Alice", db.bob: "Bob", db.carol: "Carol", db.vera: "Vera"}
    monkeypatch.setattr("robothor.engine.chat_store.get_connection", committed_connection)
    monkeypatch.setattr("robothor.engine.chat_store._embed_turns", AsyncMock())
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    monkeypatch.setattr(
        "robothor.engine.chat._resolve_webchat_identity",
        lambda auth: SimpleNamespace(display_name=names.get(auth.user_id, "")),
    )
    # The room's own run (lane 7) is a stand-in that does nothing: the route's
    # answer is what is recorded, not the run.
    started: list[Any] = []

    async def run_room(room: Any, edition: Any, **kw: Any) -> None:
        started.append(edition.id)

    monkeypatch.setattr(periods, "resolve_run_room", lambda: run_room)
    runner = MagicMock()
    runner.config = engine_config
    _sessions.clear()
    app = FastAPI()
    with patch("robothor.engine.chat.load_all_sessions", return_value={}):
        init_chat(runner, engine_config)
    app.include_router(router)
    rooms_api.register(app)
    report = dal.create_room(
        db.tenant,
        kind="report",
        title="Daily sales",
        agent_id="main",
        created_by=db.alice,
        settings={"tz": "UTC", "default_grain": "week"},
    )
    for user in (db.bob, db.vera):
        dal.add_member(db.tenant, report.id, user, added_by=db.alice)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(db=db, room=report, runner=runner, client=client, started=started)
    _sessions.clear()


def _as(db: Any, user: str, role: str = "member") -> Any:
    auth = AuthContext(user_id=user, tenant_id=db.tenant, role=role, typ="user")
    return patch("robothor.engine.chat._auth_context", return_value=auth)


def _human(db: Any, user: str, role: str = "member") -> Any:
    auth = AuthContext(user_id=user, tenant_id=db.tenant, role=role, typ="user")
    return patch("robothor.engine.rooms_api.request_context", return_value=auth)


def _record(response: Any) -> dict[str, Any]:
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text
    return {"status": response.status_code, "body": body}


def _email_receipt(s: Any) -> None:
    """A sent email's receipt turn, written as the engine writes one
    (``robothor.rooms.outbound``: its receipt, its words, as message extras)."""
    from robothor.engine import chat_store
    from robothor.rooms import outbound

    row = {
        "id": "9f9f9f9f-0000-4000-8000-000000000001",
        "status": "completed",
        "receipt": {"sent_at": "2026-11-21T10:00:02+00:00", "id": "msg-1"},
        "expires_at": datetime(2999, 1, 1, tzinfo=UTC),
        "decided_by": f"user:{s.db.bob}",
        "decided_at": datetime(2026, 11, 21, 10, 0, tzinfo=UTC),
        "payload": {
            "sender": "assistant@example.com",
            "reply_to": "bob@example.com",
            "recipient": ["carol.client@example.org"],
            "cc": [],
            "subject": "Checking in",
            "room_id": s.room.id,
            "task_id": None,
        },
    }
    receipt = outbound._receipt(s.db.tenant, row, None)
    assert receipt is not None
    chat_store.save_message(
        s.room.session_key,
        "assistant",
        outbound._text(receipt),
        channel="webchat",
        tenant_id=s.db.tenant,
        extras={"kind": "room_email_receipt", "receipt": receipt},
    )


async def _chat(s: Any, replies: dict[str, Any]) -> None:
    rid = s.room.id
    s.runner.execute = AsyncMock(return_value=_run("Sales were 1,200."))
    with _as(s.db, s.db.alice):
        sent = await s.client.post(
            "/chat/send",
            json={"room_id": rid, "message": "how did we do?", "client_id": CLIENT_ID},
        )
    assert sent.status_code == 200
    assert sent.text.startswith("event: room\n")
    replies["send_stream"] = {"status": sent.status_code, "body": sent.text}

    _email_receipt(s)

    with _as(s.db, s.db.bob):
        history = await s.client.get(f"/chat/history?room_id={rid}")
    replies["history"] = _record(history)
    page = history.json()
    assert page["roomId"] == rid and "sessionKey" not in page
    assert page["messages"][0]["authorName"] == "Alice"
    assert page["messages"][0]["clientId"] == CLIENT_ID
    receipt = page["messages"][-1]
    assert receipt["kind"] == "room_email_receipt" and receipt["receipt"]["status"] == "sent"

    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs: Any) -> AgentRun:
        entered.set()
        await release.wait()
        return _run("answer for Bob")

    s.runner.execute = AsyncMock(side_effect=slow)
    with _as(s.db, s.db.bob):
        first = asyncio.create_task(
            s.client.post("/chat/send", json={"room_id": rid, "message": "and the week?"})
        )
        await asyncio.wait_for(entered.wait(), 5)
    with _as(s.db, s.db.alice):
        busy = await s.client.post("/chat/send", json={"room_id": rid, "message": "me too"})
    release.set()
    await first
    replies["send_busy"] = _record(busy)
    assert busy.json() == {"error": "room_busy", "answering": "Bob"}

    with _as(s.db, s.db.carol):
        replies["history_not_found"] = _record(await s.client.get(f"/chat/history?room_id={rid}"))
        replies["send_not_found"] = _record(
            await s.client.post("/chat/send", json={"room_id": rid, "message": "hi"})
        )
    with _as(s.db, s.db.vera, role="viewer"):
        replies["history_viewer"] = _record(await s.client.get(f"/chat/history?room_id={rid}"))
        replies["send_viewer"] = _record(
            await s.client.post("/chat/send", json={"room_id": rid, "message": "hi"})
        )
    assert replies["send_viewer"]["status"] == 403


async def _periods(s: Any, replies: dict[str, Any]) -> None:
    rid = s.room.id
    path = f"/api/rooms/{rid}/periods"
    built, _ = dal.create_edition(
        s.db.tenant, rid, grain="week", period_start=date(2026, 11, 2), period_end=date(2026, 11, 8)
    )
    dal.publish_edition(
        s.db.tenant,
        built.id,
        title="Week 45",
        summary="Up 4%.",
        blocks=[{"component": "render_markdown", "props": {"content": "Up 4%."}}],
    )
    with _human(s.db, s.db.bob):
        replies["period_existing"] = _record(
            await s.client.post(path, json={"grain": "week", "anchor": "2026-11-04"})
        )
        replies["period_started"] = _record(
            await s.client.post(path, json={"grain": "week", "anchor": "2026-10-28"})
        )
        replies["period_unfinished"] = _record(
            await s.client.post(path, json={"grain": "day", "anchor": "2999-01-01"})
        )
    with _human(s.db, s.db.vera, role="viewer"):
        replies["period_viewer"] = _record(
            await s.client.post(path, json={"grain": "week", "anchor": "2026-10-21"})
        )
    with _human(s.db, s.db.carol):
        replies["period_not_found"] = _record(
            await s.client.post(path, json={"grain": "week", "anchor": "2026-10-21"})
        )
    # Nothing is capped: one person asking for many periods in a row starts every run.
    weeks = ("2026-10-14", "2026-10-07", "2026-09-30", "2026-09-23", "2026-09-16", "2026-09-09")
    with _human(s.db, s.db.bob):
        many = [
            _record(await s.client.post(path, json={"grain": "week", "anchor": week}))
            for week in weeks
        ]
    replies["period_many"] = {"statuses": [r["status"] for r in many]}
    assert [replies[k]["status"] for k in ("period_existing", "period_started")] == [200, 202]
    assert replies["period_many"]["statuses"] == [202] * 6, many


async def test_recorded_replies_match_the_dashboard_contract(engine) -> None:
    from robothor.rooms.tests.contract import Normaliser, check_golden

    s = engine
    replies: dict[str, Any] = {}
    await _chat(s, replies)
    await _periods(s, replies)
    names = {s.db.alice: "u-alice", s.db.bob: "u-bob", s.db.carol: "u-carol", s.db.vera: "u-vera"}
    names[s.room.id] = "room-report"
    names[CLIENT_ID] = CLIENT_ID  # a fixed value already: kept as the dashboard sent it
    # The refusal of an unfinished period names today's date.
    unfinished = replies["period_unfinished"]["body"]
    unfinished["message"] = unfinished["message"].replace(
        datetime.now(UTC).date().isoformat(), "<today>"
    )
    recorded = Normaliser(names)(replies)
    check_golden("engine-replies.json", recorded)
    assert ":room:" not in str(recorded)
