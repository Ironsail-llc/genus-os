"""Activation by signed round-trip over the real transport.

`ConnectionManager.activate()` had no production caller, so every connection
stayed PENDING and every op was refused. Adding a call somewhere would have
produced the opposite defect — rows marked ACTIVE whose transport had never
carried a byte, which is exactly what `federation status` reports today.

So activation *is* the handshake:

    child  --hello-->  parent      hello proves: holds the connection secret,
                                   holds the private key for the public key it
                                   presents, and is bound to THIS connection id
    child  <--ack---   parent      ack proves the parent is the instance whose
                                   public key was in the invite

Neither side flips to ACTIVE without a verified message from the other, so a
broken wire leaves both sides PENDING and visible. The invite already proves
the *issuer's* identity to the consumer, which is why the hello carries the
secret and the ack does not need to.

What the handshake deliberately does NOT do: negotiate capabilities. A peer
that could ask for its own exports would make every other gate advisory. The
grant is whatever the operator configured when issuing the invite.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from robothor.federation.identity import _load_private_key, get_identity
from robothor.federation.models import Connection, ConnectionState

if TYPE_CHECKING:
    from robothor.federation.config import FederationConfig

logger = logging.getLogger(__name__)

HANDSHAKE_OP = "handshake"
HANDSHAKE_VERSION = 1


class HandshakeError(Exception):
    """A handshake was refused. The message is safe to log; it never contains
    the secret or a key."""


@dataclass
class HandshakeResult:
    ok: bool
    peer_id: str = ""
    peer_name: str = ""


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _sign(config: FederationConfig, payload_json: str) -> str:
    identity = get_identity(config)
    if not identity:
        raise HandshakeError("instance identity not initialised")
    private_key = _load_private_key(identity)
    return base64.b64encode(private_key.sign(payload_json.encode())).decode()


def _verify_signature(public_pem: str, payload_json: str, signature_b64: str) -> None:
    try:
        public_key = serialization.load_pem_public_key(public_pem.encode())
        if not isinstance(public_key, Ed25519PublicKey):
            raise HandshakeError("peer key is not Ed25519")
        public_key.verify(base64.b64decode(signature_b64), payload_json.encode())
    except HandshakeError:
        raise
    except Exception as exc:
        raise HandshakeError("handshake signature verification failed") from exc


def _unwrap(raw: bytes) -> tuple[dict[str, Any], str, str]:
    """(payload, canonical_json, signature) or a HandshakeError."""
    try:
        envelope = json.loads(raw)
        payload_json = envelope["payload_json"]
        signature = envelope["signature"]
        payload = json.loads(payload_json)
    except HandshakeError:
        raise
    except Exception as exc:
        raise HandshakeError("malformed handshake envelope") from exc
    if not isinstance(payload, dict):
        raise HandshakeError("malformed handshake payload")
    return payload, payload_json, signature


# ── hello: child → parent ────────────────────────────────────────────


def build_handshake(config: FederationConfig, connection: Connection, secret: str) -> bytes:
    """The consumer's opening message, signed with its own private key."""
    identity = get_identity(config)
    if not identity:
        raise HandshakeError("instance identity not initialised")

    payload = {
        "v": HANDSHAKE_VERSION,
        "op": HANDSHAKE_OP,
        "connection_id": connection.id,
        "instance_id": identity.id,
        "instance_name": identity.display_name,
        "public_key": identity.public_key,
        # Proof of possession that is not the secret itself, bound to this
        # connection id so it cannot be replayed against another invite.
        "secret_proof": secret_proof(secret, connection.id),
        "sent_at": datetime.now(UTC).isoformat(),
    }
    payload_json = _canonical(payload)
    # `op` appears twice deliberately: once inside the signed payload, where it
    # is covered by the signature, and once on the envelope, where the
    # responder can route on it without parsing anything it has not yet
    # verified. Only the signed copy is trusted for anything.
    return json.dumps(
        {
            "op": HANDSHAKE_OP,
            "payload_json": payload_json,
            "signature": _sign(config, payload_json),
        }
    ).encode()


def verify_handshake(
    config: FederationConfig, connection: Connection, raw: bytes
) -> HandshakeResult:
    """Verify a peer's hello and, on success, activate ``connection`` in place.

    Raises HandshakeError on every refusal — the caller turns that into a reply
    the peer can see, and a log line the operator can find. Silence is what
    this feature has had for five months; it is not an acceptable outcome.
    """
    payload, payload_json, signature = _unwrap(raw)

    if payload.get("v") != HANDSHAKE_VERSION:
        raise HandshakeError(f"unsupported handshake version: {payload.get('v')}")

    if payload.get("connection_id") != connection.id:
        raise HandshakeError("handshake is for a different connection")

    public_key = payload.get("public_key") or ""
    if not public_key:
        raise HandshakeError("handshake presented no public key")

    # Signature first: everything below trusts fields inside the payload.
    _verify_signature(public_key, payload_json, signature)

    # Key substitution. Once a connection is bound to a key, a later handshake
    # presenting a different one is an attempt to take the link over — the
    # single-use secret is not enough to re-key an established connection.
    if connection.peer_public_key and connection.peer_public_key != public_key:
        raise HandshakeError("connection is already bound to a different peer key")

    expires_at = (connection.metadata or {}).get("invite_expires_at")
    if expires_at:
        try:
            if datetime.now(UTC) > datetime.fromisoformat(expires_at):
                raise HandshakeError("invite has expired")
        except HandshakeError:
            raise
        except Exception:
            raise HandshakeError("invite has an unreadable expiry") from None

    expected = (connection.metadata or {}).get("secret_proof_hash")
    if not expected:
        raise HandshakeError("connection has no secret on file — reissue the invite")
    if not _proof_matches(payload.get("secret_proof") or "", expected):
        raise HandshakeError("handshake presented the wrong connection secret")

    # Everything below this line is the only state the peer gets to write, and
    # `exports` is deliberately not among it.
    connection.peer_id = payload.get("instance_id", "")
    connection.peer_name = payload.get("instance_name", "")
    connection.peer_public_key = public_key
    connection.state = ConnectionState.ACTIVE
    now = datetime.now(UTC).isoformat()
    connection.activated_at = connection.activated_at or now
    connection.last_seen_at = now
    connection.last_error = ""
    connection.updated_at = now

    logger.info(
        "Federation: connection %s activated by handshake from %s",
        connection.id,
        connection.peer_name or connection.peer_id,
    )
    return HandshakeResult(ok=True, peer_id=connection.peer_id, peer_name=connection.peer_name)


def secret_proof(secret: str, connection_id: str) -> str:
    """What the consumer presents: HMAC(secret, connection_id).

    Bound to the connection id so a proof captured from one pairing cannot be
    replayed against another invite from the same issuer.
    """
    return hmac.new(secret.encode(), connection_id.encode(), hashlib.sha256).hexdigest()


def secret_proof_hash(secret: str, connection_id: str) -> str:
    """What the ISSUER stores: sha256 of the proof it expects.

    The issuer minted the secret, so it can precompute this at invite time and
    then forget the secret entirely. A dump of `federation_connections`
    therefore yields nothing an attacker can present — the stored value is one
    hash further along than the thing the wire carries.
    """
    return hashlib.sha256(secret_proof(secret, connection_id).encode()).hexdigest()


def _proof_matches(proof: str, expected_hash: str) -> bool:
    if not proof:
        return False
    return hmac.compare_digest(hashlib.sha256(proof.encode()).hexdigest(), expected_hash)


# ── ack: parent → child ──────────────────────────────────────────────


def build_ack(config: FederationConfig, connection: Connection) -> bytes:
    """The issuer's reply, signed with the key the invite already published."""
    identity = get_identity(config)
    if not identity:
        raise HandshakeError("instance identity not initialised")

    payload = {
        "v": HANDSHAKE_VERSION,
        "op": f"{HANDSHAKE_OP}_ack",
        "connection_id": connection.id,
        "instance_id": identity.id,
        "instance_name": identity.display_name,
        # What the peer is actually allowed to do, so the child can show it
        # without asking. Informational: the parent enforces its own copy.
        "granted": list(connection.exports),
        "sent_at": datetime.now(UTC).isoformat(),
    }
    payload_json = _canonical(payload)
    return json.dumps(
        {
            "op": f"{HANDSHAKE_OP}_ack",
            "payload_json": payload_json,
            "signature": _sign(config, payload_json),
        }
    ).encode()


def verify_ack(connection: Connection, raw: bytes) -> bool:
    """Verify the issuer's ack against the public key from the invite.

    Returns False rather than raising: the child is a client here, and a failed
    ack means "stay PENDING and retry", not "crash the daemon".
    """
    try:
        payload, payload_json, signature = _unwrap(raw)
    except HandshakeError as e:
        logger.warning("Federation: ack rejected — %s", e)
        return False

    if payload.get("connection_id") != connection.id:
        logger.warning("Federation: ack is for a different connection")
        return False

    if not connection.peer_public_key:
        logger.warning("Federation: no peer key on file to verify the ack against")
        return False

    try:
        _verify_signature(connection.peer_public_key, payload_json, signature)
    except HandshakeError as e:
        logger.warning("Federation: ack rejected — %s", e)
        return False

    now = datetime.now(UTC).isoformat()
    connection.state = ConnectionState.ACTIVE
    connection.activated_at = connection.activated_at or now
    connection.last_seen_at = now
    connection.last_error = ""
    connection.updated_at = now
    logger.info(
        "Federation: connection %s activated by ack from %s", connection.id, connection.peer_name
    )
    return True
