"""``GET /api/controls/audit`` — who changed which guardrail, and why.

The one surface in the Helm an AUDITOR can reach that an ordinary member
cannot, and the reason this router is not part of ``controls.py``: that module
is operator-only in all four of its locks and says so, and a route an auditor
may read does not belong behind a docstring that promises the opposite.

What is asserted here:

* an auditor reads it; a member, a viewer, an agent and another tenant's owner
  do not;
* it is a READ — no mutation route appears under this path at all;
* the page is keyset-paginated newest-first, and a cursor that is not an id is
  a flat 422 rather than a 500.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

AUDIT = "/api/controls/audit"

#: One row as ``feature_flag_audit`` stores it: id, name, from, to, actor,
#: reason, at.
_ROW = (
    7,
    "ROBOTHOR_RBAC_MODE",
    "observe",
    "enforce",
    "operator:alice",
    "soak is clean",
    datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
)


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, list(params or [])))

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _Conn:
    def __init__(self, rows):
        self.cur = _Cursor(rows)

    def cursor(self, *_a, **_k):
        return self.cur


@pytest.fixture
def fake_db(monkeypatch):
    """A connection that answers with canned rows and records the SQL."""
    from routers import flag_audit

    conn = _Conn([_ROW])

    @contextmanager
    def _conn():
        yield conn

    monkeypatch.setattr(flag_audit, "get_connection", _conn)
    return conn


@pytest.fixture
def no_db(monkeypatch):
    """Any query in this lane is a refusal that happened too late."""
    from routers import flag_audit

    def _boom(*_a, **_k):
        raise AssertionError("the refusal must happen before the query")

    monkeypatch.setattr(flag_audit, "get_connection", _boom)


# ── the gate ─────────────────────────────────────────────────────────────────


def test_an_auditor_may_read_the_change_log(controls_client_as_auditor, fake_db):
    """ "Read-only, plus the audit log" — this IS the audit log for guardrails."""
    resp = controls_client_as_auditor.get(AUDIT)
    assert resp.status_code == 200
    assert resp.json()["changes"][0]["flag"] == "ROBOTHOR_RBAC_MODE"


def test_an_operator_may_read_the_change_log(controls_client_as_operator, fake_db):
    assert controls_client_as_operator.get(AUDIT).status_code == 200


def test_a_service_token_may_not(controls_client_as_service, no_db):
    """An agent must not read the record of what was done TO it."""
    assert controls_client_as_service.get(AUDIT).status_code == 403


def test_an_ordinary_member_may_not(controls_client_as_viewer, controls_client_as_user, no_db):
    assert controls_client_as_viewer.get(AUDIT).status_code == 403
    assert controls_client_as_user.get(AUDIT).status_code == 403


def test_another_tenants_owner_may_not(controls_client_as_other_tenant_owner, no_db):
    """``feature_flag_audit`` has no tenant column: the flags are platform-wide,
    so the change log is too, and only the platform tenant may read it."""
    assert controls_client_as_other_tenant_owner.get(AUDIT).status_code == 403


# ── the payload ──────────────────────────────────────────────────────────────


def test_a_change_is_rendered_with_both_values_and_the_reason(controls_client_as_operator, fake_db):
    change = controls_client_as_operator.get(AUDIT).json()["changes"][0]
    assert change == {
        "id": 7,
        "flag": "ROBOTHOR_RBAC_MODE",
        "old_value": "observe",
        "new_value": "enforce",
        "changed_by": "operator:alice",
        "reason": "soak is clean",
        "changed_at": "2026-09-15T12:00:00+00:00",
    }


def test_the_flag_filter_is_a_parameter_not_a_string_format(controls_client_as_operator, fake_db):
    controls_client_as_operator.get(f"{AUDIT}?flag=ROBOTHOR_RBAC_MODE")
    sql, params = fake_db.cur.executed[-1]
    assert "ROBOTHOR_RBAC_MODE" not in sql
    assert "ROBOTHOR_RBAC_MODE" in params


def test_a_full_page_offers_a_cursor(controls_client_as_operator, fake_db):
    body = controls_client_as_operator.get(f"{AUDIT}?limit=1").json()
    assert body["next_cursor"] == "7"


def test_a_short_page_offers_none(controls_client_as_operator, fake_db):
    assert controls_client_as_operator.get(f"{AUDIT}?limit=50").json()["next_cursor"] is None


@pytest.mark.parametrize("limit", ["0", "501", "abc", "-1"])
def test_limit_is_bounded(controls_client_as_operator, no_db, limit):
    assert controls_client_as_operator.get(f"{AUDIT}?limit={limit}").status_code == 422


@pytest.mark.parametrize("cursor", ["abc", "-1", "1.5"])
def test_a_cursor_that_is_not_an_id_is_a_flat_422(controls_client_as_operator, no_db, cursor):
    resp = controls_client_as_operator.get(f"{AUDIT}?cursor={cursor}")
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str)


def test_the_change_log_router_contributes_no_mutation_route():
    """The auditor widening is bounded by there being nothing here to widen ONTO.

    Asserted against the assembled app rather than by trying verbs: ``PATCH
    /api/controls/audit`` is matched by ``controls.set_control``'s
    ``/{name}`` — the operator-gated flag write, which answers "unknown flag" —
    so a verb probe would be testing that route, not this one.
    """
    from bridge_service import app
    from fastapi.routing import iter_route_contexts
    from routers import flag_audit

    mutations = [
        route
        for route in iter_route_contexts(app.routes)
        if getattr(route, "endpoint", None) is not None
        and route.endpoint.__module__ == flag_audit.__name__
        and (route.methods or set()) - {"GET", "HEAD", "OPTIONS"}
    ]
    assert mutations == []


def test_an_auditor_cannot_reach_the_operator_half_of_controls(controls_client_as_auditor):
    """The change log, and nothing else under ``/api/controls``."""
    assert controls_client_as_auditor.get("/api/controls").status_code == 403
    assert (
        controls_client_as_auditor.patch(
            "/api/controls/ROBOTHOR_RBAC_MODE", json={"value": "enforce", "reason": "no"}
        ).status_code
        == 403
    )
