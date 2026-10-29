"""Tenant reads are scoped to what the caller may reach.

``GET /api/tenants`` and ``GET /api/tenants/{id}`` used to return every tenant
row to any owner/admin. With a hierarchy that is a leak: a child tenant's admin
would see its siblings (and the root). Now a caller sees
``resolve_accessible_tenants(home, role)`` — its home and, for an owner/admin,
the home's descendants. The root operator at home sees every tenant.

Hierarchy::

    acme-parent          (the platform tenant for these tests)
    ├── acme-child-a
    └── acme-child-b
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "test-signing-key-at-least-32-bytes-long-xyz"
ROOT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"

ROWS = {
    ROOT: {"id": ROOT, "displayName": "Parent", "parentTenantId": None},
    CHILD_A: {"id": CHILD_A, "displayName": "Child A", "parentTenantId": ROOT},
    CHILD_B: {"id": CHILD_B, "displayName": "Child B", "parentTenantId": ROOT},
    "acme-elsewhere": {"id": "acme-elsewhere", "displayName": "Other", "parentTenantId": None},
}


def _accessible(tenant, role=None, **_kwargs):
    if role not in ("owner", "admin"):
        return (tenant,)
    return {ROOT: (ROOT, CHILD_A, CHILD_B), CHILD_A: (CHILD_A,), CHILD_B: (CHILD_B,)}[tenant]


def _list(parent_id=None, active_only=True):
    return [r for r in ROWS.values() if parent_id is None or r["parentTenantId"] == parent_id]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    import routers._operator as operator

    from robothor.auth import tokens

    monkeypatch.setattr(operator, "PLATFORM_TENANT", ROOT)
    tokens.reset_signing_key_cache()
    with (
        patch("routers.tenants.list_tenants", side_effect=_list),
        patch("routers.tenants.get_tenant", side_effect=lambda tid: ROWS.get(tid)),
        patch("robothor.engine.permissions.resolve_accessible_tenants", new=_accessible),
    ):
        yield
    tokens.reset_signing_key_cache()


def _client(tenant: str, role: str = "admin", *, home: str | None = None):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app, raise_server_exceptions=False)
    token = tokens.issue_access_token("alice", tenant, role, home_tenant_id=home)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _ids(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()["tenants"]}


def test_a_child_admin_lists_only_its_own_tenant():
    assert _ids(_client(CHILD_A).get("/api/tenants")) == {CHILD_A}


def test_a_root_admin_acting_in_a_child_lists_what_its_home_reaches():
    assert _ids(_client(CHILD_A, home=ROOT).get("/api/tenants")) == {ROOT, CHILD_A, CHILD_B}


def test_the_root_operator_at_home_lists_every_tenant():
    assert _ids(_client(ROOT).get("/api/tenants")) == set(ROWS)


def test_a_child_admin_cannot_read_a_sibling():
    assert _client(CHILD_A).get(f"/api/tenants/{CHILD_B}").status_code == 404


def test_a_child_admin_cannot_read_the_root():
    assert _client(CHILD_A).get(f"/api/tenants/{ROOT}").status_code == 404


def test_a_child_admin_reads_its_own_tenant():
    response = _client(CHILD_A).get(f"/api/tenants/{CHILD_A}")
    assert response.status_code == 200, response.text
    assert response.json()["id"] == CHILD_A


def test_the_root_operator_reads_any_tenant():
    assert _client(ROOT).get("/api/tenants/acme-elsewhere").status_code == 200


def test_the_hierarchy_change_gate_is_the_shared_root_gate():
    """S1's local helper now defers to ``require_root_operator``."""
    import inspect

    from routers import tenants

    assert "require_root_operator(" in inspect.getsource(tenants._is_root_tenant_operator)
