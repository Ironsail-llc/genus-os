"""Shared, pooled ``httpx.AsyncClient``s for hot request paths.

Building a client per request throws away its connection pool: every call pays
TCP (and TLS) setup again and leaves a socket in TIME_WAIT. Hot paths ask this
module for a named client instead, and pass their timeout on each request.

A client belongs to the event loop that created it (its pooled connections are
bound to that loop), so the registry keeps one client per name *and* loop: a
call on a new loop (a test's fresh loop, an ``asyncio.run`` sync wrapper) gets
a new client rather than one whose sockets live on a dead loop. A closed
client is rebuilt on the next call. ``close_shared_clients`` is awaited on
engine shutdown (``robothor/engine/daemon.py``).

The constructor is looked up on ``httpx`` at call time and is part of the key,
so a test that patches ``httpx.AsyncClient`` gets a client from its patch.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Pool limits for a shared client. Keep-alive expiry sits under uvicorn's
#: 5 s idle close, so a pooled connection is never reused just as the server
#: hangs up on it.
SHARED_CLIENT_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=4.0,
)


@dataclass
class _Entry:
    client: httpx.AsyncClient
    loop: asyncio.AbstractEventLoop
    factory: Any
    transport: httpx.AsyncBaseTransport | None


_clients: dict[str, _Entry] = {}


def shared_async_client(
    name: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """The pooled client registered as ``name`` for the running event loop.

    Must be called from a coroutine. Pass per-request timeouts on each request;
    the client itself carries no timeout of its own that callers rely on.
    """
    loop = asyncio.get_running_loop()
    factory = httpx.AsyncClient
    entry = _clients.get(name)
    if (
        entry is not None
        and entry.loop is loop
        and entry.factory is factory
        and entry.transport is transport
        and not entry.client.is_closed
    ):
        return entry.client
    # A client left on another loop cannot be closed from this one; drop it and
    # let its sockets be collected with it.
    client = factory(limits=SHARED_CLIENT_LIMITS, transport=transport)
    _clients[name] = _Entry(client=client, loop=loop, factory=factory, transport=transport)
    return client


async def close_shared_clients() -> None:
    """Close every shared client created on the running loop; forget the rest."""
    loop = asyncio.get_running_loop()
    entries = list(_clients.values())
    _clients.clear()
    for entry in entries:
        if entry.loop is not loop:
            continue
        try:
            await entry.client.aclose()
        except Exception as e:  # noqa: BLE001 - shutdown must not raise
            logger.debug("Closing a shared HTTP client failed: %s", e)
