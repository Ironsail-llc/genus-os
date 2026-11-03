"""Self-service account routes work while a root admin is acting in a child.

The account lives in the HOME tenant. A request acting in ``acme-child-a`` is
bound to that child, and with RLS on the home row is invisible there — so a
password change or an MFA enrolment that looked the account up under the
request's binding answered "wrong password" to the right one.

Real Postgres, real RLS, the non-superuser role: the fixtures are shared with
``test_tenant_switch_rls_integration.py``.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _rls_fixtures():
    """The S1 RLS fixtures, loaded by path: ``tests`` names the repo-root package."""
    import importlib.util

    path = Path(__file__).with_name("test_tenant_switch_rls_integration.py")
    spec = importlib.util.spec_from_file_location("_bridge_rls_fixtures", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_shared = _rls_fixtures()
_signing_key = _shared._signing_key
app_role = _shared.app_role
tenants = _shared.tenants

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple-1"


@pytest.fixture(autouse=True)
def _local_login_on(monkeypatch):
    monkeypatch.setenv("GENUS_LOCAL_LOGIN", "true")
    from robothor.auth import local_login
    from robothor.settings import reset_settings

    reset_settings()
    for name in ("reset_throttle", "reset_flood_limiter", "reset_rate_limits"):
        reset = getattr(local_login, name, None)
        if callable(reset):
            reset()
    yield
    reset_settings()


def _seed_root_admin(db_conn, tenant: str) -> str:
    from robothor.auth.local_login import hash_password

    account_id = str(uuid.uuid4())
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO user_accounts (id, tenant_id, email, display_name, role, status, "
            "password_hash) VALUES (%s, %s, %s, 'Alice', 'admin', 'active', %s)",
            (account_id, tenant, f"alice-{account_id[:8]}@example.com", hash_password(PASSWORD)),
        )
    return account_id


def _acting_headers(account_id: str, tenants: dict[str, str]) -> dict[str, str]:
    from robothor.auth import tokens

    token = tokens.issue_access_token(
        account_id, tenants["a"], "admin", home_tenant_id=tenants["parent"]
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_a_root_admin_acting_in_a_child_can_change_their_password(
    test_client, db_conn, tenants, app_role
):
    account_id = _seed_root_admin(db_conn, tenants["parent"])
    response = await test_client.post(
        "/api/auth/password",
        json={"current_password": PASSWORD, "new_password": PASSWORD + "-new"},
        headers=_acting_headers(account_id, tenants),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"success": True}


@pytest.mark.asyncio
async def test_a_root_admin_acting_in_a_child_can_enrol_mfa(
    test_client, db_conn, tenants, app_role
):
    account_id = _seed_root_admin(db_conn, tenants["parent"])
    response = await test_client.post(
        "/api/auth/mfa/enroll",
        json={"password": PASSWORD},
        headers=_acting_headers(account_id, tenants),
    )
    assert response.status_code == 200, response.text
    assert response.json().get("secret") or response.json().get("otpauth_uri")
