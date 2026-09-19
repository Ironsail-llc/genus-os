"""Cold follow-ups require confirmed owned sends and a fresh, quiet conversation."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from psycopg2.extras import Json
from pydantic import ValidationError

from robothor.operations.store import Conflict, digest
from robothor.sales.models import SalesSettings
from robothor.sales.tests.test_guards import approved, prepared


def confirmed(sales):
    p = prepared(sales)
    sales.configure({"followup_delays_business_days": [3, 4]}, "operator:test")
    action = approved(sales, p)
    sales.ops.finish_action(action["id"], action["lease_token"], "completed", {"id": "campaign-1"})
    sent = datetime.now(UTC) - timedelta(days=8)
    message = {
        "provider_id": "message-1",
        "prospect_id": str(p["id"]),
        "direction": "outbound",
        "occurred_at": sent.isoformat(),
        "campaign_id": "campaign-1",
        "thread_id": "thread-1",
        **{k: action["payload"][k] for k in ("sender", "recipient", "subject", "body")},
    }
    from robothor.sales.ingestion import record_provider_message

    with sales.ops.transaction() as cur:
        for kind, receipt, payload_hash in (
            ("instantly.campaign", {"id": "campaign-1"}, "fixture"),
            ("instantly.activate", {"id": "campaign-1"}, digest({"campaign_id": "campaign-1"})),
        ):
            cur.execute(
                "INSERT INTO operation_effects(tenant_id,kind,dedup_key,payload_hash,status,receipt) "
                "VALUES(%s,%s,%s,%s,'completed',%s)",
                (sales.tenant, kind, str(action["id"]), payload_hash, Json(receipt)),
            )
        record_provider_message(sales, action, message, cur)
    scan = sales.ops.enqueue(
        "sales.reconcile",
        "fixture-scan",
        {"campaign_id": "campaign-1", "action_id": str(action["id"])},
    )
    job = sales.ops.claim("sales.reconcile")
    now = datetime.now(UTC).isoformat()
    sales.ops.complete(
        scan,
        job["lease_token"],
        {"final": True, "through": now, "full_at": now, "workspace": "workspace-1"},
    )
    return p, action, message


def followup(action):
    return {
        k: action["payload"][k]
        for k in (
            "sender",
            "recipient",
            "subject",
            "body",
            "claim_ids",
            "knowledge_version",
            "evidence_ids",
        )
    } | {"purpose": "followup", "reply_to_uuid": "message-1"}


def test_cadence_is_explicit_bounded_and_strict():
    assert SalesSettings().followup_delays_business_days == []
    assert SalesSettings(followup_delays_business_days=[3, 4]).followup_delays_business_days == [
        3,
        4,
    ]
    for value in ([0], [31], [True], ["3"], [1.5], [1, 2, 3]):
        with pytest.raises(ValidationError):
            SalesSettings(followup_delays_business_days=value)


def test_business_days_preserve_local_hour_across_weekend_and_dst():
    from robothor.sales.followups import due_at

    sent = datetime.fromisoformat("2026-10-30T15:00:00+00:00")
    assert due_at(sent, 1, "America/New_York") == datetime.fromisoformat(
        "2026-11-02T16:00:00+00:00"
    )


def test_concurrent_planners_enqueue_one_review_job_without_sending(sales):
    from robothor.sales.followups import Followups

    p, action, _ = confirmed(sales)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: Followups(sales).plan(), range(3)))
    job = sales.ops.claim("sales.draft")
    assert job["payload"]["purpose"] == "followup"
    assert job["payload"]["followup_basis"]["root_action_id"] == str(action["id"])
    assert job["payload"]["prospect_id"] == str(p["id"])
    assert sales.ops.claim("sales.draft") is None
    assert sales.ops.claim_action(kind="sales.email") is None


@pytest.mark.parametrize(
    "change",
    [
        "unconfirmed",
        "fabricated",
        "stale_scan",
        "pending_scan",
        "inbound",
        "auto_reply",
        "owner",
        "milestone",
        "uncertain",
        "disabled",
    ],
)
def test_invalid_or_interrupted_chain_never_generates(sales, change):
    from robothor.sales.followups import Followups

    p, action, message = confirmed(sales)
    with sales.ops.transaction() as cur:
        if change == "unconfirmed":
            cur.execute(
                "UPDATE operation_actions SET receipt='{}' WHERE tenant_id=%s", (sales.tenant,)
            )
        elif change == "fabricated":
            cur.execute("DELETE FROM operation_effects WHERE tenant_id=%s", (sales.tenant,))
        elif change == "stale_scan":
            cur.execute(
                "UPDATE operation_jobs SET result=result || %s WHERE tenant_id=%s AND kind='sales.reconcile'",
                (
                    Json({"through": (datetime.now(UTC) - timedelta(hours=1)).isoformat()}),
                    sales.tenant,
                ),
            )
        elif change == "pending_scan":
            sales.ops.enqueue(
                "sales.reconcile", "pending-page", {"campaign_id": "campaign-1"}, cur=cur
            )
        elif change == "owner":
            cur.execute(
                "UPDATE sales_prospects SET owner='operator:test' WHERE tenant_id=%s",
                (sales.tenant,),
            )
        elif change == "milestone":
            cur.execute(
                "UPDATE sales_prospects SET outcome_version=1 WHERE tenant_id=%s", (sales.tenant,)
            )
        elif change == "uncertain":
            sales.ops.propose("sales.email", "uncertain", {"prospect_id": str(p["id"])}, cur=cur)
            cur.execute(
                "UPDATE operation_actions SET status='unknown' WHERE tenant_id=%s AND dedup_key='uncertain'",
                (sales.tenant,),
            )
    if change in {"inbound", "auto_reply"}:
        sales.record_message(
            message
            | {
                "provider_id": "inbound-1",
                "direction": "inbound",
                "sender": message["recipient"],
                "recipient": message["sender"],
                "auto_reply": change == "auto_reply",
            }
        )
    if change == "disabled":
        sales.configure({"followup_delays_business_days": []}, "operator:test")
    Followups(sales).plan()
    assert sales.ops.claim("sales.draft") is None
    with pytest.raises(Conflict):
        sales.draft(p["id"], followup(action))


def test_followup_exact_approval_rechecks_cadence_and_does_not_regenerate_rejection(sales):
    from robothor.sales.followups import Followups

    p, original, _ = confirmed(sales)
    action_id = sales.draft(p["id"], followup(original))
    assert sales.ops.claim_action(kind="sales.email") is None
    sales.ops.decide(action_id, True, "operator:test")
    leased = sales.ops.claim_action(kind="sales.email")
    sales.validate_send(leased)
    sales.configure({"followup_delays_business_days": [4, 4]}, "operator:test")
    with pytest.raises(Conflict, match="[Cc]adence|[Ff]ollow-up"):
        sales.validate_send(leased)
    sales.ops.finish_action(leased["id"], leased["lease_token"], "cancelled", {})
    Followups(sales).plan()
    assert sales.ops.claim("sales.draft") is None
    with pytest.raises(Conflict):
        sales.draft(p["id"], followup(original))


@pytest.mark.asyncio
async def test_native_sdr_plans_and_proposes_a_followup_without_approval(sales):
    from robothor.sales.runtime import DraftWorker
    from robothor.sales.tests.test_runtime import RunnerStub

    _, original, _ = confirmed(sales)
    sales.configure(
        {
            "agents": {"draft": "sdr-agent"},
            "monthly_limit_units": 10_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )
    runner = RunnerStub(followup(original))
    assert await DraftWorker(sales, runner).tick()
    assert len(runner.calls) == 1
    reviews = [a for a in sales.overview()["actions"] if a["status"] == "review"]
    assert len(reviews) == 1
    assert reviews[0]["payload"]["followup_basis"]["ordinal"] == 1
    assert sales.ops.claim_action(kind="sales.email") is None
    assert not await DraftWorker(sales, runner).tick()


@pytest.mark.asyncio
async def test_native_followup_rechecks_new_reply_during_generation(sales):
    from robothor.sales.runtime import DraftWorker
    from robothor.sales.tests.test_runtime import RunnerStub

    _, original, message = confirmed(sales)
    sales.configure(
        {
            "agents": {"draft": "sdr-agent"},
            "monthly_limit_units": 10_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )

    class ReplyDuringRun(RunnerStub):
        async def run(self, **kwargs):
            sales.record_message(
                message
                | {
                    "provider_id": "reply-during-run",
                    "direction": "inbound",
                    "sender": message["recipient"],
                    "recipient": message["sender"],
                }
            )
            return await super().run(**kwargs)

    runner = ReplyDuringRun(followup(original))
    assert await DraftWorker(sales, runner).tick()
    assert len(runner.calls) == 1
    assert not any(a["status"] == "review" for a in sales.overview()["actions"])


def test_changed_queued_cadence_does_not_replace_or_crash_existing_job(sales):
    from robothor.sales.followups import Followups

    confirmed(sales)
    Followups(sales).plan()
    sales.configure({"followup_delays_business_days": [4, 4]}, "operator:test")
    assert Followups(sales).plan() == 0
    job = sales.ops.claim("sales.draft")
    assert job["payload"]["followup_basis"]["delays_business_days"] == [3, 4]


@pytest.mark.parametrize(
    "change",
    ["recipient", "thread", "approved_hash", "negative_event", "pending_inbox", "account_error"],
)
def test_identity_and_provider_event_holds(sales, change):
    from robothor.sales.followups import Followups
    from robothor.sales.tests.test_ingestion import event, receive

    p, action, _ = confirmed(sales)
    if change in {"negative_event", "account_error"}:
        receive(
            sales, event("lead_not_interested" if change == "negative_event" else "account_error")
        )
    elif change == "pending_inbox":
        sales.ops.receive(
            "instantly", "pending", {"campaign_id": "campaign-1", "kind": "email_sent"}
        )
    elif change == "thread":
        wrong = followup(action) | {"reply_to_uuid": "unowned-message"}
        with pytest.raises(Conflict):
            sales.draft(p["id"], wrong)
        return
    else:
        with sales.ops.transaction() as cur:
            if change == "recipient":
                cur.execute(
                    "UPDATE sales_messages SET data=data || %s WHERE tenant_id=%s",
                    (Json({"recipient": "someone@example.com"}), sales.tenant),
                )
            else:
                cur.execute(
                    "UPDATE operation_actions SET approved_hash='tampered' WHERE tenant_id=%s",
                    (sales.tenant,),
                )
    Followups(sales).plan()
    assert sales.ops.claim("sales.draft") is None


def test_second_delay_starts_at_actual_followup_send_not_initial_due_date(sales):
    from robothor.sales.followups import Followups, due_at
    from robothor.sales.ingestion import record_provider_message

    p, original, message = confirmed(sales)
    action_id = sales.draft(p["id"], followup(original))
    sales.ops.decide(action_id, True, "operator:test")
    action = sales.ops.claim_action(kind="sales.email")
    sales.ops.finish_action(action_id, action["lease_token"], "completed", {"id": "followup-1"})
    actual_send = datetime.now(UTC) - timedelta(seconds=1)
    with sales.ops.transaction() as cur:
        cur.execute(
            "INSERT INTO operation_effects(tenant_id,kind,dedup_key,payload_hash,status,receipt) VALUES(%s,'instantly.reply',%s,%s,'completed',%s)",
            (sales.tenant, action_id, digest(action["payload"]), Json({"id": "followup-1"})),
        )
        record_provider_message(
            sales,
            original,
            message | {"provider_id": "followup-1", "occurred_at": actual_send.isoformat()},
            cur,
        )
    with pytest.raises(Conflict, match="not due"):
        Followups(sales).basis(p["id"])
    due = due_at(actual_send, 4, "America/Chicago")
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET result=result || %s WHERE tenant_id=%s AND kind='sales.reconcile'",
            (Json({"through": due.isoformat(), "full_at": due.isoformat()}), sales.tenant),
        )
    basis = Followups(sales).basis(p["id"], now=due)
    assert basis["ordinal"] == 2
    assert basis["reply_to_uuid"] == "followup-1"
    assert basis["due_at"] == due.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_workspace", [False, True])
async def test_followup_delivery_checks_workspace_and_uses_existing_mailbox_quota(
    sales, wrong_workspace
):
    from robothor.sales.delivery import DeliveryWorker
    from robothor.sales.tests.test_delivery import MailProvider

    p, original, _ = confirmed(sales)
    sales.configure(
        {
            "mailbox_daily_limit": 1,
            "mailbox_approved_until": {
                "sales@example.com": (datetime.now(UTC) + timedelta(days=2)).isoformat()
            },
        },
        "operator:test",
    )
    action_id = sales.draft(p["id"], followup(original))
    sales.ops.decide(action_id, True, "operator:test")

    class Provider(MailProvider):
        async def secret(self, key):
            return "other-workspace" if wrong_workspace else "workspace-1"

        async def reply(self, payload):
            self.calls.append("reply")
            assert payload["reply_to_uuid"] == "message-1"
            return {"id": "followup-1"}

    provider = Provider()
    worker = DeliveryWorker(sales, provider, clock=lambda: datetime(2026, 9, 18, 15, tzinfo=UTC))
    assert await worker.tick()
    if wrong_workspace:
        assert not provider.calls
        return
    assert provider.calls == ["reply"]
    action = next(a for a in sales.overview()["actions"] if str(a["id"]) == action_id)
    assert action["receipt"]["delivery_status"] == "provider_accepted"
    # An ordinary reviewed reply competes for the same one-message mailbox cap.
    reply_id = sales.draft(p["id"], followup(original) | {"purpose": "reply"})
    sales.ops.decide(reply_id, True, "operator:test")
    assert await worker.tick()
    assert provider.calls == ["reply"]
