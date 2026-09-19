"""Real PostgreSQL tests for shared budgets and recoverable work."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from robothor.db.connection import get_connection
from robothor.operations.store import BudgetExceeded, Conflict, Operations


@pytest.fixture
def ops():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                (
                    Path(__file__).parents[3] / "crm/migrations/126_durable_operations.sql"
                ).read_text()
            )
        conn.commit()
    return Operations("test-" + uuid4().hex)


def test_work_claim_is_exclusive_and_fenced(ops):
    job = ops.enqueue("research", "company:1", {"company_id": "1"})
    assert ops.enqueue("research", "company:1", {"company_id": "1"}) == job
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: ops.claim("research"), range(4)))
    claimed = [c for c in claims if c]
    assert len(claimed) == 1
    with pytest.raises(Conflict):
        ops.complete(job, str(uuid4()), {"ok": True})
    ops.complete(job, claimed[0]["lease_token"], {"ok": True})
    assert ops.claim("research") is None
    assert Operations("another-tenant").get_job(job) is None


def test_parallel_budget_reservations_cannot_overspend(ops):
    ops.set_budget("pilot", 100)

    def reserve(i):
        try:
            return ops.reserve("pilot", str(i), 40)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        reservations = [r for r in pool.map(reserve, range(4)) if r]
    assert len(reservations) == 2
    ops.settle(reservations[0], 20)
    ops.reserve("pilot", "after-settlement", 40)
    with pytest.raises(BudgetExceeded):
        ops.reserve("missing", "fail-closed", 1)


def test_event_dedup_and_action_approval_are_tenant_bound(ops):
    assert ops.receive("provider", "event-1", {"kind": "reply"})
    assert not ops.receive("provider", "event-1", {"kind": "reply"})
    action = ops.propose("email", "draft:1", {"to": "agent@example.com", "body": "Hello"})
    assert ops.claim_action() is None
    ops.decide(action, True, "operator:1")
    send = ops.claim_action()
    assert send["id"] == action
    assert ops.claim_action() is None
    ops.finish_action(action, send["lease_token"], "unknown", {})
    assert ops.claim_action() is None  # timeout must never resend


def test_approval_cannot_authorize_modified_payload(ops):
    action = ops.propose("email", "draft:1", {"body": "approved"})
    ops.decide(action, True, "operator:1")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE operation_actions SET payload = %s::jsonb WHERE id = %s",
                ('{"body":"changed"}', action),
            )
        conn.commit()
    with pytest.raises(Conflict):
        ops.claim_action()


def test_inbox_reused_identity_cannot_hide_changed_event(ops):
    assert ops.receive("provider", "event-1", {"kind": "reply"})
    with pytest.raises(Conflict):
        ops.receive("provider", "event-1", {"kind": "unsubscribe"})


def test_expired_executor_cannot_commit_a_completed_action(ops):
    action = ops.propose("email", "draft:1", {"body": "Hello"})
    ops.decide(action, True, "operator:test")
    claimed = ops.claim_action()
    with ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET lease_until=now()-interval '1 second' WHERE tenant_id=%s AND id=%s",
            (ops.tenant, action),
        )
    with pytest.raises(Conflict):
        ops.finish_action(action, claimed["lease_token"], "completed", {"id": "receipt"})
