"""SSO JIT routing by email domain, and the tenant API's domain-claim validation."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.crm.tenant_domains import InvalidSsoDomainError, SsoDomainConflictError  # noqa: E402

SSO_BODY = {
    "issuer": "https://idp",
    "subject": "s1",
    "email": "alice@example.com",
    "email_verified": True,
    "display_name": "Alice",
}


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", "test-signing-key-at-least-32-bytes-long-xyz")
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "dashboard-shared-secret")
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", "https://idp")
    from robothor.auth import tokens

    tokens.reset_signing_key_cache()
    yield
    tokens.reset_signing_key_cache()


@pytest.mark.asyncio
async def test_sso_provisions_into_the_tenant_that_claims_the_domain(test_client):
    account = {
        "id": "uid-1",
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": "member",
        "tenant_id": "acme-child-a",
        "status": "active",
    }
    with (
        patch("routers.auth.accounts.tenant_for_email", return_value="acme-child-a") as route,
        patch("routers.auth.accounts.jit_provision", return_value=account) as jit,
        patch("routers.auth.accounts.create_session", return_value="sess-1"),
    ):
        r = await test_client.post(
            "/api/auth/sso", json=SSO_BODY, headers={"X-Bridge-Auth": "dashboard-shared-secret"}
        )
    assert r.status_code == 200
    route.assert_called_once_with("alice@example.com")
    # The same tenant is what jit_provision searches for an email match and a
    # binding grant, so a grant armed for that tenant is spendable.
    assert jit.call_args.kwargs["tenant_id"] == "acme-child-a"
    assert r.json()["user"]["tenant_id"] == "acme-child-a"


@pytest.mark.asyncio
async def test_sso_routing_is_never_taken_from_the_caller(test_client):
    with (
        patch("routers.auth.accounts.tenant_for_email", return_value="acme-child-a") as route,
        patch("routers.auth.accounts.jit_provision") as jit,
    ):
        r = await test_client.post(
            "/api/auth/sso",
            json={**SSO_BODY, "tenant_id": "acme-parent"},
            headers={"X-Bridge-Auth": "dashboard-shared-secret"},
        )
    assert r.status_code == 422
    route.assert_not_called()
    jit.assert_not_called()


@pytest.mark.asyncio
async def test_patch_tenant_rejects_a_domain_another_tenant_claims(test_client):
    with patch(
        "routers.tenants.update_tenant",
        side_effect=SsoDomainConflictError("example.com", "acme-parent"),
    ):
        res = await test_client.patch(
            "/api/tenants/acme-child-a", json={"settings": {"sso_domains": ["example.com"]}}
        )
    assert res.status_code == 409
    assert "example.com" in res.json()["error"]


@pytest.mark.asyncio
async def test_patch_tenant_rejects_a_malformed_domain(test_client):
    with patch(
        "routers.tenants.update_tenant",
        side_effect=InvalidSsoDomainError("'not a domain' is not a domain name"),
    ):
        res = await test_client.patch(
            "/api/tenants/acme-child-a", json={"settings": {"sso_domains": ["not a domain"]}}
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_create_tenant_rejects_a_claimed_domain(test_client):
    with patch(
        "routers.tenants.create_tenant",
        side_effect=SsoDomainConflictError("example.com", "acme-parent"),
    ):
        res = await test_client.post(
            "/api/tenants",
            json={
                "id": "acme-child-a",
                "displayName": "Acme Child A",
                "settings": {"sso_domains": ["example.com"]},
            },
        )
    assert res.status_code == 409
