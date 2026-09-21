"""Isolated component integration rehearsal, NOT a live sales pilot.

PostgreSQL and the production workers are real. LLM answers, provider HTTP
effects, operator decisions and business milestones are synthetic fixtures.
"""

from datetime import UTC, datetime, timedelta

import pytest

from robothor.sales.delivery import DeliveryWorker, StopWorker
from robothor.sales.models import QualificationPolicy
from robothor.sales.promotion import PromotionWorker
from robothor.sales.runtime import DraftWorker, ResearchWorker
from robothor.sales.stages import ActivationWorker, ContactWorker, ConversationWorker, ScoutWorker
from robothor.sales.tests.test_contact_sources import ContactRunnerStub
from robothor.sales.tests.test_delivery import MailProvider
from robothor.sales.tests.test_promotion import PipedriveStub
from robothor.sales.tests.test_runtime import RunnerStub
from robothor.sales.tests.test_verification import Verifier
from robothor.sales.verification import VerificationWorker


class CRM(PipedriveStub):
    async def search_people(self, email):
        return {"items": []}

    async def create_person(self, name, email, organization_id):
        self.created.append("person")
        return {"id": 22}


class Mail(MailProvider):
    async def reply(self, draft):
        self.calls.append("reply")
        return {"id": "sent-reply-1"}


@pytest.mark.asyncio
async def test_discovery_to_reviewed_outreach_reply_optout_and_fulfillment(sales):
    now = datetime.now(UTC)
    sales.publish_policy(
        QualificationPolicy(
            version="1",
            buying_case="network_access",
            required=["prescribing"],
            weights={"prescribing": 100},
            threshold=80,
        ),
        "operator:synthetic-reviewer",
    )
    sales.publish_knowledge(
        "1",
        {"claims": {"access": "Access the partner network."}},
        "operator:synthetic-reviewer",
    )
    sales.configure(
        {
            "research_enabled": True,
            "enrichment_enabled": True,
            "promotion_enabled": True,
            "sending_enabled": True,
            "outcomes_enabled": True,
            "agents": {
                stage: stage + "-agent"
                for stage in (
                    "scout",
                    "research",
                    "contacts",
                    "draft",
                    "conversation",
                    "activation",
                )
            },
            "monthly_limit_units": 500_000_000,
            "daily_limit_units": 20_000_000,
            "verification_allowance_units": 10000,
            "senders": ["sales@example.com"],
            "mailbox_approved_until": {"sales@example.com": (now + timedelta(days=2)).isoformat()},
            "postal_address": "TEST POSTAL ADDRESS",
            "unsubscribe_url": "https://example.com/unsubscribe",
            "active_policy_versions": {"network_access": "1"},
            "active_knowledge_version": "1",
        },
        "operator:synthetic-reviewer",
    )
    sales.ops.enqueue(
        "sales.scout", "synthetic-segment", {"segment": "Synthetic provider practices"}
    )
    await ScoutWorker(
        sales,
        RunnerStub(
            {
                "companies": [
                    {
                        "name": "Example Clinic",
                        "website": "https://clinic.example.com",
                        "source_url": "https://directory.example.com",
                        "reason": "Prescribing services",
                    }
                ]
            }
        ),
    ).tick()
    p = sales.overview()["prospects"][0]
    researcher = ResearchWorker(
        sales,
        RunnerStub(
            {
                "buying_case": "network_access",
                "criteria": {"prescribing": ["services"]},
                "evidence": [
                    {
                        "id": "services",
                        "field": "prescribing",
                        "value": True,
                        "url": "https://clinic.example.com/services",
                        "excerpt": "Our providers offer prescription management.",
                        "retrieved_at": now.isoformat(),
                    }
                ],
            }
        ),
    )
    assert await researcher.tick()
    assert await researcher.qualify_tick()
    assert sales.get(p["id"])["qualification"]["score"] == 100
    assert await ContactWorker(
        sales,
        ContactRunnerStub(
            {
                "contacts": [
                    {
                        "name": "Alice",
                        "role": "Owner",
                        "email": "alice@example.com",
                        "source_url": "https://clinic.example.com/team",
                        "verification": "unknown",
                        "verified_at": None,
                    }
                ]
            }
        ),
    ).tick()
    assert await VerificationWorker(
        sales,
        Verifier(
            {"email": "alice@example.com", "verification_status": "verified", "catch_all": False}
        ),
    ).tick()
    crm = CRM()
    promotion = PromotionWorker(sales, crm)
    assert await promotion.tick() is False  # qualified is not human-accepted
    sales.accept(
        p["id"],
        True,
        "operator:synthetic-reviewer",
        expected_version=1,
        expected_policy_version="1",
    )
    assert await promotion.tick()
    assert crm.created == ["organization", "person", "lead"]
    assert await promotion.tick() is False
    draft = {
        "sender": "sales@example.com",
        "recipient": "alice@example.com",
        "subject": "Workflow question",
        "body": "Access the partner network. TEST POSTAL ADDRESS https://example.com/unsubscribe",
        "claim_ids": ["access"],
        "knowledge_version": "1",
        "evidence_ids": ["services"],
    }
    assert await DraftWorker(sales, RunnerStub(draft)).tick()
    mail = Mail()
    delivery = DeliveryWorker(sales, mail, clock=lambda: datetime(2026, 9, 18, 15, tzinfo=UTC))
    assert await delivery.tick() is False
    initial = sales.overview()["actions"][0]
    sales.ops.decide(initial["id"], True, "operator:synthetic-reviewer")
    assert await delivery.tick()
    assert mail.calls == ["create", "lead", "activate"]
    assert sales.messages(p["id"]) == []
    sent = {
        "prospect_id": p["id"],
        "provider_id": "sent-1",
        "direction": "outbound",
        "occurred_at": now.isoformat(),
        "sender": draft["sender"],
        "recipient": draft["recipient"],
        "subject": draft["subject"],
        "body": draft["body"],
        "campaign_id": "campaign-1",
    }
    sales.record_message(sent)
    sales.record_message(sent)
    question = sent | {
        "provider_id": "reply-1",
        "direction": "inbound",
        "sender": draft["recipient"],
        "recipient": draft["sender"],
        "body": "How does ordering work?",
    }
    sales.record_message(question)
    response = draft | {
        "purpose": "reply",
        "reply_to_uuid": "reply-1",
        "subject": "Re: Workflow question",
    }
    assert await ConversationWorker(
        sales,
        RunnerStub(
            {"classification": "question", "reason": "Ordering question", "draft": response}
        ),
    ).tick()
    review = next(a for a in sales.overview()["actions"] if a["status"] == "review")
    assert await delivery.tick() is False
    sales.ops.decide(review["id"], True, "operator:synthetic-reviewer")
    assert await delivery.tick()
    assert mail.calls[-1] == "reply"
    sales.record_message(sent | {"provider_id": "sent-reply-1", "body": response["body"]})
    sales.record_message(question | {"provider_id": "optout-1", "body": "Please stop emailing me."})
    assert await ConversationWorker(
        sales,
        RunnerStub({"classification": "opt_out", "reason": "Explicit opt-out", "draft": None}),
    ).tick()
    assert await StopWorker(sales, mail).tick()
    assert mail.calls[-2:] == ["suppress", "pause"]
    assert await delivery.tick() is False
    sales.bind_customer(p["id"], "synthetic-customer-1", "operator:synthetic-reviewer")
    event = {
        "external_company_id": "synthetic-customer-1",
        "event_id": "placement-1",
        "kind": "first_order_placed",
        "occurred_at": now.isoformat(),
    }
    sales.record_outcome(event)
    activation = ActivationWorker(
        sales,
        RunnerStub(
            {"next_step": "Observe verified fulfillment", "human_required": False, "draft": None}
        ),
    )
    assert await activation.tick()
    assert sales.retention(p["id"])["completed_orders"] == 0
    sales.record_outcome(
        event
        | {"event_id": "fulfillment-1", "kind": "order_completed", "order_ref": "opaque-order-1"}
    )
    assert await activation.tick()
    assert sales.get(p["id"])["status"] == "active"
    assert sales.retention(p["id"])["completed_orders"] == 1
    assert sales.retention(p["id"])["active_days_61_90"] is None
