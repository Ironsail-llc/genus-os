"""SSO domain routing with RLS on, as ``robothor_app``: a bound account keeps its tenant.

Claiming a domain decides where a NEW account is created. It must not change
where an account already bound to the person's IdP subject lives: the lookup
of ``(issuer, subject)`` has to stay unbound even though the routed tenant is
some other child. Bound to the routed tenant, the lookup misses, and the
INSERT collides with the platform-wide ``(issuer, subject)`` unique index.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# The fixtures (robothor_app connections, three tenants) live with the
# tenant-switch RLS tests; reuse them rather than keep a second copy.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tenant_switch_rls_integration import (  # noqa: E402, F401, F811 - pytest fixtures
    _signing_key,
    app_role,
    tenants,
)

pytestmark = pytest.mark.integration

ISSUER = "https://idp.example.com"


@pytest.fixture(autouse=True)
def _sso_env(monkeypatch):
    monkeypatch.setenv("GENUS_BRIDGE_SSO_SECRET", "dashboard-shared-secret")
    monkeypatch.setenv("GENUS_OIDC_ISSUERS", ISSUER)
    monkeypatch.delenv("GENUS_AUTH_ENFORCE", raising=False)


@pytest.mark.asyncio
async def test_a_bound_account_signs_in_to_its_own_tenant_when_its_domain_is_claimed_elsewhere(
    test_client,
    db_conn,
    tenants,  # noqa: F811 - the imported fixture
    app_role,  # noqa: F811 - the imported fixture
):
    subject = f"sub-{uuid.uuid4().hex[:8]}"
    email = f"alice-{subject}@example.com"
    with db_conn.cursor() as cur:
        cur.execute(
            'UPDATE crm_tenants SET settings = \'{"sso_domains": ["example.com"]}\'::jsonb '
            "WHERE id = %s",
            (tenants["a"],),
        )
        cur.execute(
            "INSERT INTO user_accounts (tenant_id, email, idp_issuer, idp_subject, "
            "display_name, role, status) VALUES (%s, %s, %s, %s, 'Alice', 'member', 'active') "
            "RETURNING id",
            (tenants["b"], email, ISSUER, subject),
        )
        bound_id = str(cur.fetchone()[0])

    from robothor.auth import accounts

    # The premise: this address IS routed to child a, not to the bound tenant.
    assert accounts.tenant_for_email(email) == tenants["a"]

    r = await test_client.post(
        "/api/auth/sso",
        json={
            "issuer": ISSUER,
            "subject": subject,
            "email": email,
            "email_verified": True,
            "display_name": "Alice",
        },
        headers={"X-Bridge-Auth": "dashboard-shared-secret"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == bound_id
    assert r.json()["user"]["tenant_id"] == tenants["b"]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT tenant_id FROM user_accounts WHERE idp_issuer = %s AND idp_subject = %s",
            (ISSUER, subject),
        )
        assert [row[0] for row in cur.fetchall()] == [tenants["b"]]
