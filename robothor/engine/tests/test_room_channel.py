"""The ``room`` delivery channel (story B9).

A manifest agent with ``delivery.channel: room`` and ``delivery.to: <room id>``
delivers its output as an attributed assistant turn in that workspace, the
room's activity mark moves, and the members are told with a short note and
the link. An unknown, malformed or archived target is a failed receipt with a
reason, and nothing is written.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from robothor.engine.channels.base import SendReceipt
from robothor.rooms import dal, fanout
from robothor.rooms.access import room_session_key
from robothor.rooms.models import Room

TENANT = "test-tenant"


def _room(archived: bool = False) -> Room:
    room_id = str(uuid.uuid4())
    return Room(
        tenant_id=TENANT,
        id=room_id,
        kind="agent",
        title="Ops digest",
        agent_id="main",
        session_key=room_session_key("main", room_id),
        created_by=str(uuid.uuid4()),
        archived_at=datetime.now(UTC) if archived else None,
    )


@pytest.fixture
def env(monkeypatch):
    rooms: dict[str, Room] = {}
    saved: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []
    activity: list[tuple[Any, ...]] = []
    events: list[tuple[Any, ...]] = []

    monkeypatch.setattr(dal, "get_room", lambda tenant, room_id: rooms.get(room_id))
    monkeypatch.setattr(dal, "record_activity", lambda *a: activity.append(a))
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: events.append(a))

    async def fake_surface(session_key: str, content: str, agent_id: str, **kw: Any) -> int:
        saved.append({"key": session_key, "content": content, "agent": agent_id, **kw})
        return 77

    monkeypatch.setattr("robothor.engine.chat_store.save_channel_surface_async", fake_surface)

    async def fake_note(room: Any, text: str, **kw: Any) -> list[SendReceipt]:
        notes.append({"room": room, "text": text, **kw})
        return [SendReceipt(acknowledged=2, expected=2)]

    monkeypatch.setattr(fanout, "fan_out_note", fake_note)
    return SimpleNamespace(
        rooms=rooms, saved=saved, notes=notes, activity=activity, events=events
    )


def _config() -> Any:
    return SimpleNamespace(id="ops-digest", name="Ops digest agent", delivery_to="")


def _run() -> Any:
    return SimpleNamespace(id="run-1", tenant_id=TENANT)


async def test_output_lands_as_an_attributed_turn_and_members_are_told(env):
    from robothor.engine.channels.room import RoomChannel

    room = _room()
    env.rooms[room.id] = room

    receipt = await RoomChannel().send(room.id, "All systems green.", config=_config(), run=_run())

    assert receipt.complete and receipt.status is None
    (row,) = env.saved
    assert row["key"] == room.session_key
    assert "All systems green." in row["content"]
    assert row["agent"] == "ops-digest"
    assert row["author_display_name"] == "Ops digest agent"
    assert row["tenant_id"] == TENANT and row["channel"] == "room"
    assert env.activity == [(TENANT, room.id, 77)]
    assert env.events[-1][:3] == (
        "agent",
        "room.message",
        {"room_id": room.id, "tenant_id": TENANT, "message_id": 77},
    )
    (note,) = env.notes
    assert note["room"].id == room.id
    assert note["metadata"]["room_id"] == room.id
    assert room.session_key not in str(note) and room.session_key not in receipt.target


@pytest.mark.parametrize("target", ["", "not-a-uuid", str(uuid.uuid4())])
async def test_an_unknown_target_fails_with_a_reason(env, target):
    from robothor.engine.channels.room import RoomChannel

    receipt = await RoomChannel().send(target, "hello", config=_config(), run=_run())

    assert not receipt.complete
    assert receipt.status == "failed:room_unknown_target"
    assert env.saved == [] and env.notes == []


async def test_an_archived_target_fails_with_a_reason(env):
    from robothor.engine.channels.room import RoomChannel

    room = _room(archived=True)
    env.rooms[room.id] = room

    receipt = await RoomChannel().send(room.id, "hello", config=_config(), run=_run())

    assert receipt.status == "failed:room_archived"
    assert env.saved == []


async def test_a_turn_that_was_not_saved_is_a_failed_receipt(env, monkeypatch):
    from robothor.engine.channels.room import RoomChannel

    async def fails(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr("robothor.engine.chat_store.save_channel_surface_async", fails)
    room = _room()
    env.rooms[room.id] = room

    receipt = await RoomChannel().send(room.id, "hello", config=_config(), run=_run())

    assert receipt.status == "failed:room_no_session_write"
    assert env.notes == []


async def test_the_delivery_waits_for_a_turn_in_the_room(env):
    import asyncio

    from robothor.engine.channels.room import RoomChannel

    room = _room()
    env.rooms[room.id] = room
    lock = fanout.room_lock(room.id)
    await lock.acquire()
    task = asyncio.create_task(RoomChannel().send(room.id, "x", config=_config(), run=_run()))
    await asyncio.sleep(0.05)
    assert env.saved == []
    lock.release()
    await asyncio.wait_for(task, 2)
    assert len(env.saved) == 1


def test_room_is_a_builtin_channel():
    from robothor.engine.channels import registry

    assert "room" in registry.BUILTIN_CHANNELS
    registry.reset_channels()
    try:
        from robothor.engine.channels.room import RoomChannel

        assert isinstance(registry.get_channel("room"), RoomChannel)
    finally:
        registry.reset_channels()
