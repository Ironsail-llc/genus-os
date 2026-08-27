"""The whole feature, against a real nats-server. Nothing here is mocked.

`test_nats_request.py` has passed green since July while federation carried
zero messages, because it mocks `NATSManager` and calls the one function
production never reached. So the governing rule for this feature is that no
test of it may mock `NATSManager` — and this file is where that is honoured:
a real broker process, a real account config, real Ed25519 signatures, and a
real request/reply.

Two claims are proved here, and the second is the one Philip asked for:

  1. a pairing handshake completes over the wire and activates BOTH sides
  2. a child cannot reach anything it was not exported — and the refusal comes
     from the broker, not from the application agreeing to behave

The absence of an error proves nothing. These assert on the presence of the
right error, in nats-server's own log.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from robothor.federation.handshake import verify_ack
from robothor.federation.identity import consume_invite_token, create_invite_token
from robothor.federation.models import ConnectionState, Relationship
from robothor.federation.nats import NATSManager, command_subject
from robothor.federation.nats_config import PeerAccount, render_config
from robothor.federation.transport import FederationTransport

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server not installed"),
]

ENGINE_PASSWORD = "engine-test-pw"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Broker:
    """A real nats-server, with its log available for assertions."""

    def __init__(self, tmp_path: Path, peers: list[PeerAccount]):
        self.port = _free_port()
        self.log_path = tmp_path / "nats-server.log"
        self.config_path = tmp_path / "nats-server.conf"
        self.config_path.write_text(
            render_config(
                listen=f"127.0.0.1:{self.port}",
                engine_password=ENGINE_PASSWORD,
                peers=peers,
            )
        )
        self.proc = subprocess.Popen(
            ["nats-server", "-c", str(self.config_path), "-l", str(self.log_path), "-DV"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_ready()

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self.port}"

    def _wait_ready(self, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"nats-server exited: {self.log()}")
            with socket.socket() as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return
            time.sleep(0.05)
        raise RuntimeError(f"nats-server did not start: {self.log()}")

    def log(self) -> str:
        try:
            return self.log_path.read_text()
        except OSError:
            return ""

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.proc.kill()


@pytest.fixture
def org(tmp_path, parent_config, child_config, saved):
    """A child that invited a parent in — Philip's organisational shape.

    The CHILD issues the invite ("you are my parent"), so the child is the one
    dialled into and the parent is the client. That is what makes Robothor a
    principal inside the subordinate instance rather than the other way round.
    """
    invite = create_invite_token(child_config, relationship=Relationship.CHILD)
    parent_conn = consume_invite_token(parent_config, invite.token)
    child_conn = saved.get(invite.connection_id)

    peer = PeerAccount(invite.connection_id)
    broker = Broker(tmp_path, [peer])
    try:
        yield broker, invite, peer, child_conn, parent_conn
    finally:
        broker.stop()


# ── 1. The handshake actually completes over the wire ────────────────


async def test_a_pairing_handshake_completes_over_a_real_broker(org, parent_config, child_config):
    broker, invite, peer, child_conn, parent_conn = org
    from robothor.engine.federation_responder import make_command_handler
    from robothor.federation.handshake import build_handshake

    assert child_conn.state == ConnectionState.PENDING
    assert parent_conn.state == ConnectionState.PENDING

    # The CHILD listens, in the ENGINE account, as the daemon would.
    listener = FederationTransport(
        hub_url=broker.url,
        hub_options={"user": "engine", "password": ENGINE_PASSWORD},
    )
    # The PARENT dials in, holding only the credential minted for this link.
    parent_conn.transport = {
        "kind": "nats",
        "url": broker.url,
        "user": peer.user,
        "password": peer.password,
    }
    dialer = FederationTransport(hub_url="")

    try:
        assert await listener.attach(
            child_conn,
            handler=make_command_handler(child_conn, None, config=child_config),
            pending_ok=True,
        )
        assert await dialer.attach(parent_conn, pending_ok=True)

        hello = build_handshake(parent_config, parent_conn, invite.connection_secret)
        reply = await dialer.request(parent_conn.id, hello, timeout=5.0)

        assert reply is not None, f"no reply came back. broker log:\n{broker.log()}"
        assert b"error" not in reply, reply
        assert verify_ack(parent_conn, reply)
    finally:
        await listener.close()
        await dialer.close()

    # Both sides activated, and each learned who the other is.
    assert child_conn.state == ConnectionState.ACTIVE, "the listener never activated"
    assert parent_conn.state == ConnectionState.ACTIVE, "the dialer never activated"
    assert child_conn.peer_public_key, "the child never learned its parent's key"
    assert child_conn.activated_at and parent_conn.activated_at


async def test_a_wrong_secret_is_refused_over_the_wire(org, parent_config, child_config):
    """The refusal has to survive the transport, not just the unit test."""
    broker, _invite, peer, child_conn, parent_conn = org
    from robothor.engine.federation_responder import make_command_handler
    from robothor.federation.handshake import build_handshake

    listener = FederationTransport(
        hub_url=broker.url, hub_options={"user": "engine", "password": ENGINE_PASSWORD}
    )
    parent_conn.transport = {
        "kind": "nats",
        "url": broker.url,
        "user": peer.user,
        "password": peer.password,
    }
    dialer = FederationTransport(hub_url="")
    try:
        await listener.attach(
            child_conn,
            handler=make_command_handler(child_conn, None, config=child_config),
            pending_ok=True,
        )
        await dialer.attach(parent_conn, pending_ok=True)

        hello = build_handshake(parent_config, parent_conn, "wrong-secret")
        reply = await dialer.request(parent_conn.id, hello, timeout=5.0)
    finally:
        await listener.close()
        await dialer.close()

    assert reply is not None
    assert "wrong connection secret" in json.loads(reply)["error"]
    assert child_conn.state == ConnectionState.PENDING


# ── 2. The broker refuses what the application never sees ────────────


async def test_the_peer_credential_cannot_reach_another_connections_subject(org):
    """Account isolation, proved by the broker's own refusal.

    A child holding a valid credential for its own link must not be able to
    address a DIFFERENT connection's command subject — that is how one
    subordinate instance would reach another, or reach a sibling's parent.
    """
    broker, invite, peer, _child_conn, _parent_conn = org
    other_subject = command_subject("99999999-9999-9999-9999-999999999999")

    client = NATSManager(broker.url, user=peer.user, password=peer.password)
    assert await client.connect(), f"the peer could not connect at all:\n{broker.log()}"
    try:
        import nats.errors

        with pytest.raises(
            (nats.errors.NoRespondersError, nats.errors.Error, asyncio.TimeoutError)
        ):
            await client._nc.request(other_subject, b"{}", timeout=1.5)
        await asyncio.sleep(0.3)
    finally:
        await client.disconnect()

    # The presence of the right error, not the absence of a wrong one. A silent
    # non-delivery would look exactly like a peer that never tried.
    log = broker.log()
    assert "Permissions Violation" in log, (
        "the broker did not REFUSE the cross-connection request; it may merely "
        "have failed to route it, which leaves no evidence:\n" + log
    )
    assert other_subject in log, "the refusal does not name the subject that was attempted"


async def test_the_peer_credential_cannot_reach_jetstream(org):
    """A peer account has no `jetstream` key, so $JS.API.> is not addressable
    from it. An unauthenticated leafnode used to have exactly this reach."""
    broker, _invite, peer, _child_conn, _parent_conn = org

    client = NATSManager(broker.url, user=peer.user, password=peer.password)
    assert await client.connect()
    try:
        import nats.errors

        with pytest.raises((nats.errors.NoRespondersError, asyncio.TimeoutError)):
            await client._nc.request("$JS.API.STREAM.LIST", b"{}", timeout=1.5)
    finally:
        await client.disconnect()


async def test_the_peer_credential_cannot_subscribe_to_the_engines_traffic(org):
    """Subscribing across accounts must fail. If a child could subscribe to
    `robothor.>` it would see every other connection's traffic without ever
    making a request the application could audit."""
    broker, _invite, peer, _child_conn, _parent_conn = org

    client = NATSManager(broker.url, user=peer.user, password=peer.password)
    assert await client.connect()
    received: list[bytes] = []
    try:

        async def _collect(msg):
            received.append(msg.data)

        await client._nc.subscribe("robothor.>", cb=_collect)
        await client._nc.flush(timeout=2)

        engine = NATSManager(broker.url, user="engine", password=ENGINE_PASSWORD)
        assert await engine.connect()
        try:
            await engine._nc.publish("robothor.secret.channel", b"engine-only")
            await engine._nc.flush(timeout=2)
            await asyncio.sleep(0.3)
        finally:
            await engine.disconnect()
    finally:
        await client.disconnect()

    assert received == [], "a peer account received the engine account's traffic"


async def test_a_bad_credential_is_refused_by_the_broker(org):
    """`nats.connect(url)` used to present nothing at all, which was invisible
    while the server had no authorization block."""
    broker, _invite, peer, _child_conn, _parent_conn = org

    client = NATSManager(
        broker.url, user=peer.user, password="not-the-password", allow_reconnect=False
    )
    assert await client.connect() is False
    assert "Authorization Violation" in broker.log()


async def test_no_credential_at_all_is_refused(org):
    broker, _invite, _peer, _child_conn, _parent_conn = org
    client = NATSManager(broker.url, allow_reconnect=False)
    assert await client.connect() is False
