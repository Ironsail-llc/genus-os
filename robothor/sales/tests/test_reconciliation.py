"""Missed provider messages are recovered with bounded, restartable reads."""

from unittest.mock import AsyncMock

import pytest

from robothor.sales.reconciliation import ReconciliationWorker
from robothor.sales.tests.test_ingestion import email, event, owned_campaign, receive


def provider():
    p = AsyncMock()
    p.secret.return_value = "workspace-1"
    p.emails.return_value = {"items": [email()], "next_starting_after": None}
    return p


@pytest.mark.asyncio
async def test_missed_reply_is_recovered_and_stops_outreach_with_switches_off(sales):
    p, _ = owned_campaign(sales)
    sales.configure({"research_enabled": False, "sending_enabled": False}, "operator:test")
    api = provider()
    assert await ReconciliationWorker(sales, api).tick()
    assert sales.messages(p["id"])[0]["provider_id"] == "message-1"
    assert sales.overview()["actions"][0]["status"] == "cancelled"
    assert sales.ops.claim("sales.stop") is not None
    assert sales.ops.claim("sales.conversation") is not None
    call = api.emails.await_args.kwargs
    assert call["campaign_id"] == "campaign-1" and call["sort_order"] == "asc"
    assert call["latest_of_thread"] is False
    assert call["max_timestamp_created"]
    assert not await ReconciliationWorker(sales, api).tick()


@pytest.mark.asyncio
async def test_next_page_and_messages_commit_together_and_survive_restart(sales):
    p, _ = owned_campaign(sales)
    api = provider()
    first = email()
    api.emails.return_value = {"items": [first], "next_starting_after": "page-2"}
    assert await ReconciliationWorker(sales, api).tick()
    api.emails.return_value = {
        "items": [email(id="message-2", timestamp_created=first["timestamp_created"])],
        "next_starting_after": None,
    }
    assert await ReconciliationWorker(sales, api).tick()
    assert api.emails.await_args.kwargs["cursor"] == "page-2"
    assert len(sales.messages(p["id"])) == 2
    assert not await ReconciliationWorker(sales, api).tick()


@pytest.mark.asyncio
async def test_webhook_and_poll_do_not_create_duplicate_messages_or_conversation_jobs(sales):
    from robothor.sales.ingestion import InstantlyInboxWorker

    p, _ = owned_campaign(sales)
    record = email()
    api = provider()
    api.get_email.return_value = record
    api.emails.return_value = {"items": [record]}
    receive(sales, event())
    await InstantlyInboxWorker(sales, api).tick()
    version = sales.get(p["id"])["conversation_version"]
    await ReconciliationWorker(sales, api).tick()
    assert len(sales.messages(p["id"])) == 1
    assert sales.get(p["id"])["conversation_version"] == version
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM operation_jobs WHERE tenant_id=%s AND kind='sales.conversation'",
            (sales.tenant,),
        )
        assert cur.fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_unowned_or_conflicting_api_records_do_not_advance_the_cursor(sales):
    p, _ = owned_campaign(sales)
    api = provider()
    api.emails.return_value = {
        "items": [email(campaign_id="other-campaign")],
        "next_starting_after": "page-2",
    }
    assert await ReconciliationWorker(sales, api).tick()
    assert sales.messages(p["id"]) == []
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status,result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.reconcile'",
            (sales.tenant,),
        )
        jobs = cur.fetchall()
        assert len(jobs) == 1 and jobs[0]["status"] == "pending"
        assert jobs[0]["result"] == {"workspace": "workspace-1"}


@pytest.mark.asyncio
async def test_no_owned_campaigns_means_no_provider_calls(sales):
    api = provider()
    assert not await ReconciliationWorker(sales, api).tick()
    api.secret.assert_not_awaited()
    api.emails.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_commit_rolls_back_messages_and_next_page(sales, monkeypatch):
    from robothor.operations.store import Conflict

    p, _ = owned_campaign(sales)
    api = provider()
    api.emails.return_value = {"items": [email()], "next_starting_after": "page-2"}

    def fail(*args, **kwargs):
        raise Conflict("test lost lease")

    monkeypatch.setattr(sales.ops, "complete", fail)
    await ReconciliationWorker(sales, api).tick()
    assert sales.messages(p["id"]) == []
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT payload,status FROM operation_jobs WHERE tenant_id=%s AND kind='sales.reconcile'",
            (sales.tenant,),
        )
        rows = cur.fetchall()
        assert len(rows) == 1 and rows[0]["payload"]["cursor"] is None
        assert rows[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_repeated_cursor_is_held_without_skipping_messages(sales):
    p, _ = owned_campaign(sales)
    api = provider()
    api.emails.return_value = {"items": [email()], "next_starting_after": "page-2"}
    await ReconciliationWorker(sales, api).tick()
    api.emails.return_value = {"items": [email(id="message-2")], "next_starting_after": "page-2"}
    await ReconciliationWorker(sales, api).tick()
    assert len(sales.messages(p["id"])) == 1


@pytest.mark.asyncio
async def test_scan_refuses_out_of_window_message(sales):
    from datetime import UTC, datetime, timedelta

    p, _ = owned_campaign(sales)
    api = provider()
    api.emails.return_value = {
        "items": [email(timestamp_created=(datetime.now(UTC) + timedelta(minutes=1)).isoformat())]
    }
    await ReconciliationWorker(sales, api).tick()
    assert sales.messages(p["id"]) == []


@pytest.mark.asyncio
async def test_reconciliation_has_native_binding_independent_of_sending(sales):
    from robothor.sales.queue import QueueDriver

    sales.configure({"workflow_bindings": {"reconcile": "reconcile-workflow"}}, "operator:test")
    assert await QueueDriver(sales).tick("reconcile", "reconcile-workflow") == {
        "stage": "reconcile",
        "worked": False,
    }


def test_read_recovery_retry_is_human_audited_and_does_not_rearm_delivery(sales):
    from robothor.operations.store import Conflict

    job_id = sales.ops.enqueue("sales.reconcile", "retry-test", {"campaign_id": "campaign-1"})
    sales.retry_provider_read(job_id, "operator:test", "Workspace configuration was corrected")
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT event,actor FROM operation_audit WHERE tenant_id=%s AND entity_id=%s ORDER BY id DESC LIMIT 1",
            (sales.tenant, job_id),
        )
        assert dict(cur.fetchone()) == {"event": "provider.read_retried", "actor": "operator:test"}
    with pytest.raises(Conflict):
        sales.retry_provider_read(job_id, "agent:test", "Please retry")
    delivery = sales.ops.enqueue("sales.promote", "not-read", {})
    with pytest.raises(Conflict):
        sales.retry_provider_read(delivery, "operator:test", "Cannot rearm external writes")
    sales.ops.claim("sales.reconcile")
    with pytest.raises(Conflict):
        sales.retry_provider_read(job_id, "operator:test", "Do not replace an active lease")


@pytest.mark.asyncio
async def test_recurring_scan_has_overlap_and_daily_full_history_backstop(sales):
    from datetime import UTC, datetime, timedelta

    from psycopg2.extras import Json

    _, action = owned_campaign(sales)
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET created_at=now()-interval '10 days' WHERE tenant_id=%s AND id=%s",
            (sales.tenant, action),
        )
    api = provider()
    api.emails.return_value = {"items": []}
    await ReconciliationWorker(sales, api).tick()
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT id,result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.reconcile'",
            (sales.tenant,),
        )
        job = cur.fetchone()
        result = job["result"]
        assert result["full_at"]
        result["through"] = (datetime.now(UTC) - timedelta(minutes=11)).isoformat()
        cur.execute(
            "UPDATE operation_jobs SET result=%s WHERE tenant_id=%s AND id=%s",
            (Json(result), sales.tenant, job["id"]),
        )
    await ReconciliationWorker(sales, api).tick()
    assert datetime.fromisoformat(
        api.emails.await_args.kwargs["min_timestamp_created"]
    ) > datetime.now(UTC) - timedelta(days=2)
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT id,result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.reconcile' ORDER BY created_at DESC LIMIT 1",
            (sales.tenant,),
        )
        job = cur.fetchone()
        result = {
            **job["result"],
            "through": (datetime.now(UTC) - timedelta(minutes=11)).isoformat(),
            "full_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
        }
        cur.execute(
            "UPDATE operation_jobs SET result=%s WHERE tenant_id=%s AND id=%s",
            (Json(result), sales.tenant, job["id"]),
        )
    await ReconciliationWorker(sales, api).tick()
    assert datetime.fromisoformat(
        api.emails.await_args.kwargs["min_timestamp_created"]
    ) < datetime.now(UTC) - timedelta(days=9)


@pytest.mark.asyncio
async def test_recovered_send_updates_the_original_receipt(sales):
    from psycopg2.extras import Json

    _, action = owned_campaign(sales)
    draft = sales.overview()["actions"][0]["payload"]
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET status='completed',receipt=%s WHERE tenant_id=%s AND id=%s",
            (Json({"id": "campaign-1", "delivery_status": "scheduled"}), sales.tenant, action),
        )
    api = provider()
    api.emails.return_value = {
        "items": [
            email(
                ue_type=1,
                from_address_email=draft["sender"],
                to_address_email_list=draft["recipient"],
                subject=draft["subject"],
                body={"text": draft["body"]},
            )
        ]
    }
    await ReconciliationWorker(sales, api).tick()
    receipt = sales.overview()["actions"][0]["receipt"]
    assert receipt["delivery_status"] == "sent" and receipt["id"] == "campaign-1"
    assert receipt["provider_message_id"] == "message-1"


@pytest.mark.asyncio
async def test_recovered_approved_reply_updates_its_own_action_not_initial_campaign(sales):
    from psycopg2.extras import Json

    _, original = owned_campaign(sales)
    original_draft = sales.overview()["actions"][0]["payload"]
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_actions SET status='completed',receipt=%s WHERE tenant_id=%s AND id=%s",
            (Json({"id": "campaign-1", "delivery_status": "sent"}), sales.tenant, original),
        )
    draft = {**original_draft, "subject": "Follow up", "body": "Our approved response"}
    reply = sales.ops.propose("sales.email", "approved-reply", draft)
    sales.ops.decide(reply, True, "operator:test")
    claimed = sales.ops.claim_action(kind="sales.email")
    sales.ops.finish_action(
        reply,
        claimed["lease_token"],
        "completed",
        {"id": "message-2", "delivery_status": "provider_accepted"},
    )
    with sales.ops.transaction() as cur:
        cur.execute(
            "INSERT INTO operation_effects(tenant_id,kind,dedup_key,payload_hash,status,receipt) VALUES(%s,'instantly.reply',%s,'test','completed',%s)",
            (sales.tenant, reply, Json({"id": "message-2"})),
        )
    api = provider()
    api.emails.return_value = {
        "items": [
            email(
                id="message-2",
                ue_type=3,
                from_address_email=draft["sender"],
                to_address_email_list=draft["recipient"],
                subject=draft["subject"],
                body={"text": draft["body"]},
            )
        ]
    }
    await ReconciliationWorker(sales, api).tick()
    with sales.ops.transaction() as cur:
        cur.execute("SELECT id,receipt FROM operation_actions WHERE tenant_id=%s", (sales.tenant,))
        receipts = {str(r["id"]): r["receipt"] for r in cur.fetchall()}
    assert receipts[original] == {"id": "campaign-1", "delivery_status": "sent"}
    assert receipts[reply]["provider_message_id"] == "message-2"
    assert receipts[reply]["delivery_status"] == "sent"


@pytest.mark.asyncio
async def test_outbound_text_changed_outside_genus_is_held_for_human_review(sales):
    p, _ = owned_campaign(sales)
    api = provider()
    api.emails.return_value = {
        "items": [
            email(
                ue_type=1,
                from_address_email="sales@example.com",
                to_address_email_list="alice@example.com",
                body={"text": "Terms changed outside the approved draft"},
            )
        ]
    }
    await ReconciliationWorker(sales, api).tick()
    assert sales.get(p["id"])["owner"] == "human_review"
    assert sales.ops.claim("sales.stop") is not None


@pytest.mark.asyncio
async def test_resumed_cursor_cannot_switch_provider_workspaces(sales):
    owned_campaign(sales)
    api = provider()
    api.emails.return_value = {"items": [email()], "next_starting_after": "page-2"}
    await ReconciliationWorker(sales, api).tick()
    api.secret.return_value = "different-workspace"
    api.emails.reset_mock()
    api.emails.return_value = {"items": []}
    await ReconciliationWorker(sales, api).tick()
    api.emails.assert_not_awaited()
