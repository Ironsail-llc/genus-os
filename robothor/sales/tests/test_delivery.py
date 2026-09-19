"""Approved sends, provider scheduling receipts, suppression races and recovery."""

from datetime import UTC, datetime, timedelta

import pytest

from robothor.sales.delivery import DeliveryWorker, StopWorker
from robothor.sales.providers import UnknownEffect
from robothor.sales.tests.test_guards import draft, prepared


class MailProvider:
    def __init__(self):
        self.calls = []
        self.on_lead = None
        self.ambiguous = False

    async def account(self, email):
        return {
            "email": email,
            "status": 1,
            "warmup_status": 1,
            "setup_pending": False,
            "stat_warmup_score": 99,
            "timestamp_warmup_start": "2025-01-01T00:00:00Z",
        }

    async def create_campaign(self, *args):
        self.calls.append("create")
        return {"id": "campaign-1"}

    async def add_lead(self, campaign_id, email):
        self.calls.append("lead")
        if self.on_lead:
            self.on_lead()
        return {"id": "lead-1"}

    async def activate(self, campaign_id):
        self.calls.append("activate")
        if self.ambiguous:
            raise UnknownEffect("Transport failure")
        return {"id": campaign_id}

    async def pause(self, campaign_id):
        self.calls.append("pause")
        return {"id": campaign_id}

    async def suppress(self, email):
        self.calls.append("suppress")
        return {"id": "block-1"}


def setup(sales):
    p = prepared(sales)
    sales.configure(
        {
            "mailbox_approved_until": {
                "sales@example.com": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            }
        },
        "operator:test",
    )
    provider = MailProvider()
    # Friday morning; the worker clock controls only the sending window and quota day.
    worker = DeliveryWorker(sales, provider, clock=lambda: datetime(2026, 9, 18, 15, tzinfo=UTC))
    return p, provider, worker


@pytest.mark.asyncio
async def test_only_exact_approved_message_is_scheduled_once(sales):
    p, provider, worker = setup(sales)
    action = draft(sales, p)
    assert await worker.tick() is False
    assert provider.calls == []
    sales.ops.decide(action, True, "operator:test")
    assert await worker.tick() is True
    assert provider.calls == ["create", "lead", "activate"]
    result = next(a for a in sales.overview()["actions"] if a["id"] == action)
    assert result["status"] == "completed"
    assert result["receipt"]["delivery_status"] == "scheduled"
    assert sales.messages(p["id"]) == []  # activation is not delivery evidence
    assert await worker.tick() is False


@pytest.mark.asyncio
async def test_suppression_between_campaign_creation_and_activation_stops_send(sales):
    p, provider, worker = setup(sales)
    action = draft(sales, p)
    sales.ops.decide(action, True, "operator:test")
    provider.on_lead = lambda: sales.suppress("alice@example.com", "opt_out", "operator:test")
    await worker.tick()
    assert "activate" not in provider.calls
    assert "pause" in provider.calls


@pytest.mark.asyncio
async def test_unknown_activation_is_never_retried(sales):
    p, provider, worker = setup(sales)
    provider.ambiguous = True
    action = draft(sales, p)
    sales.ops.decide(action, True, "operator:test")
    await worker.tick()
    assert next(a for a in sales.overview()["actions"] if a["id"] == action)["status"] == "unknown"
    assert await worker.tick() is False
    assert provider.calls.count("activate") == 1


@pytest.mark.asyncio
async def test_mailbox_cap_counts_queued_messages_and_blocks_later_actions(sales):
    p, provider, worker = setup(sales)
    sales.configure({"mailbox_daily_limit": 1}, "operator:test")
    for _ in range(2):
        action = draft(sales, p)
        sales.ops.decide(action, True, "operator:test")
        await worker.tick()
    assert provider.calls.count("activate") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["opt_out", "takeover", "global_pause"])
async def test_stop_worker_pauses_previously_scheduled_campaigns(sales, reason):
    p, provider, worker = setup(sales)
    action = draft(sales, p)
    sales.ops.decide(action, True, "operator:test")
    await worker.tick()
    if reason == "opt_out":
        sales.suppress("alice@example.com", "opt_out", "operator:test")
    elif reason == "takeover":
        sales.takeover(p["id"], "operator:test")
    else:
        sales.configure({"sending_enabled": False}, "operator:test")
    stop = StopWorker(sales, provider)
    while await stop.tick():
        pass
    assert "pause" in provider.calls
    if reason == "opt_out":
        assert "suppress" in provider.calls
