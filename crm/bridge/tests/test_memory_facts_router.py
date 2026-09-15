"""The Helm's Memory page: list facts, preview a forget, forget one fact.

What these tests pin, and why each is a test rather than a docstring:

* **The gate is the operator's.** Forgetting a fact is an irreversible edit to
  what the fleet believes; an agent's own token must not reach it, and neither
  must a member session, an auditor, or another tenant's owner.
* **Preview writes nothing.** The whole point of a preview is that an operator
  can look before they act, so "read-only" is asserted against the row itself
  rather than trusted from the handler's shape.
* **A second forget is a 409, not a silent success.** Idempotence that reports
  success hides a double-click from the person who has to explain, later, when
  the fact stopped being believed.
* **The audit row names the fact, never quotes it.** ``fact_text`` is the
  content the operator is deleting, and the audit log is exported to a SIEM.

The DB-backed half lives in ``test_memory_facts_query_integration.py`` — this
module is the unit lane, so every assertion here holds with no database at all.
"""

from __future__ import annotations

import pytest

FACTS = "/api/memory/facts"
PREVIEW = "/api/memory/facts/1/forget/preview"
FORGET = "/api/memory/facts/1/forget"


@pytest.fixture
def no_db(monkeypatch):
    """Any query this router makes is a test failure in this lane.

    Not a convenience: every assertion below is about a refusal, and a refusal
    that happens AFTER the row is read is a different (worse) route than one
    that happens before. Binding the connection to an exception is what proves
    which of the two shipped.
    """
    from routers import memory_facts

    def _boom(*_args, **_kwargs):
        raise AssertionError("the unit lane must not open a database connection")

    monkeypatch.setattr(memory_facts, "get_connection", _boom)


# ── the gate ─────────────────────────────────────────────────────────────────


def test_every_route_rejects_a_service_token(controls_client_as_service, no_db):
    """An agent must not be able to read, preview or erase the fleet's memory."""
    assert controls_client_as_service.get(FACTS).status_code == 403
    assert controls_client_as_service.post(PREVIEW).status_code == 403
    assert controls_client_as_service.post(FORGET, json={"reason": "spam"}).status_code == 403


def test_every_route_rejects_a_non_operator_human(controls_client_as_viewer, no_db):
    assert controls_client_as_viewer.get(FACTS).status_code == 403
    assert controls_client_as_viewer.post(PREVIEW).status_code == 403
    assert controls_client_as_viewer.post(FORGET, json={"reason": "spam"}).status_code == 403


def test_every_route_rejects_another_tenants_owner(controls_client_as_other_tenant_owner, no_db):
    client = controls_client_as_other_tenant_owner
    assert client.get(FACTS).status_code == 403
    assert client.post(PREVIEW).status_code == 403
    assert client.post(FORGET, json={"reason": "spam"}).status_code == 403


def test_an_auditor_may_not_touch_memory(controls_client_as_auditor, no_db):
    """Read-only plus the audit log — not a licence to edit what is believed."""
    assert controls_client_as_auditor.get(FACTS).status_code == 403
    assert controls_client_as_auditor.post(FORGET, json={"reason": "spam"}).status_code == 403


# ── validation, all of it before any query ───────────────────────────────────


@pytest.mark.parametrize("limit", ["0", "201", "-1", "abc"])
def test_limit_is_bounded(controls_client_as_operator, no_db, limit):
    assert controls_client_as_operator.get(f"{FACTS}?limit={limit}").status_code == 422


@pytest.mark.parametrize("active", ["yes", "1", "TRUE-ish", ""])
def test_active_takes_only_the_three_words(controls_client_as_operator, no_db, active):
    assert controls_client_as_operator.get(f"{FACTS}?active={active}").status_code == 422


@pytest.mark.parametrize("cursor", ["abc", "1.5", "-", "0"])
def test_a_cursor_that_is_not_a_fact_id_is_422(controls_client_as_operator, no_db, cursor):
    assert controls_client_as_operator.get(f"{FACTS}?cursor={cursor}").status_code == 422


@pytest.mark.parametrize("fact_id", ["abc", "1.5", "-3", "0", "1%20OR%201=1"])
def test_a_fact_id_that_is_not_an_id_is_422(controls_client_as_operator, no_db, fact_id):
    """Validated at the boundary: ``WHERE id = %s`` on an integer column turns
    a typo into a 500, and a 500 is indistinguishable from a broken appliance."""
    client = controls_client_as_operator
    assert client.post(f"/api/memory/facts/{fact_id}/forget/preview").status_code == 422
    assert (
        client.post(f"/api/memory/facts/{fact_id}/forget", json={"reason": "spam"}).status_code
        == 422
    )


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "  "}, {"reason": "ab"}])
def test_a_forget_without_a_real_reason_is_422(controls_client_as_operator, no_db, body):
    resp = controls_client_as_operator.post(FORGET, json=body)
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], str), "the Helm was promised a flat 422"


def test_a_reason_longer_than_the_audit_column_is_422(controls_client_as_operator, no_db):
    assert controls_client_as_operator.post(FORGET, json={"reason": "x" * 501}).status_code == 422
