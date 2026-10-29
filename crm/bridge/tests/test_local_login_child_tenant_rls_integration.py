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
