"""Activation is a signed round-trip, not a database write.

`ConnectionManager.activate()` had zero production callers, so every connection
stayed PENDING forever and every op was refused. The obvious fix — call
`activate()` somewhere — would have produced the opposite failure: connections
marked ACTIVE whose transport had never carried a byte, which is precisely what
`federation status` has been printing.

So activation is defined as the handshake completing over the real transport.
If the wire does not work, nothing activates, and the operator finds out at
pairing time rather than five months later.

What the handshake has to establish, given the invite already proves the
issuer's identity to the consumer:

  - the peer holds the connection secret (they were invited, not guessing ids)
  - the peer holds the private key matching the public key they present
  - the invite has not expired
  - the peer is bound to THIS connection id, not replaying another one
"""

from __future__ import annotations

import base64
import json

import pytest

from robothor.federation.handshake import (
    HandshakeError,
    build_ack,
    build_handshake,
    verify_ack,
    verify_handshake,
)
from robothor.federation.identity import (
    consume_invite_token,
    create_invite_token,
    get_identity,
)
from robothor.federation.models import ConnectionState, Relationship


@pytest.fixture
def paired(parent_config, child_config, saved):
    """A parent that issued an invite and a child that consumed it."""
    invite = create_invite_token(parent_config, relationship=Relationship.PARENT)
    child_conn = consume_invite_token(child_config, invite.token)
    parent_conn = saved.get(invite.connection_id)
    return invite, parent_conn, child_conn


# ── The happy path ───────────────────────────────────────────────────


def test_a_valid_handshake_activates_the_parent_side(parent_config, child_config, paired):
    invite, parent_conn, child_conn = paired
    hello = build_handshake(child_config, child_conn, invite.connection_secret)

    result = verify_handshake(parent_config, parent_conn, hello)

    assert result.ok
    assert parent_conn.state == ConnectionState.ACTIVE
    assert parent_conn.activated_at, "activation must record when"


def test_the_handshake_teaches_the_parent_who_its_child_is(parent_config, child_config, paired):
    """The parent issued the invite before it knew who would redeem it, so its
    row has an empty peer_id and no peer key. The handshake is where it learns."""
    invite, parent_conn, child_conn = paired
    assert parent_conn.peer_id == "" and parent_conn.peer_public_key == ""

    verify_handshake(
        parent_config,
        parent_conn,
        build_handshake(child_config, child_conn, invite.connection_secret),
    )

    child_identity = get_identity(child_config)
    assert parent_conn.peer_id == child_identity.id
    assert parent_conn.peer_public_key == child_identity.public_key
    assert parent_conn.peer_name == child_identity.display_name


def test_the_ack_activates_the_child_side(parent_config, child_config, paired):
    invite, parent_conn, child_conn = paired
    verify_handshake(
        parent_config,
        parent_conn,
        build_handshake(child_config, child_conn, invite.connection_secret),
    )

    ack = build_ack(parent_config, parent_conn)
    assert verify_ack(child_conn, ack)
    assert child_conn.state == ConnectionState.ACTIVE
    assert child_conn.activated_at


# ── Refusals ─────────────────────────────────────────────────────────


def test_a_wrong_secret_is_refused(parent_config, child_config, paired):
    _invite, parent_conn, child_conn = paired
    hello = build_handshake(child_config, child_conn, "not-the-secret")

    with pytest.raises(HandshakeError, match="secret"):
        verify_handshake(parent_config, parent_conn, hello)
    assert parent_conn.state == ConnectionState.PENDING


def test_a_tampered_handshake_is_refused(parent_config, child_config, paired):
    """The signature covers the connection id and the peer's identity, so a
    peer cannot claim a different instance id than the key it holds."""
    invite, parent_conn, child_conn = paired
    hello = json.loads(build_handshake(child_config, child_conn, invite.connection_secret))
    payload = json.loads(hello["payload_json"])
    payload["instance_id"] = "00000000-0000-0000-0000-000000000000"
    hello["payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    with pytest.raises(HandshakeError, match="signature"):
        verify_handshake(parent_config, parent_conn, json.dumps(hello).encode())


def test_a_handshake_for_a_different_connection_is_refused(parent_config, child_config, paired):
    """A child holding one valid invite must not be able to redeem it against
    another connection id it happens to learn."""
    invite, parent_conn, child_conn = paired
    other = create_invite_token(parent_config, relationship=Relationship.PARENT)
    hello = build_handshake(child_config, child_conn, invite.connection_secret)

    parent_conn.id = other.connection_id  # the parent looks up a DIFFERENT row
    with pytest.raises(HandshakeError, match="connection"):
        verify_handshake(parent_config, parent_conn, hello)


def test_an_expired_invite_cannot_be_redeemed(parent_config, child_config, paired):
    invite, parent_conn, child_conn = paired
    parent_conn.metadata["invite_expires_at"] = "2020-01-01T00:00:00+00:00"
    hello = build_handshake(child_config, child_conn, invite.connection_secret)

    with pytest.raises(HandshakeError, match="expire"):
        verify_handshake(parent_config, parent_conn, hello)


def test_a_second_handshake_cannot_swap_the_peers_key(parent_config, child_config, paired):
    """Key substitution. Once a connection is bound to a public key, a later
    handshake presenting a different key must be refused — otherwise anyone who
    learns the (single-use) secret can take over an established link."""
    invite, parent_conn, child_conn = paired
    verify_handshake(
        parent_config,
        parent_conn,
        build_handshake(child_config, child_conn, invite.connection_secret),
    )
    original_key = parent_conn.peer_public_key

    import tempfile
    from pathlib import Path

    from robothor.federation.config import FederationConfig
    from robothor.federation.identity import init_identity

    with tempfile.TemporaryDirectory() as tmp:
        impostor_dir = Path(tmp) / "impostor"
        impostor_dir.mkdir()
        impostor = FederationConfig(
            config_dir=impostor_dir, identity_file=impostor_dir / "identity.json"
        )
        init_identity(impostor, display_name="impostor")
        hello = build_handshake(impostor, child_conn, invite.connection_secret)

        with pytest.raises(HandshakeError, match="key|bound"):
            verify_handshake(parent_config, parent_conn, hello)

    assert parent_conn.peer_public_key == original_key


def test_an_ack_from_the_wrong_instance_is_refused(parent_config, child_config, paired):
    """The child already knows its parent's public key from the invite. An ack
    that does not verify against it is an impostor on the parent's subject."""
    invite, parent_conn, child_conn = paired
    verify_handshake(
        parent_config,
        parent_conn,
        build_handshake(child_config, child_conn, invite.connection_secret),
    )

    ack = json.loads(build_ack(parent_config, parent_conn))
    ack["signature"] = base64.b64encode(b"\x00" * 64).decode()

    assert verify_ack(child_conn, json.dumps(ack).encode()) is False
    assert child_conn.state == ConnectionState.PENDING


def test_garbage_is_refused_without_raising_something_unhelpful(parent_config, paired):
    _invite, parent_conn, _child_conn = paired
    with pytest.raises(HandshakeError):
        verify_handshake(parent_config, parent_conn, b"not json at all")


# ── The handshake grants nothing on its own ──────────────────────────


def test_activation_does_not_widen_the_exports(parent_config, child_config, paired):
    """A successful handshake proves identity. It must not be an opportunity
    for the peer to ask for capabilities."""
    invite, parent_conn, child_conn = paired
    before = list(parent_conn.exports)

    hello = json.loads(build_handshake(child_config, child_conn, invite.connection_secret))
    payload = json.loads(hello["payload_json"])
    payload["exports"] = ["trigger_agent", "push_config"]
    # Re-sign so the request is otherwise valid: the point is that a
    # well-formed, correctly-signed request still cannot widen the grant.
    from robothor.federation.handshake import _sign

    hello["payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    hello["signature"] = _sign(child_config, hello["payload_json"])

    verify_handshake(parent_config, parent_conn, json.dumps(hello).encode())

    assert parent_conn.exports == before, "the peer negotiated its own capabilities"
    assert "trigger_agent" not in parent_conn.exports
