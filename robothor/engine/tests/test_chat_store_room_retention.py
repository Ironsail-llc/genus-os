"""Thread history survives idle cleanup; ordinary idle sessions are still removed.

``cleanup_stale_sessions`` deletes chat sessions idle past the TTL. A room's
conversation is an ordinary session, but it belongs to a company-owned thread
that people come back to weeks later, so its session must never be swept.
Runs inside the ``db_conn`` transaction the integration fixtures roll back.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from robothor.engine.chat_store import cleanup_stale_sessions
from robothor.rooms.access import room_session_key

pytestmark = pytest.mark.integration


def _session(cur: Any, tenant: str, key: str, idle_days: int) -> int:
    cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel, last_active_at) "
        "VALUES (%s, %s, 'webchat', NOW() - make_interval(days => %s)) RETURNING id",
        (tenant, key, idle_days),
    )
    session_id = int(cur.fetchone()["id"])
    cur.execute(
        'INSERT INTO chat_messages (session_id, message) VALUES (%s, \'{"role": "user"}\')',
        (session_id,),
    )
    return session_id


def _room(cur: Any, tenant: str) -> str:
    room = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by) "
        "VALUES (%s, %s, 'report', 'Weekly', 'main', %s, %s)",
        (tenant, room, room_session_key("main", room), str(uuid.uuid4())),
    )
    return room_session_key("main", room)


def _alive(cur: Any, session_id: int) -> bool:
    cur.execute("SELECT 1 FROM chat_sessions WHERE id = %s", (session_id,))
    return cur.fetchone() is not None


def test_a_rooms_session_survives_and_an_ordinary_one_goes(db_cursor, mock_get_connection) -> None:
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    room_key = _room(db_cursor, tenant)
    room_session = _session(db_cursor, tenant, room_key, idle_days=90)
    ordinary = _session(db_cursor, tenant, f"agent:main:user:{uuid.uuid4()}", idle_days=90)
    fresh = _session(db_cursor, tenant, f"agent:main:user:{uuid.uuid4()}", idle_days=0)
    # Shaped like a room key, but no room backs it: an ordinary idle session.
    orphan = _session(db_cursor, tenant, room_session_key("main", str(uuid.uuid4())), 90)

    deleted = cleanup_stale_sessions(ttl_days=7)

    assert deleted >= 2
    assert _alive(db_cursor, room_session)
    assert _alive(db_cursor, fresh)
    assert not _alive(db_cursor, ordinary)
    assert not _alive(db_cursor, orphan)
    db_cursor.execute(
        "SELECT COUNT(*) AS n FROM chat_messages WHERE session_id = %s", (room_session,)
    )
    assert db_cursor.fetchone()["n"] == 1


def test_a_room_in_another_tenant_does_not_shield_a_session(db_cursor, mock_get_connection) -> None:
    tenant, other = (f"test-tenant-{uuid.uuid4().hex[:8]}" for _ in range(2))
    room_key = _room(db_cursor, other)
    session = _session(db_cursor, tenant, room_key, idle_days=90)

    cleanup_stale_sessions(ttl_days=7)

    assert not _alive(db_cursor, session)
