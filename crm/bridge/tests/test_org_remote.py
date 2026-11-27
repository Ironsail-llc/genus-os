"""The bridge's remote-tool routes: a parent's agent running a descendant's tool.

``GET /api/org/connections`` answers a parent's owner or admin, signed in at
home, with what each descendant's engine reports (``GET
/api/admin/remote-tools``): one company down is that entry's ``status:
"error"`` only.

``GET /api/org/remote/catalogue`` and ``POST /api/org/remote/call`` answer an
ENGINE: a service token carrying the ``org:remote`` scope. The token's tenant
is the home tenant; the body never names it. The bridge checks the mode, that
the target is a descendant, and who the call is for, then forwards to the
target engine with its OWN attestations and returns the target's status and
body unchanged. The target engine is faked: ``engine_request`` records what it
was asked and answers what the test says.

Hierarchy::

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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robothor.tenancy.remote_contract import (  # noqa: E402
    BRIDGE_TIMEOUT_S,
    ERR_BAD_ARGUMENTS,
    ERR_NOT_PERMITTED,
    ERR_TOOLS_OFF,
    ERR_WRITE_FORBIDDEN,
    SCOPE_ORG_REMOTE,
)

KEY = "test-signing-key-at-least-32-bytes-long-xyz"
ROOT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"
ALICE = "11111111-1111-1111-1111-111111111111"
BOB = "22222222-2222-2222-2222-222222222222"
CALL = "/api/org/remote/call"
CATALOGUE = "/api/org/remote/catalogue"
CONNECTIONS = "/api/org/connections"
ENGINE_SCOPES = ("bridge:read", "bridge:write", SCOPE_ORG_REMOTE)


def _accessible(tenant, role=None, **_kwargs):
    if role not in ("owner", "admin"):
        return (tenant,)
    return {ROOT: (ROOT, CHILD_A, CHILD_B), CHILD_A: (CHILD_A,), CHILD_B: (CHILD_B,)}[tenant]


class _Engine:
    """A stand-in for ``engine_request``: records each call, answers per tenant."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: dict[str, tuple[int, Any]] = {}
        self.hang: set[str] = set()  # tenants whose engine never answers

    async def __call__(self, method, path, *, json=None, timeout=30.0, tenant=None):
        self.calls.append(
            {"method": method, "path": path, "json": json, "timeout": timeout, "tenant": tenant}
        )
        if tenant in self.hang:
            import asyncio

            await asyncio.sleep(30)
        if tenant in self.answers:
            return self.answers[tenant]
        if path == "/api/admin/remote-tools":
            return 200, {
                "tenant_id": tenant,
                "as_of": "2026-01-01T00:00:00Z",
                "mode": "full",
                "plugins": [{"name": "example-plugin", "version": "1.2.3", "enabled": True}],
                "tools": [
                    {
                        "name": "example_read",
                        "plugin": "example-plugin",
                        "access": "read",
                        "attestations": [],
                        "description": "Reads an example.",
                    }
                ],
                "checks": [{"name": "example-plugin.connection", "status": "ok", "detail": ""}],
            }
        return 200, {
            "outcome": "ok",
            "request_id": (json or {}).get("request_id"),
            "tool": (json or {}).get("tool"),
            "result": {"rows": 3},
            "would": None,
        }


class _Acting:
    """``acting_role``: Alice is the parent's owner, Bob its admin, anyone else none."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, user_id, home, here):
        self.calls.append((user_id, home, here))
        if home != ROOT or here not in (CHILD_A, CHILD_B):
            return None
        return {ALICE: "owner", BOB: "admin"}.get(user_id)


@pytest.fixture
def engine():
    return _Engine()


@pytest.fixture
def acting():
    return _Acting()


@pytest.fixture
def audit_rows():
    return []


@pytest.fixture
def child_access():
    return {"value": False}


@pytest.fixture(autouse=True)
def _env(monkeypatch, engine, acting, audit_rows, child_access):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    monkeypatch.setenv("GENUS_ORG_REMOTE_TOOLS", "full")
    monkeypatch.setenv("GENUS_ORG_REMOTE_ATTESTATIONS", "baa")
    import routers._operator as operator

    from robothor.auth import tokens
    from robothor.settings import reset_settings

    monkeypatch.setattr(operator, "PLATFORM_TENANT", ROOT)
    # The engine's agent: known to the capabilities manifest, endpoints open,
    # so a refusal below comes from the route, not an incidental manifest gap.
    monkeypatch.setattr("middleware._known_agent_ids", lambda: frozenset({"main"}))
    monkeypatch.setattr("middleware.check_endpoint_access", lambda *a, **k: True)
    tokens.reset_signing_key_cache()
    reset_settings()

    def _log_event(event_type, **kw):
        audit_rows.append({"event_type": event_type, **kw})

    with (
        patch("robothor.engine.permissions.resolve_accessible_tenants", new=_accessible),
        patch("robothor.flags.store.resolve", side_effect=lambda name: None),
        patch("robothor.auth.acting.acting_role", new=acting),
        patch(
            "robothor.crm.dal.tenant_child_data_access",
            side_effect=lambda tenant: child_access["value"] and tenant == ROOT,
        ),
        patch("robothor.crm.dal.get_tenant", side_effect=lambda t: {"displayName": t.upper()}),
        patch("routers._engine_client.engine_request", new=engine),
        patch("routers._audit.log_event", side_effect=_log_event),
    ):
        yield
    tokens.reset_signing_key_cache()
    reset_settings()


def _mode(monkeypatch, mode: str) -> None:
    from robothor.settings import reset_settings

    monkeypatch.setenv("GENUS_ORG_REMOTE_TOOLS", mode)
    reset_settings()


def _person(tenant: str = ROOT, role: str = "owner", *, home: str | None = None):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app, raise_server_exceptions=False)
    token = tokens.issue_access_token("alice", tenant, role, home_tenant_id=home)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _engine_client(tenant: str = ROOT, scopes: tuple[str, ...] = ENGINE_SCOPES):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app, raise_server_exceptions=False)
    token = tokens.issue_service_token("engine:main", tenant, agent_id="main", scopes=scopes)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "tenant": CHILD_A,
        "tool": "example_read",
        "args": {"q": "x"},
        "access": "read",
        "dry_run": False,
        "request_id": None,
        "requested_by": ALICE,
        "agent_id": "main",
    }
    body.update(over)
    return body


def _forwards(engine: _Engine) -> list[dict[str, Any]]:
    return [c for c in engine.calls if c["path"] == "/api/admin/remote-tools/call"]


def _code(response) -> str:
    return response.json()["error"]


# ── Who may call the engine routes ──────────────────────────────────────────


def test_the_call_needs_the_org_remote_scope(engine):
    response = _engine_client(scopes=("bridge:read", "bridge:write")).post(CALL, json=_body())

    assert response.status_code == 403
    assert engine.calls == []


def test_the_catalogue_needs_the_org_remote_scope(engine):
    response = _engine_client(scopes=("bridge:read", "bridge:write")).get(
        CATALOGUE, params={"tenant": CHILD_A}
    )

    assert response.status_code == 403
    assert engine.calls == []


def test_a_person_may_not_call_a_remote_tool_even_as_owner(engine):
    assert _person().post(CALL, json=_body()).status_code == 403
    assert _person().get(CATALOGUE, params={"tenant": CHILD_A}).status_code == 403
    assert engine.calls == []


# ── Where the call goes ─────────────────────────────────────────────────────


def test_a_read_is_forwarded_to_the_target_engine_and_answered_unchanged(engine):
    response = _engine_client().post(CALL, json=_body())

    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"rows": 3}
    (call,) = _forwards(engine)
    assert call["method"] == "POST"
    assert call["tenant"] == CHILD_A
    assert call["timeout"] == BRIDGE_TIMEOUT_S
    sent = call["json"]
    assert sent["tool"] == "example_read"
    assert sent["args"] == {"q": "x"}
    assert sent["access"] == "read"
    assert sent["requested_by"] == ALICE
    assert sent["agent_id"] == "main"


def test_the_home_tenant_is_the_tokens_never_the_body(engine):
    _engine_client().post(CALL, json={**_body(), "home_tenant": CHILD_B})

    assert _forwards(engine)[0]["json"]["home_tenant"] == ROOT


def test_attestations_are_the_bridges_own_setting_not_the_body(engine):
    _engine_client().post(CALL, json={**_body(), "attestations": ["phi", "sox"]})

    assert _forwards(engine)[0]["json"]["attestations"] == ["baa"]


def test_the_agent_is_the_tokens_not_the_body(engine):
    _engine_client().post(CALL, json=_body(agent_id="someone-else"))

    assert _forwards(engine)[0]["json"]["agent_id"] == "main"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (403, {"error": "remote_attestation_missing", "message": "No.", "attestation": "baa"}),
        (404, {"error": "remote_tool_unknown", "message": "No such tool.", "attestation": None}),
        (409, {"error": "remote_outcome_unknown", "message": "In flight.", "attestation": None}),
        (422, {"error": "remote_bad_arguments", "message": "Bad.", "attestation": None}),
        (504, {"error": "remote_timeout", "message": "Too slow.", "attestation": None}),
        (200, {"outcome": "error", "request_id": None, "tool": "t", "result": {"error": "x"}}),
    ],
)
def test_the_targets_status_and_body_come_back_unchanged(engine, status, body):
    engine.answers[CHILD_A] = (status, body)

    response = _engine_client().post(CALL, json=_body())

    assert response.status_code == status
    assert response.json() == body


def test_a_post_is_never_retried(engine):
    engine.answers[CHILD_A] = (502, {"error": "engine unavailable"})

    response = _engine_client().post(CALL, json=_body())

    assert response.status_code == 502
    assert len(_forwards(engine)) == 1


def test_the_engine_client_does_not_retry_a_post_on_a_dropped_connection():
    import httpx
    from routers import _engine_client

    assert "POST" not in _engine_client._IDEMPOTENT_METHODS
    dropped = httpx.RemoteProtocolError("connection dropped")
    assert _engine_client._worth_one_retry("POST", dropped) is False


# ── The target must be a descendant ─────────────────────────────────────────


@pytest.mark.parametrize("target", ["acme-elsewhere", ROOT, "", 7])
def test_a_target_that_is_not_a_descendant_is_answered_as_unreachable(engine, target):
    from robothor.engine.tools.cross_tenant import UNREACHABLE

    response = _engine_client().post(CALL, json=_body(tenant=target))

    assert response.status_code == 404
    assert response.json()["message"] == UNREACHABLE
    assert engine.calls == []


def test_a_child_engine_cannot_reach_its_sibling(engine):
    response = _engine_client(tenant=CHILD_A).post(
        CALL, json=_body(tenant=CHILD_B, requested_by=None)
    )

    assert response.status_code == 404
    assert engine.calls == []


# ── Who the call is for ─────────────────────────────────────────────────────


def test_an_owner_may_read_and_write(engine):
    assert _engine_client().post(CALL, json=_body()).status_code == 200
    write = _body(access="write", request_id="eff-1")
    assert _engine_client().post(CALL, json=write).status_code == 200
    assert len(_forwards(engine)) == 2


def test_an_admin_may_read_but_not_write(engine):
    assert _engine_client().post(CALL, json=_body(requested_by=BOB)).status_code == 200

    write = _body(access="write", request_id="eff-1", requested_by=BOB)
    response = _engine_client().post(CALL, json=write)

    assert response.status_code == 403
    assert _code(response) == ERR_WRITE_FORBIDDEN
    assert len(_forwards(engine)) == 1


def test_a_person_with_no_role_in_the_target_is_refused(engine, acting):
    response = _engine_client().post(CALL, json=_body(requested_by="33333333-0000-0000-0000-0"))

    assert response.status_code == 403
    assert _code(response) == ERR_NOT_PERMITTED
    assert engine.calls == []
    assert acting.calls == [("33333333-0000-0000-0000-0", ROOT, CHILD_A)]


def test_an_unattended_read_needs_the_parents_child_data_access(engine, child_access):
    response = _engine_client().post(CALL, json=_body(requested_by=None))
    assert response.status_code == 403
    assert _code(response) == ERR_NOT_PERMITTED
    assert engine.calls == []

    child_access["value"] = True
    assert _engine_client().post(CALL, json=_body(requested_by=None)).status_code == 200
    assert _forwards(engine)[0]["json"]["requested_by"] is None


def test_an_unattended_write_is_refused_whatever_the_grant(engine, child_access):
    child_access["value"] = True
    write = _body(access="write", request_id="eff-1", requested_by=None)

    response = _engine_client().post(CALL, json=write)

    assert response.status_code == 403
    assert _code(response) == ERR_WRITE_FORBIDDEN
    assert engine.calls == []


# ── The body ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "over",
    [
        {"access": "admin"},
        {"tool": ""},
        {"tool": "../../etc"},
        {"args": ["not", "an", "object"]},
        {"dry_run": "true"},
        {"access": "write", "request_id": None},
    ],
)
def test_a_malformed_body_is_refused_before_anything_travels(engine, over):
    response = _engine_client().post(CALL, json=_body(**over))

    assert response.status_code == 422
    assert _code(response) == ERR_BAD_ARGUMENTS
    assert engine.calls == []


# ── The mode ────────────────────────────────────────────────────────────────


def test_off_refuses_every_call_and_the_catalogue(engine, monkeypatch):
    _mode(monkeypatch, "off")

    read = _engine_client().post(CALL, json=_body())
    catalogue = _engine_client().get(CATALOGUE, params={"tenant": CHILD_A})

    assert (read.status_code, _code(read)) == (403, ERR_TOOLS_OFF)
    assert (catalogue.status_code, _code(catalogue)) == (403, ERR_TOOLS_OFF)
    assert engine.calls == []


def test_read_allows_reads_and_refuses_writes(engine, monkeypatch):
    _mode(monkeypatch, "read")

    assert _engine_client().post(CALL, json=_body()).status_code == 200
    write = _engine_client().post(CALL, json=_body(access="write", request_id="eff-1"))

    assert (write.status_code, _code(write)) == (403, ERR_WRITE_FORBIDDEN)
    assert len(_forwards(engine)) == 1


def test_dry_run_forces_every_write_to_a_dry_run(engine, monkeypatch):
    _mode(monkeypatch, "dry_run")

    response = _engine_client().post(CALL, json=_body(access="write", request_id="eff-1"))

    assert response.status_code == 200
    assert _forwards(engine)[0]["json"]["dry_run"] is True


def test_full_sends_a_write_as_asked(engine):
    _engine_client().post(CALL, json=_body(access="write", request_id="eff-1"))
    _engine_client().post(CALL, json=_body(access="write", request_id="eff-2", dry_run=True))

    assert [c["json"]["dry_run"] for c in _forwards(engine)] == [False, True]
    assert [c["json"]["request_id"] for c in _forwards(engine)] == ["eff-1", "eff-2"]


# ── The catalogue ───────────────────────────────────────────────────────────


def test_the_catalogue_lists_a_companys_tools(engine):
    response = _engine_client().get(CATALOGUE, params={"tenant": CHILD_A})

    assert response.status_code == 200, response.text
    assert response.json()["tools"][0]["name"] == "example_read"
    (call,) = engine.calls
    assert (call["method"], call["path"], call["tenant"]) == (
        "GET",
        "/api/admin/remote-tools",
        CHILD_A,
    )


def test_the_catalogue_describes_one_tool(engine):
    _engine_client().get(CATALOGUE, params={"tenant": CHILD_A, "tool": "example_read"})

    assert engine.calls[0]["path"] == "/api/admin/remote-tools/example_read"


def test_the_catalogue_refuses_a_tool_name_that_is_not_one(engine):
    response = _engine_client().get(CATALOGUE, params={"tenant": CHILD_A, "tool": "a/b"})

    assert response.status_code == 422
    assert engine.calls == []


def test_the_catalogue_of_a_non_descendant_is_unreachable(engine):
    response = _engine_client().get(CATALOGUE, params={"tenant": "acme-elsewhere"})

    assert response.status_code == 404
    assert engine.calls == []


# ── Audit ───────────────────────────────────────────────────────────────────


def test_a_forwarded_call_is_audited_under_the_target_without_its_arguments(engine, audit_rows):
    from robothor.tenancy.remote_contract import args_digest

    _engine_client().post(CALL, json=_body(args={"secret": "do-not-log"}))

    (row,) = [r for r in audit_rows if r["event_type"] == "org.remote_call"]
    assert row["tenant_id"] == CHILD_A
    assert row["action"] == "example_read"
    details = row["details"]
    assert details["home_tenant_id"] == ROOT
    assert details["target_tenant_id"] == CHILD_A
    assert details["args_digest"] == args_digest({"secret": "do-not-log"})
    assert "do-not-log" not in repr(row)


def test_a_refused_call_is_audited_as_denied(engine, audit_rows):
    _engine_client().post(CALL, json=_body(requested_by=BOB, access="write", request_id="e"))

    (row,) = [r for r in audit_rows if r["event_type"] == "org.remote_call"]
    assert row["status"] == "denied"


# ── Connections (a person) ──────────────────────────────────────────────────


def test_an_owner_at_home_sees_every_companys_connections(engine):
    response = _person().get(CONNECTIONS)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["home_tenant_id"] == ROOT
    assert body["mode"] == "full"
    assert [t["tenant_id"] for t in body["tenants"]] == [CHILD_A, CHILD_B]
    first = body["tenants"][0]
    assert first["status"] == "ok"
    assert first["error"] is None
    assert first["display_name"] == CHILD_A.upper()
    assert first["tools"][0]["name"] == "example_read"
    assert first["plugins"][0]["name"] == "example-plugin"
    assert first["checks"][0]["status"] == "ok"
    assert {c["tenant"] for c in engine.calls} == {CHILD_A, CHILD_B}


def test_one_company_down_is_that_entry_only(engine):
    engine.answers[CHILD_B] = (502, {"error": "engine unavailable"})

    tenants = _person().get(CONNECTIONS).json()["tenants"]

    by_id = {t["tenant_id"]: t for t in tenants}
    assert by_id[CHILD_A]["status"] == "ok"
    assert by_id[CHILD_B]["status"] == "error"
    assert by_id[CHILD_B]["error"]
    assert by_id[CHILD_B]["tools"] == []


@pytest.mark.parametrize("role", ["member", "viewer", "auditor"])
def test_connections_refuse_a_role_that_is_not_owner_or_admin(engine, role):
    assert _person(role=role).get(CONNECTIONS).status_code == 403
    assert engine.calls == []


def test_connections_refuse_an_engine(engine):
    assert _engine_client().get(CONNECTIONS).status_code == 403
    assert engine.calls == []


def test_connections_refuse_a_person_acting_in_another_tenant(engine, monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    reset_settings()

    assert _person(CHILD_A, "owner", home=ROOT).get(CONNECTIONS).status_code == 403
    assert engine.calls == []


def test_connections_are_audited_one_row_per_company(engine, audit_rows):
    _person().get(CONNECTIONS)

    rows = [r for r in audit_rows if r["event_type"] == "org.connections"]
    assert sorted(r["tenant_id"] for r in rows) == [CHILD_A, CHILD_B]


def test_connections_when_off_ask_no_engine(engine, monkeypatch):
    _mode(monkeypatch, "off")

    body = _person().get(CONNECTIONS).json()

    assert body["mode"] == "off"
    assert engine.calls == []
    assert all(t["status"] == "error" for t in body["tenants"])


def test_connections_ask_each_engine_with_a_short_timeout(engine):
    _person().get(CONNECTIONS)

    from routers.org_remote import CONNECTIONS_TIMEOUT_S

    assert CONNECTIONS_TIMEOUT_S == 10
    assert {c["timeout"] for c in engine.calls} == {CONNECTIONS_TIMEOUT_S}


def test_a_slow_company_does_not_hold_the_page(engine, monkeypatch):
    import time

    monkeypatch.setattr("routers.org_remote.CONNECTIONS_TIMEOUT_S", 0.2)
    engine.hang.add(CHILD_B)

    started = time.monotonic()
    response = _person().get(CONNECTIONS)
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 5
    by_id = {t["tenant_id"]: t for t in response.json()["tenants"]}
    assert by_id[CHILD_A]["status"] == "ok"
    assert by_id[CHILD_B]["status"] == "error"
    assert ROOT not in by_id
