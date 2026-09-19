"""Native source planning and read-only dispatch, using isolated PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.operations.store import Conflict
from robothor.sales.business import BusinessObservations
from robothor.sales.providers import ProviderError, RateLimited
from robothor.sales.queue import QueueDriver
from robothor.sales.tests.test_business import practice, prospect


def configure(sales, **changes):
    sales.configure(
        {
            "outcomes_enabled": True,
            "business_sources": [{"source": "orders_app", "account_id": "account-1"}],
            "workflow_bindings": {"business": "business-workflow"},
            **changes,
        },
        "operator:test",
    )


def jobs(sales):
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT * FROM operation_jobs WHERE tenant_id=%s AND kind='sales.business' ORDER BY created_at,id",
            (sales.tenant,),
        )
        return list(cur.fetchall())


def provider(monkeypatch, read):
    from robothor.engine import services

    adapter = SimpleNamespace(business_page=AsyncMock(side_effect=read))

    def factory(tenant):
        return adapter

    def lookup(name):
        return factory if name == "sales.business.orders_app" else None

    monkeypatch.setattr(services, "get_service", lookup)
    return adapter


def empty(scan):
    return {
        **{key: scan[key] for key in ("source", "account_id", "kind", "practice_id", "after")},
        "observed_at": datetime.now(UTC).isoformat(),
        "next_cursor": None,
        "items": [],
    }


def test_concurrent_planners_bound_scopes_and_do_not_skip_unfinished_reads(sales):
    from robothor.sales.business_queue import BusinessPlanner

    configure(sales)
    business = BusinessObservations(sales)
    identity = practice(business)
    planner = BusinessPlanner(sales)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(lambda _: planner.plan(), range(4))) == 2
    assert {j["payload"]["kind"] for j in jobs(sales)} == {"practice", "signup"}
    business.bind(prospect(sales), identity, "v1", "operator:test", "Reviewed practice identity")
    assert planner.plan() == 1
    order = next(j for j in jobs(sales) if j["payload"]["kind"] == "order")
    assert order["payload"]["practice_id"] == "practice-1"
    with sales.ops.transaction() as cur:
        cur.execute("UPDATE operation_jobs SET status='failed' WHERE id=%s", (order["id"],))
    assert planner.plan() == 0  # A fresh root cannot bypass a failed cursor.


def test_planning_is_off_by_default_and_uses_current_source_account(sales):
    from robothor.sales.business_queue import BusinessPlanner

    planner = BusinessPlanner(sales)
    assert planner.plan() == 0
    configure(sales, outcomes_enabled=False)
    assert planner.plan() == 0
    configure(sales, business_sources=[{"source": "orders_app", "account_id": "account-2"}])
    assert planner.plan() == 2
    assert {j["payload"]["account_id"] for j in jobs(sales)} == {"account-2"}


@pytest.mark.asyncio
async def test_native_business_workflow_resumes_pages_then_waits_for_refresh_interval(
    sales, monkeypatch
):
    from robothor.sales.business_queue import BusinessPlanner

    configure(sales)
    calls = []

    def read(scan):
        calls.append(scan)
        result = empty(scan)
        if scan["kind"] == "practice" and scan["after"] is None:
            result.update(
                next_cursor="next-practice",
                items=[
                    {
                        "external_id": "practice-1",
                        "revision": "v1",
                        "data": {
                            "business_unit_id": "unit-1",
                            "name": "Example Practice",
                            "active": True,
                            "created_at": "2026-01-01T00:00:00Z",
                            "source_updated_at": "2026-09-01T00:00:00Z",
                        },
                    }
                ],
            )
        return result

    adapter = provider(monkeypatch, read)
    driver = QueueDriver(sales)
    for _ in range(3):
        assert await driver.tick("business", "business-workflow") == {
            "stage": "business",
            "worked": True,
        }
    assert await driver.tick("business", "business-workflow") == {
        "stage": "business",
        "worked": False,
    }
    assert adapter.business_page.await_count == 3
    assert all(j["status"] == "completed" for j in jobs(sales))
    assert calls[-1]["after"] == "next-practice"
    assert len(sales.business_records()["items"]) == 1
    future = datetime.now(UTC) + timedelta(hours=7)
    assert BusinessPlanner(sales, clock=lambda: future).plan() == 2


@pytest.mark.asyncio
async def test_pausing_during_fetch_prevents_evidence_and_cursor_commit(sales, monkeypatch):
    configure(sales)

    def read(scan):
        sales.configure({"outcomes_enabled": False}, "operator:test")
        return empty(scan)

    provider(monkeypatch, read)
    await QueueDriver(sales).tick("business", "business-workflow")
    assert all(j["status"] == "pending" for j in jobs(sales))
    assert all(j["result"] is None for j in jobs(sales))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        ProviderError("private response"),
        RateLimited(123),
        ValueError("private record"),
        RuntimeError("private adapter failure"),
    ],
)
async def test_provider_failures_are_redacted_and_retry_same_page(sales, monkeypatch, failure):
    configure(sales)
    provider(monkeypatch, lambda _: (_ for _ in ()).throw(failure))
    await QueueDriver(sales).tick("business", "business-workflow")
    attempted = next(j for j in jobs(sales) if j["error"])
    assert attempted["status"] == "pending" and attempted["payload"]["after"] is None
    assert "private" not in attempted["error"]
    if isinstance(failure, RateLimited):
        assert attempted["attempts"] == 0
        assert attempted["available_at"] > datetime.now(UTC) + timedelta(seconds=100)


@pytest.mark.asyncio
async def test_disabled_source_and_unregistered_provider_make_no_requests(sales, monkeypatch):
    from robothor.engine import services
    from robothor.sales.business_queue import BusinessPlanner, BusinessWorker

    configure(sales)
    BusinessPlanner(sales).plan()
    sales.configure({"outcomes_enabled": False}, "operator:test")
    lookup = AsyncMock()
    monkeypatch.setattr(services, "get_service", lookup)
    assert not await BusinessWorker(sales).tick()
    lookup.assert_not_called()
    configure(sales)
    monkeypatch.setattr(services, "get_service", lambda _: None)
    assert await BusinessWorker(sales).tick()
    assert all(j["status"] == "pending" for j in jobs(sales))


@pytest.mark.asyncio
async def test_queue_binding_and_old_account_are_checked_before_provider(sales, monkeypatch):
    from robothor.sales.business_queue import BusinessPlanner

    configure(sales)
    BusinessPlanner(sales).plan()
    sales.configure(
        {"business_sources": [{"source": "orders_app", "account_id": "account-2"}]}, "operator:test"
    )
    adapter = provider(monkeypatch, empty)
    with pytest.raises(Conflict):
        await QueueDriver(sales).tick("business", "wrong-workflow")
    await QueueDriver(sales).tick("business", "business-workflow")
    adapter.business_page.assert_not_awaited()


def test_duplicate_sources_and_unbounded_polling_are_rejected(sales):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        configure(sales, business_sources=[{"source": "orders_app", "account_id": "a"}] * 2)
    with pytest.raises(ValidationError):
        configure(
            sales,
            business_sources=[{"source": "orders_app", "account_id": "a", "refresh_seconds": 1}],
        )


@pytest.mark.asyncio
async def test_expired_worker_cannot_import_or_release_replacement_lease(sales, monkeypatch):
    configure(sales)

    def read(scan):
        with sales.ops.transaction() as cur:
            cur.execute(
                "UPDATE operation_jobs SET lease_token=gen_random_uuid() WHERE tenant_id=%s AND kind='sales.business' AND status='running'",
                (sales.tenant,),
            )
        return empty(scan)

    provider(monkeypatch, read)
    assert (await QueueDriver(sales).tick("business", "business-workflow"))["worked"]
    running = [j for j in jobs(sales) if j["status"] == "running"]
    assert len(running) == 1 and running[0]["result"] is None


@pytest.mark.asyncio
async def test_business_native_engine_run_uses_service_registry_and_persists_receipt(
    sales, monkeypatch, tmp_path
):
    from robothor.engine.models import RunStatus
    from robothor.engine.tools.registry import ToolRegistry
    from robothor.engine.workflow import WorkflowEngine, parse_workflow

    configure(sales)
    adapter = provider(monkeypatch, empty)
    workflow_id = "business-" + sales.tenant
    sales.configure({"workflow_bindings": {"business": workflow_id}}, "operator:test")
    engine = WorkflowEngine(
        SimpleNamespace(tenant_id=sales.tenant, workspace=tmp_path),
        SimpleNamespace(registry=ToolRegistry()),
    )
    monkeypatch.setattr(engine, "_notify_run_failure", lambda run: None)
    engine._workflows[workflow_id] = parse_workflow(
        {
            "id": workflow_id,
            "timeout_seconds": 110,
            "steps": [
                {
                    "id": "read",
                    "type": "tool",
                    "tool_name": "sales_process_queue",
                    "tool_args": {"stage": "business"},
                    "tool_timeout_seconds": 100,
                }
            ],
        }
    )
    run = await engine.execute(workflow_id, trigger_type="cron")
    assert run.status == RunStatus.COMPLETED, run.error_message
    adapter.business_page.assert_awaited_once()
    assert len([j for j in jobs(sales) if j["status"] == "completed"]) == 1
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT status FROM workflow_runs WHERE id=%s AND tenant_id=%s", (run.id, sales.tenant)
        )
        assert cur.fetchone()["status"] == "completed"
