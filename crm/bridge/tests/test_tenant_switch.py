"""Tenant switching (AssumeRole) and the per-request tenant binding.

One deployment serves a tenant hierarchy. A root owner/admin may switch INTO a
descendant tenant and act there; a child admin can never reach its parent or a
sibling. The acting tenant lives in the token (``tid``, with ``home_tid`` and
``act``) — never in a header — and the bridge binds every authenticated
request's database scope to that ``tid``, so a forgotten WHERE leaks nothing.

Hierarchy used throughout::

    acme-parent
    ├── acme-child-a
    └── acme-child-b
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "test-signing-key-at-least-32-bytes-long-xyz"

CHILDREN = {
    "acme-parent": [
        {"id": "acme-child-a", "displayName": "Child A", "parentTenantId": "acme-parent"},
        {"id": "acme-child-b", "displayName": "Child B", "parentTenantId": "acme-parent"},
    ],
}

TENANTS = {
    "acme-parent": {"id": "acme-parent", "displayName": "Parent", "parentTenantId": None},
    "acme-child-a": {
        "id": "acme-child-a",
        "displayName": "Child A",
        "parentTenantId": "acme-parent",
    },
    "acme-child-b": {
        "id": "acme-child-b",
        "displayName": "Child B",
        "parentTenantId": "acme-parent",
    },
}


def _list_tenants(parent_id=None, active_only=True):
    return list(CHILDREN.get(parent_id or "", []))


def _account(user_id: str, tenant: str, role: str) -> dict:
    return {
        "id": user_id,
        "email": "alice@example.com",
        "display_name": "Alice",
        "role": role,
        "tenant_id": tenant,
        "status": "active",
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    monkeypatch.delenv("GENUS_TENANT_SWITCH_MODE", raising=False)
    monkeypatch.delenv("ROBOTHOR_TENANT_SWITCH_MODE", raising=False)
    from robothor.auth import tokens
    from robothor.settings import reset_settings

    tokens.reset_signing_key_cache()
    reset_settings()
    yield
    tokens.reset_signing_key_cache()
    reset_settings()


@pytest.fixture(autouse=True)
def rls_enforcing(monkeypatch):
    """Row-level security genuinely enforcing, unless a test says otherwise.

    enforce refuses to mint an acting token while RLS is inert (the gate
    shared with federation); the tests below that are about the hierarchy
    rules start from a deployment where that gate is satisfied.
    """
    from robothor.federation import rls_gate

    monkeypatch.delenv("GENUS_TENANT_SWITCH_ALLOW_INERT_RLS", raising=False)
    monkeypatch.delenv("ROBOTHOR_TENANT_SWITCH_ALLOW_INERT_RLS", raising=False)
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: True)
    monkeypatch.setattr(rls_gate, "_connection_is_superuser", lambda: False)


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value: str) -> None:
        from robothor.settings import reset_settings

        monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", value)
        reset_settings()

    return set_mode


@pytest.fixture
def hierarchy():
    with patch("robothor.crm.dal.list_tenants", side_effect=_list_tenants):
        yield


def _token(user_id: str, tenant: str, role: str, **kw) -> str:
    from robothor.auth import tokens

    return tokens.issue_access_token(user_id, tenant, role, **kw)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _switch(client, token: str, target: str, account: dict | None):
    with (
        patch("robothor.auth.accounts.get_account_by_id", return_value=account),
        patch("routers.auth.audited") as audit,
    ):
        response = await client.post(
            "/api/auth/switch-tenant", json={"tenant": target}, headers=_bearer(token)
        )
    return response, audit


# ── switch-tenant ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parent_admin_switches_into_a_child(test_client, mode, hierarchy):
    from robothor.auth.deps import verify_token

    mode("enforce")
    token = _token("u-alice", "acme-parent", "admin")
    r, audit = await _switch(
        test_client, token, "acme-child-a", _account("u-alice", "acme-parent", "admin")
    )
    assert r.status_code == 200, r.text
    body = r.json()
    ctx = verify_token(body["access_token"])
    assert ctx.tenant_id == "acme-child-a"
    assert ctx.home_tenant_id == "acme-parent"
    assert ctx.is_acting is True
    assert ctx.user_id == "u-alice" and ctx.role == "admin"
    assert ctx.scopes == verify_token(token).scopes
    # Same shape as login/refresh, so the dashboard can swap it in.
    assert body["user"]["id"] == "u-alice"
    assert body["user"]["tenant_id"] == "acme-child-a"
    assert body["user"]["role"] == "admin"
    # The refresh token is untouched: it keeps the home tenant.
    assert body.get("refresh_token") is None

    events = [c for c in audit.call_args_list if c.args[1] == "tenant.switch"]
    assert events, "the switch was not audited"
    details = events[-1].kwargs
    assert details.get("home_tenant_id") == "acme-parent"
    assert details.get("target_tenant_id") == "acme-child-a"


@pytest.mark.asyncio
async def test_child_admin_cannot_reach_the_parent(test_client, mode, hierarchy):
    mode("enforce")
    token = _token("u-bob", "acme-child-a", "admin")
    r, _ = await _switch(
        test_client, token, "acme-parent", _account("u-bob", "acme-child-a", "admin")
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_child_admin_cannot_reach_a_sibling(test_client, mode, hierarchy):
    mode("enforce")
    token = _token("u-bob", "acme-child-a", "admin")
    r, _ = await _switch(
        test_client, token, "acme-child-b", _account("u-bob", "acme-child-a", "admin")
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_switching_from_an_acting_token_checks_against_home(test_client, mode, hierarchy):
    """A parent admin acting in child-a may hop to child-b (both are the
    home's descendants); the check is against home, not the acting tenant."""
    from robothor.auth.deps import verify_token

    mode("enforce")
    token = _token("u-alice", "acme-child-a", "admin", home_tenant_id="acme-parent")
    r, _ = await _switch(
        test_client, token, "acme-child-b", _account("u-alice", "acme-parent", "admin")
    )
    assert r.status_code == 200, r.text
    ctx = verify_token(r.json()["access_token"])
    assert ctx.tenant_id == "acme-child-b" and ctx.home_tenant_id == "acme-parent"


@pytest.mark.asyncio
async def test_service_token_is_refused(test_client, mode, hierarchy):
    from robothor.auth import tokens

    mode("enforce")
    token = tokens.issue_service_token("svc-agent", "acme-parent")
    r, _ = await _switch(test_client, token, "acme-child-a", None)
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["member", "viewer", "user", "auditor"])
@pytest.mark.asyncio
async def test_non_admin_roles_are_refused(test_client, mode, hierarchy, role):
    mode("enforce")
    token = _token("u-alice", "acme-parent", role)
    r, _ = await _switch(
        test_client, token, "acme-child-a", _account("u-alice", "acme-parent", role)
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_target_equal_to_home_returns_a_plain_home_token(test_client, mode, hierarchy):
    from robothor.auth.deps import verify_token

    mode("enforce")
    token = _token("u-alice", "acme-child-a", "owner", home_tenant_id="acme-parent")
    r, _ = await _switch(
        test_client, token, "acme-parent", _account("u-alice", "acme-parent", "owner")
    )
    assert r.status_code == 200, r.text
    ctx = verify_token(r.json()["access_token"])
    assert ctx.tenant_id == "acme-parent"
    assert ctx.home_tenant_id == "acme-parent"
    assert ctx.is_acting is False


@pytest.mark.asyncio
async def test_an_inactive_account_cannot_switch(test_client, mode, hierarchy):
    """Otherwise a 15-minute token could be renewed forever by switching."""
    mode("enforce")
    token = _token("u-alice", "acme-parent", "admin")
    disabled = {**_account("u-alice", "acme-parent", "admin"), "status": "disabled"}
    r, _ = await _switch(test_client, token, "acme-child-a", disabled)
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_flag_off_by_default_refuses_as_disabled(test_client, hierarchy):
    token = _token("u-alice", "acme-parent", "admin")
    r, _ = await _switch(
        test_client, token, "acme-child-a", _account("u-alice", "acme-parent", "admin")
    )
    assert r.status_code == 403
    assert r.json()["error"] == "tenant switching disabled"


@pytest.mark.asyncio
async def test_flag_alias_is_honoured(test_client, monkeypatch, hierarchy):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_TENANT_SWITCH_MODE", "enforce")
    reset_settings()
    token = _token("u-alice", "acme-parent", "admin")
    r, _ = await _switch(
        test_client, token, "acme-child-a", _account("u-alice", "acme-parent", "admin")
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_observe_logs_would_allow_and_refuses(test_client, mode, hierarchy, caplog):
    mode("observe")
    token = _token("u-alice", "acme-parent", "admin")
    with caplog.at_level(logging.INFO):
        r, _ = await _switch(
            test_client, token, "acme-child-a", _account("u-alice", "acme-parent", "admin")
        )
    assert r.status_code == 403
    lines = [rec.getMessage() for rec in caplog.records]
    hits = [m for m in lines if "tenant.switch would-allow" in m]
    assert hits, lines
    assert "acme-parent" in hits[0] and "acme-child-a" in hits[0]


@pytest.mark.asyncio
async def test_observe_logs_would_deny_and_refuses(test_client, mode, hierarchy, caplog):
    mode("observe")
    token = _token("u-bob", "acme-child-a", "admin")
    with caplog.at_level(logging.INFO):
        r, _ = await _switch(
            test_client, token, "acme-parent", _account("u-bob", "acme-child-a", "admin")
        )
    assert r.status_code == 403
    hits = [m for m in (rec.getMessage() for rec in caplog.records) if "would-deny" in m]
    assert hits
    assert "acme-child-a" in hits[0] and "acme-parent" in hits[0]


@pytest.mark.asyncio
async def test_enforce_refuses_with_503_while_rls_is_inert(
    test_client, mode, hierarchy, monkeypatch
):
    from robothor.federation import rls_gate

    mode("enforce")
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: False)
    token = _token("u-alice", "acme-parent", "admin")
    r, audit = await _switch(
        test_client, token, "acme-child-a", _account("u-alice", "acme-parent", "admin")
    )
    assert r.status_code == 503
    assert "access_token" not in r.json()
    assert "ROBOTHOR_RLS_ENABLED" in r.json()["error"]
    assert "GENUS_TENANT_SWITCH_ALLOW_INERT_RLS" in r.json()["error"]


@pytest.mark.asyncio
async def test_enforce_refuses_with_503_for_a_superuser_connection(
    test_client, mode, hierarchy, monkeypatch
):
    from robothor.federation import rls_gate

    mode("enforce")
    monkeypatch.setattr(rls_gate, "_connection_is_superuser", lambda: True)
    token = _token("u-alice", "acme-parent", "admin")
    r, _ = await _switch(
        test_client, token, "acme-child-a", _account("u-alice", "acme-parent", "admin")
    )
    assert r.status_code == 503
    assert "access_token" not in r.json()


@pytest.mark.parametrize(
    "env", ["GENUS_TENANT_SWITCH_ALLOW_INERT_RLS", "ROBOTHOR_TENANT_SWITCH_ALLOW_INERT_RLS"]
)
@pytest.mark.asyncio
async def test_enforce_with_the_override_issues_and_warns(
    test_client, mode, hierarchy, monkeypatch, caplog, env
):
    from robothor.federation import rls_gate
    from robothor.settings import reset_settings

    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: False)
    monkeypatch.setenv(env, "1")
    reset_settings()
    mode("enforce")
    token = _token("u-alice", "acme-parent", "admin")
    with caplog.at_level(logging.WARNING):
        r, _ = await _switch(
            test_client, token, "acme-child-a", _account("u-alice", "acme-parent", "admin")
        )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
    assert any(
        rec.levelno >= logging.WARNING and "GENUS_TENANT_SWITCH_ALLOW_INERT_RLS" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_observe_logs_would_refuse_while_rls_is_inert(
    test_client, mode, hierarchy, monkeypatch, caplog
):
    from robothor.federation import rls_gate

    mode("observe")
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: False)
    token = _token("u-alice", "acme-parent", "admin")
    with caplog.at_level(logging.INFO):
        r, _ = await _switch(
            test_client, token, "acme-child-a", _account("u-alice", "acme-parent", "admin")
        )
    assert r.status_code == 403
    assert any("would-refuse" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_a_header_cannot_select_the_acting_tenant(test_client, mode, hierarchy):
    """The AssumeRole rule: X-Tenant-Id that disagrees with tid is refused."""
    token = _token("u-alice", "acme-parent", "admin")
    r = await test_client.get(
        "/api/tenants/accessible",
        headers={**_bearer(token), "X-Tenant-Id": "acme-child-a"},
    )
    assert r.status_code == 403


# ── /api/tenants/accessible ───────────────────────────────────────────


async def _accessible(client, token: str):
    with patch("robothor.crm.dal.get_tenant", side_effect=lambda tid: TENANTS.get(tid)):
        return await client.get("/api/tenants/accessible", headers=_bearer(token))


@pytest.mark.asyncio
async def test_accessible_lists_home_first_then_descendants(test_client, hierarchy):
    r = await _accessible(test_client, _token("u-alice", "acme-parent", "owner"))
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["id"] for row in rows] == ["acme-parent", "acme-child-a", "acme-child-b"]
    assert rows[0]["is_home"] is True
    assert all(row["is_home"] is False for row in rows[1:])
    assert rows[1]["parent_tenant_id"] == "acme-parent"
    assert rows[0]["display_name"] == "Parent"
    assert set(rows[0]) == {"id", "display_name", "parent_tenant_id", "is_home"}


@pytest.mark.asyncio
async def test_accessible_while_acting_is_still_rooted_at_home(test_client, hierarchy):
    token = _token("u-alice", "acme-child-a", "admin", home_tenant_id="acme-parent")
    r = await _accessible(test_client, token)
    assert r.status_code == 200, r.text
    assert r.json()[0] == {
        "id": "acme-parent",
        "display_name": "Parent",
        "parent_tenant_id": None,
        "is_home": True,
    }


@pytest.mark.parametrize("role", ["member", "viewer"])
@pytest.mark.asyncio
async def test_a_single_tenant_user_gets_only_their_home(test_client, hierarchy, role):
    r = await _accessible(test_client, _token("u-bob", "acme-child-a", role))
    assert r.status_code == 200, r.text
    assert [row["id"] for row in r.json()] == ["acme-child-a"]


@pytest.mark.asyncio
async def test_accessible_works_with_the_flag_off(test_client, hierarchy):
    r = await _accessible(test_client, _token("u-alice", "acme-parent", "admin"))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_accessible_refuses_a_service_token(test_client, hierarchy):
    from robothor.auth import tokens

    token = tokens.issue_service_token(
        "svc-agent", "acme-parent", scopes=("bridge:*", "tenant:admin")
    )
    r = await _accessible(test_client, token)
    assert r.status_code == 403


# ── per-request tenant binding ────────────────────────────────────────


@pytest.fixture
def scope_probe():
    """Two throwaway routes that report the tenant scope they ran under."""
    from bridge_service import app

    from robothor.db.connection import current_tenant_scope

    def sync_probe():
        return {"scope": current_tenant_scope()}

    async def async_probe():
        return {"scope": current_tenant_scope()}

    before = list(app.router.routes)
    app.add_api_route("/api/_scope_probe/sync", sync_probe, methods=["GET"])
    app.add_api_route("/api/_scope_probe/async", async_probe, methods=["GET"])
    try:
        yield
    finally:
        app.router.routes[:] = before


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.asyncio
async def test_an_authenticated_request_runs_inside_its_tokens_tenant(
    test_client, scope_probe, kind
):
    token = _token("u-alice", "acme-child-a", "admin", home_tenant_id="acme-parent")
    r = await test_client.get(f"/api/_scope_probe/{kind}", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"scope": "acme-child-a"}


@pytest.mark.parametrize("kind", ["sync", "async"])
@pytest.mark.asyncio
async def test_an_unauthenticated_request_gets_no_scope(test_client, scope_probe, kind):
    # The suite's loopback insecure-dev mode lets an anonymous call through.
    r = await test_client.get(f"/api/_scope_probe/{kind}")
    assert r.status_code == 200, r.text
    assert r.json() == {"scope": None}


@pytest.mark.asyncio
async def test_the_scope_does_not_outlive_the_request(test_client, scope_probe):
    from robothor.db.connection import current_tenant_scope

    token = _token("u-alice", "acme-child-a", "admin", home_tenant_id="acme-parent")
    await test_client.get("/api/_scope_probe/sync", headers=_bearer(token))
    assert current_tenant_scope() is None


# ── the caller's own account while acting ─────────────────────────────


@pytest.fixture
def local_login_on(monkeypatch):
    from robothor.auth import local_login

    monkeypatch.setattr(local_login, "local_login_enabled", lambda: True)
    monkeypatch.setattr(local_login, "flood_limited", lambda *a, **k: False)
    monkeypatch.setattr(local_login, "throttled", lambda *a, **k: False)


def _scope_recorder(seen: list, result):
    def record(*args, **kwargs):
        from robothor.db.connection import current_tenant_scope

        seen.append(current_tenant_scope())
        return result

    return record


@pytest.mark.parametrize(
    ("path", "body", "fn", "result"),
    [
        (
            "/api/auth/password",
            {"current_password": "old-password-123", "new_password": "new-password-123456"},
            "change_password",
            True,
        ),
        ("/api/auth/mfa/enroll", {"password": "old-password-123"}, "begin_enrollment", {}),
        ("/api/auth/mfa/confirm", {"code": "123456"}, "confirm_enrollment", True),
        (
            "/api/auth/mfa/disable",
            {"password": "old-password-123", "code": "123456"},
            "disable_mfa",
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_self_account_routes_run_in_the_home_tenant_while_acting(
    test_client, local_login_on, monkeypatch, path, body, fn, result
):
    """The account lives at home; under the acting scope RLS would hide it."""
    from robothor.auth import local_login

    seen: list = []
    monkeypatch.setattr(local_login, fn, _scope_recorder(seen, result))
    token = _token("u-alice", "acme-child-a", "admin", home_tenant_id="acme-parent")
    with patch("routers.auth.audited"):
        r = await test_client.post(path, json=body, headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert seen == ["acme-parent"]


@pytest.mark.asyncio
async def test_me_reads_the_account_in_the_home_tenant_while_acting(test_client):
    seen: list = []
    account = {**_account("u-alice", "acme-parent", "admin"), "mfa_enabled": True}
    token = _token("u-alice", "acme-child-a", "admin", home_tenant_id="acme-parent")
    with (
        patch(
            "robothor.auth.accounts.get_account_by_id",
            side_effect=_scope_recorder(seen, account),
        ),
        patch("robothor.auth.local_login.mfa_setup_required_for", return_value=False),
    ):
        r = await test_client.get("/api/auth/me", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.json()["mfa_enabled"] is True
    assert r.json()["tenant_id"] == "acme-child-a"
    assert seen == ["acme-parent"]


# ── observe is readable: an audit row and a WARNING ───────────────────


def _observe_rows(audit) -> list:
    return [c for c in audit.call_args_list if c.args[1] == "tenant.switch"]


@pytest.mark.parametrize(
    ("user", "home", "target", "outcome"),
    [
        ("u-alice", "acme-parent", "acme-child-a", "would-allow"),
        ("u-bob", "acme-child-a", "acme-parent", "would-deny"),
    ],
)
@pytest.mark.asyncio
async def test_observe_writes_an_audit_row_and_a_warning(
    test_client, mode, hierarchy, caplog, user, home, target, outcome
):
    """The audit table is the durable record the rollout reads; an INFO line
    the bridge never configured a handler for was nothing at all."""
    mode("observe")
    token = _token(user, home, "admin")
    with caplog.at_level(logging.WARNING):
        r, audit = await _switch(test_client, token, target, _account(user, home, "admin"))
    assert r.status_code == 403

    rows = _observe_rows(audit)
    assert len(rows) == 1, audit.call_args_list
    assert rows[0].kwargs["status"] == outcome
    assert rows[0].kwargs["home_tenant_id"] == home
    assert rows[0].kwargs["target_tenant_id"] == target

    warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and f"tenant.switch {outcome}" in rec.getMessage()
    ]
    assert warnings, [rec.getMessage() for rec in caplog.records]


@pytest.mark.asyncio
async def test_observe_audits_would_refuse_while_rls_is_inert(
    test_client, mode, hierarchy, monkeypatch
):
    from robothor.federation import rls_gate

    mode("observe")
    monkeypatch.setattr(rls_gate, "_rls_flag_on", lambda: False)
    token = _token("u-alice", "acme-parent", "admin")
    r, audit = await _switch(
        test_client, token, "acme-child-a", _account("u-alice", "acme-parent", "admin")
    )
    assert r.status_code == 403
    rows = _observe_rows(audit)
    assert [c.kwargs["status"] for c in rows] == ["would-refuse"]


@pytest.mark.asyncio
async def test_the_bridge_configures_logging_at_startup(monkeypatch):
    """Without a handler on the root logger, INFO from every bridge module
    is dropped by the stdlib's last-resort handler."""
    import bridge_service

    from robothor.autonomy import handoff_worker

    calls: list[int] = []
    monkeypatch.setattr(bridge_service, "ensure_logging_configured", lambda: calls.append(1))

    async def idle():
        return None

    monkeypatch.setattr(handoff_worker, "recover_checks", idle)
    monkeypatch.setattr(handoff_worker, "purge_expired_observations", idle)
    monkeypatch.setattr(bridge_service, "_routine_trigger_loop", idle)
    async with bridge_service.lifespan(bridge_service.app):
        pass
    assert calls == [1]
