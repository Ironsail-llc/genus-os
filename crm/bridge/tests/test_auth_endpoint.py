"""Bridge auth endpoints + AuthMiddleware shadow-mode behavior."""

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
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)  # shadow mode
    from robothor.auth import tokens

    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


@pytest.mark.asyncio
async def test_sso_exchange_rejects_missing_secret(test_client):
    r = await test_client.post(
        "/api/auth/sso",
        json={"issuer": "https://idp", "subject": "s1", "email": "a@x.com"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_sso_exchange_mints_tokens(test_client):
    account = {
        "id": "uid-1", "email": "a@x.com", "display_name": "Ann",
        "role": "member", "tenant_id": "default", "status": "active",
    }
    with patch("routers.auth.accounts.jit_provision", return_value=account) as jit, \
         patch("routers.auth.accounts.create_session", return_value="sess-1"):
        r = await test_client.post(
            "/api/auth/sso",
            json={"issuer": "https://idp", "subject": "s1", "email": "a@x.com", "display_name": "Ann"},
            headers={"X-Bridge-Auth": "dashboard-shared-secret"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["email"] == "a@x.com" and body["user"]["role"] == "member"
    assert jit.call_args.kwargs["issuer"] == "https://idp"

    # The minted access token verifies and yields the right identity.
    from robothor.auth.deps import verify_token

    ctx = verify_token(body["access_token"])
    assert ctx.user_id == "uid-1" and ctx.tenant_id == "default" and ctx.role == "member"


@pytest.mark.asyncio
async def test_me_requires_auth(test_client):
    r = await test_client.get("/api/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_user_with_valid_token(test_client):
    from robothor.auth import tokens

    token = tokens.issue_access_token("uid-9", "default", "admin")
    account = {"id": "uid-9", "email": "z@x.com", "display_name": "Zed", "role": "admin", "tenant_id": "default"}
    with patch("routers.auth.accounts.get_account_by_id", return_value=account):
        r = await test_client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "z@x.com" and r.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_shadow_mode_does_not_block_unauthenticated(test_client):
    # GENUS_AUTH_ENFORCE off → the middleware never injects a 401 for a missing
    # token (the route may still 5xx without a DB; what matters is auth passes through).
    r = await test_client.get("/health")
    assert r.status_code != 401
