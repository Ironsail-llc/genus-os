"""The engine side of approved workspace emails: started through the task
registry, woken by ``operation_action.approved``, with a sweep as the backup.

One engine runs per tenant, all on one Redis (``robothor/engine/hooks.py``).
So this engine reads with its own consumer group, acts only on its own
tenant's approvals, and sweeps only its own tenant.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

MINE = "tenant-mine"
THEIRS = "tenant-theirs"


def _config(tenant: str = MINE, group: str = "engine-mine") -> Any:
    return SimpleNamespace(tenant_id=tenant, event_group=group)


def _event(tenant: str = MINE, kind: str = "room.email", envelope: str | None = None) -> dict:
    return {
        "type": "operation_action.approved",
        "tenant_id": tenant if envelope is None else envelope,
        "payload": {"action_id": "a-1", "kind": kind, "tenant_id": tenant},
    }


def test_starting_spawns_the_listener_and_the_sweep_through_the_registry(monkeypatch):
    from robothor.engine import room_outbound

    registry = MagicMock()
    monkeypatch.setattr(room_outbound, "get_task_registry", lambda: registry)

    async def go() -> None:
        room_outbound.start_room_outbound(_config())

    asyncio.run(go())

    names = sorted(call.kwargs["name"] for call in registry.spawn.call_args_list)
    assert names == ["room-outbound-listener", "room-outbound-sweep"]
    for call in registry.spawn.call_args_list:
        call.args[0].close()


def test_each_engine_reads_with_its_own_consumer_group():
    from robothor.engine import room_outbound

    assert room_outbound.consumer_group(_config(group="engine-a")) == "room-outbound:engine-a"
    assert room_outbound.consumer_group(_config(group="engine-b")) == "room-outbound:engine-b"
    assert room_outbound.consumer_group(_config(group="")) == "room-outbound:engine"


@pytest.mark.parametrize(
    ("event", "delivered"),
    [
        (_event(), True),
        (_event(kind="sales.email"), False),
        (_event(tenant=""), False),
        (_event(tenant=THEIRS), False),
        (_event(tenant=MINE, envelope=THEIRS), False),
        ({"payload": {"kind": "room.email", "tenant_id": MINE}}, False),
    ],
    ids=[
        "mine",
        "not-a-workspace-email",
        "untagged",
        "another-tenant",
        "envelope-disagrees",
        "no-action",
    ],  # fmt: skip
)
def test_an_approval_event_delivers_only_this_tenants_workspace_email(
    monkeypatch, event, delivered
):
    from robothor.engine import room_outbound
    from robothor.rooms import outbound

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(outbound, "deliver", lambda t, a: seen.append((t, a)) or "sent")

    room_outbound.on_approved(event, _config())

    assert (seen == [(MINE, "a-1")]) is delivered


def test_a_failing_delivery_never_kills_the_listener(monkeypatch):
    from robothor.engine import room_outbound
    from robothor.rooms import outbound

    def boom(t: str, a: str) -> Any:
        raise RuntimeError("db down")

    monkeypatch.setattr(outbound, "deliver", boom)

    room_outbound.on_approved(_event(), _config())


def test_the_sweep_works_only_on_this_engines_tenant_and_keeps_going(monkeypatch):
    from robothor.engine import room_outbound
    from robothor.rooms import outbound

    calls: list[str] = []

    def sweep(tenant_id: str) -> int:
        calls.append(tenant_id)
        if len(calls) == 1:
            raise RuntimeError("db down")
        raise asyncio.CancelledError

    monkeypatch.setattr(outbound, "sweep", sweep)
    monkeypatch.setattr(room_outbound, "SWEEP_SECONDS", 0)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(room_outbound.sweep_loop(_config()))
    assert calls == [MINE, MINE]


def test_the_daemon_starts_it_with_its_config():
    import inspect

    from robothor.engine import daemon

    assert "start_room_outbound(config)" in inspect.getsource(daemon)
