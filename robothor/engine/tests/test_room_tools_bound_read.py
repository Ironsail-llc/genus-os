# ruff: noqa: F811 - pytest fixtures are imported and requested by name
"""Inside a workspace run, the room tools see only that workspace.

A workspace run (scheduled, on demand, period or preview) has no person
behind it, so it may not change anything; it may READ, but only the room its
run-context carrier binds it to, in the same tenant, with that room's run-as
user's current access. Any other room reads as not found, whoever runs it
(another run-as user, the same one, another tenant), by id or through
``room_list``. A run bound to no room reads nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.engine.tools.handlers import rooms as room_tools
from robothor.rooms import definition
from robothor.rooms.access import Actor
from robothor.rooms.run_context import RoomRun, room_run_scope
from robothor.rooms.tests.conftest import rooms_db  # noqa: F401
from robothor.rooms.tests.staff import PEOPLE, staff_db  # noqa: F401

pytestmark = pytest.mark.integration


def _make(db: Any, owner: str, title: str, tenant: str | None = None) -> Any:
    role = PEOPLE.get(owner, "member")
    actor = Actor(user_id=getattr(db, owner), role=role, tenant_id=tenant or db.tenant)
    return definition.create_room_with_schedule(
        actor, kind="report", title=title, instructions="Summarise.", agent_id="main"
    )


@pytest.fixture
def world(staff_db):
    db = staff_db
    db.a = _make(db, "alice", "A")
    db.b = _make(db, "bob", "B")  # another run-as user; Alice is not a member
    db.c = _make(db, "alice", "C")  # the same run-as user as A
    db.d = _make(db, "olga", "D", tenant=db.other)  # another tenant
    return db


def _ctx(db: Any) -> ToolContext:
    """The run of A: it runs as A's run-as user, as ``runs._turn`` sets it."""
    return ToolContext(
        agent_id="main", run_id="run-1", tenant_id=db.tenant, user_id=db.alice, user_role="member"
    )


def _in_run_of(room: Any, tool: str, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    async def call() -> dict[str, Any]:
        carrier = RoomRun.for_session(room.tenant_id, room.session_key, unattended=True)
        with room_run_scope(carrier):
            return await room_tools.HANDLERS[tool](args, ctx)

    return asyncio.run(call())


def test_a_run_reads_its_own_room(world):
    result = _in_run_of(world.a, "room_read", {"room_id": world.a.id}, _ctx(world))
    assert result["room"]["id"] == world.a.id


@pytest.mark.parametrize("other", ["b", "c", "d"])
def test_a_run_cannot_read_another_room(world, other):
    target = getattr(world, other)
    result = _in_run_of(world.a, "room_read", {"room_id": target.id}, _ctx(world))
    assert result.get("not_found") is True
    assert target.title not in str(result)


def test_room_list_in_a_run_shows_only_its_own_room(world):
    result = _in_run_of(world.a, "room_list", {}, _ctx(world))
    assert [r["id"] for r in result["rooms"]] == [world.a.id]


def test_room_list_all_in_a_run_still_shows_only_its_own_room(world):
    result = _in_run_of(world.a, "room_list", {"scope": "all"}, _ctx(world))
    assert [r["id"] for r in result["rooms"]] == [world.a.id]


def test_a_carrier_of_another_tenant_reads_nothing(world):
    ctx = _ctx(world)
    result = _in_run_of(world.d, "room_read", {"room_id": world.d.id}, ctx)
    assert result.get("not_found") is True


def test_a_run_with_no_person_and_no_room_reads_nothing(world):
    ctx = ToolContext(agent_id="main", run_id="run-1", tenant_id=world.tenant)
    result = asyncio.run(room_tools.HANDLERS["room_read"]({"room_id": world.b.id}, ctx))
    assert result.get("not_found") is True


def test_a_persons_own_turn_still_reads_what_they_can(world):
    ctx = _ctx(world)
    result = asyncio.run(room_tools.HANDLERS["room_read"]({"room_id": world.c.id}, ctx))
    assert result["room"]["id"] == world.c.id
