"""The admin surface is tenant-aware: which gate every admin route carries.

After tenant switching, a root owner/admin can hold a token acting in a child
tenant. Every admin route therefore falls into exactly one class:

* ``tenant`` — the acting tenant's own administration (its fleet, runs,
  approvals, accounts, keys). Any owner/admin whose token acts in that tenant
  may use it: the child's own admin, and a root admin who switched in.
* ``tenant_audit`` — the same, read-only, plus the ``auditor`` role.
* ``root`` — appliance-wide state (guardrail flags, settings, logs, plugin
  installs and per-child plugin grants, the doctor). Only a root operator AT HOME: a root admin acting in a child is
  not root for that request.
* ``root_audit`` — appliance-wide and read-only: a root operator or auditor at
  home.
* ``open`` — not an admin route at all (liveness probes, scope-gated tenant
  data). Listed so the enumeration below can tell "classified as open" from
  "nobody looked".

The table is the contract, and the enumeration test makes it complete: a route
added to one of these routers without a row here fails the suite.

The gate is exercised over real HTTP — middleware, token verification, tenant
header check and all — and cut off the instant the handler's gate returns: each
gate is wrapped so that passing it raises a 418. So "allowed" means "the gate
let this identity through", and nothing behind the gate (DAL, engine,
filesystem) runs at all.

Hierarchy used throughout::

    acme-parent          (the platform tenant for these tests)
    ├── acme-child-a
    └── acme-child-b
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "test-signing-key-at-least-32-bytes-long-xyz"
ROOT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"

#: The routers whose every route must be classified below.
ADMIN_ROUTER_MODULES = (
    "routers.agent_manifests",
    "routers.installed_agents",
    "routers.channel_access",
    "routers.approvals",
    "routers.automations",
    "routers.runs",
    "routers.workflows",
    "routers.fleet",
    "routers.memory_facts",
    "routers.users",
    "routers.providers",
    "routers.audit",
    "routers.health",
    "routers.controls",
    "routers.flag_audit",
    "routers.system_health",
    "routers.logs",
    "routers.settings",
    "routers.plugins",
    "routers.n8n",
)

#: Every gate a handler may call. Each is wrapped in every admin router module
#: so that passing it is observable as a 418.
GATE_NAMES = (
    "require_tenant_admin",
    "require_tenant_operator",
    "require_tenant_audit_reader",
    "require_root_operator",
    "require_root_audit_reader",
    "require_operator",
    "require_audit_reader",
)

T, TA, R, RA, OPEN = "tenant", "tenant_audit", "root", "root_audit", "open"

ROUTE_CLASSES: dict[tuple[str, str], str] = {
    # ── agent manifests: the acting tenant's fleet, through its own engine ──
    ("GET", "/api/agent-manifests"): T,
    ("GET", "/api/agent-manifests/{agent_id}"): T,
    ("POST", "/api/agent-manifests/validate"): T,
    ("POST", "/api/agent-manifests"): T,
    ("PATCH", "/api/agent-manifests/{agent_id}"): T,
    ("POST", "/api/agent-manifests/{agent_id}/enable"): T,
    ("POST", "/api/agent-manifests/{agent_id}/disable"): T,
    ("DELETE", "/api/agent-manifests/{agent_id}"): T,
    ("POST", "/api/agent-manifests/{agent_id}/run"): T,
    # ── marketplace installs write the bridge host's own workspace ──
    ("GET", "/api/installed-agents"): R,
    ("POST", "/api/installed-agents/install"): R,
    ("POST", "/api/installed-agents/{agent_id}/update"): R,
    ("DELETE", "/api/installed-agents/{agent_id}"): R,
    ("POST", "/api/installed-agents/{agent_id}/export"): R,
    ("GET", "/api/installed-agents/{agent_id}/export/plan"): R,
    ("GET", "/api/installed-agents/{agent_id}/readiness"): R,
    # ── channel pairing: the acting tenant's identities ──
    ("GET", "/api/channels"): T,
    ("POST", "/api/channels/{name}/verify"): T,
    ("GET", "/api/channels/{name}/pending"): T,
    ("POST", "/api/channels/{name}/pairings/{code}/approve"): T,
    ("POST", "/api/channels/{name}/pairings/{code}/deny"): T,
    ("GET", "/api/channels/{name}/identities"): T,
    ("DELETE", "/api/channels/{name}/identities/{identity_id}"): T,
    # ── operations ──
    ("GET", "/api/approvals"): T,
    ("POST", "/api/approvals/{kind}/{approval_id}"): T,
    ("GET", "/api/automations"): T,
    ("POST", "/api/automations/{agent_id}/reset-breaker"): T,
    ("GET", "/api/runs"): T,
    ("GET", "/api/runs/{run_id}"): T,
    ("GET", "/api/workflows"): T,
    ("GET", "/api/workflows/{workflow_id}/runs"): T,
    ("GET", "/api/fleet"): T,
    ("GET", "/api/fleet/{agent_id}"): T,
    ("GET", "/api/memory/facts"): T,
    ("POST", "/api/memory/facts/{fact_id}/forget/preview"): T,
    ("POST", "/api/memory/facts/{fact_id}/forget"): T,
    # ── accounts ──
    ("GET", "/api/auth/roles"): T,
    ("GET", "/api/users"): T,
    ("POST", "/api/users"): T,
    ("PATCH", "/api/users/{user_id}"): T,
    ("POST", "/api/users/{user_id}/binding-grant"): T,
    ("GET", "/api/users/{user_id}/binding-grants"): T,
    # ── provider keys live in the acting tenant's vault ──
    ("GET", "/api/providers"): T,
    ("GET", "/api/models"): T,
    ("PUT", "/api/providers/{provider_id}/keys/{position}"): T,
    ("DELETE", "/api/providers/{provider_id}/keys/{position}"): T,
    ("POST", "/api/providers/{provider_id}/test"): T,
    ("GET", "/api/providers/defaults"): T,
    # _defaults.yaml is a file on the bridge host's own workspace.
    ("PATCH", "/api/providers/defaults"): R,
    # ── audit ──
    ("GET", "/api/audit/events"): TA,
    ("GET", "/api/audit/events.csv"): TA,
    ("GET", "/api/audit/stats"): TA,
    ("GET", "/api/audit"): TA,
    ("GET", "/api/audit/guardrails"): RA,
    ("GET", "/api/controls/audit"): RA,
    # ── appliance-wide ──
    ("GET", "/api/doctor"): R,
    ("GET", "/api/telemetry"): R,
    ("GET", "/api/controls"): R,
    ("PATCH", "/api/controls/{name}"): R,
    ("GET", "/api/health/system"): R,
    ("GET", "/api/logs/units"): R,
    ("GET", "/api/logs"): R,
    ("GET", "/api/settings/schema"): R,
    ("GET", "/api/settings"): R,
    ("PATCH", "/api/settings"): R,
    # ── plugins: each tenant's engine runs its own set. What is installed on
    #    the box, and the root's per-child grants, stay root. ──
    ("GET", "/api/plugins"): T,
    ("POST", "/api/plugins/reload"): T,
    ("POST", "/api/plugins/{name}/enable"): T,
    ("POST", "/api/plugins/{name}/disable"): T,
    ("POST", "/api/plugins/sync"): R,
    ("POST", "/api/plugins/install"): R,
    ("POST", "/api/plugins/{name}/remove"): R,
    ("PUT", "/api/plugins/{name}/tenants/{tenant_id}"): R,
    # The external n8n instance is watched by the appliance, not per tenant.
    ("GET", "/api/integrations/n8n/workflows"): R,
    ("GET", "/api/integrations/n8n/workflows/{workflow_id}/executions"): R,
    # ── not admin routes ──
    ("GET", "/health"): OPEN,
    ("GET", "/liveness"): OPEN,
    ("GET", "/live"): OPEN,
    ("GET", "/ready"): OPEN,
}

GATED_CLASSES = (T, TA, R, RA)


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    # Acting tokens are honoured only while switching is enforced.
    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    from robothor.auth import tokens
    from robothor.settings import reset_settings

    tokens.reset_signing_key_cache()
    reset_settings()
    yield
    tokens.reset_signing_key_cache()
    reset_settings()


@pytest.fixture
def root_is_parent(monkeypatch):
    """Make ``acme-parent`` the platform tenant for the gates.

    Not ``DEFAULT_TENANT``: a check that still compared against the default
    tenant instead of the platform tenant would pass silently otherwise.
    """
    import routers._operator as operator

    monkeypatch.setattr(operator, "PLATFORM_TENANT", ROOT)
    return ROOT


def _token(tenant: str, role: str, *, home: str | None = None, user: str = "alice") -> str:
    from robothor.auth import tokens

    return tokens.issue_access_token(user, tenant, role, home_tenant_id=home)


IDENTITIES: dict[str, dict[str, Any]] = {
    # name: tenant, role, home, extra headers
    "child_admin_own": {"tenant": CHILD_A, "role": "admin"},
    "child_owner_own": {"tenant": CHILD_A, "role": "owner"},
    "child_auditor_own": {"tenant": CHILD_A, "role": "auditor"},
    "child_member_own": {"tenant": CHILD_A, "role": "member"},
    "parent_acting_child": {"tenant": CHILD_A, "role": "admin", "home": ROOT},
    "sibling_admin": {
        "tenant": CHILD_B,
        "role": "admin",
        "headers": {"X-Tenant-Id": CHILD_A},
    },
    "parent_at_home": {"tenant": ROOT, "role": "admin"},
    "parent_auditor_at_home": {"tenant": ROOT, "role": "auditor"},
}

#: Who each class lets through. Everyone else in IDENTITIES must be refused.
ALLOWED: dict[str, set[str]] = {
    T: {"child_admin_own", "child_owner_own", "parent_acting_child", "parent_at_home"},
    TA: {
        "child_admin_own",
        "child_owner_own",
        "child_auditor_own",
        "parent_acting_child",
        "parent_at_home",
        "parent_auditor_at_home",
    },
    R: {"parent_at_home"},
    RA: {"parent_at_home", "parent_auditor_at_home"},
}


class GatePassed(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=418, detail="gate passed")


@pytest.fixture
def gated_app(root_is_parent, monkeypatch):
    """The real app, with every gate wrapped to stop the request once passed."""
    import importlib

    from bridge_service import app

    for module_name in ADMIN_ROUTER_MODULES:
        module = importlib.import_module(module_name)
        for name in GATE_NAMES:
            real = getattr(module, name, None)
            if real is None:
                continue

            def _wrapped(request, *args, __real=real, **kwargs):
                __real(request, *args, **kwargs)
                raise GatePassed

            monkeypatch.setattr(module, name, _wrapped)
    return app


# ─── Route enumeration and request synthesis ──────────────────────────


def _all_routes() -> list[Any]:
    from bridge_service import app

    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # pragma: no cover — FastAPI < 0.139
        return list(app.routes)
    return list(iter_route_contexts(app.routes))


def _admin_routes() -> list[tuple[str, str]]:
    found = []
    for route in _all_routes():
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or endpoint.__module__ not in ADMIN_ROUTER_MODULES:
            continue
        found.extend(
            (method, route.path)
            for method in sorted(set(route.methods or ()) - {"HEAD", "OPTIONS"})
        )
    return found


def _resolve(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def _sample(schema: dict[str, Any], spec: dict[str, Any]) -> Any:
    """The smallest JSON value this schema accepts (good enough for a gate test)."""
    schema = _resolve(schema, spec)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            options = [s for s in schema[key] if _resolve(s, spec).get("type") != "null"]
            return _sample(options[0] if options else schema[key][0], spec)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        return {
            name: _sample(prop, spec)
            for name, prop in (schema.get("properties") or {}).items()
            if name in set(schema.get("required") or ())
        }
    if kind == "array":
        return []
    if kind == "integer":
        return max(1, int(schema.get("minimum", 1)))
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    if schema.get("format") == "uuid":
        return "00000000-0000-4000-8000-000000000001"
    return "x" * max(3, int(schema.get("minLength", 3)))


def _request_for(method: str, path: str) -> tuple[str, dict[str, Any], Any]:
    """``(url, query params, json body)`` that clear FastAPI's own validation."""
    from bridge_service import app

    spec = app.openapi()
    operation = (spec["paths"].get(path) or {}).get(method.lower()) or {}
    url = path
    query: dict[str, Any] = {}
    for parameter in operation.get("parameters", []):
        value = _sample(parameter.get("schema") or {}, spec)
        if parameter["in"] == "path":
            url = url.replace("{" + parameter["name"] + "}", str(value))
        elif parameter["in"] == "query" and parameter.get("required"):
            query[parameter["name"]] = value
    body = None
    content = (operation.get("requestBody") or {}).get("content") or {}
    if "application/json" in content:
        body = _sample(content["application/json"].get("schema") or {}, spec)
    elif method in {"POST", "PUT", "PATCH", "DELETE"}:
        body = {}
    return url, query, body


def _call(app, identity: str, method: str, path: str) -> int:
    from fastapi.testclient import TestClient

    spec = IDENTITIES[identity]
    headers = {
        "Authorization": "Bearer " + _token(spec["tenant"], spec["role"], home=spec.get("home")),
        **(spec.get("headers") or {}),
    }
    url, query, body = _request_for(method, path)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.request(method, url, params=query, json=body, headers=headers)
    return response.status_code


# ─── The table is complete ────────────────────────────────────────────


def test_every_admin_route_is_classified() -> None:
    unclassified = sorted(set(_admin_routes()) - set(ROUTE_CLASSES))
    assert not unclassified, (
        "admin routes with no gate class in ROUTE_CLASSES — decide tenant, "
        f"tenant_audit, root, root_audit or open: {unclassified}"
    )


def test_no_classification_is_stale() -> None:
    stale = sorted(set(ROUTE_CLASSES) - set(_admin_routes()))
    assert not stale, f"ROUTE_CLASSES rows that match no route any more: {stale}"


def test_the_enumeration_did_not_collapse() -> None:
    assert len(_admin_routes()) >= 75


# ─── The gate matrix, over HTTP ───────────────────────────────────────

_MATRIX = [
    (method, path, cls, identity)
    for (method, path), cls in ROUTE_CLASSES.items()
    if cls in GATED_CLASSES
    for identity in IDENTITIES
]


@pytest.mark.parametrize(
    ("method", "path", "cls", "identity"),
    _MATRIX,
    ids=[f"{m} {p} [{c}] {i}" for m, p, c, i in _MATRIX],
)
def test_route_gate(gated_app, method: str, path: str, cls: str, identity: str) -> None:
    status = _call(gated_app, identity, method, path)
    if identity in ALLOWED[cls]:
        assert status == 418, f"{identity} should pass the {cls} gate, got {status}"
    else:
        assert status in (401, 403), f"{identity} should be refused by {cls}, got {status}"


# ─── The gates themselves ─────────────────────────────────────────────


def _request(tenant: str, role: str, *, home: str | None = None, service: bool = False):
    from types import SimpleNamespace

    from robothor.auth.deps import AuthContext

    auth = AuthContext(
        user_id="alice",
        tenant_id=tenant,
        role=role,
        typ="service" if service else "user",
        audience="genus-bridge",
        home_tenant_id=home or tenant,
    )
    return SimpleNamespace(state=SimpleNamespace(auth=auth))


class TestGates:
    def test_tenant_admin_admits_owner_and_admin_of_the_acting_tenant(self, root_is_parent):
        from routers._operator import require_tenant_admin

        assert require_tenant_admin(_request(CHILD_A, "admin"))
        assert require_tenant_admin(_request(CHILD_A, "owner"))
        assert require_tenant_admin(_request(CHILD_A, "admin", home=ROOT))
        assert require_tenant_admin(_request(ROOT, "owner"))

    @pytest.mark.parametrize("role", ["member", "user", "viewer", "auditor"])
    def test_tenant_admin_refuses_every_other_role(self, root_is_parent, role):
        from routers._operator import require_tenant_admin

        with pytest.raises(HTTPException) as refused:
            require_tenant_admin(_request(CHILD_A, role))
        assert refused.value.status_code == 403

    def test_tenant_admin_refuses_a_service_token(self, root_is_parent):
        from routers._operator import require_tenant_admin

        with pytest.raises(HTTPException):
            require_tenant_admin(_request(CHILD_A, "admin", service=True))

    def test_tenant_admin_refuses_no_session(self, root_is_parent):
        from types import SimpleNamespace

        from routers._operator import require_tenant_admin

        with pytest.raises(HTTPException):
            require_tenant_admin(SimpleNamespace(state=SimpleNamespace()))

    def test_tenant_audit_reader_adds_the_auditor(self, root_is_parent):
        from routers._operator import require_tenant_audit_reader

        assert require_tenant_audit_reader(_request(CHILD_A, "auditor"))
        with pytest.raises(HTTPException):
            require_tenant_audit_reader(_request(CHILD_A, "member"))

    def test_root_operator_is_root_only_at_home(self, root_is_parent):
        from routers._operator import require_root_operator

        assert require_root_operator(_request(ROOT, "admin"))
        for request in (
            _request(CHILD_A, "admin"),
            _request(CHILD_A, "owner"),
            _request(CHILD_A, "admin", home=ROOT),
        ):
            with pytest.raises(HTTPException) as refused:
                require_root_operator(request)
            assert refused.value.status_code == 403

    def test_require_operator_is_the_root_gate(self, root_is_parent):
        from routers import _operator

        assert _operator.require_operator is _operator.require_root_operator
        with pytest.raises(HTTPException):
            _operator.require_operator(_request(CHILD_A, "admin", home=ROOT))

    def test_root_audit_reader_is_root_only_at_home(self, root_is_parent):
        from routers._operator import require_root_audit_reader

        assert require_root_audit_reader(_request(ROOT, "auditor"))
        with pytest.raises(HTTPException):
            require_root_audit_reader(_request(CHILD_A, "auditor"))
        with pytest.raises(HTTPException):
            require_root_audit_reader(_request(CHILD_A, "admin", home=ROOT))


# ─── Accounts: a grant may not exceed the caller's authority ──────────


def _account(user_id: str, tenant: str, role: str, status: str = "active") -> dict:
    return {
        "id": user_id,
        "tenant_id": tenant,
        "email": "bob@example.com",
        "display_name": "Bob",
        "role": role,
        "status": status,
        "created_at": None,
        "last_login_at": None,
        "idp_issuer": None,
        "idp_subject": None,
        "password_hash": None,
    }


def _invite(root_is_parent, tenant: str, role: str, grant: str, *, home: str | None = None):
    from bridge_service import app
    from fastapi.testclient import TestClient

    created = _account("00000000-0000-4000-8000-0000000000b0", tenant, grant, "invited")
    with (
        patch("routers.users.accounts.create_account", return_value=created) as create,
        patch("routers._audit.log_event"),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/users",
            json={"email": "bob@example.com", "role": grant},
            headers={"Authorization": "Bearer " + _token(tenant, role, home=home)},
        )
    return response, create


class TestGrantCeiling:
    def test_a_child_admin_may_not_grant_owner(self, root_is_parent):
        response, create = _invite(root_is_parent, CHILD_A, "admin", "owner")
        assert response.status_code == 403
        create.assert_not_called()

    def test_a_child_owner_may_not_grant_owner_either(self, root_is_parent):
        """Granting owner stays with the root tenant's owner."""
        response, create = _invite(root_is_parent, CHILD_A, "owner", "owner")
        assert response.status_code == 403
        create.assert_not_called()

    def test_a_root_admin_acting_in_a_child_may_not_grant_owner(self, root_is_parent):
        response, create = _invite(root_is_parent, CHILD_A, "admin", "owner", home=ROOT)
        assert response.status_code == 403
        create.assert_not_called()

    def test_a_child_admin_may_grant_admin_in_its_own_tenant(self, root_is_parent):
        response, create = _invite(root_is_parent, CHILD_A, "admin", "admin")
        assert response.status_code == 201, response.text
        assert create.call_args.kwargs["tenant_id"] == CHILD_A

    def test_a_root_admin_acting_in_a_child_invites_into_the_child(self, root_is_parent):
        response, create = _invite(root_is_parent, CHILD_A, "admin", "member", home=ROOT)
        assert response.status_code == 201, response.text
        assert create.call_args.kwargs["tenant_id"] == CHILD_A

    def test_the_root_owner_at_home_may_still_grant_owner(self, root_is_parent):
        response, create = _invite(root_is_parent, ROOT, "owner", "owner")
        assert response.status_code == 201, response.text


# ─── Per-tenant routes read and write the ACTING tenant ───────────────


def _client(tenant: str, role: str = "admin", *, home: str | None = None):
    from bridge_service import app
    from fastapi.testclient import TestClient

    client = TestClient(app, raise_server_exceptions=False)
    client.headers.update({"Authorization": "Bearer " + _token(tenant, role, home=home)})
    return client


class TestActingTenantIsTheScope:
    def test_automations_read_the_acting_tenant(self, root_is_parent):
        with patch("routers.automations._automations", return_value=[]) as rows:
            response = _client(CHILD_A, home=ROOT).get("/api/automations")
        assert response.status_code == 200, response.text
        rows.assert_called_once_with(CHILD_A)

    def test_runs_are_filtered_by_the_acting_tenant(self, root_is_parent):
        with patch("routers.runs._rows", return_value=[]) as rows:
            response = _client(CHILD_A).get("/api/runs")
        assert response.status_code == 200, response.text
        sql, params = rows.call_args.args
        assert "tenant_id = %s" in sql
        assert CHILD_A in params

    def test_workflows_are_filtered_by_the_acting_tenant(self, root_is_parent):
        with patch("routers.workflows._rows", return_value=[]) as rows:
            response = _client(CHILD_A).get("/api/workflows")
        assert response.status_code == 200, response.text
        sql, params = rows.call_args_list[0].args
        assert "tenant_id = %s" in sql
        assert CHILD_A in params

    def test_approvals_list_the_acting_tenant(self, root_is_parent):
        with (
            patch("routers.approvals.list_pending_approvals", return_value=[]) as approvals,
            patch("routers.approvals.list_pending_questions", return_value=[]) as questions,
            patch("routers.approvals.Operations") as operations,
        ):
            operations.return_value.list_review.return_value = []
            response = _client(CHILD_A).get("/api/approvals")
        assert response.status_code == 200, response.text
        assert approvals.call_args.kwargs["tenant_id"] == CHILD_A
        assert questions.call_args.kwargs["tenant_id"] == CHILD_A
        assert operations.call_args.args == (CHILD_A,)

    def test_a_provider_key_lands_in_the_acting_tenants_vault(self, root_is_parent):
        calls: list[tuple[str, str, str | None]] = []

        async def engine(method, path, *, json=None, timeout=30, tenant=None):
            calls.append((method, path, tenant))
            return 200, {"providers": [], "reloaded": True}

        with (
            patch("routers.providers.engine_request", new=engine),
            patch("routers.providers.vault.set") as vault_set,
            patch("routers._audit.log_event"),
        ):
            response = _client(CHILD_A, home=ROOT).put(
                "/api/providers/openrouter/keys/1", json={"api_key": "sk-test-0000"}
            )
        assert response.status_code == 200, response.text
        assert vault_set.call_args.kwargs["tenant_id"] == CHILD_A
        assert ("POST", "/api/admin/secrets/reload", CHILD_A) in calls

    def test_memory_store_ingests_into_the_acting_tenant(self, root_is_parent):
        async def ingest(*args, **kwargs):
            ingest.kwargs = kwargs  # type: ignore[attr-defined]
            return {"facts_processed": 0}

        with patch("robothor.memory.ingestion.ingest_content", new=ingest):
            response = _client(CHILD_A).post(
                "/api/memory/store", json={"content": "hello", "content_type": "note"}
            )
        assert response.status_code == 200, response.text
        assert ingest.kwargs["tenant_id"] == CHILD_A  # type: ignore[attr-defined]

    def test_the_memory_pipeline_trigger_stays_root_only(self, root_is_parent):
        response = _client(CHILD_A).post("/api/memory/pipeline/trigger/1")
        assert response.status_code == 403


# ─── Engine calls reach the acting tenant's engine ────────────────────


class TestEngineClientIsTenantAware:
    def test_it_mints_for_the_tenant_it_is_calling(self, root_is_parent, monkeypatch):
        from routers import _engine_client

        from robothor.auth.tokens import decode_token
        from robothor.engine.auth import ENGINE_AUDIENCE

        claims = decode_token(
            _engine_client._engine_token(CHILD_A), expected_audience=ENGINE_AUDIENCE
        )
        assert claims["tid"] == CHILD_A
        assert claims["typ"] == "service"

    def test_the_platform_tenant_is_the_default(self, root_is_parent):
        from routers import _engine_client

        from robothor.auth.tokens import decode_token
        from robothor.engine.auth import ENGINE_AUDIENCE

        claims = decode_token(_engine_client._engine_token(None), expected_audience=ENGINE_AUDIENCE)
        assert claims["tid"] == ROOT

    def test_engine_request_dials_the_tenants_engine(self, root_is_parent, monkeypatch):
        import asyncio
        import json

        import httpx
        from routers import _engine_client

        monkeypatch.setenv(
            "GENUS_TENANT_ENGINE_URLS", json.dumps({CHILD_A: "http://engine-a.invalid:18800"})
        )
        from robothor.settings import reset_settings

        reset_settings()
        seen: dict[str, Any] = {}

        async def _request(self, method, url, *args, **kwargs):
            seen["url"] = str(url)
            seen["auth"] = kwargs["headers"]["Authorization"]
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(httpx.AsyncClient, "request", _request)
        status, _ = asyncio.run(
            _engine_client.engine_request("GET", "/api/admin/tools", tenant=CHILD_A)
        )
        assert status == 200
        assert seen["url"] == "http://engine-a.invalid:18800/api/admin/tools"

        from robothor.auth.tokens import decode_token
        from robothor.engine.auth import ENGINE_AUDIENCE

        token = seen["auth"].removeprefix("Bearer ")
        assert decode_token(token, expected_audience=ENGINE_AUDIENCE)["tid"] == CHILD_A


# ─── Audit reads are filtered to what the caller may see ──────────────


def _hierarchy(tenant, role=None, **_kwargs):
    if role not in ("owner", "admin"):
        return (tenant,)
    return {
        ROOT: (ROOT, CHILD_A, CHILD_B),
        CHILD_A: (CHILD_A,),
        CHILD_B: (CHILD_B,),
    }[tenant]


class TestAuditVisibility:
    def _events(self, tenant: str, role: str, *, home: str | None = None):
        with (
            patch("routers.audit.query_log", return_value=[]) as query,
            patch("robothor.engine.permissions.resolve_accessible_tenants", new=_hierarchy),
        ):
            response = _client(tenant, role, home=home).get("/api/audit/events")
        assert response.status_code == 200, response.text
        return query.call_args.kwargs

    def test_a_child_admin_reads_only_its_tenant_and_never_the_untenanted(self, root_is_parent):
        kwargs = self._events(CHILD_A, "admin")
        assert list(kwargs["tenants"]) == [CHILD_A]
        assert kwargs.get("include_untenanted", False) is False

    def test_a_root_admin_acting_in_a_child_reads_that_child(self, root_is_parent):
        kwargs = self._events(CHILD_A, "admin", home=ROOT)
        assert list(kwargs["tenants"]) == [CHILD_A]
        assert kwargs.get("include_untenanted", False) is False

    def test_the_root_operator_at_home_reads_the_whole_appliance_trail(self, root_is_parent):
        """Unrestricted, exactly as before tenants were recorded: every tenant —
        including independent roots like the benchmark sandbox, which no
        hierarchy walk from the platform tenant reaches — and the NULL rows."""
        kwargs = self._events(ROOT, "owner")
        assert kwargs["tenants"] is None

    def test_the_root_auditor_at_home_reads_the_whole_appliance_trail(self, root_is_parent):
        kwargs = self._events(ROOT, "auditor")
        assert kwargs["tenants"] is None

    def test_the_csv_export_is_filtered_the_same_way(self, root_is_parent):
        with (
            patch("routers.audit.query_log", return_value=[]) as query,
            patch("robothor.engine.permissions.resolve_accessible_tenants", new=_hierarchy),
        ):
            response = _client(CHILD_A, "auditor").get("/api/audit/events.csv")
        assert response.status_code == 200, response.text
        assert list(query.call_args.kwargs["tenants"]) == [CHILD_A]
        assert query.call_args.kwargs.get("include_untenanted", False) is False

    def test_the_stats_are_filtered_too(self, root_is_parent):
        # Two routers declare GET /api/audit/stats (health.py first, so it is
        # the one served); both must filter, so both seams are watched.
        with (
            patch("robothor.audit.logger.stats", return_value={}) as audit_stats,
            patch("routers.health.stats", return_value={}) as health_stats,
            patch("routers.audit._get_guardrail_stats", return_value={}),
            patch("robothor.engine.permissions.resolve_accessible_tenants", new=_hierarchy),
        ):
            response = _client(CHILD_A).get("/api/audit/stats")
        assert response.status_code == 200, response.text
        called = health_stats.call_args or audit_stats.call_args
        assert called is not None
        assert list(called.kwargs["tenants"]) == [CHILD_A]
        assert called.kwargs.get("include_untenanted", False) is False

    def test_the_legacy_audit_route_is_filtered_too(self, root_is_parent):
        with (
            patch("routers.health.query_log", return_value=[]) as query,
            patch("robothor.engine.permissions.resolve_accessible_tenants", new=_hierarchy),
        ):
            response = _client(CHILD_A).get("/api/audit")
        assert response.status_code == 200, response.text
        assert list(query.call_args.kwargs["tenants"]) == [CHILD_A]
        assert query.call_args.kwargs.get("include_untenanted", False) is False


# ─── audit_scope: one answer for every audit read ─────────────────────


def _auth(tenant: str, role: str, *, home: str | None = None):
    from robothor.auth.deps import AuthContext

    return AuthContext(
        user_id="alice",
        tenant_id=tenant,
        role=role,
        typ="user",
        audience="genus-bridge",
        home_tenant_id=home or tenant,
    )


class TestAuditScope:
    def test_the_platform_root_at_home_is_unrestricted(self, root_is_parent):
        from routers._operator import audit_scope

        assert audit_scope(_auth(ROOT, "owner")) is None
        assert audit_scope(_auth(ROOT, "auditor")) is None

    def test_the_platform_root_acting_in_a_child_sees_that_child(self, root_is_parent):
        from routers._operator import audit_scope

        with patch("robothor.engine.permissions.resolve_accessible_tenants", new=_hierarchy):
            assert audit_scope(_auth(CHILD_A, "admin", home=ROOT)) == [CHILD_A]

    def test_any_other_caller_sees_what_its_acting_tenant_reaches(self, root_is_parent):
        from routers._operator import audit_scope

        hierarchy = {CHILD_A: ("acme-child-a", "acme-grandchild")}
        with patch(
            "robothor.engine.permissions.resolve_accessible_tenants",
            new=lambda tenant, role=None, **_: hierarchy[tenant] if role == "admin" else (tenant,),
        ):
            assert audit_scope(_auth(CHILD_A, "admin")) == ["acme-child-a", "acme-grandchild"]
            assert audit_scope(_auth(CHILD_A, "auditor")) == [CHILD_A]

    def test_every_audit_read_site_uses_it(self):
        import inspect

        from routers import audit, health

        for handler in (
            audit.get_audit_events,
            audit.export_audit_events_csv,
            audit.get_audit_stats,
            health.api_query_audit,
            health.api_audit_stats,
        ):
            assert "audit_scope(" in inspect.getsource(handler), handler.__name__
