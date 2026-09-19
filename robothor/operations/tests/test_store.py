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


def test_multi_scope_admission_is_atomic_and_parallel_safe(ops):
    ops.set_budget("day", 80)
    ops.set_budget("month", 100)

    def admit(i):
        try:
            return ops.reserve_many(["month", "day"], str(i), 40)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        admitted = [r for r in pool.map(admit, range(4)) if r]
    assert len(admitted) == 2
    with ops.transaction() as cur:
        cur.execute(
            "SELECT scope,reserved_units FROM operation_budgets WHERE tenant_id=%s", (ops.tenant,)
        )
        assert {r["scope"]: r["reserved_units"] for r in cur.fetchall()} == {"day": 80, "month": 80}
        cur.execute(
            "SELECT count(*) AS n FROM operation_reservations WHERE tenant_id=%s", (ops.tenant,)
        )
        assert cur.fetchone()["n"] == 4


def test_multi_scope_missing_budget_leaves_no_partial_reservation(ops):
    ops.set_budget("a", 100)
    with pytest.raises(BudgetExceeded):
        ops.reserve_many(["a", "missing"], "attempt", 40)
    with ops.transaction() as cur:
        cur.execute(
            "SELECT reserved_units FROM operation_budgets WHERE tenant_id=%s", (ops.tenant,)
        )
        assert cur.fetchone()["reserved_units"] == 0


def test_group_settlement_cannot_partially_commit_or_reauthorize_spend(ops):
    for scope in ("day", "month"):
        ops.set_budget(scope, 100)
    reservations = ops.reserve_many(["month", "day"], "attempt", 40, active_only=True)
    assert ops.reserve_many(["day", "month"], "attempt", 40, active_only=True) == reservations
    with pytest.raises(Conflict):
        ops.settle_many([*reservations, str(uuid4())], 20)
    with ops.transaction() as cur:
        cur.execute(
            "SELECT actual_units FROM operation_reservations WHERE tenant_id=%s", (ops.tenant,)
        )
        assert all(r["actual_units"] is None for r in cur.fetchall())
    ops.settle_many(reservations, 20)
    ops.settle_many(list(reversed(reservations)), 20)
    with pytest.raises(Conflict):
        ops.reserve_many(["day", "month"], "attempt", 40, active_only=True)


def test_checkpoint_is_fenced_and_preserved_when_a_worker_is_replaced(ops):
    job = ops.enqueue("research", "checkpoint", {})
    first = ops.claim("research")
    saved = {"output": {"summary": "Evidence saved"}, "run_id": "run-1"}
    ops.checkpoint(job, first["lease_token"], saved)
    with ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET lease_until=now()-interval '1 second' WHERE tenant_id=%s AND id=%s",
            (ops.tenant, job),
        )
    replacement = ops.claim("research")
    assert replacement["result"] == saved
    with pytest.raises(Conflict):
        ops.checkpoint(job, first["lease_token"], saved)
    with pytest.raises(Conflict):
        ops.checkpoint(job, replacement["lease_token"], {"output": "changed"})
    ops.checkpoint(job, replacement["lease_token"], saved)
    ops.complete(job, replacement["lease_token"], {"run_id": "run-1"})


def test_request_admission_is_shared_bounded_and_tenant_isolated(ops):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: ops.admit_request("provider.emails", limit=3, window_seconds=60), range(8)
            )
        )
    assert sum(results) == 3
    assert not Operations(ops.tenant).admit_request("provider.emails", limit=3, window_seconds=60)
    assert Operations("test-" + uuid4().hex).admit_request(
        "provider.emails", limit=3, window_seconds=60
    )
    with ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_audit SET created_at=now()-interval '61 seconds' WHERE tenant_id=%s AND event='request.admitted'",
            (ops.tenant,),
        )
    assert ops.admit_request("provider.emails", limit=3, window_seconds=60)
