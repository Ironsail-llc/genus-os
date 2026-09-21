"""Gmail cadences use owned sent copies and fresh mailbox evidence, with separate approval."""

from datetime import UTC, datetime, timedelta

import pytest
from psycopg2.extras import Json

from robothor.operations.store import Conflict
from robothor.sales.tests.test_followups import followup
from robothor.sales.tests.test_gmail_delivery import action, sends
from robothor.sales.tests.test_gmail_sync import ThreadProvider, incoming, sent


async def confirmed(sales, monkeypatch):
    from robothor.sales.gmail_bounces import GmailBounceWorker
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, cli, gmail, original, record = await sent(sales, monkeypatch)
    sales.configure({"followup_delays_business_days": [3, 4]}, "operator:test")
    record["internalDate"] = str(int((datetime.now(UTC) - timedelta(days=8)).timestamp() * 1000))
    cli.messages[record["id"]] = record
    provider = ThreadProvider(gmail, [record])
    await GmailThreadWorker(sales, provider).tick()
    await GmailBounceWorker(sales, provider).tick()
    return p, cli, provider, original, record


@pytest.mark.asyncio
async def test_gmail_followup_is_planned_once_then_requires_its_own_approval(sales, monkeypatch):
    from robothor.sales.followups import Followups
    from robothor.sales.gmail_delivery import GmailDeliveryWorker

    p, cli, provider, original, record = await confirmed(sales, monkeypatch)
    planner = Followups(sales)
    basis = planner.basis(p["id"])
    assert basis["provider"] == "gmail" and basis["ordinal"] == 1
    assert basis["reply_to_uuid"] == provider.provider_id(record["id"])
    assert planner.plan() == 1 and planner.plan() == 0
    key = sales.draft(p["id"], followup(original) | {"reply_to_uuid": basis["reply_to_uuid"]})
    worker = GmailDeliveryWorker(
        sales, provider, clock=lambda: datetime(2026, 9, 18, 15, tzinfo=UTC)
    )
    assert not await worker.tick()
    assert sends(cli) == 1
    sales.ops.decide(key, True, "operator:test")
    cli.response = {"id": "followup456", "threadId": "def456"}
    assert await worker.tick()
    assert action(sales, key)["status"] == "completed"
    assert sends(cli) == 2
    assert not await worker.tick()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "unconfirmed",
        "effect",
        "thread",
        "mailbox",
        "stale_bounces",
        "partial_bounces",
        "stale_thread",
        "reply",
        "auto_reply",
        "bounced",
        "delayed",
        "unknown",
    ],
)
async def test_gmail_followup_cannot_use_incomplete_or_interrupted_evidence(
    sales, monkeypatch, change
):
    from robothor.sales.followups import Followups
    from robothor.sales.gmail_sync import GmailThreadWorker

    p, _, provider, original, record = await confirmed(sales, monkeypatch)
    # Establish the positive path before invalidating one piece of its proof.
    assert Followups(sales).basis(p["id"])["ordinal"] == 1
    with sales.ops.transaction() as cur:
        if change in {"unconfirmed", "bounced", "delayed"}:
            status = {
                "unconfirmed": "provider_accepted",
                "bounced": "bounced",
                "delayed": "delivery_delayed",
            }[change]
            cur.execute(
                "UPDATE operation_actions SET receipt=receipt||%s WHERE tenant_id=%s",
                (Json({"delivery_status": status}), sales.tenant),
            )
        elif change == "effect":
            cur.execute(
                "UPDATE operation_effects SET payload_hash='invalid' WHERE tenant_id=%s",
                (sales.tenant,),
            )
        elif change in {"thread", "mailbox"}:
            cur.execute(
                "UPDATE operation_effects SET receipt=receipt||%s WHERE tenant_id=%s",
                (
                    Json({"thread_id" if change == "thread" else "mailbox": "different"}),
                    sales.tenant,
                ),
            )
        elif change == "stale_thread":
            cur.execute(
                "UPDATE operation_audit SET detail=detail||%s WHERE tenant_id=%s AND event='gmail.thread_observed'",
                (
                    Json({"observed_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}),
                    sales.tenant,
                ),
            )
        elif change in {"stale_bounces", "partial_bounces"}:
            updates = (
                {"through": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}
                if change == "stale_bounces"
                else {"final": False}
            )
            cur.execute(
                "UPDATE operation_jobs SET result=result||%s WHERE tenant_id=%s AND kind='sales.gmail_bounces'",
                (Json(updates), sales.tenant),
            )
        elif change == "unknown":
            cur.execute(
                "UPDATE operation_actions SET status='unknown' WHERE tenant_id=%s", (sales.tenant,)
            )
    if change in {"reply", "auto_reply"}:
        r = incoming()
        if change == "auto_reply":
            r["payload"]["headers"].append({"name": "Auto-Submitted", "value": "auto-replied"})
        provider.records.append(r)
        await GmailThreadWorker(sales, provider).sync_prospect(p["id"])
    with pytest.raises(Conflict):
        Followups(sales).basis(p["id"])
    assert Followups(sales).plan() == 0
