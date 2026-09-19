"""Native structured stages retain review and identity boundaries."""

from datetime import UTC, datetime

import pytest

from robothor.operations.store import Conflict
from robothor.sales.stages import ActivationWorker, ContactWorker, ConversationWorker, ScoutWorker
from robothor.sales.tests.test_guards import prepared
from robothor.sales.tests.test_runtime import RunnerStub
from robothor.sales.tests.test_service import researched


def configure(sales, stage):
    sales.configure(
        {
            "agents": {stage: stage + "-agent"},
            "monthly_limit_units": 50_000_000,
            "daily_limit_units": 10_000_000,
            "enrichment_enabled": True,
        },
        "operator:test",
    )


@pytest.mark.asyncio
async def test_scout_batch_is_deduplicated_and_completes_with_research_jobs(sales):
    configure(sales, "scout")
    job = sales.ops.enqueue(
        "sales.scout", "segment-1", {"segment": "provider practices in one region"}
    )
    output = {
        "companies": [
            {
                "name": "Example Clinic",
                "website": "https://clinic.example.com",
                "source_url": "https://directory.example.com",
                "reason": "Public prescribing services",
            }
        ]
    }
    worker = ScoutWorker(sales, RunnerStub(output))
    assert await worker.tick()
    assert sales.ops.get_job(job)["status"] == "completed"
    assert len(sales.overview()["prospects"]) == 1
    assert sales.ops.claim("sales.research") is not None


def test_discovery_stops_at_daily_admission_and_review_backlog_limits(sales):
    sales.configure({"discovery_daily_limit": 1, "review_backlog_limit": 2}, "operator:test")
    first = sales.discover("One", "https://one.example.com", "https://directory.example.com")
    assert (
        sales.discover("Duplicate", "https://one.example.com", "https://directory.example.com")[
            "id"
        ]
        == first["id"]
    )
    with pytest.raises(Conflict, match="daily"):
        sales.discover("Two", "https://two.example.com", "https://directory.example.com")
    sales.configure({"discovery_daily_limit": 20, "review_backlog_limit": 1}, "operator:test")
    with pytest.raises(Conflict, match="backlog"):
        sales.discover("Two", "https://two.example.com", "https://directory.example.com")


@pytest.mark.asyncio
async def test_empty_discovery_reason_survives_job_completion(sales):
    configure(sales, "scout")
    job = sales.ops.enqueue("sales.scout", "empty-explained", {"segment": "provider practices"})
    output = {
        "companies": [],
        "empty_reason": "Only out-of-region directory listings were available.",
    }
    assert await ScoutWorker(sales, RunnerStub(output)).tick()
    completed = sales.ops.get_job(job)
    assert completed["status"] == "completed"
    assert completed["result"]["empty_reason"] == output["empty_reason"]
    assert completed["result"]["prospect_ids"] == []


@pytest.mark.asyncio
async def test_contact_research_cannot_assert_email_verification(sales):
    p = researched(sales)
    configure(sales, "contacts")
    output = {
        "contacts": [
            {
                "name": "Alice",
                "role": "Owner",
                "email": "alice@example.com",
                "source_url": "https://clinic.example.com/team",
                "verification": "valid",
                "verified_at": datetime.now(UTC).isoformat(),
            }
        ]
    }
    assert await ContactWorker(sales, RunnerStub(output)).tick()
    assert sales.contacts(p["id"]) == []
    output["contacts"][0].update(verification="unknown", verified_at=None)
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND kind='sales.contacts'",
            (sales.tenant,),
        )
    assert await ContactWorker(sales, RunnerStub(output)).tick()
    assert sales.contacts(p["id"])[0]["data"]["verification"] == "unknown"
    assert sales.ops.claim("sales.verify") is not None


def inbound(sales, prospect):
    sales.record_message(
        {
            "prospect_id": prospect["id"],
            "provider_id": "inbound-1",
            "direction": "inbound",
            "occurred_at": datetime.now(UTC).isoformat(),
            "sender": "alice@example.com",
            "recipient": "sales@example.com",
            "subject": "Re: workflow",
            "body": "Please stop emailing me.",
        }
    )


@pytest.mark.asyncio
async def test_opt_out_suppresses_without_requiring_a_send_approval(sales):
    p = prepared(sales)
    configure(sales, "conversation")
    inbound(sales, p)
    worker = ConversationWorker(
        sales,
        RunnerStub({"classification": "opt_out", "reason": "Explicit request", "draft": None}),
    )
    assert await worker.tick()
    with sales.ops.transaction() as cur:
        assert sales._suppressed("alice@example.com", cur)
    assert sales.ops.claim("sales.suppress") is not None
    assert sales.overview()["actions"] == []


@pytest.mark.asyncio
async def test_custom_terms_escalate_without_autonomous_reply(sales):
    p = prepared(sales)
    configure(sales, "conversation")
    inbound(sales, p)
    worker = ConversationWorker(
        sales,
        RunnerStub({"classification": "human_required", "reason": "Custom terms", "draft": None}),
    )
    assert await worker.tick()
    assert sales.get(p["id"])["owner"] == "human_review"
    assert sales.overview()["actions"] == []


@pytest.mark.asyncio
async def test_inbound_question_produces_only_a_thread_bound_review_draft(sales):
    p = prepared(sales)
    configure(sales, "conversation")
    sales.record_message(
        {
            "prospect_id": p["id"],
            "provider_id": "question-1",
            "direction": "inbound",
            "occurred_at": datetime.now(UTC).isoformat(),
            "sender": "alice@example.com",
            "recipient": "sales@example.com",
            "subject": "Question",
            "body": "How does ordering work?",
        }
    )
    output = {
        "classification": "question",
        "reason": "Asking about ordering",
        "draft": {
            "recipient": "alice@example.com",
            "sender": "sales@example.com",
            "subject": "Re: Question",
            "body": "Access participating pharmacies. TEST POSTAL ADDRESS https://example.com/unsubscribe",
            "claim_ids": ["access"],
            "knowledge_version": "v1",
            "evidence_ids": ["service"],
            "purpose": "reply",
            "reply_to_uuid": "question-1",
        },
    }
    assert await ConversationWorker(sales, RunnerStub(output)).tick()
    actions = sales.overview()["actions"]
    assert len(actions) == 1
    assert actions[0]["status"] == "review"
    assert actions[0]["payload"]["reply_to_uuid"] == "question-1"
    assert sales.ops.claim_action(kind="sales.email") is None


@pytest.mark.asyncio
async def test_activation_uses_fulfillment_and_never_converts_placement_into_delivery(sales):
    p = prepared(sales)
    configure(sales, "activation")
    sales.configure({"outcomes_enabled": True}, "operator:test")
    sales.bind_customer(p["id"], "customer-1", "operator:test")
    event = {
        "external_company_id": "customer-1",
        "event_id": "placed-1",
        "kind": "first_order_placed",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    sales.record_outcome(event)
    worker = ActivationWorker(
        sales,
        RunnerStub(
            {"next_step": "Await verified fulfillment", "human_required": False, "draft": None}
        ),
    )
    assert await worker.tick()
    assert sales.get(p["id"])["status"] == "onboarding"
    assert sales.retention(p["id"])["completed_orders"] == 0
    sales.record_outcome(
        event
        | {"kind": "order_completed", "event_id": "fulfilled-1", "order_ref": "opaque-order-1"}
    )
    assert await worker.tick()
    assert sales.get(p["id"])["status"] == "active"
    assert sales.retention(p["id"])["completed_orders"] == 1


@pytest.mark.asyncio
async def test_question_without_supported_response_is_not_silently_dropped(sales):
    p = prepared(sales)
    configure(sales, "conversation")
    inbound(sales, p)
    assert await ConversationWorker(
        sales,
        RunnerStub(
            {
                "classification": "question",
                "reason": "No approved claim answers this",
                "draft": None,
            }
        ),
    ).tick()
    assert sales.get(p["id"])["owner"] == "human_review"
