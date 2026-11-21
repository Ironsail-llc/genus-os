"""The engine side of approved workspace emails: started through the task
registry, woken by ``operation_action.approved``, with a sweep as the backup."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest


def test_starting_spawns_the_listener_and_the_sweep_through_the_registry(monkeypatch):
    from robothor.engine import room_outbound

    registry = MagicMock()
    monkeypatch.setattr(room_outbound, "get_task_registry", lambda: registry)

    async def go() -> None:
        room_outbound.start_room_outbound()

    asyncio.run(go())

    names = sorted(call.kwargs["name"] for call in registry.spawn.call_args_list)
    assert names == ["room-outbound-listener", "room-outbound-sweep"]
    for call in registry.spawn.call_args_list:
        call.args[0].close()


@pytest.mark.parametrize(
    ("payload", "delivered"),
    [
        ({"action_id": "a-1", "kind": "room.email", "tenant_id": "test-tenant"}, True),
        ({"action_id": "a-1", "kind": "sales.email", "tenant_id": "test-tenant"}, False),
        ({"action_id": "a-1", "kind": "room.email", "tenant_id": ""}, False),
        ({"kind": "room.email", "tenant_id": "test-tenant"}, False),
    ],
)
def test_an_approval_event_delivers_only_a_workspace_email(monkeypatch, payload, delivered):
    from robothor.engine import room_outbound
    from robothor.rooms import outbound

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(outbound, "deliver", lambda t, a: seen.append((t, a)) or "sent")

    room_outbound.on_approved({"type": "operation_action.approved", "payload": payload})

    assert (seen == [("test-tenant", "a-1")]) is delivered


def test_a_failing_delivery_never_kills_the_listener(monkeypatch):
    from robothor.engine import room_outbound
    from robothor.rooms import outbound

    def boom(t: str, a: str) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(outbound, "deliver", boom)

    room_outbound.on_approved(
        {"payload": {"action_id": "a-1", "kind": "room.email", "tenant_id": "test-tenant"}}
    )


def test_the_sweep_loop_keeps_going_after_a_failure(monkeypatch):
    from robothor.engine import room_outbound
    from robothor.rooms import outbound

    calls = {"n": 0}

    def sweep() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db down")
        raise asyncio.CancelledError

    monkeypatch.setattr(outbound, "sweep", sweep)
    monkeypatch.setattr(room_outbound, "SWEEP_SECONDS", 0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(room_outbound.sweep_loop())
    assert calls["n"] == 2


def test_the_daemon_starts_it():
    import inspect

    from robothor.engine import daemon

    assert "start_room_outbound()" in inspect.getsource(daemon)
