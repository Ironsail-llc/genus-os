"""Pairing must produce one connection_id that both sides agree on.

The v1 token carried no connection id at all. `create_invite_token` wrote no
database row, and `consume_invite_token` minted its own `uuid4()`. Because the
NATS subject is `robothor.{connection_id}.command`, the child subscribed on an
id the parent had never heard of, and the parent — having persisted nothing —
loaded zero connections and served no responder.

That is why federation has been silent since 2026-03-09. Fixing the transport
alone would not have produced a single message: the two sides were not talking
about the same connection.
"""

from __future__ import annotations

import base64
import json

import pytest

from robothor.federation.identity import (
    consume_invite_token,
    create_invite_token,
    decode_invite_token,
)
from robothor.federation.models import (
    ConnectionState,
    Relationship,
    principal_role_for_peer,
)


def _payload(token_str: str) -> dict:
    bundle = json.loads(base64.urlsafe_b64decode(token_str))
    return json.loads(bundle["payload_json"])


# ── The shared id ────────────────────────────────────────────────────


def test_the_token_carries_a_connection_id(parent_config):
    invite = create_invite_token(parent_config, relationship=Relationship.PARENT)
    assert invite.connection_id, "token has no connection id — the subject cannot match"
    assert _payload(invite.token)["connection_id"] == invite.connection_id


def test_both_sides_end_up_on_the_same_connection_id(parent_config, child_config):
    invite = create_invite_token(parent_config, relationship=Relationship.PARENT)
    child_conn = consume_invite_token(child_config, invite.token)

    assert child_conn.id == invite.connection_id, (
        "the child minted its own id — it will subscribe on "
        f"robothor.{child_conn.id}.command while the parent serves "
        f"robothor.{invite.connection_id}.command, and neither ever hears the other"
    )


def test_the_issuer_persists_its_side_before_handing_out_the_token(parent_config, saved):
    """A token whose row does not exist is a token that can never be redeemed:
    the parent loads its connections at boot and would find nothing to serve."""
    invite = create_invite_token(parent_config, relationship=Relationship.PARENT)

    row = saved.get(invite.connection_id)
    assert row is not None, "issuing an invite persisted nothing on the issuer side"
    assert row.state == ConnectionState.PENDING
    assert row.relationship == Relationship.PARENT
    assert row.direction == "inbound", "the peer will dial us, so the link is inbound"


# ── The principal each side grants the other ─────────────────────────


def test_the_parent_grants_its_child_the_deny_all_role(parent_config, saved):
    invite = create_invite_token(parent_config, relationship=Relationship.PARENT)
    row = saved.get(invite.connection_id)

    assert row.local_principal_role == "federation_child", (
        "the child's principal on the parent must be the deny-all role; "
        f"got {row.local_principal_role!r}"
    )
    assert row.local_principal_id == f"federation:{invite.connection_id}"


def test_the_child_grants_its_parent_the_read_only_role(parent_config, child_config):
    invite = create_invite_token(parent_config, relationship=Relationship.PARENT)
    child_conn = consume_invite_token(child_config, invite.token)

    assert child_conn.relationship == Relationship.CHILD
    assert child_conn.local_principal_role == "federation_parent"
    assert child_conn.local_principal_id == f"federation:{invite.connection_id}"


def test_the_role_mapping_is_the_inverse_of_the_relationship():
    """`local_principal_role` is the role the PEER acts as here — so it is
    derived from OUR relationship, inverted."""
    assert principal_role_for_peer(Relationship.PARENT) == "federation_child"
    assert principal_role_for_peer(Relationship.CHILD) == "federation_parent"


def test_a_peer_link_grants_no_elevated_role():
    """PEER is not in Philip's org model. It must not silently become a parent."""
    assert principal_role_for_peer(Relationship.PEER) == "federation_child"


# ── Version discipline ───────────────────────────────────────────────


def test_a_v1_token_is_refused_rather_than_silently_paired(parent_config, child_config):
    """A v1 token has no connection_id. Accepting it would recreate the exact
    silent mismatch this change exists to remove, so it must fail loudly."""
    invite = create_invite_token(parent_config, relationship=Relationship.PARENT)
    bundle = json.loads(base64.urlsafe_b64decode(invite.token))
    payload = json.loads(bundle["payload_json"])
    payload["v"] = 1
    payload.pop("connection_id", None)
    bundle["payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    downgraded = base64.urlsafe_b64encode(json.dumps(bundle).encode()).decode()

    with pytest.raises(ValueError, match="version|connection_id"):
        consume_invite_token(child_config, downgraded, trust=True)


def test_the_connection_id_is_covered_by_the_signature(parent_config, child_config):
    """If the id were outside the signed payload, anyone could repoint a token
    at a connection the issuer never created."""
    invite = create_invite_token(parent_config, relationship=Relationship.PARENT)
    bundle = json.loads(base64.urlsafe_b64decode(invite.token))
    payload = json.loads(bundle["payload_json"])
    payload["connection_id"] = "00000000-0000-0000-0000-000000000000"
    bundle["payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    tampered = base64.urlsafe_b64encode(json.dumps(bundle).encode()).decode()

    with pytest.raises(ValueError, match="signature"):
        decode_invite_token(tampered)
