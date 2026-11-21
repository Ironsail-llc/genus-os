"""Story I6: a room's history pages stay within budget, through the real route.

A room with 1,000 stored messages, read by Bob (a shared member) over the
engine's ``GET /chat/history?room_id=...&limit=50&before=<id>`` with the room
access check, the chat store and the room's attribution all real against
Postgres. Paging back from the newest page returns every message exactly
once and in order, each page well under a second, and every page costs the
same number of SQL statements (the access check and one page read), however
deep into the history it is.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from psycopg2.extras import Json, execute_values

from robothor.auth.deps import AuthContext
from robothor.engine.chat import _sessions, init_chat, router
from robothor.rooms import dal
from robothor.rooms.tests.conftest import committed_connection, rooms_db  # noqa: F401
from robothor.rooms.tests.perf import counting_connection

pytestmark = pytest.mark.integration

MESSAGES = 1_000
PAGE = 50
PAGE_BUDGET_S = 1.0


@pytest.fixture
async def setup(rooms_db, engine_config, isolated_plan_claims, monkeypatch):  # noqa: F811
    from fastapi import FastAPI

    monkeypatch.setattr("robothor.engine.chat_store.get_connection", committed_connection)
    monkeypatch.setattr("robothor.engine.chat_store._embed_turns", AsyncMock())
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
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
    ids = _store_history(rooms_db, room, MESSAGES)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(db=rooms_db, room=room, client=client, ids=ids)
    _sessions.clear()


def _store_history(db: Any, room: Any, n: int) -> list[int]:
    """``n`` alternating turns in the room's session, as the chat store keeps
    them (Alice's turns attributed); their ids, oldest first."""
    db.cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel) VALUES (%s, %s, 'webchat') "
        "RETURNING id",
        (room.tenant_id, room.session_key),
    )
    session_id = db.cur.fetchone()[0]
    rows = []
    for i in range(n):
        if i % 2 == 0:
            message = {
                "role": "user",
                "content": f"turn {i}",
                "author_user_id": db.alice,
                "author_display_name": "Alice",
            }
        else:
            message = {"role": "assistant", "content": f"turn {i}"}
        rows.append((session_id, Json(message)))
    inserted = execute_values(
        db.cur,
        "INSERT INTO chat_messages (session_id, message) VALUES %s RETURNING id",
        rows,
        fetch=True,
        page_size=n,
    )
    return sorted(int(r[0]) for r in inserted)


def _as(db: Any, user: str) -> Any:
    auth = AuthContext(user_id=user, tenant_id=db.tenant, role="member", typ="user")
    return patch("robothor.engine.chat._auth_context", return_value=auth)


async def _page(s: Any, monkeypatch: Any, before: int | None) -> tuple[dict[str, Any], int, float]:
    """One history page as Bob: the body, its statement count and its seconds."""
    log: list[str] = []
    url = f"/chat/history?room_id={s.room.id}&limit={PAGE}"
    if before is not None:
        url += f"&before={before}"
    with monkeypatch.context() as patched, _as(s.db, s.db.bob):
        counting = counting_connection(log)
        patched.setattr("robothor.rooms.dal.get_connection", counting)
        patched.setattr("robothor.engine.chat_store.get_connection", counting)
        started = time.perf_counter()
        response = await s.client.get(url)
        elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    return response.json(), len(log), elapsed


async def test_paging_back_through_1000_messages_is_complete_ordered_and_bounded(
    setup, monkeypatch
) -> None:
    s = setup
    pages: list[list[dict[str, Any]]] = []
    statements: list[int] = []
    timings: list[float] = []
    before: int | None = None
    while True:
        body, count, elapsed = await _page(s, monkeypatch, before)
        messages = body["messages"]
        assert messages, f"empty page after {len(pages)} pages"
        pages.append(messages)
        statements.append(count)
        timings.append(elapsed)
        if not body["hasMore"]:
            break
        assert len(pages) <= MESSAGES // PAGE, "hasMore never turned false"
        before = messages[0]["id"]  # each page is oldest first

    print(
        f"I6 room history: {len(pages)} pages, statements per page {sorted(set(statements))}, "
        f"slowest {max(timings) * 1000:.1f} ms, mean {sum(timings) / len(timings) * 1000:.1f} ms"
    )
    assert len(pages) == MESSAGES // PAGE
    assert all(len(p) == PAGE for p in pages)
    every = [m["id"] for p in reversed(pages) for m in p]
    assert every == s.ids  # each message once, oldest to newest
    for page in pages:
        ids = [m["id"] for m in page]
        assert ids == sorted(ids)
    first = pages[-1][0]  # the oldest message: Alice's, attributed
    assert first["content"] == "turn 0"
    assert first["authorUserId"] == s.db.alice and first["authorName"] == "Alice"
    assert "sessionKey" not in pages and all("timestamp" in m for p in pages for m in p)
    assert len(set(statements)) == 1, statements  # page 20 costs what page 1 does
    assert statements[0] == 3  # room, Bob's membership, the page
    assert max(timings) < PAGE_BUDGET_S, timings


async def test_a_page_before_the_oldest_message_is_empty(setup, monkeypatch) -> None:
    s = setup
    body, count, elapsed = await _page(s, monkeypatch, s.ids[0])

    assert body["messages"] == [] and body["hasMore"] is False
    assert count == 3
    assert elapsed < PAGE_BUDGET_S
