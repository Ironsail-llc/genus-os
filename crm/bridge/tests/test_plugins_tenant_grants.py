"""Plugins per tenant: each tenant's engine, and the root tenant's grants.

Every tenant runs its own engine, so "which plugins are on" is a question for
the ACTING tenant's engine. The listing, enable, disable and reload are that
tenant's own administration (``require_tenant_admin``) and go to its engine.
Installing, removing and recording distributions change what is on a box, so
they stay root-only, and name the tenant whose engine they act on.

The root tenant also decides for its children:
``PUT /api/plugins/{name}/tenants/{tenant_id}`` forces a plugin on or off for
one child (or clears the grant), writes ``tenant_plugin_grants``, audits it,
and reloads that child's engine so the decision applies now. A reload that
fails is reported in the answer, never hidden behind the grant's success.

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
OUTSIDER = "acme-unrelated"


class TenantEngines:
    """Every tenant's engine at once, recording which one each call reached."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, dict | None]] = []
        self.status = 200
        self.reload_status = 200

    async def __call__(self, method, path, *, json=None, timeout=30, tenant=None):
        self.calls.append((method, path, tenant, json))
        if path.endswith("/reload"):
            if self.reload_status >= 400:
                return self.reload_status, {"error": "engine unreachable"}
            return self.reload_status, {"generation": 7, "loaded": 1, "failures": []}
        if path.endswith(("/enable", "/disable")):
            return self.status, {"name": "acme-tools", "enabled": True, "reloaded": False}
        return self.status, {"generation": 1, "plugins": [], "tenant": tenant}

    def tenants(self) -> list[str | None]:
        return [call[2] for call in self.calls]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    import routers._operator as operator

    from robothor.auth import tokens
    from robothor.settings import reset_settings

    monkeypatch.setattr(operator, "PLATFORM_TENANT", ROOT)
    tokens.reset_signing_key_cache()
    reset_settings()
    yield
    tokens.reset_signing_key_cache()


def _hierarchy(tenant_id: str, user_role: str | None = None, **_kw):
    if tenant_id == ROOT and user_role in ("owner", "admin"):
        return (ROOT, CHILD_A, CHILD_B)
    return (tenant_id,)


@pytest.fixture
def engines():
    fake = TenantEngines()
    with (
        patch("routers.plugins.engine_request", new=fake),
        patch("robothor.engine.permissions.resolve_accessible_tenants", new=_hierarchy),
        patch("routers._audit.log_event"),
    ):
        yield fake


@pytest.fixture
def grant_store():
    """``tenant_plugin_grants``, in memory, patched where the router reads it."""
    store: dict[tuple[str, str], tuple[str, str]] = {}

    def set_grant(tenant_id, plugin, state, set_by, **_kw):
        store[(tenant_id, plugin)] = (state, set_by)

    def clear_grant(tenant_id, plugin, **_kw):
        return store.pop((tenant_id, plugin), None) is not None

    with (
        patch("robothor.plugins.grants.set_grant", new=set_grant),
        patch("robothor.plugins.grants.clear_grant", new=clear_grant),
    ):
        yield store


def _client(tenant: str, role: str = "admin", *, home: str | None = None):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app, raise_server_exceptions=False)
    token = tokens.issue_access_token("alice", tenant, role, home_tenant_id=home)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def child_admin():
    return _client(CHILD_A)


def root_admin_in_child():
    return _client(CHILD_A, home=ROOT)


def root_admin():
    return _client(ROOT)


# ─── the acting tenant's own plugins ──────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/plugins"),
        ("post", "/api/plugins/acme-tools/enable"),
        ("post", "/api/plugins/acme-tools/disable"),
        ("post", "/api/plugins/reload"),
    ],
)
class TestTheActingTenantsEngine:
    def test_a_child_admin_reaches_the_childs_engine(self, engines, method, path):
        response = getattr(child_admin(), method)(path)
        assert response.status_code == 200, response.text
        assert engines.tenants() == [CHILD_A]

    def test_a_root_admin_acting_in_a_child_reaches_the_childs_engine(
        self, engines, method, path
    ):
        response = getattr(root_admin_in_child(), method)(path)
        assert response.status_code == 200, response.text
        assert engines.tenants() == [CHILD_A]

    def test_the_root_admin_at_home_reaches_the_root_engine(self, engines, method, path):
        response = getattr(root_admin(), method)(path)
        assert response.status_code == 200, response.text
        assert engines.tenants() == [ROOT]

    def test_a_member_of_the_child_is_refused(self, engines, method, path):
        assert getattr(_client(CHILD_A, "member"), method)(path).status_code == 403
        assert not engines.calls


def test_a_forced_plugins_409_reaches_the_child_admin(engines):
    """The engine refuses to toggle a plugin the root forced; the proxy keeps that."""
    engines.status = 409
    assert child_admin().post("/api/plugins/acme-tools/disable").status_code == 409


# ─── what is on the box stays root ────────────────────────────────────


@pytest.mark.parametrize(
    "path", ["/api/plugins/install", "/api/plugins/sync", "/api/plugins/acme-tools/remove"]
)
class TestBoxLevelActsStayRoot:
    def test_a_child_admin_is_refused(self, engines, path):
        assert child_admin().post(path, json={"name": "acme-tools"}).status_code == 403
        assert not engines.calls

    def test_a_root_admin_acting_in_a_child_is_refused(self, engines, path):
        assert root_admin_in_child().post(path, json={"name": "acme-tools"}).status_code == 403
        assert not engines.calls

    def test_the_root_defaults_to_its_own_engine(self, engines, path):
        response = root_admin().post(path, json={"name": "acme-tools"})
        assert response.status_code == 200, response.text
        assert engines.tenants() == [ROOT]

    def test_the_root_may_name_a_child_engine(self, engines, path):
        response = root_admin().post(path, json={"name": "acme-tools", "tenant_id": CHILD_B})
        assert response.status_code == 200, response.text
        assert engines.tenants() == [CHILD_B]

    def test_a_tenant_outside_the_roots_tree_never_reaches_an_engine(self, engines, path):
        response = root_admin().post(path, json={"name": "acme-tools", "tenant_id": OUTSIDER})
        assert response.status_code == 404
        assert not engines.calls


def test_the_target_tenant_is_not_forwarded_to_the_engine(engines):
    root_admin().post("/api/plugins/install", json={"name": "acme-tools", "tenant_id": CHILD_A})
    [(_method, _path, _tenant, body)] = engines.calls
    assert "tenant_id" not in body


# ─── the root forces a plugin for one child ───────────────────────────

GRANT = f"/api/plugins/acme-tools/tenants/{CHILD_A}"


class TestTheGrantGate:
    def test_a_child_admin_is_refused(self, engines, grant_store):
        assert child_admin().put(GRANT, json={"state": "on"}).status_code == 403
        assert not grant_store and not engines.calls

    def test_a_root_admin_acting_in_a_child_is_refused(self, engines, grant_store):
        assert root_admin_in_child().put(GRANT, json={"state": "on"}).status_code == 403
        assert not grant_store and not engines.calls


class TestTheGrant:
    @pytest.mark.parametrize("state", ["on", "off"])
    def test_it_writes_the_grant_and_reloads_that_childs_engine(self, engines, grant_store, state):
        response = root_admin().put(GRANT, json={"state": state})
        assert response.status_code == 200, response.text
        assert grant_store == {(CHILD_A, "acme-tools"): (state, "operator:alice")}
        assert engines.calls == [("POST", "/api/admin/plugins/reload", CHILD_A, None)]
        body = response.json()
        assert body["plugin"] == "acme-tools"
        assert body["tenant_id"] == CHILD_A
        assert body["state"] == state
        assert body["reloaded"] is True

    def test_null_clears_the_grant(self, engines, grant_store):
        grant_store[(CHILD_A, "acme-tools")] = ("off", "operator:bob")
        response = root_admin().put(GRANT, json={"state": None})
        assert response.status_code == 200, response.text
        assert grant_store == {}
        assert response.json()["state"] is None
        assert engines.tenants() == [CHILD_A]

    @pytest.mark.parametrize("body", [{"state": "enabled"}, {"state": True}, {}, {"state": "ON"}])
    def test_a_state_that_is_not_on_off_or_null_is_a_422(self, engines, grant_store, body):
        assert root_admin().put(GRANT, json=body).status_code == 422
        assert not grant_store and not engines.calls

    def test_a_tenant_outside_the_roots_tree_is_a_404(self, engines, grant_store):
        response = root_admin().put(f"/api/plugins/acme-tools/tenants/{OUTSIDER}", json={"state": "off"})
        assert response.status_code == 404
        assert not grant_store and not engines.calls

    def test_the_root_cannot_grant_itself(self, engines, grant_store):
        """The root decides for itself with enable and disable; a grant is for a child."""
        response = root_admin().put(f"/api/plugins/acme-tools/tenants/{ROOT}", json={"state": "off"})
        assert response.status_code == 422
        assert not grant_store and not engines.calls

    def test_a_name_that_is_not_a_distribution_is_refused(self, engines, grant_store):
        response = root_admin().put(f"/api/plugins/bad name/tenants/{CHILD_A}", json={"state": "on"})
        assert response.status_code in (404, 422)
        assert not grant_store and not engines.calls

    def test_a_failed_reload_is_reported_not_hidden(self, engines, grant_store):
        engines.reload_status = 502
        response = root_admin().put(GRANT, json={"state": "off"})
        assert response.status_code == 200
        body = response.json()
        assert body["reloaded"] is False
        assert body["reload_status"] == 502
        assert body["reload"] == {"error": "engine unreachable"}
        assert grant_store[(CHILD_A, "acme-tools")][0] == "off"

    def test_a_grant_that_cannot_be_written_is_a_503_and_reloads_nothing(self, engines):
        def broken(*_a, **_kw):
            raise RuntimeError("database unavailable")

        with patch("robothor.plugins.grants.set_grant", new=broken):
            response = root_admin().put(GRANT, json={"state": "off"})
        assert response.status_code == 503
        assert "database unavailable" not in response.text
        assert not engines.calls

    def test_it_is_audited_with_identifiers_and_filed_under_the_child(self, engines, grant_store):
        with patch("routers.plugins.audited") as audited:
            root_admin().put(GRANT, json={"state": "off"})
        assert audited.call_count == 1
        args, kwargs = audited.call_args
        assert args[1] == "plugin.grant"
        assert kwargs["plugin"] == "acme-tools"
        assert kwargs["tenant"] == CHILD_A
        assert kwargs["state"] == "off"
        assert kwargs["audit_tenant_id"] == CHILD_A
        assert set(kwargs) <= {"action", "status", "plugin", "tenant", "state", "audit_tenant_id"}
