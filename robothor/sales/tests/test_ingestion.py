"""Authenticated provider events bind to owned campaigns before CRM mutation."""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from robothor.sales.ingestion import InstantlyInboxWorker, receive_instantly, router
from robothor.sales.tests.test_guards import prepared

SECRET = "test-webhook-secret-with-at-least-32-characters"


def event(kind="reply_received", **changes):
    return {
        "event_type": kind,
        "workspace": "workspace-1",
        "campaign_id": "campaign-1",
        "timestamp": datetime.now(UTC).isoformat(),
        "lead_email": "alice@example.com",
        "email_account": "sales@example.com",
        "email_id": "message-1",
        "reply_text": "What are your terms?",
        "reply_subject": "Question",
        **changes,
    }


def owned_campaign(sales):
    from psycopg2.extras import Json

    p = prepared(sales)
    action = sales.draft(
        p["id"],
        {
            "sender": "sales@example.com",
            "recipient": "alice@example.com",
            "subject": "Hello",
            "body": "Access participating pharmacies. TEST POSTAL ADDRESS https://example.com/unsubscribe",
            "claim_ids": ["access"],
            "knowledge_version": "v1",
            "evidence_ids": ["service"],
        },
    )
    with sales.ops.transaction() as cur:
        cur.execute(
            "INSERT INTO operation_effects(tenant_id,kind,dedup_key,payload_hash,status,receipt) VALUES(%s,'instantly.campaign',%s,'test','completed',%s)",
            (sales.tenant, action, Json({"id": "campaign-1"})),
        )
    return p, action


def email(**changes):
    return {
        "id": "message-1",
        "organization_id": "workspace-1",
        "campaign_id": "campaign-1",
        "timestamp_created": datetime.now(UTC).isoformat(),
        "timestamp_email": "2026-09-01T12:00:00Z",
        "eaccount": "sales@example.com",
        "lead": "alice@example.com",
        "ue_type": 2,
        "from_address_email": "alice@example.com",
        "to_address_email_list": "sales@example.com",
        "subject": "Question",
        "body": {"text": "What are your terms?"},
        "thread_id": "thread-1",
        **changes,
    }


def receive(sales, source):
    return receive_instantly(
        sales, json.dumps(source).encode(), "Bearer " + SECRET, SECRET, "workspace-1"
    )


def test_intake_and_stop_are_atomic_idempotent_and_minimized(sales):
    p, action = owned_campaign(sales)
    source = event(extra_private_field="discard")
    assert receive(sales, source)
    assert not receive(sales, source)
    assert sales.overview()["actions"][0]["status"] == "cancelled"
    assert sales.get(p["id"])["conversation_version"] == 1
    with sales.ops.transaction() as cur:
        cur.execute("SELECT payload FROM operation_inbox WHERE tenant_id=%s", (sales.tenant,))
        assert "extra_private_field" not in cur.fetchone()["payload"]
        cur.execute(
            "SELECT count(*) AS n FROM operation_jobs WHERE tenant_id=%s AND kind='sales.inbound'",
            (sales.tenant,),
        )
        assert cur.fetchone()["n"] == 1
    assert sales.ops.claim("sales.stop") is not None


def test_unsubscribe_takes_effect_before_intake_returns(sales):
    p, _ = owned_campaign(sales)
    assert receive(sales, event("lead_unsubscribed"))
    with sales.ops.transaction() as cur:
        cur.execute("SELECT email FROM sales_suppression WHERE tenant_id=%s", (sales.tenant,))
        assert cur.fetchone()["email"] == "alice@example.com"
    assert sales.overview()["actions"][0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_worker_reads_canonical_message_then_commits_inbox_and_followup_together(sales):
    p, _ = owned_campaign(sales)
    receive(sales, event(reply_text="Untrusted webhook text"))
    provider = AsyncMock()
    provider.secret.return_value = "workspace-1"
    provider.get_email.return_value = email()
    assert await InstantlyInboxWorker(sales, provider).tick()
    messages = sales.messages(p["id"])
    assert len(messages) == 1
    assert messages[0]["data"]["body"] == "What are your terms?"
    assert messages[0]["data"]["sender"] == "alice@example.com"
    assert sales.ops.claim("sales.conversation") is not None
    with sales.ops.transaction() as cur:
        cur.execute("SELECT processed_at FROM operation_inbox WHERE tenant_id=%s", (sales.tenant,))
        assert cur.fetchone()["processed_at"] is not None
    assert not await InstantlyInboxWorker(sales, provider).tick()
    provider.get_email.assert_awaited_once_with("message-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"organization_id": "other-workspace"},
        {"campaign_id": "other-campaign"},
        {"from_address_email": "someone@example.com"},
        {"ue_type": 4},
    ],
)
async def test_conflicting_api_identity_never_becomes_a_message(sales, changes):
    p, _ = owned_campaign(sales)
    receive(sales, event())
    provider = AsyncMock()
    provider.secret.return_value = "workspace-1"
    provider.get_email.return_value = email(**changes)
    assert await InstantlyInboxWorker(sales, provider).tick()
    assert sales.messages(p["id"]) == []
    assert sales.ops.claim("sales.conversation") is None


@pytest.mark.asyncio
async def test_missing_email_id_requires_unique_api_match(sales):
    p, _ = owned_campaign(sales)
    receive(sales, event(email_id=None))
    provider = AsyncMock()
    provider.secret.return_value = "workspace-1"
    provider.emails.return_value = {"items": [email()]}
    assert await InstantlyInboxWorker(sales, provider).tick()
    assert sales.messages(p["id"])[0]["provider_id"] == "message-1"
    assert provider.emails.await_args.kwargs["campaign_id"] == "campaign-1"


def test_webhook_route_authenticates_before_storage_and_never_accepts_body_tenant(
    sales, monkeypatch
):
    import robothor.sales.ingestion as ingestion

    p, _ = owned_campaign(sales)

    def secret(key, *, tenant_id):
        if tenant_id != sales.tenant:
            return None
        return {
            "providers/instantly/webhook_secret": SECRET,
            "providers/instantly/workspace_id": "workspace-1",
        }.get(key)

    monkeypatch.setattr(ingestion.vault, "get", secret)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = "/api/integrations/instantly/" + sales.tenant + "/webhook"
    assert client.post(path, json=event()).status_code == 401
    assert sales.overview()["actions"][0]["status"] == "review"
    assert (
        client.post(
            path,
            json=event(tenant_id="other-tenant"),
            headers={"Authorization": "Bearer " + SECRET},
        ).status_code
        == 202
    )
    assert sales.overview()["actions"][0]["status"] == "cancelled"
    assert (
        client.post(path, json=event(), headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert (
        client.post(
            path, content=b"x" * 262145, headers={"Authorization": "Bearer " + SECRET}
        ).status_code
        == 413
    )


@pytest.mark.asyncio
async def test_automatic_reply_is_recorded_without_starting_a_reply_loop(sales):
    p, _ = owned_campaign(sales)
    receive(sales, event("auto_reply_received"))
    provider = AsyncMock()
    provider.secret.return_value = "workspace-1"
    provider.get_email.return_value = email(is_auto_reply=1)
    assert await InstantlyInboxWorker(sales, provider).tick()
    assert sales.messages(p["id"])[0]["data"]["auto_reply"] is True
    assert sales.ops.claim("sales.conversation") is None


@pytest.mark.asyncio
async def test_commit_failure_rolls_back_message_and_keeps_inbox_pending(sales, monkeypatch):
    p, _ = owned_campaign(sales)
    receive(sales, event())
    provider = AsyncMock()
    provider.secret.return_value = "workspace-1"
    provider.get_email.return_value = email()

    def fail(*args, **kwargs):
        from robothor.operations.store import Conflict

        raise Conflict("simulated lease replacement")

    monkeypatch.setattr(sales.ops, "complete", fail)
    assert await InstantlyInboxWorker(sales, provider).tick()
    assert sales.messages(p["id"]) == []
    assert sales.ops.claim("sales.conversation") is None
    with sales.ops.transaction() as cur:
        cur.execute("SELECT processed_at FROM operation_inbox WHERE tenant_id=%s", (sales.tenant,))
        assert cur.fetchone()["processed_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        {"items": [email(), email(id="message-2")]},
        {"items": [email()], "next_starting_after": "another-page"},
        {"items": []},
    ],
)
async def test_missing_id_is_not_guessed_from_ambiguous_or_incomplete_results(sales, page):
    p, _ = owned_campaign(sales)
    receive(sales, event(email_id=None))
    provider = AsyncMock()
    provider.secret.return_value = "workspace-1"
    provider.emails.return_value = page
    assert await InstantlyInboxWorker(sales, provider).tick()
    assert sales.messages(p["id"]) == []


def test_intake_failure_rolls_back_suppression_and_event(sales, monkeypatch):
    _, _ = owned_campaign(sales)

    def fail(*args, **kwargs):
        raise RuntimeError("simulated queue failure")

    monkeypatch.setattr(sales.ops, "enqueue", fail)
    with pytest.raises(RuntimeError):
        receive(sales, event("lead_unsubscribed"))
    with sales.ops.transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM operation_inbox WHERE tenant_id=%s", (sales.tenant,))
        assert cur.fetchone()["n"] == 0
        cur.execute(
            "SELECT count(*) AS n FROM sales_suppression WHERE tenant_id=%s", (sales.tenant,)
        )
        assert cur.fetchone()["n"] == 0
    assert sales.overview()["actions"][0]["status"] == "review"


@pytest.mark.asyncio
async def test_inbox_workflow_runs_while_business_switches_are_off(sales):
    from robothor.sales.queue import QueueDriver

    sales.configure({"workflow_bindings": {"inbox": "inbox-workflow"}}, "operator:test")
    assert await QueueDriver(sales).tick("inbox", "inbox-workflow") == {
        "stage": "inbox",
        "worked": False,
    }


@pytest.mark.asyncio
async def test_sent_event_records_sent_not_delivered_and_preserves_campaign_receipt(sales):
    from psycopg2.extras import Json

    p, action = owned_campaign(sales)
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET status='completed',receipt=%s WHERE tenant_id=%s AND id=%s",
            (
                Json(
                    {
                        "id": "campaign-1",
                        "campaign_id": "campaign-1",
                        "delivery_status": "scheduled",
                    }
                ),
                sales.tenant,
                action,
            ),
        )
    receive(sales, event("email_sent"))
    provider = AsyncMock()
    provider.secret.return_value = "workspace-1"
    draft = sales.overview()["actions"][0]["payload"]
    provider.get_email.return_value = email(
        ue_type=1,
        from_address_email="sales@example.com",
        to_address_email_list="alice@example.com",
        subject=draft["subject"],
        body={"text": draft["body"]},
    )
    assert await InstantlyInboxWorker(sales, provider).tick()
    receipt = sales.overview()["actions"][0]["receipt"]
    assert receipt == {
        "id": "campaign-1",
        "campaign_id": "campaign-1",
        "delivery_status": "sent",
        "provider_message_id": "message-1",
    }
    assert sales.messages(p["id"])[0]["direction"] == "outbound"


def test_account_error_revokes_only_affected_mailbox_readiness(sales):
    from datetime import timedelta

    p, _ = owned_campaign(sales)
    until = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    sales.configure(
        {"mailbox_approved_until": {"sales@example.com": until, "other@example.com": until}},
        "operator:test",
    )
    receive(sales, event("account_error", campaign_id=None, lead_email=None))
    assert "sales@example.com" not in sales.settings()["mailbox_approved_until"]
    assert "other@example.com" in sales.settings()["mailbox_approved_until"]
    assert sales.overview()["actions"][0]["status"] == "cancelled"
    job = sales.ops.claim("sales.stop")
    assert job["payload"] == {"sender": "sales@example.com"}


def test_unknown_campaign_does_not_bind_by_email_or_change_known_prospect(sales):
    p, _ = owned_campaign(sales)
    receive(sales, event(campaign_id="unowned-campaign"))
    assert sales.get(p["id"])["conversation_version"] == 0
    assert sales.overview()["actions"][0]["status"] == "review"
    assert sales.ops.claim("sales.stop") is None


def test_mailbox_stop_does_not_pause_campaigns_on_other_mailboxes(sales):
    from robothor.sales.delivery import StopWorker

    owned_campaign(sales)
    assert StopWorker(sales)._campaigns({"sender": "other@example.com"}) == []
    assert StopWorker(sales)._campaigns({"sender": "sales@example.com"}) == [{"id": "campaign-1"}]


def test_owned_single_recipient_campaign_stops_when_optional_addresses_are_absent(sales):
    p, _ = owned_campaign(sales)
    receive(sales, event(lead_email=None, email_account=None))
    assert sales.get(p["id"])["conversation_version"] == 1
    assert sales.overview()["actions"][0]["status"] == "cancelled"


@pytest.mark.parametrize(
    "changes", [{"lead_email": {"bad": "object"}}, {"email_account": ["sales@example.com"]}]
)
def test_malformed_addresses_return_validation_error(changes):
    from robothor.sales.inbound import instantly_event

    with pytest.raises(ValueError):
        instantly_event(
            json.dumps(event(**changes)).encode(), "Bearer " + SECRET, SECRET, "workspace-1"
        )
