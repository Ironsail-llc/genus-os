"""The engine side of approved workspace emails (``robothor/rooms/outbound.py``).

Two tasks, started once through the task registry by the daemon:

- a listener on the ``operations`` stream: each ``operation_action.approved``
  for a ``room.email`` action is delivered at once;
- a sweep every ``SWEEP_SECONDS``: delivers any approval whose event was lost,
  settles executions whose lease ran out as ``unknown`` (never re-sent) and
  writes every receipt still owed.

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

logger = logging.getLogger(__name__)

__all__ = ["SWEEP_SECONDS", "on_approved", "start_room_outbound", "sweep_loop"]

SWEEP_SECONDS = 30
_GROUP = "room-outbound"


def on_approved(event: dict[str, Any]) -> None:
    """Deliver the approved action an event names, if it is a workspace email."""
    from robothor.rooms import outbound

    payload = event.get("payload") or {}
    tenant, action = payload.get("tenant_id"), payload.get("action_id")
    if payload.get("kind") != outbound.KIND or not tenant or not action:
        return
    try:
        outbound.deliver(str(tenant), str(action))
    except Exception:
        logger.exception("room email %s: delivery failed; the sweep will look again", action)


async def listen_loop() -> None:
    from robothor.events import bus
    from robothor.operations.store import OPERATIONS_STREAM

    consumer = f"{socket.gethostname()}-{os.getpid()}"
    while True:
        try:
            await asyncio.to_thread(
                bus.subscribe,
                OPERATIONS_STREAM,
                _GROUP,
                consumer,
                handler=on_approved,
                max_iterations=1,
                block_ms=5000,
            )
        except Exception:
            logger.warning("room email listener: read failed", exc_info=True)
        await asyncio.sleep(1)


async def sweep_loop() -> None:
    from robothor.rooms import outbound

    while True:
        try:
            await asyncio.to_thread(outbound.sweep)
        except Exception:
            logger.warning("room email sweep failed; trying again", exc_info=True)
        await asyncio.sleep(SWEEP_SECONDS)


def start_room_outbound() -> None:
    """Start the listener and the sweep (the daemon calls this once)."""
    registry = get_task_registry()
    registry.spawn(listen_loop(), name="room-outbound-listener")
    registry.spawn(sweep_loop(), name="room-outbound-sweep")
