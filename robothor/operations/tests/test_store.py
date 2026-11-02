"""Real PostgreSQL tests for shared budgets and recoverable work."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from robothor.db.connection import get_connection
from robothor.operations.store import BudgetExceeded, Conflict, Operations

#: The operations ledger's schema, in order. Other suites' fixtures reuse it.
OPERATIONS_MIGRATIONS = ("126_durable_operations.sql", "158_operation_action_proposer.sql")


@pytest.fixture
def ops():
    with get_connection() as conn:
        with conn.cursor() as cur:
            for filename in OPERATIONS_MIGRATIONS:
                cur.execute((Path(__file__).parents[3] / "crm/migrations" / filename).read_text())
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


def test_completion_rechecks_wall_clock_after_a_transaction_wait(ops):
    job_id = ops.enqueue("clock-test", "one", {})
    job = ops.claim("clock-test")
    with pytest.raises(Conflict):
        with ops.transaction() as cur:
            cur.execute(
                "UPDATE operation_jobs SET lease_until=clock_timestamp()+interval '30 milliseconds' WHERE tenant_id=%s AND id=%s",
                (ops.tenant, job_id),
            )
            cur.execute("SELECT pg_sleep(0.06)")
            ops.complete(job_id, job["lease_token"], {}, cur=cur)


def test_review_listing_is_this_tenants_open_drafts_only(ops):
    open_draft = ops.propose("email", "draft:open", {"subject": "Hello"})
    decided = ops.propose("email", "draft:decided", {"subject": "Done"})
    ops.decide(decided, True, "operator:1")
    expired = ops.propose("email", "draft:expired", {"subject": "Late"}, expires_seconds=-1)
    neighbour = Operations(ops.tenant + "-neighbour").propose("email", "draft:n", {"subject": "N"})

    listed = ops.list_review()

    assert [str(row["id"]) for row in listed] == [open_draft]
    assert listed[0]["kind"] == "email"
    assert listed[0]["payload"] == {"subject": "Hello"}
    assert listed[0]["expires_at"] is not None
    assert {expired, neighbour}.isdisjoint(str(row["id"]) for row in listed)


def test_an_action_is_read_only_within_its_tenant(ops):
    action = ops.propose("email", "draft:1", {"subject": "Hello"})

    assert str(ops.get_action(action)["id"]) == action
    assert ops.get_action(action)["status"] == "review"
    assert Operations(ops.tenant + "-neighbour").get_action(action) is None
    assert ops.get_action(str(uuid4())) is None


def _audit(ops, action):
    with ops.transaction() as cur:
        cur.execute(
            "SELECT event,actor,detail FROM operation_audit WHERE tenant_id=%s AND entity_id=%s "
            "ORDER BY id",
            (ops.tenant, action),
        )
        return [dict(r) for r in cur.fetchall()]


def _row(ops, action):
    with ops.transaction() as cur:
        cur.execute(
            "SELECT * FROM operation_actions WHERE tenant_id=%s AND id=%s", (ops.tenant, action)
        )
        return dict(cur.fetchone())


def test_policy_actor_decides_its_own_plugins_proposal(ops):
    action = ops.propose("acme-plugin.note", "n:1", {"body": "Hi"}, proposed_by="acme-plugin")
    ops.decide(action, True, "policy:acme-plugin:2026-10-29")
    row = _row(ops, action)
    assert row["status"] == "approved"
    assert row["proposed_by"] == "acme-plugin"
    assert row["decided_by"] == "policy:acme-plugin:2026-10-29"
    decided = [a for a in _audit(ops, action) if a["event"] == "action.approved"]
    assert decided == [
        {
            "event": "action.approved",
            "actor": "policy:acme-plugin:2026-10-29",
            "detail": {"policy": "acme-plugin", "version": "2026-10-29"},
        }
    ]
    assert ops.claim_action(kind="acme-plugin.note")["id"] == action


def test_policy_actor_for_another_plugin_is_refused(ops):
    action = ops.propose("acme-plugin.note", "n:1", {"body": "Hi"}, proposed_by="acme-plugin")
    with pytest.raises(Conflict):
        ops.decide(action, True, "policy:other-plugin:1")
    assert _row(ops, action)["status"] == "review"
    assert ops.claim_action() is None


def test_policy_actor_cannot_decide_an_unattributed_proposal(ops):
    action = ops.propose("email", "draft:1", {"body": "Hello"})
    with pytest.raises(Conflict):
        ops.decide(action, True, "policy:acme-plugin:1")
    assert _row(ops, action)["status"] == "review"
    ops.decide(action, True, "operator:1")
    assert _row(ops, action)["status"] == "approved"


@pytest.mark.parametrize(
    "actor", ["agent:main", "policy:acme-plugin", "policy::1", "policy:acme-plugin:", ""]
)
def test_malformed_or_unknown_actors_are_refused(ops, actor):
    action = ops.propose("acme-plugin.note", "n:1", {"body": "Hi"}, proposed_by="acme-plugin")
    with pytest.raises(Conflict):
        ops.decide(action, True, actor)
    assert _row(ops, action)["status"] == "review"


def test_policy_rejection_is_recorded_for_the_proposing_plugin(ops):
    action = ops.propose("acme-plugin.note", "n:1", {"body": "Hi"}, proposed_by="acme-plugin")
    ops.decide(action, False, "policy:acme-plugin:3")
    assert _row(ops, action)["status"] == "rejected"
    assert _audit(ops, action)[-1]["actor"] == "policy:acme-plugin:3"


def test_draft_key_cannot_be_rebound_to_another_proposer(ops):
    payload = {"body": "Hi"}
    action = ops.propose("acme-plugin.note", "n:1", payload, proposed_by="acme-plugin")
    assert ops.propose("acme-plugin.note", "n:1", payload, proposed_by="acme-plugin") == action
    with pytest.raises(Conflict):
        ops.propose("acme-plugin.note", "n:1", payload, proposed_by="other-plugin")
    with pytest.raises(Conflict):
        ops.propose("acme-plugin.note", "n:1", payload)


def test_approval_publishes_operation_action_approved(ops):
    published = []
    with patch("robothor.events.bus.publish", side_effect=lambda *a, **k: published.append((a, k))):
        by_operator = ops.propose("email", "draft:1", {"body": "Hello"})
        ops.decide(by_operator, True, "operator:1")
        by_policy = ops.propose(
            "acme-plugin.note", "n:1", {"body": "Hi"}, proposed_by="acme-plugin"
        )
        ops.decide(by_policy, True, "policy:acme-plugin:1")
        rejected = ops.propose("email", "draft:2", {"body": "No"})
        ops.decide(rejected, False, "operator:1")
    assert [(a[:3], k["tenant_id"]) for a, k in published] == [
        (
            (
                "operations",
                "operation_action.approved",
                {
                    "action_id": by_operator,
                    "kind": "email",
                    "tenant_id": ops.tenant,
                    "proposed_by": None,
                },
            ),
            ops.tenant,
        ),
        (
            (
                "operations",
                "operation_action.approved",
                {
                    "action_id": by_policy,
                    "kind": "acme-plugin.note",
                    "tenant_id": ops.tenant,
                    "proposed_by": "acme-plugin",
                },
            ),
            ops.tenant,
        ),
    ]
    assert [k["actor"] for _a, k in published] == ["operator:1", "policy:acme-plugin:1"]


# ── Reading, claiming by id and cancelling: the plugin seam ────────────────


def _note(ops, key, proposer="acme-plugin", kind="acme-plugin.note"):
    return ops.propose(kind, key, {"body": key}, proposed_by=proposer)


def test_get_action_reads_one_row_within_the_tenant(ops):
    action = _note(ops, "n:1")
    row = ops.get_action(action)
    assert row["id"] == action
    assert row["status"] == "review"
    assert row["proposed_by"] == "acme-plugin"
    assert row["payload"] == {"body": "n:1"}
    assert ops.get_action(str(uuid4())) is None
    assert Operations("test-" + uuid4().hex).get_action(action) is None


def test_list_actions_filters_by_kind_status_and_proposer(ops):
    first = _note(ops, "n:1")
    second = _note(ops, "n:2")
    other = _note(ops, "o:1", proposer="other-plugin", kind="other-plugin.note")
    unattributed = ops.propose("email", "draft:1", {"body": "Hello"})
    ops.decide(second, True, "policy:acme-plugin:1")

    def ids(**filters):
        return {str(r["id"]) for r in ops.list_actions(**filters)}

    assert ids() == {first, second, other, unattributed}
    assert ids(kinds=["acme-plugin.note"]) == {first, second}
    assert ids(kinds=["acme-plugin.note", "email"]) == {first, second, unattributed}
    assert ids(status="approved") == {second}
    assert ids(proposed_by="other-plugin") == {other}
    assert ids(proposed_by="acme-plugin", status="review") == {first}
    assert len(ops.list_actions(limit=2)) == 2
    assert Operations("test-" + uuid4().hex).list_actions() == []
    with pytest.raises(ValueError):
        ops.list_actions(limit=0)


def test_list_actions_is_newest_first(ops):
    older = _note(ops, "n:1")
    newer = _note(ops, "n:2")
    with ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET created_at=now()-interval '1 hour' "
            "WHERE tenant_id=%s AND id=%s",
            (ops.tenant, older),
        )
    assert [str(r["id"]) for r in ops.list_actions()] == [newer, older]


def test_claim_action_by_id_takes_exactly_that_approved_action(ops):
    first = _note(ops, "n:1")
    second = _note(ops, "n:2")
    for action in (first, second):
        ops.decide(action, True, "policy:acme-plugin:1")
    claimed = ops.claim_action_by_id(second)
    assert str(claimed["id"]) == second
    assert claimed["status"] == "executing"
    assert claimed["lease_token"]
    assert ops.claim_action_by_id(second) is None  # already executing
    assert ops.get_action(first)["status"] == "approved"
    ops.finish_action(second, claimed["lease_token"], "completed", {"id": "ack"})
    assert ops.get_action(second)["status"] == "completed"
    assert [a["event"] for a in _audit(ops, second)][-2:] == ["action.claimed", "action.completed"]


def test_claim_action_by_id_refuses_unapproved_expired_and_foreign_actions(ops):
    in_review = _note(ops, "n:1")
    assert ops.claim_action_by_id(in_review) is None
    expired = _note(ops, "n:2")
    ops.decide(expired, True, "policy:acme-plugin:1")
    with ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET expires_at=now()-interval '1 second' "
            "WHERE tenant_id=%s AND id=%s",
            (ops.tenant, expired),
        )
    assert ops.claim_action_by_id(expired) is None
    approved = _note(ops, "n:3")
    ops.decide(approved, True, "policy:acme-plugin:1")
    assert Operations("test-" + uuid4().hex).claim_action_by_id(approved) is None
    assert ops.get_action(approved)["status"] == "approved"


def test_claim_action_by_id_is_fenced_like_claim_action(ops):
    action = _note(ops, "n:1")
    ops.decide(action, True, "policy:acme-plugin:1")
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: ops.claim_action_by_id(action), range(4)))
    claimed = [c for c in claims if c]
    assert len(claimed) == 1
    with pytest.raises(Conflict):
        ops.finish_action(action, str(uuid4()), "completed", {"id": "ack"})
    with ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET lease_until=now()-interval '1 second' "
            "WHERE tenant_id=%s AND id=%s",
            (ops.tenant, action),
        )
    assert ops.claim_action_by_id(action) is None  # an expired send is unknown, never ready
    assert ops.get_action(action)["status"] == "unknown"


def test_claim_action_by_id_refuses_a_modified_payload(ops):
    action = _note(ops, "n:1")
    ops.decide(action, True, "policy:acme-plugin:1")
    with ops.transaction() as cur:
        cur.execute(
            'UPDATE operation_actions SET payload=\'{"body":"changed"}\'::jsonb '
            "WHERE tenant_id=%s AND id=%s",
            (ops.tenant, action),
        )
    with pytest.raises(Conflict):
        ops.claim_action_by_id(action)


@pytest.mark.parametrize("decide_first", [False, True])
def test_proposer_cancels_its_own_draft_with_an_audited_reason(ops, decide_first):
    action = _note(ops, "n:1")
    if decide_first:
        ops.decide(action, True, "policy:acme-plugin:1")
    ops.cancel_action(action, "superseded by n:2", proposed_by="acme-plugin")
    assert ops.get_action(action)["status"] == "cancelled"
    assert ops.claim_action_by_id(action) is None
    assert _audit(ops, action)[-1] == {
        "event": "action.cancelled",
        "actor": "plugin:acme-plugin",
        "detail": {
            "reason": "superseded by n:2",
            "previous_status": "approved" if decide_first else "review",
        },
    }


def test_cancel_is_refused_for_another_proposer_or_a_settled_action(ops):
    action = _note(ops, "n:1")
    with pytest.raises(Conflict):
        ops.cancel_action(action, "not mine", proposed_by="other-plugin")
    with pytest.raises(Conflict):
        ops.cancel_action(action, "no proposer named")
    assert ops.get_action(action)["status"] == "review"
    ops.decide(action, True, "policy:acme-plugin:1")
    claimed = ops.claim_action_by_id(action)
    with pytest.raises(Conflict):
        ops.cancel_action(action, "too late", proposed_by="acme-plugin")
    ops.finish_action(action, claimed["lease_token"], "completed", {"id": "ack"})
    with pytest.raises(Conflict):
        ops.cancel_action(action, "too late", proposed_by="acme-plugin")
    with pytest.raises(Conflict):
        ops.cancel_action(str(uuid4()), "missing", proposed_by="acme-plugin")
    with pytest.raises(ValueError):
        ops.cancel_action(action, " ", proposed_by="acme-plugin")


@pytest.mark.parametrize("decide_first", [False, True])
def test_an_unattributed_action_is_cancelled_only_by_a_named_operator(ops, decide_first):
    action = ops.propose("email", "draft:1", {"body": "Hello"})
    if decide_first:
        ops.decide(action, True, "operator:1")
    for refused in (
        {"proposed_by": "acme-plugin"},
        {},
        {"actor": "agent:main"},
        {"actor": "policy:acme-plugin:1"},
    ):
        with pytest.raises(Conflict):
            ops.cancel_action(action, "not mine", **refused)
    assert ops.get_action(action)["status"] == ("approved" if decide_first else "review")
    ops.cancel_action(action, "operator withdrew it", actor="operator:2")
    assert ops.get_action(action)["status"] == "cancelled"
    assert _audit(ops, action)[-1]["actor"] == "operator:2"


def test_a_plugins_action_is_not_cancelled_by_an_operator_actor_alone(ops):
    action = _note(ops, "n:1")
    with pytest.raises(Conflict):
        ops.cancel_action(action, "not the proposer", actor="operator:2")
    assert ops.get_action(action)["status"] == "review"
