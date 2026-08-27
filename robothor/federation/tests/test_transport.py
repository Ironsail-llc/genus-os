"""One instance is a hub for its children AND a client of its parent.

A single `NATSManager` cannot express that: it holds one connection to one
URL. The old code had exactly one, reached through a module-level singleton
that nothing in production ever set, so:

  - a parent could never dial its own parent, and
  - the singleton was None forever, so every federation tool returned
    "federation transport unavailable" no matter how healthy NATS was.

`FederationTransport` owns one endpoint per direction and routes by
connection. These tests use fakes for the broker itself — the real-broker
proof is `test_transport_real_broker.py`, which is where the rule "no test in
this feature may mock NATSManager" is honoured. Here NATSManager is the thing
being routed TO, not the thing under test.
"""

from __future__ import annotations

import pytest

from robothor.federation.models import Connection, ConnectionState, Relationship
from robothor.federation.transport import FederationTransport


class FakeManager:
    def __init__(self, url="nats://fake:4222", **options):
        self.url = url
        self.options = options
        self.connected = False
        self.served: list[str] = []
        self.requests: list[tuple[str, bytes]] = []

    async def connect(self):
        self.connected = True
        return True

    async def disconnect(self):
        self.connected = False

    @property
    def is_connected(self):
        return self.connected

    async def serve_requests(self, connection_id, handler):
        self.served.append(connection_id)
        return True

    async def request(self, connection_id, data, timeout=5.0):
        self.requests.append((connection_id, data))
        return b'{"ok": true}'

    async def unsubscribe_all(self, connection_id):
        self.served = [c for c in self.served if c != connection_id]


@pytest.fixture
def factory():
    made: list[FakeManager] = []

    def _make(url, **options):
        m = FakeManager(url, **options)
        made.append(m)
        return m

    _make.made = made  # type: ignore[attr-defined]
    return _make


def _conn(direction, *, state=ConnectionState.ACTIVE, url="nats://peer:4222", **kw):
    return Connection(
        id=kw.pop("id", "c1"),
        peer_id="p1",
        peer_name="peer",
        relationship=Relationship.CHILD if direction == "outbound" else Relationship.PARENT,
        state=state,
        direction=direction,
        transport={"kind": "nats", "url": url, **kw.pop("transport", {})},
        **kw,
    )


# ── Routing ──────────────────────────────────────────────────────────


async def test_an_inbound_connection_is_served_on_the_hub(factory):
    """The peer dials us, so we listen on our own broker."""
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(_conn("inbound"), handler=lambda data: b"{}")

    hub = t.hub
    assert hub is not None and hub.url == "nats://127.0.0.1:4222"
    assert hub.served == ["c1"], "the responder was not registered on the hub"
    assert len(factory.made) == 1, "an inbound link must not open an outbound client"


async def test_an_outbound_connection_dials_the_peer(factory):
    """We consumed their invite, so we are the client."""
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(_conn("outbound", url="nats://parent.example:4222"), handler=None)

    assert t.hub is None, "an outbound-only instance must not open a hub"
    mgr = t.endpoint_for("c1")
    assert mgr is not None and mgr.url == "nats://parent.example:4222"


async def test_one_instance_holds_both_directions_at_once(factory):
    """This is the case a single NATSManager could not represent, and it is
    the whole point of an organisational hierarchy more than two levels deep:
    a middle instance is a child upward and a parent downward."""
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(_conn("inbound", id="down"), handler=lambda data: b"{}")
    await t.attach(_conn("outbound", id="up", url="nats://parent:4222"), handler=None)

    assert t.hub is not None
    assert t.endpoint_for("up").url == "nats://parent:4222"
    assert t.endpoint_for("down") is t.hub


async def test_every_outbound_peer_gets_its_own_endpoint(factory):
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(_conn("outbound", id="a", url="nats://a:4222"), handler=None)
    await t.attach(_conn("outbound", id="b", url="nats://b:4222"), handler=None)

    assert t.endpoint_for("a") is not t.endpoint_for("b")


# ── Credentials ──────────────────────────────────────────────────────


async def test_the_connection_credential_is_presented_when_dialling(factory):
    """`nats.connect(url)` presented nothing at all — with no `authorization`
    block on the server that was invisible, because everything landed in the
    same global account."""
    conn = _conn("outbound", url="nats://parent:4222")
    conn.transport["user"] = "fed_c1"
    conn.transport["password"] = "s3cret"
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(conn, handler=None)

    opts = t.endpoint_for("c1").options
    assert opts.get("user") == "fed_c1"
    assert opts.get("password") == "s3cret"


async def test_a_credential_never_appears_in_the_repr(factory):
    conn = _conn("outbound")
    conn.transport["password"] = "s3cret"
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(conn, handler=None)

    assert "s3cret" not in repr(t)


# ── Refusals ─────────────────────────────────────────────────────────


async def test_a_pending_connection_is_not_attached_by_default(factory):
    """Activation is the handshake completing. Attaching on the strength of a
    database row alone would make activation paperwork."""
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    ok = await t.attach(_conn("inbound", state=ConnectionState.PENDING), handler=lambda d: b"{}")

    assert ok is False
    assert t.endpoint_for("c1") is None


async def test_a_pending_connection_can_be_attached_for_pairing(factory):
    """The parent must be listening before the child's hello arrives, because
    that hello is what activates the connection. The responder is what keeps
    the exposure to a single op."""
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    ok = await t.attach(
        _conn("inbound", state=ConnectionState.PENDING), handler=lambda d: b"{}", pending_ok=True
    )

    assert ok is True
    assert t.endpoint_for("c1") is t.hub


async def test_a_suspended_connection_is_not_attached(factory):
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    ok = await t.attach(_conn("inbound", state=ConnectionState.SUSPENDED), handler=lambda d: b"{}")

    assert ok is False


async def test_pairing_mode_still_refuses_a_suspended_connection(factory):
    """The kill switch must not be undone by whatever flag pairing needs."""
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    ok = await t.attach(
        _conn("inbound", state=ConnectionState.SUSPENDED),
        handler=lambda d: b"{}",
        pending_ok=True,
    )

    assert ok is False


async def test_an_outbound_connection_with_no_url_is_refused(factory):
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    conn = _conn("outbound")
    conn.transport = {"kind": "nats"}
    ok = await t.attach(conn, handler=None)

    assert ok is False, "dialling nowhere must fail loudly, not connect to the hub"


async def test_detaching_closes_only_that_peers_endpoint(factory):
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(_conn("outbound", id="a", url="nats://a:4222"), handler=None)
    await t.attach(_conn("outbound", id="b", url="nats://b:4222"), handler=None)
    b = t.endpoint_for("b")

    await t.detach("a")

    assert t.endpoint_for("a") is None
    assert t.endpoint_for("b") is b and b.is_connected


async def test_detaching_an_inbound_peer_stops_serving_it_but_keeps_the_hub(factory):
    """Suspending one child must not take every other child off the wire.

    The hub is shared by every inbound peer, so detaching one has to
    unsubscribe that connection's subject rather than close the connection.
    """
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(_conn("inbound", id="a"), handler=lambda d: b"{}")
    await t.attach(_conn("inbound", id="b"), handler=lambda d: b"{}")
    hub = t.hub

    await t.detach("a")

    assert t.endpoint_for("a") is None
    assert t.endpoint_for("b") is hub
    assert hub.is_connected, "detaching one child closed the hub for all of them"
    assert hub.served == ["b"]


async def test_close_disconnects_every_endpoint(factory):
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(_conn("inbound", id="down"), handler=lambda d: b"{}")
    await t.attach(_conn("outbound", id="up", url="nats://up:4222"), handler=None)

    await t.close()

    assert all(not m.is_connected for m in factory.made)
    assert t.endpoint_for("up") is None and t.hub is None


# ── Requests route to the right endpoint ─────────────────────────────


async def test_a_request_goes_out_on_that_connections_endpoint(factory):
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    await t.attach(_conn("outbound", id="up", url="nats://up:4222"), handler=None)

    reply = await t.request("up", b'{"op":"health"}')

    assert reply == b'{"ok": true}'
    assert t.endpoint_for("up").requests == [("up", b'{"op":"health"}')]


async def test_a_request_on_an_unknown_connection_returns_none(factory):
    t = FederationTransport(hub_url="nats://127.0.0.1:4222", manager_factory=factory)
    assert await t.request("nope", b"{}") is None
