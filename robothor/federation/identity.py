"""Instance identity — Ed25519 keypair generation, token creation/consumption."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from robothor.constants import DEFAULT_TENANT
from robothor.federation.config import FederationConfig, load_identity, save_identity
from robothor.federation.connections import save_connection
from robothor.federation.models import (
    Connection,
    ConnectionState,
    Instance,
    InviteToken,
    Relationship,
    default_exports_for,
    principal_role_for_peer,
)

logger = logging.getLogger(__name__)


def generate_keypair() -> tuple[str, str]:
    """Generate an Ed25519 keypair. Returns (public_pem, private_pem)."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    public_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    return public_pem, private_pem


def init_identity(
    config: FederationConfig,
    display_name: str = "",
) -> Instance:
    """Generate and persist this instance's identity (idempotent).

    If identity already exists, returns the existing one.
    """
    existing = load_identity(config)
    if existing:
        return Instance(
            id=existing["id"],
            display_name=existing["display_name"],
            public_key=existing["public_key"],
            private_key_ref=existing.get("private_key_ref", ""),
            created_at=existing.get("created_at", ""),
        )

    instance_id = str(uuid.uuid4())
    public_pem, private_pem = generate_keypair()

    # Store private key as a file in the config dir (SOPS-encrypted in production)
    private_key_path = config.config_dir / "identity.key"
    private_key_path.write_text(private_pem)
    private_key_path.chmod(0o600)

    now = datetime.now(UTC).isoformat()
    identity_data: dict[str, Any] = {
        "id": instance_id,
        "display_name": display_name or f"robothor-{instance_id[:8]}",
        "public_key": public_pem,
        "private_key_ref": str(private_key_path),
        "created_at": now,
    }
    save_identity(config, identity_data)

    return Instance(
        id=instance_id,
        display_name=identity_data["display_name"],
        public_key=public_pem,
        private_key_ref=str(private_key_path),
        created_at=now,
    )


def get_identity(config: FederationConfig) -> Instance | None:
    """Load the existing identity, or None if not initialized."""
    data = load_identity(config)
    if not data:
        return None
    return Instance(
        id=data["id"],
        display_name=data["display_name"],
        public_key=data["public_key"],
        private_key_ref=data.get("private_key_ref", ""),
        created_at=data.get("created_at", ""),
    )


def _load_private_key(instance: Instance) -> Ed25519PrivateKey:
    """Load the private key from the reference path."""
    from pathlib import Path

    key_path = Path(instance.private_key_ref)
    if not key_path.exists():
        raise FileNotFoundError(f"Private key not found: {key_path}")

    private_pem = key_path.read_text().encode()
    return serialization.load_pem_private_key(private_pem, password=None)  # type: ignore[return-value]


def _secret_proof_hash(secret: str, connection_id: str) -> str:
    # Imported lazily: handshake imports identity, so a module-level import
    # here would be a cycle.
    from robothor.federation.handshake import secret_proof_hash

    return secret_proof_hash(secret, connection_id)


def create_invite_token(
    config: FederationConfig,
    relationship: Relationship = Relationship.PEER,
    ttl_hours: int = 24,
    tenant_id: str = "",
) -> InviteToken:
    """Generate a one-time invite token for connection establishment.

    The token contains this instance's endpoint, public key, and a shared
    connection secret. It's base64-encoded for easy transfer.
    """
    identity = get_identity(config)
    if not identity:
        raise RuntimeError("Instance identity not initialized. Run `robothor federation init`.")

    connection_secret = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    expires = now + timedelta(hours=ttl_hours)

    # The issuer mints the connection id, and BOTH sides adopt it. The NATS
    # subject is robothor.{connection_id}.command, so if each side minted its
    # own the child would subscribe on a subject the parent never serves. That
    # is exactly what v1 did, and it is why this feature has been silent since
    # 2026-03-09 without ever logging an error.
    connection_id = str(uuid.uuid4())

    # The credential this peer will present to our broker, minted here so the
    # signed token carries it. Without this the operator hands over a token and
    # the peer still has to be told which broker to dial and what password to
    # use — so the credential travels by chat message instead of inside the
    # signed blob designed to carry exactly this.
    from robothor.federation.provisioning import peer_credentials

    pending = Connection(id=connection_id, transport={})
    credentials = peer_credentials(pending)
    transport: dict[str, Any] = {
        "kind": "nats",
        "url": config.public_endpoint or config.nats_url,
        **credentials,
    }

    token_data = {
        "v": 2,  # token format version
        "connection_id": connection_id,
        "transport": transport,
        "issuer_id": identity.id,
        "issuer_name": identity.display_name,
        "issuer_endpoint": config.public_endpoint,
        "issuer_public_key": identity.public_key,
        "relationship": relationship.value,
        "connection_secret": connection_secret,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    }

    # Sign the canonical JSON bytes (this exact string is what gets verified)
    private_key = _load_private_key(identity)
    payload_json = json.dumps(token_data, sort_keys=True, separators=(",", ":"))
    signature = private_key.sign(payload_json.encode())

    # Bundle the canonical JSON string (not the dict) + signature
    # This preserves the exact bytes that were signed
    bundle = {
        "payload_json": payload_json,
        "signature": base64.b64encode(signature).decode(),
    }
    token_str = base64.urlsafe_b64encode(json.dumps(bundle).encode()).decode()

    # Persist OUR side before the token leaves the building. An invite whose row
    # does not exist can never be redeemed: `load_connections()` at boot finds
    # nothing, so the daemon serves no responder and the peer's first request
    # times out against a subject nobody is listening on.
    iso_now = now.isoformat()
    save_connection(
        Connection(
            id=connection_id,
            peer_id="",  # unknown until the peer completes the handshake
            peer_name="",
            peer_endpoint="",
            peer_public_key="",
            relationship=relationship,
            state=ConnectionState.PENDING,
            exports=default_exports_for(_invert_relationship(relationship)),
            imports=default_exports_for(relationship),
            tenant_id=tenant_id or DEFAULT_TENANT,
            local_principal_id=f"federation:{connection_id}",
            local_principal_role=principal_role_for_peer(relationship),
            direction="inbound",  # we issued the invite, so the peer dials us
            transport=dict(pending.transport),
            metadata={
                # sha256 of the HMAC the consumer will present — precomputed
                # here so the secret itself is never stored. See
                # handshake.secret_proof_hash.
                "secret_proof_hash": _secret_proof_hash(connection_secret, connection_id),
                "invite_expires_at": expires.isoformat(),
            },
            created_at=iso_now,
            updated_at=iso_now,
        )
    )

    return InviteToken(
        token=token_str,
        connection_id=connection_id,
        issuer_id=identity.id,
        issuer_name=identity.display_name,
        issuer_endpoint=config.public_endpoint,
        issuer_public_key=identity.public_key,
        relationship=relationship,
        connection_secret=connection_secret,
        transport=dict(transport),
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )


def decode_invite_token(token_str: str, *, verify_signature: bool = True) -> InviteToken:
    """Decode and verify an invite token.

    Verifies the Ed25519 signature to ensure the token hasn't been tampered with.
    Does NOT check expiry — caller should check expires_at.

    Args:
        token_str: Base64-encoded invite token.
        verify_signature: If False, skip Ed25519 signature verification.
            Use only for pre-shared tokens on trusted networks.
    """
    try:
        bundle = json.loads(base64.urlsafe_b64decode(token_str))
    except Exception as exc:
        raise ValueError("Invalid token format") from exc

    # Support both v1 format (payload_json string) and legacy (payload dict)
    payload_json = bundle.get("payload_json")
    payload = bundle.get("payload")
    signature_b64 = bundle.get("signature")

    if payload_json:
        # v1: canonical JSON string preserved — verify against exact signed bytes
        payload = json.loads(payload_json)
        payload_bytes = payload_json.encode()
    elif payload:
        # Legacy: payload was a nested dict, re-serialize for verification
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    else:
        raise ValueError("Token missing payload")

    if not signature_b64:
        raise ValueError("Token missing signature")

    if verify_signature:
        # Verify signature
        public_pem = payload.get("issuer_public_key", "").encode()
        try:
            public_key = serialization.load_pem_public_key(public_pem)
            if not isinstance(public_key, Ed25519PublicKey):
                raise ValueError("Token public key is not Ed25519")
            signature = base64.b64decode(signature_b64)
            public_key.verify(signature, payload_bytes)
        except Exception as exc:
            raise ValueError(f"Token signature verification failed: {exc}") from exc
    else:
        logger.warning("Signature verification skipped (--trust mode)")

    # v1 tokens carried no connection_id, so both sides minted their own and
    # silently failed to find each other. Refusing them is the point: a
    # downgrade must be an error the operator sees, not a pairing that appears
    # to succeed and then never carries a message.
    version = payload.get("v")
    if version != 2:
        raise ValueError(
            f"Unsupported token version: {version}. Tokens issued before "
            f"connection ids were shared (v1) cannot be paired — reissue with "
            f"`robothor federation invite`."
        )
    if not payload.get("connection_id"):
        raise ValueError("Token has no connection_id — reissue the invite")

    return InviteToken(
        token=token_str,
        connection_id=payload["connection_id"],
        transport=dict(payload.get("transport") or {}),
        issuer_id=payload["issuer_id"],
        issuer_name=payload["issuer_name"],
        issuer_endpoint=payload["issuer_endpoint"],
        issuer_public_key=payload["issuer_public_key"],
        relationship=Relationship(payload["relationship"]),
        connection_secret=payload["connection_secret"],
        created_at=payload["created_at"],
        expires_at=payload["expires_at"],
    )


def consume_invite_token(
    config: FederationConfig,
    token_str: str,
    *,
    trust: bool = False,
    tenant_id: str = "",
) -> Connection:
    """Consume an invite token to establish a connection.

    Returns the new Connection (in PENDING state, ready for handshake).

    Args:
        trust: If True, skip signature verification (for pre-shared tokens).
    """
    invite = decode_invite_token(token_str, verify_signature=not trust)

    # Check expiry
    expires = datetime.fromisoformat(invite.expires_at)
    if datetime.now(UTC) > expires:
        raise ValueError("Invite token has expired")

    identity = get_identity(config)
    if not identity:
        raise RuntimeError("Instance identity not initialized. Run `robothor federation init`.")

    if invite.issuer_id == identity.id:
        raise ValueError("Cannot connect to yourself")

    # Determine our relationship (inverse of issuer's perspective)
    our_relationship = _invert_relationship(invite.relationship)

    now = datetime.now(UTC).isoformat()
    connection = Connection(
        # Adopt the ISSUER's id. Both sides must name the same connection or
        # the subjects never line up.
        id=invite.connection_id,
        peer_id=invite.issuer_id,
        peer_name=invite.issuer_name,
        peer_endpoint=invite.issuer_endpoint,
        peer_public_key=invite.issuer_public_key,
        relationship=our_relationship,
        state=ConnectionState.PENDING,
        exports=default_exports_for(invite.relationship),
        imports=default_exports_for(our_relationship),
        tenant_id=tenant_id or DEFAULT_TENANT,
        local_principal_id=f"federation:{invite.connection_id}",
        local_principal_role=principal_role_for_peer(our_relationship),
        direction="outbound",  # we consumed the invite, so we dial them
        transport=dict(invite.transport),
        metadata={
            "connection_secret_hash": hashlib.sha256(invite.connection_secret.encode()).hexdigest(),
        },
        created_at=now,
        updated_at=now,
    )

    return connection


def _invert_relationship(r: Relationship) -> Relationship:
    """Invert a relationship (parent ↔ child, peer stays peer)."""
    if r == Relationship.PARENT:
        return Relationship.CHILD
    if r == Relationship.CHILD:
        return Relationship.PARENT
    return Relationship.PEER
