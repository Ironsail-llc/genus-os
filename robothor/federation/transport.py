"""Federation transport — one endpoint per direction, routed by connection.

An instance in an organisation is rarely only a parent or only a child. A
regional instance is a child of headquarters and a parent to its own sites, so
it must simultaneously *listen* on its own broker and *dial* its parent's. A
single ``NATSManager`` holds one connection to one URL and cannot express that.

The old design was worse than incomplete: the manager lived in a module-level
singleton that no production code ever set, so ``get_nats_manager()`` returned
``None`` on a fully healthy box and every federation tool answered
"transport unavailable". Phase 1 fixed the caller; this replaces the singleton
with something that can hold both directions at once.

Routing rule, and it follows from who dialled whom:

    direction == "inbound"   the peer consumed our invite, so they dial us
                             -> serve the responder on OUR hub
    direction == "outbound"  we consumed their invite, so we dial them
                             -> open a dedicated client to their endpoint

Attachment requires ``ConnectionState.ACTIVE``. A row alone is not permission
to hold a live subject; activation is the signed handshake completing, which
is what makes the handshake the proof the transport works.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.federation.models import Connection, ConnectionState
from robothor.federation.nats import NATSManager

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Transport keys that carry a secret and must never be logged or repr'd.
_SECRET_KEYS = frozenset({"password", "token", "creds", "nkey_seed", "seed"})


def _redact(transport: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if k in _SECRET_KEYS else v) for k, v in transport.items()}


def _auth_options(transport: dict[str, Any]) -> dict[str, Any]:
    """The credential to present when dialling, drawn from the connection row.

    ``nats.connect(url)`` presented nothing at all. With no ``authorization``
    block on the server that was invisible — every client landed in the same
    global account, which is also how an unauthenticated leafnode ended up with
    reach into JetStream.
    """
    options: dict[str, Any] = {}
    for key in ("user", "password", "token", "creds", "nkey_seed"):
        value = transport.get(key)
        if value:
            options[key] = value
    return options


class FederationTransport:
    """Owns every NATS endpoint this instance holds, keyed by connection."""

    def __init__(
        self,
        hub_url: str = "",
        *,
        manager_factory: Callable[..., Any] = NATSManager,
        hub_options: dict[str, Any] | None = None,
    ) -> None:
        self._hub_url = hub_url
        self._hub_options = dict(hub_options or {})
        self._factory = manager_factory
        self._hub: Any = None
        self._endpoints: dict[str, Any] = {}
        self._inbound: set[str] = set()

    # ── Introspection ────────────────────────────────────────────────

    @property
    def hub(self) -> Any:
        """The endpoint serving inbound peers, or None if none are attached."""
        return self._hub

    def endpoint_for(self, connection_id: str) -> Any:
        """The endpoint carrying this connection, or None if not attached."""
        return self._endpoints.get(connection_id)

    def attached(self) -> list[str]:
        return list(self._endpoints)

    def __repr__(self) -> str:
        return (
            f"FederationTransport(hub={self._hub_url or None!r}, "
            f"attached={sorted(self._endpoints)})"
        )

    # ── Lifecycle ────────────────────────────────────────────────────

    async def attach(
        self, connection: Connection, *, handler: Any = None, pending_ok: bool = False
    ) -> bool:
        """Bring one connection onto the wire.

        Returns False — and attaches nothing — when the connection may not
        carry traffic, when an outbound link names no endpoint, or when the
        broker refuses. Failing to attach is never silent: the caller records
        it so `federation status` can stop claiming `active` for a link whose
        transport is dead.

        ``pending_ok`` admits a PENDING connection so a pairing can complete —
        the parent has to be listening before the child's hello arrives, and
        that hello is what activates it. It is not a weakening: the responder
        serves exactly one op on a pending connection (the handshake) and
        refuses everything else on state. Keeping that restriction in one
        place, rather than half here and half there, is deliberate.

        SUSPENDED is never admitted by either path. Suspending a child is the
        operator's kill switch, and a kill switch that a peer can undo by
        resending its hello is not one.
        """
        allowed = {ConnectionState.ACTIVE}
        if pending_ok:
            allowed.add(ConnectionState.PENDING)
        if connection.state not in allowed:
            logger.warning(
                "Federation: refusing to attach connection %s in state %s",
                connection.id,
                connection.state.value,
            )
            return False

        if connection.direction == "inbound":
            return await self._attach_inbound(connection, handler)
        return await self._attach_outbound(connection)

    async def _attach_inbound(self, connection: Connection, handler: Any) -> bool:
        if handler is None:
            logger.warning(
                "Federation: connection %s is inbound but no responder was "
                "supplied — the peer would reach a subject nobody answers",
                connection.id,
            )
            return False

        if self._hub is None:
            if not self._hub_url:
                logger.warning(
                    "Federation: connection %s is inbound but this instance has "
                    "no hub URL, so there is nothing for the peer to dial",
                    connection.id,
                )
                return False
            self._hub = self._factory(self._hub_url, **self._hub_options)
            if not await self._hub.connect():
                logger.error("Federation: hub %s refused the connection", self._hub_url)
                self._hub = None
                return False

        if not await self._hub.serve_requests(connection.id, handler):
            return False

        self._endpoints[connection.id] = self._hub
        self._inbound.add(connection.id)
        logger.info("Federation: serving inbound connection %s on the hub", connection.id)
        return True

    async def _attach_outbound(self, connection: Connection) -> bool:
        url = (connection.transport or {}).get("url") or connection.peer_endpoint
        if not url:
            logger.warning(
                "Federation: connection %s is outbound but names no endpoint "
                "(transport=%s) — refusing to fall back to the local hub, which "
                "would silently talk to ourselves",
                connection.id,
                _redact(connection.transport or {}),
            )
            return False

        manager = self._factory(url, **_auth_options(connection.transport or {}))
        if not await manager.connect():
            logger.error("Federation: could not dial peer %s at %s", connection.peer_name, url)
            return False

        self._endpoints[connection.id] = manager
        logger.info("Federation: dialled connection %s at %s", connection.id, url)
        return True

    async def detach(self, connection_id: str) -> None:
        """Take one connection off the wire, leaving the others alone."""
        manager = self._endpoints.pop(connection_id, None)
        if manager is None:
            return
        if connection_id in self._inbound:
            self._inbound.discard(connection_id)
            await manager.unsubscribe_all(connection_id)
            # The hub is shared; it closes in close(), not here.
            return
        await manager.disconnect()

    async def close(self) -> None:
        """Drop every endpoint. Safe to call when nothing is attached."""
        for connection_id, manager in list(self._endpoints.items()):
            if connection_id in self._inbound:
                continue
            try:
                await manager.disconnect()
            except Exception as e:  # pragma: no cover - best effort teardown
                logger.warning("Federation: disconnect failed for %s: %s", connection_id, e)
        if self._hub is not None:
            try:
                await self._hub.disconnect()
            except Exception as e:  # pragma: no cover
                logger.warning("Federation: hub disconnect failed: %s", e)
            self._hub = None
        self._endpoints.clear()
        self._inbound.clear()

    # ── Traffic ──────────────────────────────────────────────────────

    async def request(self, connection_id: str, data: bytes, timeout: float = 5.0) -> bytes | None:
        """Send a request on the endpoint that carries this connection."""
        manager = self._endpoints.get(connection_id)
        if manager is None:
            logger.warning(
                "Federation: no endpoint for connection %s — it is not attached",
                connection_id,
            )
            return None
        reply: bytes | None = await manager.request(connection_id, data, timeout=timeout)
        return reply

    def is_attached(self, connection_id: str) -> bool:
        manager = self._endpoints.get(connection_id)
        return manager is not None and bool(manager.is_connected)


# ── Process singleton ─────────────────────────────────────────────────
# Registered inside `_start_federation` so it cannot be forgotten at a call
# site the way `set_nats_manager` was. `test_wiring.py` fails if this loses
# its non-test caller.

_transport: FederationTransport | None = None


def get_transport() -> FederationTransport | None:
    """The live transport, or None when federation is not running."""
    return _transport


def set_transport(transport: FederationTransport | None) -> None:
    global _transport
    _transport = transport
