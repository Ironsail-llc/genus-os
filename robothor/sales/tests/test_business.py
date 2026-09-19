"""Current business evidence and reviewed customer identities, in real PostgreSQL."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from robothor.db.connection import get_connection
from robothor.operations.store import Conflict
from robothor.sales.business import BusinessObservations
from robothor.sales.service import Sales


@pytest.fixture
def business(sales):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                (
                    Path(__file__).parents[3] / "crm/migrations/128_sales_business_observations.sql"
                ).read_text()
            )
        conn.commit()
    return BusinessObservations(sales)


def practice(business, *, name="Example Practice", unit="group-1", revision="v1", at=None):
    return business.observe(
        "orders_app",
        "account-1",
        "practice",
        "practice-1",
        revision,
        at or datetime.now(UTC),
        {
            "business_unit_id": unit,
            "name": name,
            "state": "TX",
            "active": True,
            "created_at": "2026-01-01T00:00:00Z",
            "source_updated_at": "2026-09-01T00:00:00Z",
        },
    )


def prospect(sales, suffix="one"):
    return sales.discover(
        "Example Practice", f"https://{suffix}.example.com", f"https://{suffix}.example.com"
    )["id"]


def order(business, *, state="verified", revision="v1", at=None, identity="order-1"):
    return business.observe(
        "orders_app",
        "account-1",
        "order",
        identity,
        revision,
        at or datetime.now(UTC),
        {
            "practice_id": "practice-1",
            "category": "standard_order",
            "placed_at": "2026-07-01T00:00:00Z",
            "fulfilled_at": "2026-07-03T00:00:00Z" if state == "verified" else None,
            "fulfillment": state,
            "source_updated_at": "2026-09-01T00:00:00Z",
        },
    )


def test_exact_revision_human_binding_is_required_and_orders_backfill_after_review(sales, business):
    p = prospect(sales)
    identity = practice(business)
    order(business)
    assert sales.retention(p)["completed_orders"] == 0
    with pytest.raises(Conflict, match="Human operator"):
        business.bind(p, identity, "v1", "agent:worker", "Reviewed identity evidence")
    with pytest.raises(Conflict, match="revision"):
        business.bind(p, identity, "stale", "operator:test", "Reviewed identity evidence")
    business.bind(p, identity, "v1", "operator:test", "Reviewed identity evidence")
    metrics = sales.retention(p)
    assert metrics["completed_orders"] == 1
    assert metrics["requires_review"] is False
    assert metrics["first_completed_order"] == "2026-07-03T00:00:00+00:00"
    with pytest.raises(Conflict):
        business.bind(
            prospect(sales, "two"), identity, "v1", "operator:test", "Reviewed another company"
        )


def test_corrections_replay_and_restoration_use_current_state_not_permanent_reversal(
    sales, business
):
    p = prospect(sales)
    identity = practice(business)
    business.bind(p, identity, "v1", "operator:test", "Reviewed identity evidence")
    stamp = datetime.now(UTC) - timedelta(minutes=2)
    item = order(business, at=stamp)
    version = sales.get(p)["outcome_version"]
    assert order(business, at=stamp) == item
    assert sales.get(p)["outcome_version"] == version
    assert sales.retention(p)["completed_orders"] == 1
    order(business, state="excluded", revision="v2", at=stamp + timedelta(seconds=1))
    assert sales.retention(p)["completed_orders"] == 0
    # A restored state can reuse its original content revision; still a new observation.
    order(business, revision="v1", at=stamp + timedelta(seconds=2))
    assert sales.retention(p)["completed_orders"] == 1
    with pytest.raises(Conflict, match="older"):
        order(business, state="excluded", revision="v2", at=stamp)
    with pytest.raises(Conflict, match="revision"):
        order(business, state="excluded", revision="v1", at=stamp + timedelta(seconds=3))


def test_changed_practice_identity_holds_mapping_and_cancels_pending_message(sales, business):
    p = prospect(sales)
    identity = practice(business)
    business.bind(p, identity, "v1", "operator:test", "Reviewed identity evidence")
    order(business)
    action = sales.ops.propose("sales.email", "pending-test", {"prospect_id": str(p)})
    practice(business, unit="another-group", revision="v2")
    metrics = sales.retention(p)
    assert metrics["requires_review"] is True
    assert metrics["completed_orders"] is None
    assert sales.ops.claim_action(kind="sales.email") is None
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status FROM operation_actions WHERE tenant_id=%s AND id=%s",
            (sales.tenant, action),
        )
        assert cur.fetchone()["status"] == "cancelled"
    business.bind(p, identity, "v2", "operator:test", "Reviewed changed group evidence")
    assert sales.retention(p)["completed_orders"] == 1


def test_foreign_observation_and_patient_fields_cannot_enter_binding(sales, business):
    identity = practice(business)
    other = Sales("foreign-business-test")
    with pytest.raises(Conflict):
        BusinessObservations(other).bind(
            "00000000-0000-4000-8000-000000000001",
            identity,
            "v1",
            "operator:test",
            "Reviewed identity evidence",
        )
    with pytest.raises(ValueError):
        business.observe(
            "orders_app",
            "account-1",
            "practice",
            "private",
            "v1",
            datetime.now(UTC),
            {
                "business_unit_id": "group",
                "name": "Example",
                "active": True,
                "patient_name": "private",
            },
        )


def test_page_transaction_failure_rolls_back_observations_and_customer_state(sales, business):
    p = prospect(sales)
    identity = practice(business)
    business.bind(p, identity, "v1", "operator:test", "Reviewed identity evidence")
    with pytest.raises(RuntimeError):
        with sales.ops.transaction() as cur:
            business.observe(
                "orders_app",
                "account-1",
                "order",
                "atomic-order",
                "v1",
                datetime.now(UTC),
                {
                    "practice_id": "practice-1",
                    "category": "wholesale_order",
                    "placed_at": "2026-07-01T00:00:00Z",
                    "fulfilled_at": "2026-07-02T00:00:00Z",
                    "fulfillment": "verified",
                    "source_updated_at": "2026-09-01T00:00:00Z",
                },
                cur=cur,
            )
            raise RuntimeError("Simulated page checkpoint failure")
    assert sales.retention(p)["completed_orders"] == 0


def test_legacy_attribution_cannot_be_mixed_with_reviewed_current_evidence(sales, business):
    p = prospect(sales)
    identity = practice(business)
    business.bind(p, identity, "v1", "operator:test", "Reviewed identity evidence")
    with pytest.raises(Conflict, match="attribution"):
        sales.bind_customer(p, "legacy-company", "operator:test")


def test_inactive_practice_cannot_have_a_ready_account_from_stale_signup(sales, business):
    p = prospect(sales)
    identity = practice(business)
    business.bind(p, identity, "v1", "operator:test", "Reviewed identity evidence")
    business.observe(
        "orders_app",
        "account-1",
        "signup",
        "signup-1",
        "v1",
        datetime.now(UTC),
        {
            "source_user_id": "user-1",
            "email": "owner@example.com",
            "name": "Example Practice",
            "state": "TX",
            "signed_up_at": "2026-07-01T00:00:00Z",
            "onboarding_status": "completed",
            "lifecycle_status": "active",
            "practice_id": "practice-1",
            "business_unit_id": "group-1",
            "approved_at": "2026-07-02T00:00:00Z",
            "account_ready": True,
            "source_updated_at": "2026-09-01T00:00:00Z",
        },
    )
    assert sales.retention(p)["account_ready"] is True
    business.observe(
        "orders_app",
        "account-1",
        "practice",
        "practice-1",
        "v2",
        datetime.now(UTC),
        {
            "business_unit_id": "group-1",
            "name": "Example Practice",
            "state": "TX",
            "active": False,
            "created_at": "2026-01-01T00:00:00Z",
            "source_updated_at": "2026-09-01T00:00:00Z",
        },
    )
    assert sales.retention(p)["account_ready"] is False


def test_page_cursor_and_evidence_commit_together_under_the_job_lease(sales, business, monkeypatch):
    payload = {
        "source": "orders_app",
        "account_id": "account-1",
        "kind": "practice",
        "practice_id": None,
        "after": None,
        "seen_cursors": [],
        "scan_id": "scan-1",
    }
    job_id = sales.ops.enqueue("sales.business", "scan-1", payload)
    job = sales.ops.claim("sales.business")
    page = {
        "source": "orders_app",
        "account_id": "account-1",
        "kind": "practice",
        "practice_id": None,
        "after": None,
        "observed_at": datetime.now(UTC).isoformat(),
        "next_cursor": "next-1",
        "items": [
            {
                "external_id": "practice-1",
                "revision": "v1",
                "data": {
                    "business_unit_id": "group-1",
                    "name": "Example",
                    "active": True,
                    "created_at": "2026-01-01T00:00:00Z",
                    "source_updated_at": "2026-09-01T00:00:00Z",
                },
            }
        ],
    }
    real_complete = sales.ops.complete
    monkeypatch.setattr(
        sales.ops,
        "complete",
        lambda *args, **kwargs: (_ for _ in ()).throw(Conflict("Simulated commit failure")),
    )
    with pytest.raises(Conflict):
        business.commit_page(job, page)
    assert business.records()["items"] == []
    assert sales.ops.claim("sales.business") is None
    monkeypatch.setattr(sales.ops, "complete", real_complete)
    business.commit_page(job, page)
    assert sales.ops.get_job(job_id)["status"] == "completed"
    assert len(business.records()["items"]) == 1
    follow = sales.ops.claim("sales.business")
    assert follow["payload"]["after"] == "next-1"
    assert follow["payload"]["scan_id"] == "scan-1"
    with pytest.raises(Conflict):
        business.commit_page(job, page)
    # An empty terminal page is not evidence that the practice disappeared.
    business.commit_page(follow, {**page, "after": "next-1", "next_cursor": None, "items": []})
    assert len(business.records()["items"]) == 1


def test_page_rejects_changed_account_or_repeated_cursor_before_persisting(sales, business):
    payload = {
        "source": "orders_app",
        "account_id": "account-1",
        "kind": "practice",
        "practice_id": None,
        "after": "cursor-1",
        "seen_cursors": ["cursor-1"],
        "scan_id": "scan-1",
    }
    sales.ops.enqueue("sales.business", "scan-1", payload)
    job = sales.ops.claim("sales.business")
    page = {
        "source": "orders_app",
        "account_id": "foreign",
        "kind": "practice",
        "practice_id": None,
        "after": "cursor-1",
        "observed_at": datetime.now(UTC).isoformat(),
        "next_cursor": None,
        "items": [],
    }
    with pytest.raises(Conflict, match="identity"):
        business.commit_page(job, page)
    with pytest.raises(Conflict, match="cursor"):
        business.commit_page(job, {**page, "account_id": "account-1", "next_cursor": "cursor-1"})


def test_partial_pages_cannot_claim_complete_retention_cohorts(sales, business):
    p = prospect(sales)
    identity = practice(business)
    business.bind(p, identity, "v1", "operator:test", "Reviewed identity evidence")
    order(business)
    metrics = sales.retention(p)
    assert metrics["coverage_complete"] is False
    assert metrics["repeat_within_30_days"] is None
    assert metrics["active_days_31_60"] is None
    assert metrics["active_days_61_90"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("human_required", [False, True])
async def test_activation_does_not_assume_onboarding_from_incomplete_order_history(
    sales, business, human_required
):
    from robothor.sales.stages import ActivationWorker
    from robothor.sales.tests.test_runtime import RunnerStub
    from robothor.sales.tests.test_stages import configure

    p = prospect(sales)
    identity = practice(business)
    business.bind(p, identity, "v1", "operator:test", "Reviewed identity evidence")
    configure(sales, "activation")
    sales.configure({"outcomes_enabled": True}, "operator:test")
    worker = ActivationWorker(
        sales,
        RunnerStub(
            {
                "next_step": "Review the next onboarding step",
                "human_required": human_required,
                "draft": None,
            }
        ),
    )
    assert await worker.tick()
    assert sales.get(p)["status"] == "awaiting_outcome_evidence"
    assert sales.get(p)["owner"] == ("human_review" if human_required else "agent")


def test_database_enforces_tenant_foreign_keys_and_forced_row_security(sales, business):
    from psycopg2.errors import ForeignKeyViolation

    p = prospect(sales)
    identity = practice(business)
    with pytest.raises(ForeignKeyViolation):
        with sales.ops.transaction() as cur:
            cur.execute(
                "INSERT INTO sales_customer_bindings(tenant_id,observation_id,prospect_id,reviewed_revision,identity_hash,status,reviewed_by,reason) VALUES(%s,%s,%s,'v1','hash','confirmed','operator:test','Reviewed evidence')",
                ("foreign-tenant", identity, p),
            )
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT relrowsecurity,relforcerowsecurity FROM pg_class WHERE relname=ANY(%s)",
            (
                [
                    "sales_business_observations",
                    "sales_business_observation_history",
                    "sales_customer_bindings",
                ],
            ),
        )
        flags = list(cur.fetchall())
        assert len(flags) == 3
        assert all(row["relrowsecurity"] and row["relforcerowsecurity"] for row in flags)
