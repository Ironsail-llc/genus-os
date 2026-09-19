"""Reviewed recovery cannot revive stale approvals or repeat unknown sends."""

import pytest

from robothor.operations.store import Conflict
from robothor.sales.tests.test_guards import draft, prepared


def recovery(sales):
    from robothor.sales.recovery import Recovery

    return Recovery(sales)


def test_takeover_resume_preserves_cancelled_approvals_and_requires_fresh_review(sales):
    p = prepared(sales)
    action = draft(sales, p)
    sales.ops.decide(action, True, "operator:test")
    sales.takeover(p["id"], "operator:test")
    control = recovery(sales)
    before = control.snapshot(p["id"])
    result = control.change(
        p["id"],
        "resume",
        before["state_hash"],
        "operator:test",
        "Return the reviewed conversation to the agent",
    )
    assert result["owner"] == "agent"
    assert (
        next(a for a in sales.overview()["actions"] if str(a["id"]) == str(action))["status"]
        == "cancelled"
    )
    with pytest.raises(Conflict):
        control.change(
            p["id"], "resume", before["state_hash"], "operator:test", "Replay an old review"
        )
    assert sales.ops.claim_action() is None


def test_reprepare_initial_cancels_old_review_and_queues_fresh_unapproved_work(sales):
    p = prepared(sales)
    action = draft(sales, p)
    control = recovery(sales)
    before = control.snapshot(p["id"])
    result = control.change(
        p["id"],
        "initial",
        before["state_hash"],
        "operator:test",
        "Rewrite the opening around the reviewed workflow",
    )
    job = sales.ops.get_job(result["job_id"])
    assert job["kind"] == "sales.draft"
    assert job["payload"]["prospect_id"] == str(p["id"])
    assert (
        next(a for a in sales.overview()["actions"] if str(a["id"]) == str(action))["status"]
        == "cancelled"
    )
    assert sales.ops.claim_action() is None
    with pytest.raises(Conflict):
        control.change(
            p["id"], "initial", before["state_hash"], "operator:test", "Repeat stale review"
        )


@pytest.mark.parametrize("hold", ["unknown", "executing", "running_research", "held_spending"])
def test_recovery_refuses_inflight_or_unresolved_work(sales, hold):
    p = prepared(sales)
    control = recovery(sales)
    with sales.ops.transaction() as cur:
        if hold in {"unknown", "executing"}:
            action = draft(sales, p)
            cur.execute(
                "UPDATE operation_actions SET status=%s WHERE tenant_id=%s AND id=%s",
                (hold, sales.tenant, action),
            )
        else:
            job = sales.ops.enqueue("sales.draft", "held", {"prospect_id": str(p["id"])}, cur=cur)
            if hold == "running_research":
                cur.execute("UPDATE operation_jobs SET status='running' WHERE id=%s", (job,))
    if hold == "held_spending":
        sales.ops.set_budget("test", 100)
        sales.ops.reserve("test", str(job) + ":model", 10)
    before = control.snapshot(p["id"])
    with pytest.raises(Conflict):
        control.change(
            p["id"],
            "initial",
            before["state_hash"],
            "operator:test",
            "Retry the selected preparation stage",
        )


def test_resume_rejects_agent_authority_and_a_newer_takeover(sales):
    p = prepared(sales)
    sales.takeover(p["id"], "operator:test")
    control = recovery(sales)
    before = control.snapshot(p["id"])
    with pytest.raises(Conflict):
        control.change(
            p["id"], "resume", before["state_hash"], "agent:sdr", "Agent must not regain authority"
        )
    sales.takeover(p["id"], "operator:other")
    with pytest.raises(Conflict):
        control.change(
            p["id"],
            "resume",
            before["state_hash"],
            "operator:test",
            "Old operator decision must fail",
        )


def test_initial_reprepare_refuses_a_company_with_an_existing_conversation(sales):
    from datetime import UTC, datetime

    p = prepared(sales)
    sales.record_message(
        {
            "provider_id": "reply-1",
            "prospect_id": p["id"],
            "direction": "inbound",
            "sender": "alice@example.com",
            "recipient": "sales@example.com",
            "subject": "Hello",
            "body": "Tell me more",
            "occurred_at": datetime.now(UTC).isoformat(),
        }
    )
    control = recovery(sales)
    with pytest.raises(Conflict):
        control.change(
            p["id"],
            "initial",
            control.snapshot(p["id"])["state_hash"],
            "operator:test",
            "Should prepare a reply instead",
        )


def test_new_research_withdraws_acceptance_and_supersedes_pending_preparation(sales):
    p = prepared(sales)
    job = sales.ops.enqueue("sales.draft", "old-draft", {"prospect_id": str(p["id"])})
    control = recovery(sales)
    control.change(
        p["id"],
        "research",
        control.snapshot(p["id"])["state_hash"],
        "operator:test",
        "The business services need fresh source evidence",
    )
    assert sales.get(p["id"])["status"] == "needs_research"
    assert sales.get(p["id"])["qualification"] is None
    assert sales.ops.get_job(job)["status"] == "failed"
