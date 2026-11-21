"""The engine side of approved workspace emails (``robothor/rooms/outbound.py``).

Two tasks, started once through the task registry by the daemon:

- a listener on the ``operations`` stream: each ``operation_action.approved``
  for a ``room.email`` action of THIS engine's tenant is delivered at once;
- a sweep every ``SWEEP_SECONDS``, of this engine's tenant only: delivers any
  approval whose event was lost, settles executions whose lease ran out as
  ``unknown`` (never re-sent) and writes every receipt still owed.

One engine runs per tenant, all on one Redis (``robothor/engine/hooks.py``):
each engine reads with its own consumer group (``consumer_group``, from
``EngineConfig.event_group``) so it sees every event, and acts only on its own
tenant's (``owns``). Another tenant's email is never claimed here, so it is
never sent through this engine's mailbox.

Delivery claims the action by id under a lease, so the listener, the sweep and
other engine replicas never send one email twice. Every database and mail call
runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any

from robothor.engine.task_registry import get_task_registry
from robothor.sanitize import sanitize_log

logger = logging.getLogger(__name__)

__all__ = [
    "SWEEP_SECONDS",
    "consumer_group",
    "on_approved",
    "owns",
    "start_room_outbound",
    "sweep_loop",
]

SWEEP_SECONDS = 30
_GROUP = "room-outbound"


def consumer_group(config: Any) -> str:
    """This engine's own consumer group on the ``operations`` stream."""
    from robothor.engine.config import ROOT_EVENT_GROUP

    return f"{_GROUP}:{getattr(config, 'event_group', '') or ROOT_EVENT_GROUP}"


def owns(event: dict[str, Any], config: Any) -> bool:
    """Whether the approval belongs to this engine's tenant: the payload's
    tenant is this engine's, and the envelope (when tagged) agrees."""
    mine = str(getattr(config, "tenant_id", "") or "")
    payload = event.get("payload") or {}
    tenant = str(payload.get("tenant_id") or "")
    envelope = str(event.get("tenant_id") or "")
    return bool(mine) and tenant == mine and envelope in ("", mine)


def on_approved(event: dict[str, Any], config: Any) -> None:
    """Deliver the approved action an event names, if it is a workspace email
    of this engine's tenant."""
    from robothor.rooms import outbound

    payload = event.get("payload") or {}
    action = payload.get("action_id")
    if payload.get("kind") != outbound.KIND or not action or not owns(event, config):
        return
    try:
        outbound.deliver(str(config.tenant_id), str(action))
    except Exception:
        logger.exception(
            "room email %s: delivery failed; the sweep will look again", sanitize_log(action)
        )


async def listen_loop(config: Any) -> None:
    from robothor.events import bus
    from robothor.operations.store import OPERATIONS_STREAM

    group = consumer_group(config)
    consumer = f"{socket.gethostname()}-{os.getpid()}"

    def handler(event: dict[str, Any]) -> None:
        on_approved(event, config)

    while True:
        try:
            await asyncio.to_thread(
                bus.subscribe,
                OPERATIONS_STREAM,
                group,
                consumer,
                handler=handler,
                max_iterations=1,
                block_ms=5000,
            )
        except Exception:
            logger.warning("room email listener: read failed", exc_info=True)
        await asyncio.sleep(1)


async def sweep_loop(config: Any) -> None:
    from robothor.rooms import outbound

    tenant = str(config.tenant_id)
    while True:
        try:
            await asyncio.to_thread(outbound.sweep, tenant)
        except Exception:
            logger.warning("room email sweep failed; trying again", exc_info=True)
        await asyncio.sleep(SWEEP_SECONDS)


def start_room_outbound(config: Any) -> None:
    """Start the listener and the sweep for this engine's tenant (the daemon
    calls this once, with its ``EngineConfig``)."""
    registry = get_task_registry()
    registry.spawn(listen_loop(config), name="room-outbound-listener")
    registry.spawn(sweep_loop(config), name="room-outbound-sweep")
