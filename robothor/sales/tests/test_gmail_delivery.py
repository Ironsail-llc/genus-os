"""Direct sends retain exact approval, suppression, quotas and unknown-write holds."""

from datetime import UTC, datetime

import pytest

from robothor.sales.tests.test_delivery import setup
from robothor.sales.tests.test_gmail import GmailCLI
from robothor.sales.tests.test_guards import draft
from robothor.settings import reset_settings


def gmail_setup(sales, monkeypatch):
    from robothor.sales.gmail import Gmail
    from robothor.sales.gmail_delivery import GmailDeliveryWorker

    p, _, _ = setup(sales)
    sales.configure({"email_provider": "gmail"}, "operator:test")
    monkeypatch.setenv("ROBOTHOR_SALES_GMAIL_TENANT_ID", sales.tenant)
    monkeypatch.setenv("ROBOTHOR_SALES_GMAIL_MAILBOX", "sales@example.com")
    # The Gmail host binding is a declared setting now, and settings are
    # resolved once per process; a test that reconfigures the host has to
    # say so. `reset_settings` exists for exactly this.
    reset_settings()
    cli = GmailCLI()
    worker = GmailDeliveryWorker(
        sales, Gmail(sales.tenant, runner=cli), clock=lambda: datetime(2026, 9, 18, 15, tzinfo=UTC)
    )
    return p, cli, worker


def sends(cli):
    return sum(c[:4] == ["gmail", "users", "messages", "send"] for c in cli.calls)


def action(sales, key):
    return next(a for a in sales.overview()["actions"] if a["id"] == key)


@pytest.mark.asyncio
async def test_gmail_requires_approval_and_records_acceptance_not_delivery(sales, monkeypatch):
    p, cli, worker = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    assert not await worker.tick()
    assert cli.calls == []
    sales.ops.decide(key, True, "operator:test")
    assert await worker.tick()
    result = action(sales, key)
    assert result["status"] == "completed"
    assert result["receipt"]["delivery_status"] == "provider_accepted"
    assert sales.messages(p["id"]) == []
    assert not await worker.tick()
    assert sends(cli) == 1
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status FROM operation_effects WHERE tenant_id=%s AND kind='gmail.send'",
            (sales.tenant,),
        )
        assert cur.fetchone()["status"] == "completed"


@pytest.mark.asyncio
async def test_gmail_timeout_holds_write_without_retry_after_worker_restart(sales, monkeypatch):
    from robothor.sales.gmail_delivery import GmailDeliveryWorker

    p, cli, worker = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    cli.response = {"error": "timeout: secret and private body"}
    assert await worker.tick()
    held = action(sales, key)
    assert held["status"] == "unknown"
    assert "secret" not in str(held["receipt"])
    restarted = GmailDeliveryWorker(sales, worker.provider, clock=worker.clock)
    assert not await restarted.tick()
    assert sends(cli) == 1


@pytest.mark.asyncio
async def test_optout_during_final_profile_read_blocks_the_send(sales, monkeypatch):
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
    assert action(sales, key)["status"] in {"cancelled", "unknown"}


@pytest.mark.asyncio
async def test_gmail_shared_daily_limit_is_reserved_before_send(sales, monkeypatch):
    from robothor.sales.gmail_sync import GmailThreadWorker
    from robothor.sales.tests.test_gmail import message
    from robothor.sales.tests.test_gmail_sync import ThreadProvider

    p, cli, worker = gmail_setup(sales, monkeypatch)
    reservations = []
    reserve = worker._reserve_slot

    def reserve_checked(action, day):
        reservations.append(action["id"])
        return reserve(action, day)

    worker._reserve_slot = reserve_checked
    sales.configure({"mailbox_daily_limit": 1}, "operator:test")
    first = draft(sales, p)
    sales.ops.decide(first, True, "operator:test")
    assert await worker.tick()
    sent = action(sales, first)["payload"]
    record = message(
        message_id=worker.provider.message_id(first, sent),
        sender=sent["sender"],
        recipient=sent["recipient"],
        subject=sent["subject"],
        body=sent["body"] + "\n",
        labels=["SENT"],
    )
    record["internalDate"] = str(int(datetime.now(UTC).timestamp() * 1000))
    worker.provider = ThreadProvider(worker.provider, [record])
    await GmailThreadWorker(sales, worker.provider).sync_prospect(p["id"])
    second = draft(sales, p)
    sales.ops.decide(second, True, "operator:test")
    assert await worker.tick()
    assert action(sales, second)["status"] == "cancelled"
    assert sends(cli) == 1
    assert reservations == [first, second]


@pytest.mark.asyncio
async def test_provider_change_invalidates_prior_message_approval(sales, monkeypatch):
    p, _, _ = setup(sales)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    claimed = sales.ops.claim_action(kind="sales.email")
    sales.configure({"email_provider": "gmail"}, "operator:test")
    from robothor.operations.store import Conflict

    with pytest.raises(Conflict, match="configuration"):
        sales.validate_send(claimed)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["paused", "outside_window", "not_gmail"])
async def test_disabled_or_outside_window_gmail_performs_no_reads_or_writes(
    sales, monkeypatch, state
):
    p, cli, worker = gmail_setup(sales, monkeypatch)
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    if state == "paused":
        sales.configure({"sending_enabled": False}, "operator:test")
    elif state == "outside_window":
        worker.clock = lambda: datetime(2026, 9, 19, 15, tzinfo=UTC)
    else:
        sales.configure({"email_provider": "instantly"}, "operator:test")
    assert not await worker.tick()
    assert cli.calls == []


@pytest.mark.asyncio
async def test_legacy_delivery_worker_cannot_send_for_gmail_selection(sales):
    p, provider, worker = setup(sales)
    sales.configure({"email_provider": "gmail"}, "operator:test")
    key = draft(sales, p)
    sales.ops.decide(key, True, "operator:test")
    assert not await worker.tick()
    assert provider.calls == []
