"""Password sign-in for an account outside the platform tenant, RLS on, as robothor_app.

End-to-end QA: ``POST /api/auth/login`` authenticated against the platform
tenant only, so an active child-tenant account with the right password got a
401. Without an identity provider no child admin could sign in, and a moved
user could not sign in again. The address now resolves across tenants: one
active account signs in to ITS tenant; several is refused, because the
operator has to decide which one the address means.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tenant_switch_rls_integration import (  # noqa: E402, F401, F811 - pytest fixtures
    _signing_key,
    app_role,
    tenants,
)

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _local_login_on(monkeypatch):
    from robothor.auth import local_login

    monkeypatch.setattr(local_login, "local_login_enabled", lambda: True)
    monkeypatch.setattr(local_login, "flood_limited", lambda *a, **k: False)
    monkeypatch.setattr(local_login, "_rate_limited", lambda *a, **k: False)


def _seed(db_conn, tenant: str, email: str, role: str = "admin") -> str:
    from robothor.auth.passwords import hash_password

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (tenant_id, email, display_name, role, status, "
            "password_hash) VALUES (%s, %s, 'Alice', %s, 'active', %s) RETURNING id",
            (tenant, email, role, hash_password(PASSWORD)),
        )
        return str(cur.fetchone()[0])


async def _login(client, email: str):
    return await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})


@pytest.mark.asyncio
async def test_a_child_tenant_account_signs_in_to_its_tenant(
    test_client,
    db_conn,
    tenants,  # noqa: F811 - the imported fixture
    app_role,  # noqa: F811 - the imported fixture
):
    email = f"alice-{uuid.uuid4().hex[:8]}@example.com"
    user_id = _seed(db_conn, tenants["a"], email)
    r = await _login(test_client, email)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == user_id
    assert r.json()["user"]["tenant_id"] == tenants["a"]


@pytest.mark.asyncio
async def test_a_default_tenant_account_still_signs_in(
    test_client,
    db_conn,
    tenants,  # noqa: F811 - the imported fixture
    app_role,  # noqa: F811 - the imported fixture
):
    email = f"bob-{uuid.uuid4().hex[:8]}@example.com"
    user_id = _seed(db_conn, "default", email, role="member")
    r = await _login(test_client, email)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == user_id
    assert r.json()["user"]["tenant_id"] == "default"


@pytest.mark.asyncio
async def test_an_address_active_in_two_tenants_is_refused(
    test_client,
    db_conn,
    tenants,  # noqa: F811 - the imported fixture
    app_role,  # noqa: F811 - the imported fixture
    caplog,
):
    email = f"carol-{uuid.uuid4().hex[:8]}@example.com"
    _seed(db_conn, tenants["a"], email)
    _seed(db_conn, tenants["b"], email)
    r = await _login(test_client, email)
    assert r.status_code == 401
    assert any("more than one tenant" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_an_unknown_address_is_refused(
    test_client,
    tenants,  # noqa: F811 - the imported fixture
    app_role,  # noqa: F811 - the imported fixture
):
    r = await _login(test_client, f"nobody-{uuid.uuid4().hex[:8]}@example.com")
    assert r.status_code == 401


def _seed_passwordless(db_conn, tenant: str, email: str) -> str:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (tenant_id, email, display_name, role, status) "
            "VALUES (%s, %s, 'Alice', 'member', 'active') RETURNING id",
            (tenant, email),
        )
        return str(cur.fetchone()[0])


@pytest.mark.asyncio
async def test_two_password_accounts_prefer_the_platform_tenants_with_a_log_line(
    test_client,
    db_conn,
    tenants,  # noqa: F811 - the imported fixture
    app_role,  # noqa: F811 - the imported fixture
    caplog,
):
    email = f"dana-{uuid.uuid4().hex[:8]}@example.com"
    root_id = _seed(db_conn, "default", email, role="member")
    _seed(db_conn, tenants["a"], email)
    r = await _login(test_client, email)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == root_id
    assert any("more than one tenant" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_a_passwordless_duplicate_does_not_block_the_password_account(
    test_client,
    db_conn,
    tenants,  # noqa: F811 - the imported fixture
    app_role,  # noqa: F811 - the imported fixture
):
    email = f"erin-{uuid.uuid4().hex[:8]}@example.com"
    user_id = _seed(db_conn, "default", email, role="member")
    _seed_passwordless(db_conn, tenants["a"], email)
    r = await _login(test_client, email)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == user_id


# ─── SSO with a claimed domain must not duplicate an unbound account ─────

ISSUER = "https://idp.example.com"


@pytest.mark.asyncio
async def test_sso_refuses_rather_than_duplicating_an_unbound_account_elsewhere(
    test_client,
    db_conn,
    tenants,  # noqa: F811 - the imported fixture
    app_role,  # noqa: F811 - the imported fixture
    monkeypatch,
):
    """A ``genus user add`` + ``set-password`` account in the platform tenant,
    then its domain claimed by a child: the first SSO click must not create a
    second account in the child (which would then also break the password
    sign-in that worked before)."""
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "dashboard-shared-secret")
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", ISSUER)
    sfx = uuid.uuid4().hex[:8]
    domain = f"claimed-{sfx}.example"
    email = f"alice@{domain}"
    user_id = _seed(db_conn, "default", email, role="member")
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE crm_tenants SET settings = jsonb_build_object('sso_domains', "
            "jsonb_build_array(%s::text)) WHERE id = %s",
            (domain, tenants["a"]),
        )

    r = await test_client.post(
        "/api/auth/sso",
        json={
            "issuer": ISSUER,
            "subject": f"sub-{sfx}",
            "email": email,
            "email_verified": True,
            "display_name": "Alice",
        },
        headers={"X-Bridge-Auth": "dashboard-shared-secret"},
    )
    assert r.status_code == 403, r.text
    with db_conn.cursor() as cur:
        cur.execute("SELECT tenant_id FROM user_accounts WHERE email = %s", (email,))
        assert [row[0] for row in cur.fetchall()] == ["default"]

    login = await _login(test_client, email)
    assert login.status_code == 200, login.text
    assert login.json()["user"]["id"] == user_id
