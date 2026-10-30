"""Only a root operator may claim, change or drop a tenant's SSO domains.

A claim decides which tenant every new SSO user from that domain lands in,
across the whole deployment. That is the same kind of platform-wide decision
as re-parenting a tenant, so it needs the same root operator: an owner/admin
whose home AND acting tenant are both the platform tenant. Any other owner or
admin may still edit ``displayName`` and the other ``settings`` keys of a
tenant it can reach, provided the save leaves ``sso_domains`` as it was.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "test-signing-key-at-least-32-bytes-long-xyz"


def _root() -> str:
    from robothor.constants import DEFAULT_TENANT

    return DEFAULT_TENANT


def _tenants() -> dict[str, dict]:
    root = _root()
    return {
        root: {"id": root, "displayName": "Root", "parentTenantId": None, "settings": {}},
        "acme-child-a": {
            "id": "acme-child-a",
            "displayName": "A",
            "parentTenantId": root,
            "settings": {"sso_domains": ["child.example"], "theme": "dark"},
        },
        "acme-child-b": {
            "id": "acme-child-b",
            "displayName": "B",
            "parentTenantId": root,
            "settings": {},
        },
    }


def _list(parent_id=None, active_only=True):
    return [t for t in _tenants().values() if parent_id and t["parentTenantId"] == parent_id]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    from robothor.auth import tokens

    tokens.reset_signing_key_cache()
    with (
        patch("robothor.crm.dal.list_tenants", side_effect=_list),
        patch("robothor.crm.dal.get_tenant", side_effect=lambda tid: _tenants().get(tid)),
    ):
        yield
    tokens.reset_signing_key_cache()


def _bearer(tenant: str, role: str = "admin", home: str | None = None) -> dict[str, str]:
    from robothor.auth import tokens

    token = tokens.issue_access_token("u-alice", tenant, role, home_tenant_id=home)
    return {"Authorization": f"Bearer {token}"}


async def _patch(client, tenant_id: str, body: dict, headers: dict):
    with patch("routers.tenants.update_tenant", return_value=True) as update:
        response = await client.patch(f"/api/tenants/{tenant_id}", json=body, headers=headers)
    return response, update


@pytest.mark.asyncio
async def test_a_child_admin_cannot_claim_a_domain_for_its_own_tenant(test_client):
    r, update = await _patch(
        test_client,
        "acme-child-b",
        {"settings": {"sso_domains": ["example.com"]}},
        _bearer("acme-child-b"),
    )
    assert r.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_child_admin_cannot_change_its_claim(test_client):
    r, update = await _patch(
        test_client,
        "acme-child-a",
        {"settings": {"theme": "dark", "sso_domains": ["child.example", "example.com"]}},
        _bearer("acme-child-a"),
    )
    assert r.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_child_admin_cannot_drop_its_claim_by_omitting_it(test_client):
    """``settings`` replaces the whole object, so leaving the key out drops it."""
    r, update = await _patch(
        test_client, "acme-child-a", {"settings": {"theme": "light"}}, _bearer("acme-child-a")
    )
    assert r.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_root_admin_acting_in_a_child_cannot_claim(test_client):
    r, update = await _patch(
        test_client,
        "acme-child-b",
        {"settings": {"sso_domains": ["example.com"]}},
        _bearer("acme-child-b", home=_root()),
    )
    assert r.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_root_operator_may_claim_a_domain_for_a_child(test_client):
    r, update = await _patch(
        test_client,
        "acme-child-b",
        {"settings": {"sso_domains": ["example.com"]}},
        _bearer(_root()),
    )
    assert r.status_code == 200, r.text
    assert update.call_args.kwargs["settings"] == {"sso_domains": ["example.com"]}


@pytest.mark.asyncio
async def test_a_child_admin_may_edit_name_and_settings_leaving_the_claim_as_it_was(test_client):
    r, update = await _patch(
        test_client,
        "acme-child-a",
        {
            "displayName": "Acme Child A",
            # Same claim, written in another case: unchanged.
            "settings": {"theme": "light", "sso_domains": ["CHILD.example"]},
        },
        _bearer("acme-child-a"),
    )
    assert r.status_code == 200, r.text
    assert update.call_args.kwargs["display_name"] == "Acme Child A"


@pytest.mark.asyncio
async def test_a_child_admin_may_edit_settings_of_an_unclaimed_tenant(test_client):
    r, update = await _patch(
        test_client, "acme-child-b", {"settings": {"theme": "dark"}}, _bearer("acme-child-b")
    )
    assert r.status_code == 200, r.text
    assert update.call_args.kwargs["settings"] == {"theme": "dark"}


@pytest.mark.asyncio
async def test_a_patch_that_changes_nothing_is_200_not_404(test_client):
    """``{"parentTenantId": null}`` names no field to change. update_tenant
    reports False for an empty change, which is not a missing tenant."""
    with patch("routers.tenants.update_tenant", return_value=False):
        r = await test_client.patch(
            "/api/tenants/acme-child-a", json={"parentTenantId": None}, headers=_bearer(_root())
        )
    assert r.status_code == 200, r.text
    assert r.json() == {"success": True, "id": "acme-child-a"}
