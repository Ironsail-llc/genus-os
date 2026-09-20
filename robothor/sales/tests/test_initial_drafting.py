"""Genus can prepare accepted prospects without requiring Pipedrive promotion."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from robothor.sales.tests.test_guards import prepared


def ready(sales):
    p = prepared(sales)
    sales.configure(
        {
            "agents": {"draft": "sdr-agent"},
            "sending_enabled": False,
            "promotion_enabled": False,
            "monthly_limit_units": 5_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )
    return p


def test_concurrent_planners_queue_one_draft_without_pipedrive_or_send_access(sales):
    from robothor.sales.drafting import InitialDraftPlanner

    p = ready(sales)
    with ThreadPoolExecutor(max_workers=3) as pool:
        counts = list(pool.map(lambda _: InitialDraftPlanner(sales).plan(), range(3)))
    assert sum(counts) == 1
    job = sales.ops.claim("sales.draft")
    assert job["payload"] == {"prospect_id": str(p["id"])}
    assert sales.ops.claim("sales.draft") is None
    assert not sales.get(p["id"])["pipedrive_ids"]
    assert sales.ops.claim_action() is None


@pytest.mark.parametrize(
    "hold",
    ["unverified", "stale", "suppressed", "owner", "unaccepted", "existing_message", "prior_job"],
)
def test_unready_or_previously_attempted_prospects_are_not_automatically_redrafted(sales, hold):
    from psycopg2.extras import Json

    from robothor.sales.drafting import InitialDraftPlanner

    p = ready(sales)
    with sales.ops.transaction() as cur:
        if hold in {"unverified", "stale"}:
            change = (
                {"verification": "unknown"}
                if hold == "unverified"
                else {"verified_at": (datetime.now(UTC) - timedelta(days=31)).isoformat()}
            )
            cur.execute(
                "UPDATE sales_contacts SET data=data || %s WHERE tenant_id=%s",
                (Json(change), sales.tenant),
            )
        elif hold == "owner":
            cur.execute(
                "UPDATE sales_prospects SET owner='operator:test' WHERE tenant_id=%s",
                (sales.tenant,),
            )
        elif hold == "unaccepted":
            cur.execute(
                "UPDATE sales_prospects SET status='qualified' WHERE tenant_id=%s", (sales.tenant,)
            )
        elif hold == "prior_job":
            job = sales.ops.enqueue("sales.draft", "old", {"prospect_id": str(p["id"])}, cur=cur)
            cur.execute("UPDATE operation_jobs SET status='failed' WHERE id=%s", (job,))
    if hold == "suppressed":
        sales.suppress("alice@example.com", "Operator stop", "operator:test")
    if hold == "existing_message":
        sales.record_message(
            {
                "prospect_id": p["id"],
                "provider_id": "old-message",
                "direction": "outbound",
                "occurred_at": datetime.now(UTC).isoformat(),
                "sender": "sales@example.com",
                "recipient": "alice@example.com",
                "subject": "Already contacted",
                "body": "Hello",
            }
        )
    assert InitialDraftPlanner(sales).plan() == 0
    assert sales.ops.claim("sales.draft") is None


@pytest.mark.asyncio
async def test_native_draft_tick_completes_genus_only_path(sales):
    from robothor.sales.runtime import DraftWorker
    from robothor.sales.tests.test_runtime import RunnerStub

    ready(sales)
    model = RunnerStub(
        {
            "sender": "sales@example.com",
            "recipient": "alice@example.com",
            "subject": "Workflow question",
            "body": "Access the partner network. TEST POSTAL ADDRESS https://example.com/unsubscribe",
            "claim_ids": ["access"],
            "knowledge_version": "v1",
            "evidence_ids": ["service"],
        }
    )
    assert await DraftWorker(sales, model).tick()
    assert len(model.calls) == 1
    assert sales.overview()["actions"][0]["status"] == "review"
    assert sales.ops.claim_action() is None


def test_manual_draft_created_during_planning_prevents_duplicate_model_work(sales, monkeypatch):
    import threading

    from robothor.sales.drafting import InitialDraftPlanner
    from robothor.sales.tests.test_guards import draft

    p = ready(sales)
    entered, release = threading.Event(), threading.Event()
    original = sales.require

    def require(prospect, cur):
        if threading.current_thread().name.startswith("ThreadPoolExecutor"):
            entered.set()
            assert release.wait(5)
        return original(prospect, cur)

    monkeypatch.setattr(sales, "require", require)
    with ThreadPoolExecutor(max_workers=1) as pool:
        planned = pool.submit(InitialDraftPlanner(sales).plan)
        assert entered.wait(5)
        try:
            draft(sales, p)
        finally:
            release.set()
        assert planned.result() == 0
    assert sales.ops.claim("sales.draft") is None
