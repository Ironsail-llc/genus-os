"""Approval is bound to current evidence, claims, conversation, and delivery rules."""

from datetime import UTC, datetime

import pytest

from robothor.operations.store import Conflict
from robothor.sales.tests.test_service import researched


def prepared(sales):
    p = researched(sales)
    sales.accept(p["id"], True, "operator:test", expected_version=1, expected_policy_version="1")
    sales.add_contact(
        p["id"],
        {
            "name": "Alice",
            "role": "Owner",
            "email": "alice@example.com",
            "source_url": "https://clinic.example.com/team",
            "verification": "valid",
            "verified_at": datetime.now(UTC).isoformat(),
        },
    )
    sales.publish_knowledge(
        "v1", {"claims": {"access": "Access the partner network."}}, "operator:test"
    )
    sales.configure(
        {
            "sending_enabled": True,
            "senders": ["sales@example.com"],
            "postal_address": "TEST POSTAL ADDRESS",
            "unsubscribe_url": "https://example.com/unsubscribe",
            "active_knowledge_version": "v1",
            "active_policy_versions": {"network_access": "1"},
            "mailbox_daily_limit": 5,
        },
        "operator:test",
    )
    return p


def draft(sales, p):
    return sales.draft(
        p["id"],
        {
            "recipient": "alice@example.com",
            "sender": "sales@example.com",
            "subject": "Pharmacy workflows",
            "body": "Access the partner network.\nTEST POSTAL ADDRESS\nhttps://example.com/unsubscribe",
            "claim_ids": ["access"],
            "knowledge_version": "v1",
            "evidence_ids": ["service"],
        },
    )


def approved(sales, p):
    action = draft(sales, p)
    sales.ops.decide(action, True, "operator:test")
    return sales.ops.claim_action(kind="sales.email")


def test_configuration_rejects_truthy_strings_and_invalid_limits(sales):
    with pytest.raises(ValueError):
        sales.configure({"sending_enabled": "false"}, "operator:test")
    with pytest.raises(ValueError):
        sales.configure({"mailbox_daily_limit": -1}, "operator:test")


def test_preflight_checks_pause_takeover_suppression_and_current_knowledge(sales):
    p = prepared(sales)
    action = approved(sales, p)
    sales.validate_send(action)
    sales.configure({"sending_enabled": False}, "operator:test")
    with pytest.raises(Conflict, match="paused"):
        sales.validate_send(action)
    sales.publish_knowledge("v2", {"claims": {"access": "Updated positioning"}}, "operator:test")
    sales.configure({"sending_enabled": True, "active_knowledge_version": "v2"}, "operator:test")
    with pytest.raises(Conflict, match="knowledge"):
        sales.validate_send(action)
    sales.configure({"active_knowledge_version": "v1"}, "operator:test")
    action = approved(sales, p)
    sales.takeover(p["id"], "operator:test")
    with pytest.raises(Conflict, match="ownership"):
        sales.validate_send(action)


def test_new_reply_invalidates_an_already_claimed_send(sales):
    p = prepared(sales)
    action = approved(sales, p)
    event = {
        "provider_id": "reply-1",
        "prospect_id": p["id"],
        "direction": "inbound",
        "occurred_at": datetime.now(UTC).isoformat(),
        "sender": "alice@example.com",
        "recipient": "sales@example.com",
        "subject": "Question",
        "body": "Can you explain your terms?",
    }
    sales.record_message(event)
    sales.record_message(event)
    with pytest.raises(Conflict, match="conversation"):
        sales.validate_send(action)
    assert len(sales.messages(p["id"])) == 1


def test_research_history_and_new_evidence_invalidate_old_approval(sales):
    from robothor.sales.models import Dossier

    p = prepared(sales)
    action = approved(sales, p)
    old = sales.get(p["id"])
    sales.research(p["id"], Dossier.model_validate(old["dossier"]), expected_version=old["version"])
    with pytest.raises(Conflict):
        sales.validate_send(action)
    assert len(sales.history(p["id"])) == 2


def test_order_identity_dedup_and_conflicting_events(sales):
    p = prepared(sales)
    sales.bind_customer(p["id"], "customer-1", "operator:test")
    event = {
        "external_company_id": "customer-1",
        "event_id": "event-1",
        "kind": "order_completed",
        "order_ref": "order-1",
        "occurred_at": datetime.now(UTC).isoformat(),
    }
    sales.record_outcome(event)
    sales.record_outcome(event | {"event_id": "event-2"})
    assert sales.retention(p["id"])["completed_orders"] == 1
    with pytest.raises(Conflict):
        sales.record_outcome(event | {"kind": "order_reversed"})


def test_prospect_acceptance_is_bound_to_the_reviewed_dossier(sales):
    from robothor.sales.models import Dossier

    p = researched(sales)
    old = sales.get(p["id"])
    sales.research(p["id"], Dossier.model_validate(old["dossier"]), expected_version=old["version"])
    sales.qualify(p["id"], "1")
    with pytest.raises(Conflict, match="reviewed dossier"):
        sales.accept(
            p["id"],
            True,
            "operator:test",
            expected_version=old["version"],
            expected_policy_version="1",
        )
    assert sales.ops.claim("sales.promote") is None


def test_new_customer_milestone_invalidates_an_already_claimed_send(sales):
    p = prepared(sales)
    action = approved(sales, p)
    sales.bind_customer(p["id"], "customer-1", "operator:test")
    sales.record_outcome(
        {
            "external_company_id": "customer-1",
            "event_id": "signup-1",
            "kind": "signup",
            "occurred_at": datetime.now(UTC).isoformat(),
        }
    )
    with pytest.raises(Conflict, match="milestone"):
        sales.validate_send(action)
