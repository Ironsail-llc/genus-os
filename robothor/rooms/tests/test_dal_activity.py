"""A saved exchange moves a room's activity mark forward, never back.

``rooms.last_activity_at`` orders the rail and ``last_message_id`` is the
newest message of the room's conversation. The chat path bumps both after each
saved exchange; another tenant's id changes nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from robothor.rooms import dal

pytestmark = pytest.mark.integration


def _room(db: Any, tenant: str | None = None) -> Any:
    return dal.create_room(
        tenant or db.tenant, kind="agent", title="Pricing", agent_id="main", created_by=db.alice
    )


def test_record_activity_sets_the_newest_message_and_the_time(rooms_db) -> None:
    room = _room(rooms_db)
    assert room.last_message_id is None

    bumped = dal.record_activity(rooms_db.tenant, room.id, 42)

    assert bumped is not None
    assert bumped.last_message_id == 42
    assert bumped.last_activity_at is not None
    assert dal.get_room(rooms_db.tenant, room.id).last_message_id == 42


def test_record_activity_never_moves_the_message_mark_back(rooms_db) -> None:
    room = _room(rooms_db)
    dal.record_activity(rooms_db.tenant, room.id, 42)
    first = dal.get_room(rooms_db.tenant, room.id)

    later = dal.record_activity(rooms_db.tenant, room.id, 7)

    assert later.last_message_id == 42
    assert later.last_activity_at >= first.last_activity_at


def test_record_activity_in_another_tenant_changes_nothing(rooms_db) -> None:
    room = _room(rooms_db)

    assert dal.record_activity(rooms_db.other, room.id, 42) is None
    assert dal.record_activity(rooms_db.tenant, "not-a-uuid", 42) is None
    assert dal.get_room(rooms_db.tenant, room.id).last_message_id is None
