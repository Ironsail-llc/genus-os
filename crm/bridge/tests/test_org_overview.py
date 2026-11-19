"""``GET /api/org/overview``: the command centre's numbers, for a parent's operator.

A parent tenant's owner or admin, signed in AT HOME, sees one entry per company:
their home tenant and its descendants (``resolve_accessible_tenants``). The
numbers are ``robothor.tenancy.rollup.tenant_rollup``'s, the same function the
``org_overview`` engine tool answers with, so the page and the agent agree.

Everyone else is refused: a member, a service token, and a human acting in
another tenant (that tenant's own pages are theirs while they act there).

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

KEY = "test-signing-key-at-least-32-bytes-long-xyz"
ROOT = "acme-parent"
CHILD_A = "acme-child-a"
CHILD_B = "acme-child-b"
URL = "/api/org/overview"


def _accessible(tenant, role=None, **_kwargs):
    if role not in ("owner", "admin"):
        return (tenant,)
    return {ROOT: (ROOT, CHILD_A, CHILD_B), CHILD_A: (CHILD_A,), CHILD_B: (CHILD_B,)}[tenant]


def _entry(tenant_id: str, name: str, parent: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "display_name": name,
        "parent_id": parent,
        "runs_24h": 10,
        "failures_24h": 2,
        "success_rate_24h": 0.8,
        "cost_usd_24h": 0.5,
        "runs_7d": 70,
        "failures_7d": 10,
        "success_rate_7d": 0.8571,
        "cost_usd_7d": 3.25,
        "top_failures": [{"agent_id": "triage", "error_type": "timeout", "count": 2}],
        "agents_with_consecutive_errors": [],
        "overdue_schedules": [],
        "tasks": {"open": 7, "overdue": 1, "oldest_overdue": []},
        "last_activity_at": "2026-11-18T11:55:00+00:00",
        "status": "attention",
        **extra,
    }


class _Rollup:
    """A stand-in for ``tenant_rollup`` that records what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, tenant_ids, *, now=None) -> dict[str, Any]:
        ids = list(tenant_ids)
        self.calls.append(ids)
        names = {ROOT: "Acme Holdings", CHILD_A: "Acme Child A", CHILD_B: "Acme Child B"}
        parents = {ROOT: None, CHILD_A: ROOT, CHILD_B: ROOT}
        return {
            "as_of": "2026-11-18T12:00:00+00:00",
            "tenants": [_entry(t, names[t], parents[t]) for t in ids],
        }


@pytest.fixture
def rollup():
    return _Rollup()


@pytest.fixture
def audit_rows():
    return []


@pytest.fixture(autouse=True)
def _env(monkeypatch, rollup, audit_rows):
    monkeypatch.setenv("GENUS_AUTH_SIGNING_KEY", KEY)
    # Acting tokens are honoured only while switching is enforced, so the
    # acting case below reaches the route rather than the middleware's refusal.
    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "enforce")
    import routers._operator as operator

    from robothor.auth import tokens
    from robothor.settings import reset_settings

    monkeypatch.setattr(operator, "PLATFORM_TENANT", ROOT)
    tokens.reset_signing_key_cache()
    reset_settings()

    def _log_event(event_type, **kw):
        audit_rows.append({"event_type": event_type, **kw})

    with (
        patch("robothor.engine.permissions.resolve_accessible_tenants", new=_accessible),
        patch("robothor.tenancy.rollup.tenant_rollup", new=rollup),
        patch("routers._audit.log_event", side_effect=_log_event),
    ):
        yield
    tokens.reset_signing_key_cache()
    reset_settings()


def _client(tenant: str, role: str = "owner", *, home: str | None = None):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app, raise_server_exceptions=False)
    token = tokens.issue_access_token("alice", tenant, role, home_tenant_id=home)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _service_client(tenant: str):
    from bridge_service import app
    from fastapi.testclient import TestClient

    from robothor.auth import tokens

    client = TestClient(app, raise_server_exceptions=False)
    token = tokens.issue_service_token("svc-agent", tenant)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def _ids(response) -> list[str]:
    assert response.status_code == 200, response.text
    return [t["tenant_id"] for t in response.json()["tenants"]]


# --- who may read it -------------------------------------------------------


def test_an_owner_at_home_sees_their_tenant_and_every_child():
    response = _client(ROOT, "owner").get(URL)

    assert _ids(response) == [ROOT, CHILD_A, CHILD_B]
    assert response.json()["has_children"] is True
    assert response.json()["home_tenant_id"] == ROOT


def test_an_admin_at_home_sees_the_same():
    assert _ids(_client(ROOT, "admin").get(URL)) == [ROOT, CHILD_A, CHILD_B]


@pytest.mark.parametrize("role", ["member", "viewer", "auditor"])
def test_a_role_that_is_not_owner_or_admin_is_refused(rollup, role):
    assert _client(ROOT, role).get(URL).status_code == 403
    assert rollup.calls == []


def test_a_service_caller_is_refused(rollup):
    assert _service_client(ROOT).get(URL).status_code == 403
    assert rollup.calls == []


def test_a_user_acting_in_another_tenant_is_refused(rollup):
    response = _client(CHILD_A, "owner", home=ROOT).get(URL)

    assert response.status_code == 403
    assert rollup.calls == []


def test_the_owner_of_a_child_sees_only_that_child():
    response = _client(CHILD_A, "owner").get(URL)

    assert _ids(response) == [CHILD_A]
    assert ROOT not in _ids(response)
    assert CHILD_B not in _ids(response)


def test_a_tenant_with_no_children_gets_its_own_entry_and_no_children_flag():
    response = _client(CHILD_B, "admin").get(URL)

    assert _ids(response) == [CHILD_B]
    assert response.json()["has_children"] is False


# --- what it answers -------------------------------------------------------


def test_the_numbers_are_the_rollups_unchanged(rollup):
    response = _client(ROOT, "owner").get(URL)

    assert response.status_code == 200, response.text
    assert rollup.calls == [[ROOT, CHILD_A, CHILD_B]]
    expected = _Rollup()([ROOT, CHILD_A, CHILD_B])
    body = response.json()
    assert body["as_of"] == expected["as_of"]
    assert body["tenants"] == expected["tenants"]


def test_the_page_is_told_whether_switching_is_on(monkeypatch):
    from robothor.settings import reset_settings

    assert _client(ROOT, "owner").get(URL).json()["switching_enabled"] is True

    monkeypatch.setenv("GENUS_TENANT_SWITCH_MODE", "off")
    reset_settings()
    assert _client(ROOT, "owner").get(URL).json()["switching_enabled"] is False


def test_each_other_company_read_is_audited_under_that_company(audit_rows):
    _client(ROOT, "owner").get(URL)

    reads = [r for r in audit_rows if r["event_type"] == "org.overview"]
    assert sorted(r["tenant_id"] for r in reads) == [CHILD_A, CHILD_B]
    for row in reads:
        assert row["actor"] == "alice"
        assert row["details"]["home_tenant_id"] == ROOT
        assert row["details"]["target_tenant_id"] == row["tenant_id"]


def test_a_refusal_reads_and_audits_nothing(audit_rows):
    _client(ROOT, "member").get(URL)

    assert not [r for r in audit_rows if r["event_type"] == "org.overview"]


def test_the_org_router_is_read_only():
    from routers import org

    methods = {m for route in org.router.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD", "OPTIONS"}
