"""Shared, pooled ``httpx.AsyncClient``s for hot request paths.

Building a client per request throws away its connection pool: every call pays
TCP (and TLS) setup again and leaves a socket in TIME_WAIT. Hot paths ask this
module for a named client instead, and pass their timeout on each request.

A client belongs to the event loop that created it (its pooled connections are
bound to that loop), so the registry keeps one client per name *and* loop: a
call on another loop (a test's fresh loop, an ``asyncio.run`` in a worker
thread) gets its own client and never evicts the engine loop's. A closed
client is rebuilt on the next call. Shared clients keep no cookies.
``close_shared_clients`` is awaited on engine shutdown
(``robothor/engine/daemon.py``).

The constructor is looked up on ``httpx`` at call time and is part of the key,
so a test that patches ``httpx.AsyncClient`` gets a client from its patch.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
import threading
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


class _RejectAllCookies(http.cookiejar.DefaultCookiePolicy):
    """Stores nothing and sends nothing.

    A shared client outlives the call that made it and serves every agent and
    tenant; a cookie one response set must never ride along on the next call.
    """

    def set_ok(self, cookie: http.cookiejar.Cookie, request: Any) -> bool:
        return False

    def return_ok(self, cookie: http.cookiejar.Cookie, request: Any) -> bool:
        return False


def _cookieless_jar() -> http.cookiejar.CookieJar:
    return http.cookiejar.CookieJar(policy=_RejectAllCookies())


@dataclass
class _Entry:
    client: httpx.AsyncClient
    # Held strongly so the loop's id() cannot be reused while the entry lives.
    loop: asyncio.AbstractEventLoop
    factory: Any
    transport: httpx.AsyncBaseTransport | None


#: (name, id(loop)) -> entry. Each live loop keeps its own client: a worker
#: thread's ``asyncio.run`` never evicts the engine loop's client.
_clients: dict[tuple[str, int], _Entry] = {}
_closing: set[asyncio.Task[None]] = set()
# Worker threads running their own loops reach the registry too.
_lock = threading.Lock()


def _prune_closed_loops() -> None:
    """Forget clients whose loop has finished (an ``asyncio.run`` that returned).

    They cannot be closed from another loop. Their loop closed its selector, so
    dropping them leaves the sockets to the garbage collector (an ``unclosed
    transport`` warning at worst). Only short-lived worker loops end up here;
    the engine loop closes its own clients on shutdown.
    """
    for key in [k for k, e in _clients.items() if e.loop.is_closed()]:
        del _clients[key]


async def _aclose_quietly(client: httpx.AsyncClient) -> None:
    try:
        await client.aclose()
    except Exception as e:  # noqa: BLE001 - closing must not raise
        logger.debug("Closing a shared HTTP client failed: %s", e)


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
    with _lock:
        return _client_for(name, loop, factory, transport)


def _client_for(
    name: str,
    loop: asyncio.AbstractEventLoop,
    factory: Any,
    transport: httpx.AsyncBaseTransport | None,
) -> httpx.AsyncClient:
    _prune_closed_loops()
    key = (name, id(loop))
    entry = _clients.get(key)
    if entry is not None:
        if entry.factory is factory and entry.transport is transport and not entry.client.is_closed:
            return entry.client
        # Same loop, different constructor or transport (a test seam): close
        # the one being replaced rather than abandoning it.
        if not entry.client.is_closed:
            task = loop.create_task(_aclose_quietly(entry.client))
            _closing.add(task)
            task.add_done_callback(_closing.discard)
    client: httpx.AsyncClient = factory(
        limits=SHARED_CLIENT_LIMITS,
        transport=transport,
        cookies=_cookieless_jar(),
    )
    _clients[key] = _Entry(client=client, loop=loop, factory=factory, transport=transport)
    return client


async def close_shared_clients() -> None:
    """Close every shared client of the running loop.

    Clients of loops that have finished are forgotten; those of other live
    loops belong to those loops and are left to them.
    """
    loop = asyncio.get_running_loop()
    with _lock:
        _prune_closed_loops()
        mine = [_clients.pop(k).client for k, e in list(_clients.items()) if e.loop is loop]
    for client in mine:
        await _aclose_quietly(client)
