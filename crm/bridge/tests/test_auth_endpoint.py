"""Bridge auth endpoints plus explicit loopback-development behavior."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "dashboard-shared-secret")
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    from robothor.auth import tokens

    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


@pytest.mark.asyncio
async def test_sso_exchange_rejects_missing_secret(test_client):
    r = await test_client.post(
        "/api/auth/sso",
        json={
            "issuer": "https://idp",
            "subject": "s1",
            "email": "alice@example.com",
            "email_verified": True,
        },
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_sso_exchange_mints_tokens(test_client):
    account = {
        "id": "uid-1",
        "email": "alice@example.com",
        "display_name": "Ann",
        "role": "member",
        "tenant_id": "default",
        "status": "active",
    }
    with (
        patch("routers.auth.accounts.jit_provision", return_value=account) as jit,
        patch("routers.auth.accounts.create_session", return_value="sess-1"),
    ):
        r = await test_client.post(
            "/api/auth/sso",
            json={
                "issuer": "https://idp",
                "subject": "s1",
                "email": "alice@example.com",
                "email_verified": True,
                "display_name": "Ann",
            },
            headers={"X-Bridge-Auth": "dashboard-shared-secret"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["email"] == "alice@example.com" and body["user"]["role"] == "member"
    assert jit.call_args.kwargs["issuer"] == "https://idp"
    assert jit.call_args.kwargs["tenant_id"] == "default"

    # The minted access token verifies and yields the right identity.
    from robothor.auth.deps import verify_token

    ctx = verify_token(body["access_token"])
    assert ctx.user_id == "uid-1" and ctx.tenant_id == "default" and ctx.role == "member"


@pytest.mark.asyncio
async def test_sso_exchange_rejects_caller_selected_tenant(test_client):
    with patch("routers.auth.accounts.jit_provision") as jit:
        response = await test_client.post(
            "/api/auth/sso",
            json={
                "issuer": "https://idp",
                "subject": "s1",
                "email": "alice@example.com",
                "email_verified": True,
                "tenant_id": "attacker-selected-tenant",
            },
            headers={"X-Bridge-Auth": "dashboard-shared-secret"},
        )

    assert response.status_code == 422
    jit.assert_not_called()


@pytest.mark.asyncio
async def test_sso_exchange_requires_verified_email(test_client):
    with (
        patch("routers.auth.accounts.jit_provision") as jit,
        patch("routers.auth.accounts.create_session") as create_session,
    ):
        r = await test_client.post(
            "/api/auth/sso",
            json={
                "issuer": "https://idp",
                "subject": "s-unverified",
                "email": "unverified@example.com",
                "email_verified": False,
            },
            headers={"X-Bridge-Auth": "dashboard-shared-secret"},
        )
    assert r.status_code == 403
    assert r.json() == {"error": "verified email required"}
    jit.assert_not_called()
    create_session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["disabled", "invited", "pending"])
async def test_sso_exchange_never_mints_for_non_active_account(test_client, status):
    account = {
        "id": "uid-inactive",
        "email": "inactive@example.com",
        "display_name": "Inactive",
        "role": "member",
        "tenant_id": "default",
        "status": status,
    }
    with (
        patch("routers.auth.accounts.jit_provision", return_value=account),
        patch("routers.auth.accounts.create_session") as create_session,
    ):
        r = await test_client.post(
            "/api/auth/sso",
            json={
                "issuer": "https://idp",
                "subject": "s-inactive",
                "email": "inactive@example.com",
                "email_verified": True,
            },
            headers={"X-Bridge-Auth": "dashboard-shared-secret"},
        )
    assert r.status_code == 403
    assert r.json() == {"error": "account not eligible for SSO"}
    create_session.assert_not_called()


@pytest.mark.asyncio
async def test_sso_exchange_hides_existing_account_binding_collision(test_client):
    from robothor.auth.accounts import AccountBindingRequiredError

    with (
        patch(
            "routers.auth.accounts.jit_provision",
            side_effect=AccountBindingRequiredError("binding required"),
        ),
        patch("routers.auth.accounts.create_session") as create_session,
    ):
        r = await test_client.post(
            "/api/auth/sso",
            json={
                "issuer": "https://idp",
                "subject": "attacker-subject",
                "email": "owner@example.com",
                "email_verified": True,
            },
            headers={"X-Bridge-Auth": "dashboard-shared-secret"},
        )
    assert r.status_code == 403
    assert r.json() == {"error": "account not eligible for SSO"}
    create_session.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_consumes_token_before_rejecting_disabled_account(test_client):
    session = {"id": "session-1", "user_id": "uid-disabled"}
    account = {
        "id": "uid-disabled",
        "email": "disabled@example.com",
        "display_name": "Disabled",
        "role": "member",
        "tenant_id": "default",
        "status": "disabled",
    }
    with (
        patch("routers.auth.accounts.consume_active_session", return_value=session) as consume,
        patch("routers.auth.accounts.get_account_by_id", return_value=account),
        patch("routers.auth.accounts.create_session") as create_session,
    ):
        r = await test_client.post(
            "/api/auth/refresh",
            json={"refresh_token": "one-use-refresh-token"},
        )
    assert r.status_code == 401
    assert r.json() == {"error": "account inactive"}
    consume.assert_called_once()
    create_session.assert_not_called()


@pytest.mark.asyncio
async def test_me_requires_auth(test_client):
    r = await test_client.get("/api/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_user_with_valid_token(test_client):
    from robothor.auth import tokens

    token = tokens.issue_access_token("uid-9", "default", "admin")
    account = {
        "id": "uid-9",
        "email": "user@example.com",
        "display_name": "Zed",
        "role": "admin",
        "tenant_id": "default",
        "status": "active",
    }
    with patch("routers.auth.accounts.get_account_by_id", return_value=account):
        r = await test_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "user@example.com" and r.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_explicit_loopback_dev_mode_does_not_block_unauthenticated(test_client):
    # The shared bridge fixture explicitly opts into insecure loopback mode.
    # The route may still 5xx without a DB; what matters is auth passes through.
    r = await test_client.get("/health")
    assert r.status_code != 401


@pytest.mark.asyncio
async def test_enforce_mode_hides_token_error_detail(test_client, monkeypatch):
    # With enforcement on, an invalid token returns a GENERIC 401 — the raw PyJWT
    # message (expired vs bad-signature vs issuer) must not leak (token oracle).
    monkeypatch.setenv("GENUS_AUTH_ENFORCE", "true")
    r = await test_client.get(
        "/api/conversations",
        headers={"Authorization": "Bearer not.a.valid.token"},
    )
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "invalid or expired token"
    # The generic message reveals nothing specific; none of PyJWT's distinguishing
    # internal strings (which would form a token oracle) may appear.
    blob = str(body).lower()
    for leak in ("signature", "issuer", "claim", "verification", "segments", "decode"):
        assert leak not in blob
