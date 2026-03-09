"""Tests for identity — keypair generation, token lifecycle, signature verification."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from robothor.federation.config import FederationConfig
from robothor.federation.identity import (
    _invert_relationship,
    consume_invite_token,
    create_invite_token,
    decode_invite_token,
    generate_keypair,
    get_identity,
    init_identity,
)
from robothor.federation.models import (
    ConnectionState,
    Relationship,
)


@pytest.fixture()
def fed_config(tmp_path):
    """Create a FederationConfig pointing at tmp_path."""
    config_dir = tmp_path / ".robothor"
    config_dir.mkdir()
    return FederationConfig(
        instance_id="",
        instance_name="test-instance",
        config_dir=config_dir,
        identity_file=config_dir / "identity.json",
        public_endpoint="https://test.robothor.ai",
    )


class TestGenerateKeypair:
    def test_generates_valid_pem_keys(self):
        public_pem, private_pem = generate_keypair()
        assert "BEGIN PUBLIC KEY" in public_pem
        assert "END PUBLIC KEY" in public_pem
        assert "BEGIN PRIVATE KEY" in private_pem
        assert "END PRIVATE KEY" in private_pem

    def test_different_each_time(self):
        pub1, _ = generate_keypair()
        pub2, _ = generate_keypair()
        assert pub1 != pub2

    def test_key_can_sign_and_verify(self):
        """Generated keypair can actually sign/verify data."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )

        public_pem, private_pem = generate_keypair()

        private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
        public_key = serialization.load_pem_public_key(public_pem.encode())
        assert isinstance(private_key, Ed25519PrivateKey)
        assert isinstance(public_key, Ed25519PublicKey)

        message = b"test message"
        signature = private_key.sign(message)
        public_key.verify(signature, message)  # No exception = success


class TestInitIdentity:
    def test_creates_identity_files(self, fed_config):
        instance = init_identity(fed_config, display_name="My Node")
        assert instance.id
        assert instance.display_name == "My Node"
        assert "BEGIN PUBLIC KEY" in instance.public_key
        assert instance.created_at

        # Check files were created
        assert fed_config.identity_file.exists()
        key_path = fed_config.config_dir / "identity.key"
        assert key_path.exists()
        # Private key file should be 0o600
        assert oct(key_path.stat().st_mode)[-3:] == "600"

    def test_idempotent(self, fed_config):
        """Second call returns existing identity, doesn't regenerate."""
        first = init_identity(fed_config, display_name="Node A")
        second = init_identity(fed_config, display_name="Node B")
        assert first.id == second.id
        assert first.display_name == second.display_name

    def test_default_display_name(self, fed_config):
        """If no display_name given, uses robothor-{id[:8]}."""
        instance = init_identity(fed_config)
        assert instance.display_name.startswith("robothor-")
        assert len(instance.display_name) == len("robothor-") + 8


class TestGetIdentity:
    def test_returns_none_when_not_initialized(self, fed_config):
        assert get_identity(fed_config) is None

    def test_returns_instance_after_init(self, fed_config):
        created = init_identity(fed_config, display_name="Test")
        loaded = get_identity(fed_config)
        assert loaded is not None
        assert loaded.id == created.id
        assert loaded.display_name == created.display_name
        assert loaded.public_key == created.public_key


class TestInviteToken:
    def test_create_and_decode_roundtrip(self, fed_config):
        init_identity(fed_config, display_name="Issuer")
        token = create_invite_token(fed_config, Relationship.PEER, ttl_hours=1)

        assert token.token  # base64 string
        assert token.issuer_name == "Issuer"
        assert token.relationship == Relationship.PEER
        assert token.connection_secret

        decoded = decode_invite_token(token.token)
        assert decoded.issuer_id == token.issuer_id
        assert decoded.issuer_name == token.issuer_name
        assert decoded.relationship == Relationship.PEER
        assert decoded.connection_secret == token.connection_secret

    def test_create_parent_relationship(self, fed_config):
        init_identity(fed_config)
        token = create_invite_token(fed_config, Relationship.PARENT)
        decoded = decode_invite_token(token.token)
        assert decoded.relationship == Relationship.PARENT

    def test_signature_tamper_detected(self, fed_config):
        init_identity(fed_config)
        token = create_invite_token(fed_config)

        # Tamper with the canonical payload JSON string
        bundle = json.loads(base64.urlsafe_b64decode(token.token))
        payload = json.loads(bundle["payload_json"])
        payload["issuer_name"] = "TAMPERED"
        bundle["payload_json"] = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        tampered = base64.urlsafe_b64encode(json.dumps(bundle).encode()).decode()

        with pytest.raises(ValueError, match="signature verification failed"):
            decode_invite_token(tampered)

    def test_decode_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid token format"):
            decode_invite_token("not-valid-base64!!!")

    def test_decode_missing_payload(self):
        bundle = {"signature": "abc"}
        token_str = base64.urlsafe_b64encode(json.dumps(bundle).encode()).decode()
        with pytest.raises(ValueError, match="missing payload"):
            decode_invite_token(token_str)

    def test_decode_missing_signature(self):
        bundle = {"payload": {"issuer_public_key": "x"}}
        token_str = base64.urlsafe_b64encode(json.dumps(bundle).encode()).decode()
        with pytest.raises(ValueError, match="missing signature"):
            decode_invite_token(token_str)

    def test_create_requires_identity(self, fed_config):
        with pytest.raises(RuntimeError, match="not initialized"):
            create_invite_token(fed_config)

    def test_ttl_sets_expiry(self, fed_config):
        init_identity(fed_config)
        token = create_invite_token(fed_config, ttl_hours=48)
        decoded = decode_invite_token(token.token)
        created = datetime.fromisoformat(decoded.created_at)
        expires = datetime.fromisoformat(decoded.expires_at)
        diff = expires - created
        assert abs(diff.total_seconds() - 48 * 3600) < 2  # Within 2 seconds


class TestConsumeInviteToken:
    def test_consume_creates_pending_connection(self, fed_config, tmp_path):
        """Consuming a token creates a PENDING connection with inverted relationship."""
        # Create issuer identity
        init_identity(fed_config, display_name="Issuer")
        token = create_invite_token(fed_config, Relationship.PARENT)

        # Create consumer identity (different instance)
        consumer_dir = tmp_path / "consumer" / ".robothor"
        consumer_dir.mkdir(parents=True)
        consumer_config = FederationConfig(
            instance_id="",
            instance_name="consumer",
            config_dir=consumer_dir,
            identity_file=consumer_dir / "identity.json",
            public_endpoint="https://consumer.robothor.ai",
        )
        init_identity(consumer_config, display_name="Consumer")

        conn = consume_invite_token(consumer_config, token.token)
        assert conn.state == ConnectionState.PENDING
        assert conn.peer_id == token.issuer_id
        assert conn.peer_name == "Issuer"
        # Issuer says PARENT → consumer sees CHILD
        assert conn.relationship == Relationship.CHILD
        assert conn.peer_public_key == token.issuer_public_key
        assert "connection_secret_hash" in conn.metadata

    def test_consume_expired_token_raises(self, fed_config, tmp_path):
        init_identity(fed_config)
        token = create_invite_token(fed_config, ttl_hours=1)

        consumer_dir = tmp_path / "consumer" / ".robothor"
        consumer_dir.mkdir(parents=True)
        consumer_config = FederationConfig(
            config_dir=consumer_dir,
            identity_file=consumer_dir / "identity.json",
        )
        init_identity(consumer_config)

        # Fake the expiry time to the past
        with patch(
            "robothor.federation.identity.datetime",
        ) as mock_dt:
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.now.return_value = datetime.now(UTC) + timedelta(hours=2)
            with pytest.raises(ValueError, match="expired"):
                consume_invite_token(consumer_config, token.token)

    def test_consume_self_token_raises(self, fed_config):
        """Cannot connect to yourself."""
        init_identity(fed_config)
        token = create_invite_token(fed_config)
        with pytest.raises(ValueError, match="Cannot connect to yourself"):
            consume_invite_token(fed_config, token.token)

    def test_consume_requires_identity(self, fed_config, tmp_path):
        init_identity(fed_config)
        token = create_invite_token(fed_config)

        # Consumer has no identity
        consumer_dir = tmp_path / "consumer" / ".robothor"
        consumer_dir.mkdir(parents=True)
        consumer_config = FederationConfig(
            config_dir=consumer_dir,
            identity_file=consumer_dir / "identity.json",
        )
        with pytest.raises(RuntimeError, match="not initialized"):
            consume_invite_token(consumer_config, token.token)


class TestTrustMode:
    def _tamper_signature(self, token_str: str) -> str:
        """Return a token with a corrupted signature."""
        bundle = json.loads(base64.urlsafe_b64decode(token_str))
        sig_bytes = base64.b64decode(bundle["signature"])
        # Flip a byte in the signature
        corrupted = bytes([sig_bytes[0] ^ 0xFF]) + sig_bytes[1:]
        bundle["signature"] = base64.b64encode(corrupted).decode()
        return base64.urlsafe_b64encode(json.dumps(bundle).encode()).decode()

    def test_decode_trust_skips_verification(self, fed_config):
        """Tampered signature succeeds with verify_signature=False."""
        init_identity(fed_config)
        token = create_invite_token(fed_config)
        tampered = self._tamper_signature(token.token)

        decoded = decode_invite_token(tampered, verify_signature=False)
        assert decoded.issuer_id == token.issuer_id
        assert decoded.issuer_name == token.issuer_name

    def test_decode_default_still_verifies(self, fed_config):
        """Tampered signature still fails without verify_signature=False."""
        init_identity(fed_config)
        token = create_invite_token(fed_config)
        tampered = self._tamper_signature(token.token)

        with pytest.raises(ValueError, match="signature verification failed"):
            decode_invite_token(tampered)

    def test_consume_trust_mode(self, fed_config, tmp_path):
        """Consume with trust=True on tampered-sig token creates connection."""
        init_identity(fed_config, display_name="Issuer")
        token = create_invite_token(fed_config, Relationship.PEER)
        tampered = self._tamper_signature(token.token)

        consumer_dir = tmp_path / "consumer" / ".robothor"
        consumer_dir.mkdir(parents=True)
        consumer_config = FederationConfig(
            instance_id="",
            instance_name="consumer",
            config_dir=consumer_dir,
            identity_file=consumer_dir / "identity.json",
            public_endpoint="https://consumer.robothor.ai",
        )
        init_identity(consumer_config, display_name="Consumer")

        conn = consume_invite_token(consumer_config, tampered, trust=True)
        assert conn.state == ConnectionState.PENDING
        assert conn.peer_name == "Issuer"
        assert conn.relationship == Relationship.PEER


class TestInvertRelationship:
    def test_parent_to_child(self):
        assert _invert_relationship(Relationship.PARENT) == Relationship.CHILD

    def test_child_to_parent(self):
        assert _invert_relationship(Relationship.CHILD) == Relationship.PARENT

    def test_peer_stays_peer(self):
        assert _invert_relationship(Relationship.PEER) == Relationship.PEER
