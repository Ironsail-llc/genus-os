# ruff: noqa: F811 - pytest fixtures are imported and requested by name
"""The data-layer pieces the room tools add: pause, run-as, recent messages.

Small and additive on top of lane 1's ``dal``: pausing is not part of a
room's definition (it writes no revision), the run-as user changes only by
reassignment, and a room's last messages are read for the main agent's
``room_read``.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from robothor.rooms import dal
from robothor.rooms.errors import RoomNotFound
from robothor.rooms.tests.conftest import rooms_db  # noqa: F401

pytestmark = pytest.mark.integration


def _room(db: Any) -> Any:
    return dal.create_room(
        db.tenant, kind="report", title="Weekly numbers", agent_id="main", created_by=db.alice
    )


def _say(db: Any, room: Any, message: dict[str, Any]) -> int:
    db.cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel) VALUES (%s, %s, 'webchat') "
        "ON CONFLICT (tenant_id, session_key) DO UPDATE SET channel = EXCLUDED.channel "
        "RETURNING id",
        (room.tenant_id, room.session_key),
    )
    session_id = db.cur.fetchone()[0]
    db.cur.execute(
        "INSERT INTO chat_messages (session_id, message) VALUES (%s, %s::jsonb) RETURNING id",
        (session_id, json.dumps(message)),
    )
    return int(db.cur.fetchone()[0])


def test_set_paused_writes_no_revision(rooms_db):
    db = rooms_db
    room = _room(db)

    paused = dal.set_paused(db.tenant, room.id, True)

    assert paused.paused is True
    assert paused.rev == room.rev
    assert [r.rev for r in dal.list_revisions(db.tenant, room.id)] == [1]
    assert dal.set_paused(db.tenant, room.id, False).paused is False


def test_set_paused_on_another_tenants_room_is_not_found(rooms_db):
    db = rooms_db
    room = _room(db)
    with pytest.raises(RoomNotFound):
        dal.set_paused(db.other, room.id, True)
    with pytest.raises(RoomNotFound):
        dal.set_paused(db.tenant, str(uuid.uuid4()), True)
    assert dal.get_room(db.tenant, room.id).paused is False


def test_set_run_as(rooms_db):
    db = rooms_db
    room = _room(db)

    moved = dal.set_run_as(db.tenant, room.id, db.bob)

    assert moved.run_as_user_id == db.bob
    assert dal.get_room(db.tenant, room.id).run_as_user_id == db.bob
    with pytest.raises(RoomNotFound):
        dal.set_run_as(db.other, room.id, db.bob)
    with pytest.raises(ValueError):
        dal.set_run_as(db.tenant, room.id, "not-a-uuid")


def test_recent_messages_are_the_last_n_oldest_first_with_their_authors(rooms_db):
    db = rooms_db
    room = _room(db)
    _say(db, room, {"role": "user", "content": "first", "author_user_id": db.alice})
    _say(db, room, {"role": "tool", "content": "{}", "tool_call_id": "x"})
    _say(db, room, {"role": "assistant", "content": [{"type": "text", "text": "second"}]})
    _say(db, room, {"role": "user", "content": "third", "author_user_id": db.bob})

    messages = dal.recent_messages(db.tenant, room.id, limit=2)

    assert [(m["role"], m["content"]) for m in messages] == [
        ("assistant", "second"),
        ("user", "third"),
    ]
    assert messages[1]["author_user_id"] == db.bob
    assert messages[0]["author_user_id"] is None
    assert all(isinstance(m["id"], int) and m["created_at"] for m in messages)


def test_recent_messages_of_another_tenants_room_are_empty(rooms_db):
    db = rooms_db
    room = _room(db)
    _say(db, room, {"role": "user", "content": "private"})
    assert dal.recent_messages(db.other, room.id, limit=5) == []
    assert dal.recent_messages(db.tenant, "nope", limit=5) == []
