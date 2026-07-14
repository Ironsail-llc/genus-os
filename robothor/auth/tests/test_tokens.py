"""Tokens: access JWT round-trip, expiry, tampering, refresh hashing."""

from __future__ import annotations

import time

import pytest

from robothor.auth import tokens
from robothor.auth.tokens import TokenError


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


def test_access_token_round_trip():
    t = tokens.issue_access_token("user-1", "tenant-a", "admin")
    claims = tokens.decode_token(t)
    assert claims["sub"] == "user-1"
    assert claims["tid"] == "tenant-a"
    assert claims["role"] == "admin"
    assert claims["typ"] == "user"
    assert claims["type"] == "user"
    assert claims["tenant"] == "tenant-a"
    assert claims["iss"] == "genus-os"
    assert claims["aud"] == "genus-bridge"
    assert set(claims["scope"].split()) == {
        "audit:read",
        "bridge:*",
        "engine:*",
        "tenant:admin",
    }
    assert claims["jti"]


def test_expired_token_rejected():
    t = tokens.issue_access_token("u", "t", "viewer", ttl_seconds=-1)
    with pytest.raises(TokenError):
        tokens.decode_token(t)


def test_tampered_token_rejected():
    t = tokens.issue_access_token("u", "t", "viewer")
    tampered = t[:-2] + ("aa" if not t.endswith("aa") else "bb")
    with pytest.raises(TokenError):
        tokens.decode_token(tampered)


def test_wrong_key_rejected(monkeypatch):
    t = tokens.issue_access_token("u", "t", "viewer")
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "a-different-signing-key-also-32-plus-bytes-long")
    tokens.reset_signing_key_cache()
    with pytest.raises(TokenError):
        tokens.decode_token(t)


def test_short_signing_key_rejected(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "too-short")
    tokens.reset_signing_key_cache()
    with pytest.raises(TokenError, match="32 bytes"):
        tokens.issue_access_token("u", "t", "member")


def test_service_token_typ():
    t = tokens.issue_access_token("agent-x", "t", "", typ="service")
    claims = tokens.decode_token(t)
    assert claims["typ"] == "service"
    assert claims["agent_id"] == "agent-x"
    assert claims["role"] == "service"


def test_wrong_audience_rejected():
    t = tokens.issue_access_token("u", "t", "member", audience="genus-engine")
    with pytest.raises(TokenError):
        tokens.decode_token(t)
    assert tokens.decode_token(t, expected_audience="genus-engine")["aud"] == "genus-engine"


def test_service_helper_binds_agent_scope_and_audience():
    t = tokens.issue_service_token(
        "engine",
        "tenant-a",
        agent_id="email-classifier",
        scopes=("bridge:read",),
    )
    claims = tokens.decode_token(t)
    assert claims["sub"] == "engine"
    assert claims["agent_id"] == "email-classifier"
    assert claims["scope"] == "bridge:read"


def test_user_token_cannot_claim_agent_identity():
    with pytest.raises(ValueError, match="cannot carry agent_id"):
        tokens.issue_access_token("u", "t", "member", agent_id="main")


def test_refresh_token_hash_deterministic():
    raw, h = tokens.new_refresh_token()
    assert h == tokens.hash_refresh_token(raw)
    assert raw != h and len(h) == 64  # sha256 hex


def test_empty_token_raises():
    with pytest.raises(TokenError):
        tokens.decode_token("")


def test_iat_exp_window():
    t = tokens.issue_access_token("u", "t", "member", ttl_seconds=900)
    claims = tokens.decode_token(t)
    assert 800 < (claims["exp"] - int(time.time())) <= 900
