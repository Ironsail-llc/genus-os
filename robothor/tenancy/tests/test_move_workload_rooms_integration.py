"""Threads move with their workload: ``move_workload`` carries an agent's rooms.

A room's conversation lives in a session keyed ``agent:{agent}:room:{id}``,
which already moves with the agent. The room itself (its members, revisions
and editions) has to move with it, or the target tenant would hold a
conversation no room points at and the source a room whose history is gone.
A room linked to CRM tasks cannot move: the tasks stay in the source's CRM.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from robothor.rooms.access import room_session_key
from robothor.tenancy import move_workload as mw
from robothor.tenancy.tests.test_move_workload_integration import (  # noqa: F401 - fixture
    _move,
    _tenant_of,
    world,
)

pytestmark = pytest.mark.integration

ROOM_TABLES = ("room_tasks", "room_editions", "room_revisions", "room_members", "rooms")


def _seed_room(cur: Any, tenant: str, agent: str) -> str:
    room, user = str(uuid.uuid4()), str(uuid.uuid4())
    key = room_session_key(agent, room)
    cur.execute(
        "INSERT INTO rooms (tenant_id, id, kind, title, agent_id, session_key, created_by) "
        "VALUES (%s, %s, 'report', 'Weekly', %s, %s, %s)",
        (tenant, room, agent, key, user),
    )
    cur.execute(
        "INSERT INTO room_members (tenant_id, room_id, user_id, assigned) VALUES (%s, %s, %s, TRUE)",
        (tenant, room, user),
    )
    cur.execute(
        "INSERT INTO room_revisions (tenant_id, room_id, rev, changed_by, definition) "
        "VALUES (%s, %s, 1, %s, '{}')",
        (tenant, room, user),
    )
    cur.execute(
        "INSERT INTO room_editions (tenant_id, id, room_id, seq, kind, status, revision) "
        "VALUES (%s, %s, %s, 1, 'edition', 'published', 1)",
        (tenant, str(uuid.uuid4()), room),
    )
    cur.execute("INSERT INTO chat_sessions (tenant_id, session_key) VALUES (%s, %s)", (tenant, key))
    return room


@pytest.fixture
def rooms_world(world):  # noqa: F811
    cur = world["cur"]
    world["room"] = _seed_room(cur, world["parent"], world["agent"])
    world["neighbour_room"] = _seed_room(cur, world["parent"], world["neighbour"])
    try:
        yield world
    finally:
        tenants = (world["parent"], world["child_a"], world["child_b"], world["other"])
        for table in ROOM_TABLES:
            cur.execute(f"DELETE FROM {table} WHERE tenant_id IN %s", (tenants,))  # noqa: S608


def test_an_agents_rooms_move_with_it(rooms_world) -> None:
    w, cur = rooms_world, rooms_world["cur"]

    report = _move(w)

    for table in ("rooms", "room_members", "room_revisions", "room_editions"):
        assert report.counts[table] == 1, table
    assert _tenant_of(cur, "rooms", "id", w["room"]) == [w["child_a"]]
    for table in ("room_members", "room_revisions", "room_editions"):
        assert _tenant_of(cur, table, "room_id", w["room"]) == [w["child_a"]], table
    assert _tenant_of(
        cur, "chat_sessions", "session_key", room_session_key(w["agent"], w["room"])
    ) == [w["child_a"]]
    # The neighbour's room and its conversation stay.
    assert _tenant_of(cur, "rooms", "id", w["neighbour_room"]) == [w["parent"]]
    assert _tenant_of(cur, "room_members", "room_id", w["neighbour_room"]) == [w["parent"]]


def test_a_dry_run_counts_the_rooms_and_moves_none(rooms_world) -> None:
    w = rooms_world
    report = _move(w, dry_run=True)
    assert report.counts["rooms"] == 1
    assert _tenant_of(w["cur"], "rooms", "id", w["room"]) == [w["parent"]]


def test_a_room_linked_to_a_crm_task_refuses_the_move(rooms_world) -> None:
    w, cur = rooms_world, rooms_world["cur"]
    cur.execute(
        "INSERT INTO room_tasks (tenant_id, room_id, task_id, origin) VALUES (%s, %s, %s, 'linked')",
        (w["parent"], w["room"], w["task"]),
    )

    with pytest.raises(mw.WorkloadMoveError, match="room_tasks"):
        _move(w)

    assert _tenant_of(cur, "rooms", "id", w["room"]) == [w["parent"]]
