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


# ── Signing-key resolution ───────────────────────────────────────────────────
# env -> vault -> generate-and-store, and the env+vault half now runs through
# robothor.secrets so that "where is this instance's signing key?" has the same
# answer here as everywhere else. The vault row keeps its own name
# (auth/jwt_signing_key): a lookup derived from GENUS_AUTH_SIGNING_KEY would
# miss it and generate a SECOND key on a box that already had one, invalidating
# every live session and every MFA secret derived from it
# (robothor/auth/mfa_secrets.py derives its AES key from this one).

VAULT_ROW = "auth/jwt_signing_key"
LONG_ENOUGH = "vault-stored-signing-key-at-least-32-bytes"


def test_signing_key_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "env-signing-key-at-least-32-bytes-long-ok")
    tokens.reset_signing_key_cache()
    assert tokens.signing_key() == "env-signing-key-at-least-32-bytes-long-ok"


def test_signing_key_comes_from_the_vault_row_when_the_environment_has_none(monkeypatch):
    from robothor import vault

    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)
    asked: list[str] = []
    monkeypatch.setattr(vault, "get", lambda key, **kw: (asked.append(key), LONG_ENOUGH)[1])
    monkeypatch.setattr(
        vault, "set", lambda *a, **kw: pytest.fail("an existing key was overwritten")
    )
    tokens.reset_signing_key_cache()
    assert tokens.signing_key() == LONG_ENOUGH
    assert asked == [VAULT_ROW], f"the accessor read {asked}, not the existing row"


def test_signing_key_is_generated_and_stored_when_nothing_holds_one(monkeypatch):
    from robothor import vault

    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)
    stored: dict[str, str] = {}
    monkeypatch.setattr(vault, "get", lambda key, **kw: None)
    monkeypatch.setattr(vault, "set", lambda key, value, **kw: stored.setdefault(key, value))
    tokens.reset_signing_key_cache()
    generated = tokens.signing_key()
    assert len(generated.encode("utf-8")) >= 32
    assert stored == {VAULT_ROW: generated}, (
        "a generated key that is not stored is a new key on every restart, and "
        "every session and MFA secret dies with each one"
    )


def test_a_blank_signing_key_variable_is_unset_not_a_three_byte_key(monkeypatch):
    """``GENUS_AUTH_SIGNING_KEY=`` with spaces is an operator who commented the
    value out, not a 3-byte signing key.

    It used to raise "must contain at least 32 bytes" and leave the instance
    unable to mint a single token while the vault held a perfectly good key.
    Empty-is-unset is the rule the settings sources already apply to every other
    variable, and the accessor applies it here.
    """
    from robothor import vault

    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "   ")
    monkeypatch.setattr(vault, "get", lambda key, **kw: LONG_ENOUGH)
    tokens.reset_signing_key_cache()
    assert tokens.signing_key() == LONG_ENOUGH


def test_a_short_key_in_the_vault_is_refused_rather_than_used(monkeypatch):
    from robothor import vault

    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)
    monkeypatch.setattr(vault, "get", lambda key, **kw: "too-short")
    tokens.reset_signing_key_cache()
    with pytest.raises(TokenError, match="32 bytes"):
        tokens.signing_key()


def test_an_unreadable_vault_does_not_crash_the_resolution(monkeypatch):
    """A box with no vault must still be able to mint tokens from its env key.

    Before the accessor, ``vault.get`` raising propagated out of
    ``signing_key()``; it is reached only when the environment has nothing, but
    a caller that holds an env key must never pay for the vault at all.
    """
    from robothor import secrets as secrets_module
    from robothor import vault

    def boom(*a, **kw):
        raise FileNotFoundError("no master key on this box")

    monkeypatch.setattr(vault, "get", boom)
    monkeypatch.setattr(vault, "export_env", boom)
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "env-signing-key-at-least-32-bytes-long-ok")
    secrets_module.reset_vault_availability()
    tokens.reset_signing_key_cache()
    assert tokens.signing_key() == "env-signing-key-at-least-32-bytes-long-ok"


def test_an_unreadable_vault_never_generates_over_the_stored_key(monkeypatch):
    """Read fails, write works: the one shape that overwrites the live key.

    ``vault.set`` is an UPSERT. Generating here would rotate the signing key
    underneath every session and every MFA secret derived from it. The
    resolution must refuse loudly instead.
    """
    from robothor import secrets as secrets_module
    from robothor import vault

    def boom(*a, **kw):
        raise ConnectionError("vault database down")

    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)
    monkeypatch.setattr(vault, "get", boom)
    monkeypatch.setattr(vault, "export_env", boom)
    monkeypatch.setattr(
        vault, "set", lambda *a, **kw: pytest.fail("the stored key was overwritten")
    )
    secrets_module.reset_vault_availability()
    tokens.reset_signing_key_cache()
    with pytest.raises(TokenError, match="cannot be read"):
        tokens.signing_key()


def test_a_cooling_down_vault_is_probed_before_a_key_is_generated(monkeypatch):
    """A five-minute-old failure must not decide that no key exists."""
    from robothor import secrets as secrets_module
    from robothor import vault

    monkeypatch.delenv("GENUS_AUTH_SIGNING_KEY", raising=False)
    # The vault failed a moment ago and is inside its cooldown...
    secrets_module._vault_retry_after = secrets_module._clock() + 300
    # ...but it is healthy now and holds the key.
    monkeypatch.setattr(vault, "get", lambda key, **kw: LONG_ENOUGH)
    monkeypatch.setattr(
        vault, "set", lambda *a, **kw: pytest.fail("a key was generated over the stored one")
    )
    tokens.reset_signing_key_cache()
    assert tokens.signing_key() == LONG_ENOUGH
    secrets_module.reset_vault_availability()
