"""Real scheduler membership and retired-callback fencing without provider calls."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.workflow import WorkflowEngine
from robothor.operations.store import Conflict


def snapshot(release="a", name="sales-work"):
    document = {
        "id": name,
        "name": name,
        "triggers": [{"type": "cron", "cron": "0 0 1 1 *", "timezone": "UTC"}],
        "steps": [{"id": "advance", "type": "noop"}],
    }
    return SimpleNamespace(
        release_id=release * 64,
        metadata=lambda: {"workflows": ["docs/workflows/work.yaml"]},
        document=lambda _: deepcopy(document),
    )


@pytest.fixture
async def runtime():
    from robothor.engine.fleet_schedules import FleetSchedules
    from robothor.engine.scheduler import CronScheduler

    config = SimpleNamespace(tenant_id="test-tenant", default_timezone="UTC")
    engine = WorkflowEngine(config, None)
    engine.execute = AsyncMock()
    native = CronScheduler(config, None, workflow_engine=engine)
    scheduler = native.scheduler
    scheduler.start()
    manager = FleetSchedules(native, tenant="test-tenant")
    yield manager, native
    scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_reconcile_preserves_other_jobs_and_verifies_exact_membership(runtime):
    manager, native = runtime
    native.scheduler.add_job(lambda: None, "cron", year=2099, id="unrelated")
    generation = manager.reconcile(snapshot())
    proof = manager.verify(snapshot().release_id, generation)
    assert proof["workflows"] == ["sales-work"]
    assert native.scheduler.get_job("unrelated") is not None
    assert native.workflow_engine.get_workflow("sales-work").id == "sales-work"
    job = native.scheduler.get_job("workflow:sales-work")
    await job.func(*job.args)
    assert native.workflow_engine.execute.await_count == 1


@pytest.mark.asyncio
async def test_retired_callback_and_same_release_aba_are_rejected(runtime):
    manager, native = runtime
    first = manager.reconcile(snapshot())
    old = native.scheduler.get_job("workflow:sales-work")
    manager.reconcile(snapshot("b", "sales-new"))
    assert native.scheduler.get_job("workflow:sales-work") is None
    assert native.workflow_engine.get_workflow("sales-work") is None
    manager.reconcile(snapshot())
    with pytest.raises(Conflict):
        await old.func(*old.args)
    with pytest.raises(Conflict):
        manager.verify(snapshot().release_id, first)
    native.workflow_engine.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["paused", "missing", "args", "callback", "cron", "definition", "scheduler"]
)
async def test_verification_refuses_runtime_drift(runtime, fault):
    manager, native = runtime
    generation = manager.reconcile(snapshot())
    job = native.scheduler.get_job("workflow:sales-work")
    if fault == "paused":
        job.pause()
    elif fault == "missing":
        job.remove()
    elif fault == "args":
        job.modify(args=["wrong", "wrong", "wrong"])
    elif fault == "callback":
        job.modify(func=lambda *args: None)
    elif fault == "cron":
        job.reschedule("cron", year=2099)
    elif fault == "definition":
        native.workflow_engine.get_workflow("sales-work").name = "changed"
    else:
        native.scheduler.pause()
    with pytest.raises(Conflict):
        manager.verify(snapshot().release_id, generation)


@pytest.mark.asyncio
async def test_unowned_collision_refused_before_mutation(runtime):
    manager, native = runtime
    native.scheduler.add_job(lambda: None, "cron", year=2099, id="workflow:sales-work")
    with pytest.raises(Conflict):
        manager.reconcile(snapshot())
    assert native.workflow_engine.get_workflow("sales-work") is None
    assert len(native.scheduler.get_jobs()) == 1


@pytest.mark.asyncio
async def test_restore_empty_baseline_removes_only_owned_work(runtime):
    manager, native = runtime
    manager.reconcile(snapshot())
    generation = manager.reconcile(None)
    assert manager.verify(None, generation)["workflows"] == []
    assert native.workflow_engine.get_workflow("sales-work") is None
    assert native.scheduler.get_jobs() == []


@pytest.mark.asyncio
async def test_tenant_mismatch_refused(runtime):
    from robothor.engine.fleet_schedules import FleetSchedules

    _, native = runtime
    with pytest.raises(Conflict):
        FleetSchedules(native, tenant="another-tenant")


@pytest.mark.asyncio
async def test_mutating_installed_trigger_cannot_change_expected_definition(runtime):
    from zoneinfo import ZoneInfo

    manager, native = runtime
    generation = manager.reconcile(snapshot())
    native.scheduler.get_job("workflow:sales-work").trigger.timezone = ZoneInfo("Asia/Tokyo")
    with pytest.raises(Conflict):
        manager.verify(snapshot().release_id, generation)


@pytest.mark.asyncio
async def test_duplicate_owned_callback_is_drift(runtime):
    manager, native = runtime
    generation = manager.reconcile(snapshot())
    job = native.scheduler.get_job("workflow:sales-work")
    native.scheduler.add_job(job.func, "cron", year=2099, args=job.args, id="duplicate")
    with pytest.raises(Conflict):
        manager.verify(snapshot().release_id, generation)


@pytest.mark.asyncio
async def test_interrupted_reconcile_stays_invalid_and_can_be_retried(runtime, monkeypatch):
    manager, native = runtime
    original = native.scheduler.add_job
    monkeypatch.setattr(
        native.scheduler,
        "add_job",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("interrupted")),
    )
    with pytest.raises(RuntimeError):
        manager.reconcile(snapshot())
    with pytest.raises(Conflict):
        manager.verify(snapshot().release_id, "any")
    monkeypatch.setattr(native.scheduler, "add_job", original)
    generation = manager.reconcile(snapshot())
    assert manager.verify(snapshot().release_id, generation)["workflows"] == ["sales-work"]


@pytest.mark.asyncio
async def test_inflight_context_detects_cutover_and_is_cleared_after_cancellation(runtime):
    import asyncio

    from robothor.engine.fleet_context import assert_current, invocation

    manager, native = runtime
    entered, release = asyncio.Event(), asyncio.Event()

    async def execute(**kwargs):
        entered.set()
        await release.wait()
        await assert_current("test-tenant", snapshot().release_id, "sales-work")

    native.workflow_engine.execute.side_effect = execute
    manager.reconcile(snapshot())
    job = native.scheduler.get_job("workflow:sales-work")
    running = asyncio.create_task(job.func(*job.args))
    await entered.wait()
    manager.reconcile(snapshot())
    release.set()
    with pytest.raises(Conflict):
        await running
    assert invocation.get() is None
