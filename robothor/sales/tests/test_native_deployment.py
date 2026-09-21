"""Actual coordinator, native schedules and source identity in isolated PostgreSQL."""

from unittest.mock import Mock

import pytest
import yaml

from robothor.engine.tests.test_source_identity import checkout as checkout
from robothor.operations.store import Conflict
from robothor.templates.tests.test_fleet_release import source as source
from robothor.templates.tests.test_fleet_release import spec


@pytest.fixture
async def native(sales, checkout, source, tmp_path):
    from robothor.engine.config import EngineConfig
    from robothor.engine.runtime_assets import RuntimeAssets
    from robothor.engine.sales_runtime import NativeSalesRuntime
    from robothor.engine.scheduler import CronScheduler
    from robothor.engine.source_identity import SourceIdentity
    from robothor.engine.workflow import WorkflowEngine
    from robothor.templates.fleet_release import build_release
    from robothor.templates.fleet_store import stage_release

    code, revision = checkout
    (source / "docs/workflows/process.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "process",
                "triggers": [{"type": "cron", "cron": "0 0 1 1 *", "timezone": "UTC"}],
                "steps": [
                    {
                        "id": "advance",
                        "type": "tool",
                        "tool_name": "sales_process_queue",
                        "tool_args": {"stage": "plan"},
                    }
                ],
            }
        )
    )
    (source / "config/settings.yaml").write_text(
        yaml.safe_dump({"workflow_bindings": {"plan": "process"}})
    )
    candidate = tmp_path / "candidate"
    receipt = build_release(
        source, candidate, spec(platform_revision=revision, sales_settings="config/settings.yaml")
    )
    stage_release(candidate, tmp_path, expected_digest=receipt["release_id"])
    sales.configure({}, "operator:test")
    config = EngineConfig(tenant_id=sales.tenant, workspace=tmp_path)
    instances = []

    def create():
        engine = WorkflowEngine(config, None)
        scheduler = CronScheduler(config, None, engine)
        scheduler.scheduler.start()
        runtime = NativeSalesRuntime(
            scheduler, sales, tmp_path, RuntimeAssets(SourceIdentity.capture(code), None)
        )
        instances.append(scheduler)
        return runtime

    runtime = create()
    await runtime.bootstrap()
    yield runtime, receipt["release_id"], create, code
    for scheduler in instances:
        if scheduler.scheduler.running:
            scheduler.scheduler.shutdown(wait=False)


def prepare(runtime, release):
    return runtime.coordinator.prepare(
        release,
        expected_revision=runtime.coordinator.status()["settings_revision"],
        actor="operator:test",
        reason="Install the reviewed test release",
    )


@pytest.mark.asyncio
async def test_native_commit_and_empty_baseline_rollback(native):
    runtime, release, _, _ = native
    record = await runtime.prepare(
        release,
        expected_revision=runtime.coordinator.status()["settings_revision"],
        actor="operator:test",
        reason="Install the reviewed test release",
    )
    with pytest.raises(Conflict):
        await runtime.readiness()
    committed = await runtime.commit(record["id"], actor="operator:test")
    assert committed["status"] == "committed"
    assert runtime.coordinator.sales.settings()["fleet_release_id"] == release
    assert await runtime.readiness() == "ok"
    assert runtime.schedules.verify(release, committed["runtime_evidence"]["runtime_generation"])[
        "workflows"
    ] == ["process"]
    rollback = runtime.coordinator.prepare_rollback(
        record["id"],
        expected_revision=runtime.coordinator.status()["settings_revision"],
        actor="operator:test",
        reason="Restore the original empty baseline",
    )
    await runtime.commit(rollback["id"], actor="operator:test")
    assert runtime.coordinator.sales.settings()["fleet_release_id"] is None
    assert await runtime.readiness() == "ok"
    assert runtime.native.scheduler.get_job("workflow:process") is None


@pytest.mark.asyncio
async def test_restart_reconciles_selection_and_keeps_pending_transition_closed(native):
    runtime, release, create, _ = native
    record = prepare(runtime, release)
    restarted = create()
    await restarted.bootstrap()
    with pytest.raises(Conflict):
        await restarted.readiness()
    assert restarted.native.scheduler.get_job("workflow:process") is None
    await restarted.commit(record["id"], actor="operator:test")
    selected = create()
    await selected.bootstrap()
    assert await selected.readiness() == "ok"
    assert selected.native.scheduler.get_job("workflow:process") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["source", "artifact", "schedule", "settings"])
async def test_actual_runtime_drift_cannot_produce_commit_evidence(native, fault):
    runtime, release, _, code = native
    record = prepare(runtime, release)
    await runtime.reconcile(record["id"])
    if fault == "source":
        (code / "robothor/__init__.py").write_text("changed")
    elif fault == "artifact":
        path = runtime.workspace / ".robothor/fleet-releases" / release / "brain/WORKER.md"
        path.write_text("changed")
    elif fault == "schedule":
        runtime.native.scheduler.pause()
    else:
        runtime.coordinator.sales.configure({"research_enabled": False}, "operator:test")
    with pytest.raises((Conflict, ValueError)):
        await runtime.commit(record["id"], actor="operator:test")
    assert runtime.coordinator.status()["pending"]["id"] == record["id"]
    assert runtime.coordinator.sales.settings().get("fleet_release_id") is None


@pytest.mark.asyncio
async def test_abort_restores_before_unblocking(native):
    runtime, release, _, _ = native
    record = prepare(runtime, release)
    await runtime.reconcile(record["id"])
    assert runtime.native.scheduler.get_job("workflow:process") is not None
    aborted = await runtime.abort(
        record["id"], actor="operator:test", reason="Restore before clearing this transition"
    )
    assert aborted["status"] == "aborted"
    assert runtime.native.scheduler.get_job("workflow:process") is None
    assert await runtime.readiness() == "ok"


@pytest.mark.asyncio
async def test_worker_admission_rechecks_assets_after_successful_cutover(native, monkeypatch):
    from robothor.engine.fleet_context import FleetInvocation, invocation
    from robothor.sales import queue

    runtime, release, _, code = native
    record = prepare(runtime, release)
    committed = await runtime.commit(record["id"], actor="operator:test")
    generation = committed["runtime_evidence"]["runtime_generation"]
    (code / "robothor/__init__.py").write_text("changed")
    worker = Mock()
    monkeypatch.setattr(queue.DiscoveryPlanner, "plan", worker)
    token = invocation.set(
        FleetInvocation(
            runtime.coordinator.sales.tenant,
            release,
            "process",
            lambda: runtime.verify_admission(release, generation),
        )
    )
    try:
        with pytest.raises((ValueError, Conflict)):
            await queue.QueueDriver(runtime.coordinator.sales).tick("plan", "process")
        worker.assert_not_called()
    finally:
        invocation.reset(token)


@pytest.mark.asyncio
async def test_control_completion_can_recover_unfinished_bootstrap(native):
    runtime, release, create, _ = native
    record = prepare(runtime, release)
    fresh = create()  # A startup task has not completed on this instance.
    await fresh.commit(record["id"], actor="operator:test")
    assert await fresh.readiness() == "ok"


@pytest.mark.asyncio
async def test_cancellation_waits_for_control_transaction_and_preserves_its_evidence(
    native, monkeypatch
):
    import asyncio
    import threading

    runtime, release, _, _ = native
    record = prepare(runtime, release)
    entered, resume = threading.Event(), threading.Event()
    original = runtime.coordinator.commit

    def delayed(*args, **kwargs):
        entered.set()
        assert resume.wait(timeout=5)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.coordinator, "commit", delayed)
    task = asyncio.create_task(runtime.commit(record["id"], actor="operator:test"))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert runtime._lock.locked()
        task.cancel()
    finally:
        resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.coordinator.status()["pending"] is None
    assert runtime.coordinator.sales.settings()["fleet_release_id"] == release
    assert await runtime.readiness() == "ok"


@pytest.mark.asyncio
async def test_native_preparation_refuses_unsupported_legacy_baseline_before_blocking(native):
    runtime, release, _, _ = native
    runtime.coordinator.sales.configure(
        {"workflow_bindings": {"plan": "legacy-workflow"}}, "operator:test"
    )
    with pytest.raises(Conflict, match="baseline"):
        await runtime.prepare(
            release,
            expected_revision=runtime.coordinator.status()["settings_revision"],
            actor="operator:test",
            reason="Install reviewed release over legacy fleet",
        )
    assert runtime.coordinator.status()["pending"] is None


@pytest.mark.asyncio
async def test_invalid_control_identity_cannot_reconcile_schedules(native):
    runtime, release, _, _ = native
    record = prepare(runtime, release)
    with pytest.raises(Conflict):
        await runtime.commit(record["id"], actor="agent:test")
    assert runtime.native.scheduler.get_job("workflow:process") is None
    assert runtime.coordinator.status()["pending"]["id"] == record["id"]
