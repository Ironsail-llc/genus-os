"""An account that moves to another tenant leaves its rooms behind.

Rooms belong to the tenant, not to the people in them, so a membership row
cannot travel with the account (it would name a room the target tenant does
not have). It is removed in the move's transaction instead, like the account's
sessions are revoked: a later move back must not quietly restore access to
threads the account was taken out of.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from robothor.auth import accounts
from robothor.auth.tests.test_move_account_integration import world  # noqa: F401 - fixture
from robothor.rooms.access import room_session_key

pytestmark = pytest.mark.integration


def _room_with(cur: Any, tenant: str, *users: str) -> str:
    room = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by) "
        "VALUES (%s, %s, 'agent', 'Planning', 'main', %s, %s)",
        (tenant, room, room_session_key("main", room), users[0]),
    )
    for user in users:
        cur.execute(
            "INSERT INTO room_members (tenant_id, room_id, user_id, assigned) "
            "VALUES (%s, %s, %s, TRUE)",
            (tenant, room, user),
        )
    return room


def _members(cur: Any, room: str) -> set[str]:
    cur.execute("SELECT user_id::text FROM room_members WHERE room_id = %s", (room,))
    return {r[0] for r in cur.fetchall()}


@pytest.fixture
def rooms_world(world):  # noqa: F811
    cur, bob = world["cur"], str(uuid.uuid4())
    world["bob"] = bob
    world["room"] = _room_with(cur, world["parent"], world["user_id"], bob)
    try:
        yield world
    finally:
        for table in ("room_members", "rooms"):
            cur.execute(
                f"DELETE FROM {table} WHERE tenant_id IN %s",  # noqa: S608
                ((world["parent"], world["child"]),),
            )


def test_the_accounts_memberships_are_removed_and_the_room_stays(rooms_world) -> None:
    w, cur = rooms_world, rooms_world["cur"]

    report = accounts.move_account(w["user_id"], w["child"], actor="cli:test")

    assert report.room_memberships_removed == 1
    assert _members(cur, w["room"]) == {w["bob"]}
    cur.execute("SELECT tenant_id FROM rooms WHERE id = %s", (w["room"],))
    assert cur.fetchone()[0] == w["parent"]


def test_a_dry_run_counts_the_memberships_and_removes_none(rooms_world) -> None:
    w, cur = rooms_world, rooms_world["cur"]

    report = accounts.move_account(w["user_id"], w["child"], actor="cli:test", dry_run=True)

    assert report.room_memberships_removed == 1
    assert _members(cur, w["room"]) == {w["user_id"], w["bob"]}
