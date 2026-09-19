"""Owned Gmail threads feed durable conversations without opening send authority."""

import copy
from datetime import UTC, datetime

import pytest

from robothor.sales.tests.test_gmail import message
from robothor.sales.tests.test_gmail_delivery import action, gmail_setup, sends
from robothor.sales.tests.test_guards import draft


async def sent(sales, monkeypatch):
    p, cli, delivery = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    await delivery.tick()
    original = action(sales, key)
    data = original["payload"]
    record = message(
        message_id=delivery.provider.message_id(key, data),
        sender=data["sender"],
        recipient=data["recipient"],
        subject=data["subject"],
        body=data["body"] + "\n",
        labels=["SENT"],
    )
    record["internalDate"] = str(int(datetime.now(UTC).timestamp() * 1000))
    return p, cli, delivery.provider, original, record


def incoming(*, body="Can you explain the pricing?", raw_id="reply123", **changes):
    record = message(body=body, **changes)
    record["id"] = raw_id
    record["internalDate"] = str(int(datetime.now(UTC).timestamp() * 1000) + 1)
    return record


class ThreadProvider:
    def __init__(self, gmail, records):
        self.gmail = gmail
        self.mailbox = gmail.mailbox
        self.records = records
        self.reads = 0

    def __getattr__(self, name):
        return getattr(self.gmail, name)

    async def get_thread(self, thread_id):
        self.reads += 1
        assert thread_id == self.gmail.provider_id("def456")
        return {"id": "def456", "historyId": "42", "messages": copy.deepcopy(self.records)}


@pytest.mark.asyncio
async def test_owned_thread_replay_is_idempotent_and_new_reply_cancels_pending_approval(
    sales, monkeypatch
):
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, cli, gmail, original, outbound = await sent(sales, monkeypatch)
    provider = ThreadProvider(gmail, [outbound])
    worker = GmailThreadWorker(sales, provider)
    assert await worker.tick()
    assert sales.messages(p["id"])[0]["data"]["body"] == original["payload"]["body"]
    assert action(sales, original["id"])["receipt"]["delivery_status"] == "sent_copy_verified"
    pending = draft(sales, sales.get(p["id"]))
    sales.ops.decide(pending, True, "operator:test")
    provider.records.append(incoming())
    await worker.sync_prospect(p["id"])
    assert action(sales, pending)["status"] == "cancelled"
    assert len(sales.messages(p["id"])) == 2
    version = sales.get(p["id"])["conversation_version"]
    await GmailThreadWorker(sales, provider).sync_prospect(p["id"])
    assert sales.get(p["id"])["conversation_version"] == version
    assert len(sales.messages(p["id"])) == 2
    assert sends(cli) == 1
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM operation_jobs WHERE tenant_id=%s AND kind='sales.conversation'",
            (sales.tenant,),
        )
        assert cur.fetchone()["n"] == 1


@pytest.mark.asyncio
async def test_unsubscribe_is_applied_even_with_all_outbound_switches_off(sales, monkeypatch):
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, cli, gmail, _, outbound = await sent(sales, monkeypatch)
    sales.configure({"research_enabled": False, "sending_enabled": False}, "operator:test")
    provider = ThreadProvider(gmail, [outbound, incoming(body="Please remove me from your list.")])
    assert await GmailThreadWorker(sales, provider).tick()
    with sales.ops.transaction() as cur:
        assert sales._suppressed("alice@example.com", cur)
    assert sends(cli) == 1


@pytest.mark.asyncio
async def test_auto_reply_blocks_stale_approval_without_starting_agent_conversation(
    sales, monkeypatch
):
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, _, gmail, _, outbound = await sent(sales, monkeypatch)
    reply = incoming(body="I am away until Monday.")
    reply["payload"]["headers"].append({"name": "Auto-Submitted", "value": "auto-replied"})
    await GmailThreadWorker(sales, ThreadProvider(gmail, [outbound, reply])).tick()
    assert sales.messages(p["id"])[1]["data"]["auto_reply"] is True
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM operation_jobs WHERE tenant_id=%s AND kind='sales.conversation'",
            (sales.tenant,),
        )
        assert cur.fetchone()["n"] == 0


@pytest.mark.asyncio
async def test_manual_outbound_reply_escalates_ownership(sales, monkeypatch):
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, _, gmail, _, outbound = await sent(sales, monkeypatch)
    manual = incoming(
        body="I will handle this personally.",
        raw_id="manual123",
        sender="sales@example.com",
        recipient="alice@example.com",
        labels=["SENT"],
    )
    await GmailThreadWorker(sales, ThreadProvider(gmail, [outbound, manual])).tick()
    assert sales.get(p["id"])["owner"] == "human_review"
    assert len(sales.messages(p["id"])) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad", ["participants", "missing_anchor", "too_many", "duplicate", "changed_content"]
)
async def test_ambiguous_or_incomplete_thread_never_advances_checkpoint(sales, monkeypatch, bad):
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, _, gmail, _, outbound = await sent(sales, monkeypatch)
    records = [outbound, incoming()]
    if bad == "participants":
        records[1] = incoming(sender="unrelated@example.com")
    elif bad == "missing_anchor":
        records = [incoming()]
    elif bad == "too_many":
        records = [outbound] + [incoming(raw_id=f"reply{x}") for x in range(100)]
    elif bad == "duplicate":
        records = [outbound, outbound]
    else:
        records[0]["payload"]["body"] = {"data": "Y2hhbmdlZA=="}
    assert await GmailThreadWorker(sales, ThreadProvider(gmail, records)).tick()
    assert sales.messages(p["id"]) == []
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status,error,result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.gmail_sync'",
            (sales.tenant,),
        )
        job = cur.fetchone()
        assert job["status"] == "pending" and job["error"]
        assert job["result"] is None


@pytest.mark.asyncio
async def test_expired_sync_lease_cannot_commit_and_restarted_worker_can_recover(
    sales, monkeypatch
):
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, _, gmail, _, outbound = await sent(sales, monkeypatch)
    provider = ThreadProvider(gmail, [outbound])
    worker = GmailThreadWorker(sales, provider)
    worker.plan()
    abandoned = sales.ops.claim("sales.gmail_sync")
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET lease_until=now()-interval '1 second' WHERE tenant_id=%s AND id=%s",
            (sales.tenant, abandoned["id"]),
        )
    assert await GmailThreadWorker(sales, provider).tick()
    assert len(sales.messages(p["id"])) == 1
    assert sales.ops.get_job(abandoned["id"])["attempts"] == 2


@pytest.mark.asyncio
async def test_final_send_check_ingests_new_reply_and_never_posts_stale_approved_reply(
    sales, monkeypatch
):
    from robothor.sales.gmail_delivery import GmailDeliveryWorker
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, cli, gmail, _, outbound = await sent(sales, monkeypatch)
    first_reply = incoming()
    provider = ThreadProvider(gmail, [outbound, first_reply])
    await GmailThreadWorker(sales, provider).tick()
    parent = gmail.provider_id(first_reply["id"])
    cli.messages[first_reply["id"]] = first_reply
    key = sales.draft(
        p["id"],
        {
            "sender": "sales@example.com",
            "recipient": "alice@example.com",
            "subject": "Pharmacy workflows",
            "body": "Access participating pharmacies.\nTEST POSTAL ADDRESS\nhttps://example.com/unsubscribe",
            "claim_ids": ["access"],
            "knowledge_version": "v1",
            "evidence_ids": ["service"],
            "purpose": "reply",
            "reply_to_uuid": parent,
        },
    )
    sales.ops.decide(key, True, "operator:test")
    get_thread = provider.get_thread

    async def reply_during_final_check(thread_id):
        if provider.reads == 2:
            provider.records.append(incoming(body="Please unsubscribe me.", raw_id="reply456"))
        return await get_thread(thread_id)

    provider.get_thread = reply_during_final_check
    worker = GmailDeliveryWorker(
        sales, provider, clock=lambda: datetime(2026, 9, 18, 15, tzinfo=UTC)
    )
    assert await worker.tick()
    assert sends(cli) == 1
    assert action(sales, key)["status"] == "unknown"
    with sales.ops.transaction() as cur:
        assert sales._suppressed("alice@example.com", cur)


@pytest.mark.asyncio
async def test_sync_read_repair_is_scoped_and_does_not_repeat_a_send(sales, monkeypatch):
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, cli, gmail, _, outbound = await sent(sales, monkeypatch)
    provider = ThreadProvider(gmail, [outbound, incoming(sender="unrelated@example.com")])
    worker = GmailThreadWorker(sales, provider)
    await worker.tick()
    reads = sales.provider_reads(kind="sales.gmail_sync")
    assert len(reads["items"]) == 1
    job = reads["items"][0]
    assert job["scope"]["thread_id"] == gmail.provider_id("def456")
    provider.records = [outbound, incoming()]
    sales.retry_provider_read(job["id"], "operator:test", "Repaired the message identity mapping")
    assert await worker.tick()
    assert sales.ops.get_job(job["id"])["status"] == "completed"
    assert len(sales.messages(p["id"])) == 2 and sends(cli) == 1


@pytest.mark.asyncio
async def test_native_inbox_pump_routes_gmail_without_instantly(sales, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from robothor.sales import queue

    sales.configure(
        {"email_provider": "gmail", "workflow_bindings": {"inbox": "gmail-inbox"}}, "operator:test"
    )
    tick = AsyncMock(return_value=True)
    monkeypatch.setattr(queue, "GmailThreadWorker", lambda sales: SimpleNamespace(tick=tick))
    monkeypatch.setattr(
        queue, "InstantlyInboxWorker", lambda sales: pytest.fail("Instantly must not run")
    )
    assert (await queue.QueueDriver(sales).tick("inbox", "gmail-inbox"))["worked"]
    tick.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_worker_cannot_commit_observations_or_checkpoint(sales, monkeypatch):
    from robothor.operations.store import Conflict
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, _, gmail, _, outbound = await sent(sales, monkeypatch)
    worker = GmailThreadWorker(sales, ThreadProvider(gmail, [outbound]))
    worker.plan()
    job = sales.ops.claim("sales.gmail_sync")
    root, events = await worker.inspect(job["payload"]["action_id"])
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET lease_until=now()-interval '1 second' WHERE tenant_id=%s AND id=%s",
            (sales.tenant, job["id"]),
        )
    with pytest.raises(Conflict, match="lease"):
        worker.commit(str(root["id"]), events, job=job)
    assert sales.messages(p["id"]) == []
    assert sales.ops.get_job(job["id"])["result"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body,subject,expected",
    [
        ("Please remove me.", "Pharmacy workflows", True),
        ("", "Unsubscribe", True),
        ("Tell me more.\n> Reply unsubscribe to stop these emails.", "Pharmacy workflows", False),
    ],
)
async def test_optout_matches_new_reply_and_subject_not_quoted_footer(
    sales, monkeypatch, body, subject, expected
):
    from robothor.sales.gmail_sync import GmailThreadWorker

    _, _, gmail, _, outbound = await sent(sales, monkeypatch)
    await GmailThreadWorker(
        sales, ThreadProvider(gmail, [outbound, incoming(body=body, subject=subject)])
    ).tick()
    with sales.ops.transaction() as cur:
        assert sales._suppressed("alice@example.com", cur) is expected
