"""An access token can say "I am acting in a tenant that is not my home".

The acting tenant lives IN the token (``tid``), with the home tenant beside it
(``home_tid``) and ``act: true`` whenever the two differ. A header can never
carry it: ``TenantMiddleware`` already refuses an ``X-Tenant-Id`` that
disagrees with ``tid``, so the token is the only authority on where a request
runs.

A token minted before ``home_tid`` existed has to keep verifying, unchanged,
as a home-tenant token -- every live session on every instance is one.
"""

from __future__ import annotations

import time

import jwt
import pytest

from robothor.auth import tokens
from robothor.auth.deps import AuthContext, verify_token
from robothor.auth.tokens import TokenError

KEY = "test-signing-key-at-least-32-bytes-long-xyz"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


def _claims(token: str) -> dict:
    return jwt.decode(token, KEY, algorithms=["HS256"], audience="genus-bridge")


class TestIssue:
    def test_home_token_carries_home_tid_equal_to_tid_and_no_act(self) -> None:
        token = tokens.issue_access_token("u1", "acme-parent", "admin")
        claims = _claims(token)
        assert claims["tid"] == "acme-parent"
        assert claims["home_tid"] == "acme-parent"
        assert "act" not in claims

    def test_acting_token_carries_both_tenants_and_act(self) -> None:
        token = tokens.issue_access_token(
            "u1", "acme-child-a", "admin", home_tenant_id="acme-parent"
        )
        claims = _claims(token)
        assert claims["tid"] == "acme-child-a"
        assert claims["tenant"] == "acme-child-a"
        assert claims["home_tid"] == "acme-parent"
        assert claims["act"] is True

    def test_home_equal_to_tenant_is_not_acting(self) -> None:
        token = tokens.issue_access_token(
            "u1", "acme-parent", "admin", home_tenant_id="acme-parent"
        )
        claims = _claims(token)
        assert claims["home_tid"] == "acme-parent"
        assert "act" not in claims

    def test_a_service_token_cannot_act_in_another_tenant(self) -> None:
        with pytest.raises(ValueError):
            tokens.issue_access_token(
                "svc",
                "acme-child-a",
                "service",
                typ="service",
                agent_id="svc",
                home_tenant_id="acme-parent",
            )


class TestVerify:
    def test_round_trip_acting_token(self) -> None:
        token = tokens.issue_access_token(
            "u1", "acme-child-a", "admin", home_tenant_id="acme-parent"
        )
        ctx = verify_token(token)
        assert ctx.tenant_id == "acme-child-a"
        assert ctx.home_tenant_id == "acme-parent"
        assert ctx.is_acting is True

    def test_round_trip_home_token(self) -> None:
        ctx = verify_token(tokens.issue_access_token("u1", "acme-parent", "owner"))
        assert ctx.tenant_id == "acme-parent"
        assert ctx.home_tenant_id == "acme-parent"
        assert ctx.is_acting is False

    def test_a_token_minted_before_home_tid_existed_verifies_unchanged(self) -> None:
        now = int(time.time())
        legacy = jwt.encode(
            {
                "sub": "u1",
                "tid": "acme-parent",
                "tenant": "acme-parent",
                "role": "member",
                "typ": "user",
                "type": "user",
                "aud": "genus-bridge",
                "scope": "bridge:read bridge:write",
                "iss": "genus-os",
                "jti": "legacy-jti",
                "iat": now,
                "exp": now + 60,
            },
            KEY,
            algorithm="HS256",
        )
        ctx = verify_token(legacy)
        assert ctx.tenant_id == "acme-parent"
        assert ctx.home_tenant_id == "acme-parent"
        assert ctx.is_acting is False

    def test_differing_home_without_act_is_rejected(self) -> None:
        """``act`` is the explicit marker; a token that crosses tenants
        without saying so was not minted by switch-tenant."""
        now = int(time.time())
        forged_shape = jwt.encode(
            {
                "sub": "u1",
                "tid": "acme-child-a",
                "home_tid": "acme-parent",
                "role": "admin",
                "typ": "user",
                "aud": "genus-bridge",
                "scope": "bridge:read",
                "iss": "genus-os",
                "jti": "j",
                "iat": now,
                "exp": now + 60,
            },
            KEY,
            algorithm="HS256",
        )
        with pytest.raises(TokenError):
            verify_token(forged_shape)


class TestAuthContextDefaults:
    def test_home_defaults_to_tenant(self) -> None:
        ctx = AuthContext(user_id="u1", tenant_id="acme-parent", role="owner", typ="user")
        assert ctx.home_tenant_id == "acme-parent"
        assert ctx.is_acting is False

    def test_explicit_home(self) -> None:
        ctx = AuthContext(
            user_id="u1",
            tenant_id="acme-child-a",
            role="owner",
            typ="user",
            home_tenant_id="acme-parent",
        )
        assert ctx.is_acting is True
