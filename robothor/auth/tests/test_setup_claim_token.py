"""The first-run wizard's claim token, and the wall between it and a session.

A separate token type exists because the wizard's caller has no account yet:
there is no user id, no tenant and no role to put in a session token. Two
things keep the claim from becoming one — a ``typ`` of ``"setup"`` and its own
audience — and both are tested from BOTH directions, because a claim token the
bridge's ordinary verifier would accept is an unauthenticated session on every
route in the appliance.
"""

from __future__ import annotations

import time

import jwt
import pytest

from robothor.auth import tokens
from robothor.auth.tokens import TokenError


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


def test_round_trip() -> None:
    claim = tokens.issue_setup_claim_token()

    claims = tokens.decode_setup_claim_token(claim)
    assert claims["typ"] == "setup"
    assert claims["aud"] == tokens.SETUP_AUDIENCE
    assert claims["iss"] == "genus-os"
    assert claims["jti"]
    assert claims["exp"] - claims["iat"] == tokens.SETUP_CLAIM_TTL_SECONDS


def test_default_lifetime_is_five_minutes() -> None:
    assert tokens.SETUP_CLAIM_TTL_SECONDS == 5 * 60


def test_a_claim_is_not_a_bridge_session() -> None:
    """The whole point: a claim token in an Authorization header must buy
    nothing outside ``/api/setup``."""
    from robothor.auth.deps import verify_token

    claim = tokens.issue_setup_claim_token()

    with pytest.raises(TokenError):
        tokens.decode_token(claim)
    with pytest.raises(TokenError):
        verify_token(claim)


def test_a_bridge_session_is_not_a_claim() -> None:
    """And the other direction: an operator's own session must not be usable as
    a claim, or a stolen cookie would reopen the wizard's write routes."""
    session = tokens.issue_access_token("user-1", "tenant-a", "owner")

    with pytest.raises(TokenError):
        tokens.decode_setup_claim_token(session)


def test_a_service_token_is_not_a_claim() -> None:
    service = tokens.issue_service_token("engine", "tenant-a")

    with pytest.raises(TokenError):
        tokens.decode_setup_claim_token(service)


def test_a_foreign_typ_on_the_setup_audience_is_refused() -> None:
    """A token minted with the setup audience but another ``typ`` is still
    refused, so the type check is load-bearing on its own."""
    now = int(time.time())
    forged = jwt.encode(
        {
            "sub": "setup",
            "typ": "user",
            "aud": tokens.SETUP_AUDIENCE,
            "iss": "genus-os",
            "jti": "forged",
            "iat": now,
            "exp": now + 300,
        },
        tokens.signing_key(),
        algorithm=tokens.ALGORITHM,
    )

    with pytest.raises(TokenError):
        tokens.decode_setup_claim_token(forged)


def test_a_missing_typ_is_refused() -> None:
    now = int(time.time())
    forged = jwt.encode(
        {
            "sub": "setup",
            "aud": tokens.SETUP_AUDIENCE,
            "iss": "genus-os",
            "jti": "forged",
            "iat": now,
            "exp": now + 300,
        },
        tokens.signing_key(),
        algorithm=tokens.ALGORITHM,
    )

    with pytest.raises(TokenError):
        tokens.decode_setup_claim_token(forged)


def test_an_expired_claim_is_refused() -> None:
    """Minted an hour ago with a five-minute life. Forged rather than waited
    for, and with the real signing key — an expiry check that only a wrong
    signature would catch is not an expiry check."""
    now = int(time.time()) - 3600
    stale = jwt.encode(
        {
            "sub": "setup",
            "typ": tokens.SETUP_TOKEN_TYPE,
            "aud": tokens.SETUP_AUDIENCE,
            "iss": "genus-os",
            "jti": "stale",
            "iat": now,
            "exp": now + tokens.SETUP_CLAIM_TTL_SECONDS,
        },
        tokens.signing_key(),
        algorithm=tokens.ALGORITHM,
    )

    with pytest.raises(TokenError):
        tokens.decode_setup_claim_token(stale)


def test_a_tampered_signature_is_refused() -> None:
    claim = tokens.issue_setup_claim_token()
    head, payload, signature = claim.split(".")

    with pytest.raises(TokenError):
        tokens.decode_setup_claim_token(f"{head}.{payload}.{signature[:-2]}xy")


def test_an_empty_token_is_refused() -> None:
    with pytest.raises(TokenError):
        tokens.decode_setup_claim_token("")


def test_a_claim_signed_with_another_key_is_refused(monkeypatch) -> None:
    claim = tokens.issue_setup_claim_token()

    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "a-completely-different-key-of-32-plus-bytes")
    tokens.reset_signing_key_cache()

    with pytest.raises(TokenError):
        tokens.decode_setup_claim_token(claim)
