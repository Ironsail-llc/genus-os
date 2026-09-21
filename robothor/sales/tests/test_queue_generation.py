"""Generation checks happen inside shared admission before a worker is invoked."""

from unittest.mock import Mock

import pytest
from psycopg2.extras import Json

from robothor.operations.store import Conflict
from robothor.sales.queue import QueueDriver


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["absent", "release", "tenant", "workflow", "retired", "valid"])
async def test_managed_queue_requires_current_native_generation(sales, monkeypatch, fault):
    from robothor.engine.fleet_context import FleetInvocation, invocation
    from robothor.sales import queue

    release = "a" * 64
    sales.configure({"workflow_bindings": {"plan": "sales-work"}}, "operator:test")
    # This tests admission independently of the deployment controller.
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE sales_settings SET config=config || %s WHERE tenant_id=%s",
            (Json({"fleet_release_id": release}), sales.tenant),
        )
    worker = Mock(return_value=False)
    monkeypatch.setattr(queue.DiscoveryPlanner, "plan", worker)
    guard = Mock(side_effect=Conflict("Retired generation") if fault == "retired" else None)
    context = FleetInvocation(
        tenant="other" if fault == "tenant" else sales.tenant,
        release_id="b" * 64 if fault == "release" else release,
        workflow_id="other" if fault == "workflow" else "sales-work",
        verify_current=guard,
    )
    token = invocation.set(None if fault == "absent" else context)
    try:
        if fault == "valid":
            assert await QueueDriver(sales).tick("plan", "sales-work") == {
                "stage": "plan",
                "worked": False,
            }
            worker.assert_called_once()
            guard.assert_called_once()
        else:
            with pytest.raises(Conflict):
                await QueueDriver(sales).tick("plan", "sales-work")
            worker.assert_not_called()
    finally:
        invocation.reset(token)


@pytest.mark.asyncio
async def test_native_scheduled_workflow_reaches_governed_queue(sales, tmp_path, monkeypatch):
    import asyncio
    from copy import deepcopy
    from datetime import UTC, datetime
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from robothor.engine.config import EngineConfig
    from robothor.engine.fleet_context import invocation
    from robothor.engine.fleet_schedules import FleetSchedules
    from robothor.engine.models import RunStatus
    from robothor.engine.scheduler import CronScheduler
    from robothor.engine.tools.handlers import sales as handlers
    from robothor.engine.tools.registry import ToolRegistry
    from robothor.engine.workflow import WorkflowEngine

    release = "a" * 64
    sales.configure({"workflow_bindings": {"plan": "sales-work"}}, "operator:test")
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE sales_settings SET config=config || %s WHERE tenant_id=%s",
            (Json({"fleet_release_id": release}), sales.tenant),
        )
    monkeypatch.setattr(handlers, "Sales", lambda tenant: sales if tenant == sales.tenant else None)
    monkeypatch.setattr("robothor.engine.dedup.try_acquire", AsyncMock(return_value=True))
    monkeypatch.setattr("robothor.engine.dedup.release", AsyncMock())
    config = EngineConfig(tenant_id=sales.tenant, workspace=tmp_path)
    engine = WorkflowEngine(config, SimpleNamespace(registry=ToolRegistry()))
    for name in ("_persist_run_start", "_persist_run_end", "_persist_step", "_persist_step_start"):
        monkeypatch.setattr(engine, name, Mock())
    definition = {
        "id": "sales-work",
        "name": "Sales work",
        "triggers": [{"type": "cron", "cron": "0 0 1 1 *", "timezone": "UTC"}],
        "steps": [
            {
                "id": "advance",
                "type": "tool",
                "tool_name": "sales_process_queue",
                "tool_args": {"stage": "plan"},
            }
        ],
        "delivery": {"mode": "none"},
    }
    snapshot = SimpleNamespace(
        release_id=release,
        metadata=lambda: {"workflows": ["workflow.yaml"]},
        document=lambda _: deepcopy(definition),
    )
    native = CronScheduler(config, engine.runner, engine)
    native.scheduler.start()
    manager = FleetSchedules(native, tenant=sales.tenant)
    try:
        generation = manager.reconcile(snapshot)
        job = native.scheduler.get_job("workflow:sales-work")
        completed = asyncio.Event()
        observed = []

        def record_end(run):
            observed.append(run)
            completed.set()

        monkeypatch.setattr(engine, "_persist_run_end", record_end)
        # Let APScheduler invoke the registered callback, not the test itself.
        job.modify(next_run_time=datetime.now(UTC))
        await asyncio.wait_for(completed.wait(), timeout=5)
        assert len(observed) == 1
        run = observed[0]
        assert run.status == RunStatus.COMPLETED, run.error_message
        assert run.step_results[0].tool_output == {"stage": "plan", "worked": False}
        assert run.tenant_id == sales.tenant
        assert run.user_id == "service:workflow:sales-work"
        assert run.trigger_detail == f"fleet:{release}:{generation}"
        assert invocation.get() is None
    finally:
        native.scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_async_runtime_verification_finishes_before_worker_admission(sales, monkeypatch):
    from unittest.mock import AsyncMock

    from robothor.engine.fleet_context import FleetInvocation, invocation
    from robothor.sales import queue

    release = "a" * 64
    sales.configure({"workflow_bindings": {"plan": "sales-work"}}, "operator:test")
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE sales_settings SET config=config || %s WHERE tenant_id=%s",
            (Json({"fleet_release_id": release}), sales.tenant),
        )
    worker = Mock()
    monkeypatch.setattr(queue.DiscoveryPlanner, "plan", worker)
    verify = AsyncMock(side_effect=Conflict("Runtime files changed"))
    token = invocation.set(FleetInvocation(sales.tenant, release, "sales-work", verify))
    try:
        with pytest.raises(Conflict):
            await QueueDriver(sales).tick("plan", "sales-work")
        verify.assert_awaited_once()
        worker.assert_not_called()
    finally:
        invocation.reset(token)
