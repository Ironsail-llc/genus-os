"""The data-layer pieces the bridge API reads, against real Postgres.

Stories: a room restored from the company list comes back paused (A6); an
admin reassigns a room and the new user is assigned and run-as, its schedule
included (G4); one room's summary carries the caller's own unread count (C6);
members are named from their accounts, and only accounts of the room's own
company count (G1); the next scheduled run is the room's, and none while paused.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from robothor.rooms import dal
from robothor.rooms.errors import RoomNotFound

pytestmark = pytest.mark.integration


def _room(db: Any, **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "kind": "report",
        "title": "Weekly numbers",
        "agent_id": "main",
        "created_by": db.alice,
        "instructions": "Summarise the week.",
    }
    fields.update(overrides)
    return dal.create_room(db.tenant, **fields)


@pytest.fixture
def accounts(rooms_db) -> Any:
    """Accounts for Alice and Bob (active), Carol (disabled), and one in the other tenant."""
    cur = rooms_db.cur
    other_user = str(uuid.uuid4())
    rows = [
        (rooms_db.alice, rooms_db.tenant, "alice@example.com", "Alice", "member", "active"),
        (rooms_db.bob, rooms_db.tenant, "bob@example.com", "Bob", "member", "active"),
        (rooms_db.carol, rooms_db.tenant, "carol@example.com", "Carol", "member", "disabled"),
        (other_user, rooms_db.other, "eve@example.com", "Eve", "admin", "active"),
    ]
    for row in rows:
        cur.execute(
            "INSERT INTO user_accounts (id, tenant_id, email, display_name, role, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            row,
        )
    rooms_db.eve = other_user
    yield rooms_db
    cur.execute(
        "DELETE FROM user_cronjobs WHERE tenant_id = ANY(%s)", ([rooms_db.tenant, rooms_db.other],)
    )
    with contextlib.suppress(Exception):
        cur.execute(
            "DELETE FROM user_accounts WHERE tenant_id = ANY(%s)",
            ([rooms_db.tenant, rooms_db.other],),
        )


# ─── unarchive (A6) ──────────────────────────────────────────────────────


def test_unarchive_returns_the_room_paused(rooms_db) -> None:
    room = _room(rooms_db)
    dal.archive_room(rooms_db.tenant, room.id)

    restored = dal.unarchive_room(rooms_db.tenant, room.id)

    assert restored is not None
    assert restored.archived_at is None
    assert restored.paused is True
    assert dal.get_room(rooms_db.tenant, room.id).paused is True


def test_archive_does_not_change_paused(rooms_db) -> None:
    room = _room(rooms_db)
    assert dal.archive_room(rooms_db.tenant, room.id).paused is False


# ─── accounts (G1, member names) ─────────────────────────────────────────


def test_get_accounts_names_only_the_tenants_own_accounts(accounts) -> None:
    db = accounts
    found = dal.get_accounts(db.tenant, [db.alice, db.carol, db.eve, str(uuid.uuid4()), "nope"])

    assert set(found) == {db.alice, db.carol}
    assert found[db.alice]["display_name"] == "Alice"
    assert found[db.alice]["status"] == "active"
    assert found[db.alice]["role"] == "member"
    assert found[db.carol]["status"] == "disabled"


def test_get_accounts_of_nothing_is_empty(accounts) -> None:
    assert dal.get_accounts(accounts.tenant, []) == {}


# ─── reassign (G4) ───────────────────────────────────────────────────────


def _cron(db: Any, room: Any, *, next_run: datetime | None, enabled: bool = True) -> str:
    job = f"room-{uuid.uuid4().hex[:8]}"
    db.cur.execute(
        "INSERT INTO user_cronjobs (job_id, tenant_id, agent_id, schedule_kind, schedule_payload, "
        "prompt, enabled, next_run_at, room_id, run_as_user_id) "
        "VALUES (%s, %s, 'main', 'interval', '{}'::jsonb, 'run', %s, %s, %s, %s)",
        (job, db.tenant, enabled, next_run, room.id, room.run_as_user_id),
    )
    return job


def test_reassign_makes_the_user_assigned_and_run_as(accounts) -> None:
    db = accounts
    room = _room(db)
    dal.add_member(db.tenant, room.id, db.bob, added_by=db.alice)
    job = _cron(db, room, next_run=datetime.now(UTC) + timedelta(hours=1))

    moved = dal.reassign_room(db.tenant, room.id, db.bob, reassigned_by=db.carol)

    assert moved.run_as_user_id == db.bob
    members = {m.user_id: m.assigned for m in dal.list_members(db.tenant, room.id)}
    assert members == {db.bob: True, db.alice: False}
    db.cur.execute("SELECT run_as_user_id FROM user_cronjobs WHERE job_id = %s", (job,))
    assert str(db.cur.fetchone()[0]) == db.bob


def test_reassign_to_a_non_member_adds_them(accounts) -> None:
    db = accounts
    room = _room(db)

    dal.reassign_room(db.tenant, room.id, db.bob, reassigned_by=db.alice)

    bob = dal.get_member(db.tenant, room.id, db.bob)
    assert bob is not None and bob.assigned is True and bob.added_by == db.alice


def test_reassign_of_another_tenants_room_is_not_found(accounts) -> None:
    db = accounts
    room = _room(db)
    with pytest.raises(RoomNotFound):
        dal.reassign_room(db.other, room.id, db.bob, reassigned_by=db.alice)


# ─── one summary (C6) ────────────────────────────────────────────────────


def _messages(db: Any, room: Any, n: int) -> None:
    db.cur.execute(
        "INSERT INTO chat_sessions (tenant_id, session_key, channel) VALUES (%s, %s, 'webchat') "
        "ON CONFLICT (tenant_id, session_key) DO UPDATE SET channel = EXCLUDED.channel "
        "RETURNING id",
        (room.tenant_id, room.session_key),
    )
    session_id = db.cur.fetchone()[0]
    for i in range(n):
        db.cur.execute(
            "INSERT INTO chat_messages (session_id, message) "
            "VALUES (%s, jsonb_build_object('role', 'user', 'content', %s::text))",
            (session_id, f"message {i}"),
        )


def test_room_summary_carries_the_callers_own_unread(rooms_db) -> None:
    db = rooms_db
    room = _room(db)
    dal.add_member(db.tenant, room.id, db.bob, added_by=db.alice)
    _messages(db, room, 3)
    dal.mark_read(db.tenant, room.id, db.bob)

    alice = dal.room_summary(db.tenant, room.id, db.alice)
    bob = dal.room_summary(db.tenant, room.id, db.bob)
    stranger = dal.room_summary(db.tenant, room.id, db.carol)

    assert (alice.id, alice.unread) == (room.id, 3)
    assert bob.unread == 0
    assert stranger is not None and stranger.unread == 0  # an admin's view: no reader
    assert dal.room_summary(db.other, room.id, db.alice) is None


def test_room_summary_includes_an_archived_room(rooms_db) -> None:
    room = _room(rooms_db)
    dal.archive_room(rooms_db.tenant, room.id)
    summary = dal.room_summary(rooms_db.tenant, room.id, rooms_db.alice)
    assert summary is not None and summary.archived is True


# ─── assigned members of many rooms (G4 admin list) ──────────────────────


def test_members_of_many_rooms(rooms_db) -> None:
    db = rooms_db
    first, second = _room(db), _room(db, created_by=db.bob)
    dal.add_member(db.tenant, first.id, db.carol, added_by=db.alice)

    found = dal.members_of_rooms(db.tenant, [first.id, second.id, str(uuid.uuid4())])

    assert {k: [(m.user_id, m.assigned) for m in v] for k, v in found.items()} == {
        first.id: [(db.alice, True), (db.carol, False)],
        second.id: [(db.bob, True)],
    }
    assert dal.members_of_rooms(db.other, [first.id]) == {}


# ─── next run ────────────────────────────────────────────────────────────


def test_next_run_at_is_the_rooms_enabled_job(accounts) -> None:
    """A room keeps one schedule row (migration 170): its time, while enabled."""
    db = accounts
    room = _room(db)
    soon = datetime.now(UTC) + timedelta(hours=1)
    _cron(db, room, next_run=soon)

    found = dal.next_run_at(db.tenant, room.id)

    assert found is not None and abs((found - soon).total_seconds()) < 1
    db.cur.execute("UPDATE user_cronjobs SET enabled = FALSE WHERE room_id = %s", (room.id,))
    assert dal.next_run_at(db.tenant, room.id) is None


def test_next_run_at_is_none_without_a_schedule_or_when_paused(accounts) -> None:
    db = accounts
    room = _room(db)
    assert dal.next_run_at(db.tenant, room.id) is None
    _cron(db, room, next_run=datetime.now(UTC) + timedelta(hours=1))
    db.cur.execute("UPDATE rooms SET paused = TRUE WHERE id = %s", (room.id,))
    assert dal.next_run_at(db.tenant, room.id) is None
    assert dal.next_run_at(db.other, room.id) is None
