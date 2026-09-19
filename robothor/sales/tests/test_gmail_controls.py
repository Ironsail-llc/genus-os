"""Mailbox failures revoke authority; local stops require no working provider."""

import pytest

from robothor.sales.tests.test_gmail_delivery import action, gmail_setup, sends
from robothor.sales.tests.test_guards import draft


@pytest.mark.asyncio
async def test_failed_mailbox_read_revokes_review_and_recovery_does_not_restore_it(
    sales, monkeypatch
):
    from robothor.sales.gmail_controls import GmailStatusWorker

    p, cli, delivery = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    cli.profile = "wrong@example.com"
    assert await GmailStatusWorker(sales, delivery.provider).tick()
    assert sales.settings()["mailbox_approved_until"] == {}
    assert action(sales, key)["status"] == "cancelled"
    reads = sales.provider_reads(kind="sales.gmail_status")
    assert len(reads["items"]) == 1
    cli.profile = "sales@example.com"
    sales.retry_provider_read(
        reads["items"][0]["id"], "operator:test", "Fixed the connected mailbox identity"
    )
    assert await GmailStatusWorker(sales, delivery.provider).tick()
    assert sales.settings()["mailbox_approved_until"] == {}
    assert sends(cli) == 0


@pytest.mark.asyncio
async def test_delivery_profile_failure_revokes_readiness_before_any_effect(sales, monkeypatch):
    p, cli, worker = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    cli.profile = "wrong@example.com"
    assert await worker.tick()
    assert sales.settings()["mailbox_approved_until"] == {}
    assert action(sales, key)["status"] == "cancelled"
    assert sends(cli) == 0


@pytest.mark.asyncio
async def test_final_local_refusal_is_cancelled_not_an_unknown_provider_write(sales, monkeypatch):
    from robothor.sales.gmail import Gmail

    p, cli, worker = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    reads = 0

    def race(args, timeout=30):
        nonlocal reads
        result = cli(args, timeout)
        if args[:3] == ["gmail", "users", "getProfile"]:
            reads += 1
            if reads == 2:
                sales.suppress("alice@example.com", "opt_out", "operator:test")
        return result

    worker.provider = Gmail(sales.tenant, runner=race)
    assert await worker.tick()
    assert sends(cli) == 0
    assert action(sales, key)["status"] == "cancelled"
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT 1 FROM operation_effects WHERE tenant_id=%s AND kind='gmail.send' AND dedup_key=%s",
            (sales.tenant, key),
        )
        assert cur.fetchone() is None


@pytest.mark.asyncio
async def test_local_stop_keeps_unknown_send_held_and_works_without_gmail_credentials(
    sales, monkeypatch
):
    from robothor.sales.gmail_controls import GmailStopWorker

    p, cli, worker = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    cli.response = {"error": "timeout"}
    await worker.tick()
    sales.suppress("alice@example.com", "opt_out", "operator:test")
    monkeypatch.delenv("ROBOTHOR_SALES_GMAIL_TENANT_ID")
    stop = GmailStopWorker(sales)
    assert await stop.tick()
    assert action(sales, key)["status"] == "unknown"
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.suppress'",
            (sales.tenant,),
        )
        result = cur.fetchone()["result"]
        assert result["remote_recall_attempted"] is False
        assert result["unresolved_sends"] == 1
    assert sends(cli) == 1


@pytest.mark.asyncio
async def test_stale_pause_job_does_not_cancel_newly_approved_work_after_resume(sales, monkeypatch):
    from robothor.sales.gmail_controls import GmailStopWorker

    p, cli, _ = gmail_setup(sales, monkeypatch)
    old = draft(sales, p)
    sales.ops.decide(old, True, "operator:test")
    sales.configure({"sending_enabled": False}, "operator:test")
    sales.configure({"sending_enabled": True}, "operator:test")
    current = draft(sales, p)
    sales.ops.decide(current, True, "operator:test")
    assert await GmailStopWorker(sales).tick()
    assert action(sales, old)["status"] == "cancelled"
    assert action(sales, current)["status"] == "approved"
    assert sends(cli) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage,worker_name",
    [
        ("stop", "GmailStopWorker"),
        ("status", "GmailStatusWorker"),
        ("reconcile", "GmailBounceWorker"),
    ],
)
async def test_native_controls_route_to_gmail(sales, monkeypatch, stage, worker_name):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from robothor.sales import queue

    sales.configure(
        {"email_provider": "gmail", "workflow_bindings": {stage: "gmail-workflow"}}, "operator:test"
    )
    tick = AsyncMock(return_value=True)
    monkeypatch.setattr(queue, worker_name, lambda sales: SimpleNamespace(tick=tick))
    assert (await queue.QueueDriver(sales).tick(stage, "gmail-workflow"))["worked"]
    tick.assert_awaited_once()
