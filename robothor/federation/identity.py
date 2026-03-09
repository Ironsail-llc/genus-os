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

from robothor.federation.config import FederationConfig, load_identity, save_identity
from robothor.federation.models import (
    Connection,
    ConnectionState,
    Instance,
    InviteToken,
    Relationship,
    default_exports_for,
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


def create_invite_token(
    config: FederationConfig,
    relationship: Relationship = Relationship.PEER,
    ttl_hours: int = 24,
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

    token_data = {
        "v": 1,  # token format version
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

    return InviteToken(
        token=token_str,
        issuer_id=identity.id,
        issuer_name=identity.display_name,
        issuer_endpoint=config.public_endpoint,
        issuer_public_key=identity.public_key,
        relationship=relationship,
        connection_secret=connection_secret,
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

    # Check version
    if payload.get("v") != 1:
        raise ValueError(f"Unsupported token version: {payload.get('v')}")

    return InviteToken(
        token=token_str,
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
        id=str(uuid.uuid4()),
        peer_id=invite.issuer_id,
        peer_name=invite.issuer_name,
        peer_endpoint=invite.issuer_endpoint,
        peer_public_key=invite.issuer_public_key,
        relationship=our_relationship,
        state=ConnectionState.PENDING,
        exports=default_exports_for(our_relationship),
        imports=default_exports_for(invite.relationship),
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
