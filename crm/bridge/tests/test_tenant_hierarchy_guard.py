"""Only a root operator may shape the tenant hierarchy.

Tenant switching trusts ``crm_tenants.parent_tenant_id``: an admin may act in
any descendant of its home. ``crm_tenants`` has no RLS, so if any tenant's
admin could set a ``parent_tenant_id`` (or ``active``), a child admin could
re-parent its sibling — or the root — under itself and then switch in.

So creating a tenant, and changing ``parent_tenant_id`` or ``active``, needs
a ROOT operator: an owner/admin whose home AND acting tenant are both the
platform tenant. Any owner/admin may still edit ``displayName`` and
``settings`` of a tenant it can reach. A parent that would create a cycle or
push a subtree past depth 3 is refused server-side.

Hierarchy::

    <platform tenant>
    ├── acme-child-a
    │   └── acme-grandchild
    └── acme-child-b
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
        root: {"id": root, "displayName": "Root", "parentTenantId": None},
        "acme-child-a": {"id": "acme-child-a", "displayName": "A", "parentTenantId": root},
        "acme-child-b": {"id": "acme-child-b", "displayName": "B", "parentTenantId": root},
        "acme-grandchild": {
            "id": "acme-grandchild",
            "displayName": "G",
            "parentTenantId": "acme-child-a",
        },
        "acme-deep-1": {"id": "acme-deep-1", "displayName": "D1", "parentTenantId": None},
        "acme-deep-2": {"id": "acme-deep-2", "displayName": "D2", "parentTenantId": "acme-deep-1"},
        "acme-deep-3": {"id": "acme-deep-3", "displayName": "D3", "parentTenantId": "acme-deep-2"},
        "acme-deep-4": {"id": "acme-deep-4", "displayName": "D4", "parentTenantId": "acme-deep-3"},
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
async def test_a_child_admin_cannot_re_parent_its_sibling(test_client):
    r, update = await _patch(
        test_client, "acme-child-b", {"parentTenantId": "acme-child-a"}, _bearer("acme-child-a")
    )
    assert r.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_child_admin_cannot_re_parent_the_root(test_client):
    r, update = await _patch(
        test_client, _root(), {"parentTenantId": "acme-child-a"}, _bearer("acme-child-a")
    )
    assert r.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_child_admin_cannot_deactivate_itself_or_others(test_client):
    r, update = await _patch(
        test_client, "acme-child-a", {"active": False}, _bearer("acme-child-a")
    )
    assert r.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_root_admin_acting_in_a_child_is_not_a_root_operator(test_client):
    r, update = await _patch(
        test_client,
        "acme-child-b",
        {"parentTenantId": "acme-child-a"},
        _bearer("acme-child-a", home=_root()),
    )
    assert r.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_root_operator_may_re_parent(test_client):
    r, update = await _patch(
        test_client, "acme-child-b", {"parentTenantId": "acme-child-a"}, _bearer(_root())
    )
    assert r.status_code == 200, r.text
    assert update.call_args.kwargs["parent_tenant_id"] == "acme-child-a"


@pytest.mark.asyncio
async def test_a_cycle_is_refused(test_client):
    r, update = await _patch(
        test_client, "acme-child-a", {"parentTenantId": "acme-grandchild"}, _bearer(_root())
    )
    assert r.status_code == 422
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_tenant_cannot_be_its_own_parent(test_client):
    r, update = await _patch(
        test_client, "acme-child-a", {"parentTenantId": "acme-child-a"}, _bearer(_root())
    )
    assert r.status_code == 422
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_parent_past_depth_three_is_refused(test_client):
    """acme-deep-3 sits at depth 2; hanging acme-child-a (which has a child)
    under it puts acme-grandchild at depth 4, past what switching reaches."""
    r, update = await _patch(
        test_client, "acme-child-a", {"parentTenantId": "acme-deep-3"}, _bearer(_root())
    )
    assert r.status_code == 422
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_missing_parent_is_refused(test_client):
    r, update = await _patch(
        test_client, "acme-child-a", {"parentTenantId": "acme-nowhere"}, _bearer(_root())
    )
    assert r.status_code == 422
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_child_admin_may_rename_its_own_tenant(test_client):
    r, update = await _patch(
        test_client, "acme-child-a", {"displayName": "Renamed"}, _bearer("acme-child-a")
    )
    assert r.status_code == 200, r.text
    assert update.call_args.kwargs == {"display_name": "Renamed"}


@pytest.mark.asyncio
async def test_a_child_admin_may_not_rename_its_sibling(test_client):
    r, update = await _patch(
        test_client, "acme-child-b", {"displayName": "Mine now"}, _bearer("acme-child-a")
    )
    assert r.status_code == 403
    update.assert_not_called()


@pytest.mark.asyncio
async def test_a_root_admin_may_rename_a_descendant(test_client):
    r, update = await _patch(
        test_client, "acme-grandchild", {"settings": {"k": "v"}}, _bearer(_root())
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_only_a_root_operator_may_create_a_tenant(test_client):
    with patch("routers.tenants.create_tenant", return_value="acme-new") as create:
        r = await test_client.post(
            "/api/tenants",
            json={"id": "acme-new", "displayName": "New", "parentTenantId": "acme-child-a"},
            headers=_bearer("acme-child-a"),
        )
        assert r.status_code == 403
        create.assert_not_called()

        r = await test_client.post(
            "/api/tenants",
            json={"id": "acme-new", "displayName": "New", "parentTenantId": "acme-child-a"},
            headers=_bearer(_root()),
        )
        assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_create_refuses_a_parent_past_depth_three(test_client):
    with patch("routers.tenants.create_tenant", return_value="acme-new") as create:
        r = await test_client.post(
            "/api/tenants",
            json={"id": "acme-new", "displayName": "New", "parentTenantId": "acme-deep-4"},
            headers=_bearer(_root()),
        )
    assert r.status_code == 422
    create.assert_not_called()


@pytest.mark.asyncio
async def test_re_parent_then_switch_does_not_reach_the_root(test_client, monkeypatch):
    """The end-to-end attack: a child admin hangs the root under itself, then
    switches into it. The re-parent is refused, so the switch is too."""
    from robothor.federation import rls_gate
    from robothor.settings import reset_settings

    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: True)
    monkeypatch.setattr(rls_gate, "_connection_is_superuser", lambda: False)
    reset_settings()
    headers = _bearer("acme-child-a")
    try:
        r, update = await _patch(test_client, _root(), {"parentTenantId": "acme-child-a"}, headers)
        assert r.status_code == 403
        update.assert_not_called()

        account = {
            "id": "u-alice",
            "email": "alice@example.com",
            "display_name": "Alice",
            "role": "admin",
            "tenant_id": "acme-child-a",
            "status": "active",
        }
        with (
            patch("robothor.auth.accounts.get_account_by_id", return_value=account),
            patch("routers.auth.audited"),
        ):
            r = await test_client.post(
                "/api/auth/switch-tenant", json={"tenant": _root()}, headers=headers
            )
        assert r.status_code == 403
        assert "access_token" not in r.json()
    finally:
        reset_settings()


@pytest.mark.asyncio
async def test_the_root_is_the_platform_tenant_not_the_default(test_client, monkeypatch):
    """ROBOTHOR_PLATFORM_TENANT is what require_operator treats as the root.
    When it differs from DEFAULT_TENANT, the default tenant's admins must not
    reshape the hierarchy, and the platform tenant's must."""
    import routers._operator as operator

    monkeypatch.setattr(operator, "PLATFORM_TENANT", "acme-platform")

    r, update = await _patch(
        test_client, "acme-child-b", {"parentTenantId": "acme-child-a"}, _bearer(_root())
    )
    assert r.status_code == 403
    update.assert_not_called()

    r, update = await _patch(
        test_client, "acme-child-b", {"parentTenantId": "acme-child-a"}, _bearer("acme-platform")
    )
    assert r.status_code == 200, r.text
