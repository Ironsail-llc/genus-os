"""Automatic recovery is delegation, not a bypass around the spawn boundary."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.models import AgentConfig, AgentRun, RunStatus, SpawnContext, TriggerType
from robothor.engine.run_lifecycle import RunLifecycleMixin
from robothor.engine.tools.handlers import spawn


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["wrong_target", "exhausted", "too_deep"])
async def test_recovery_helper_respects_parent_admission(monkeypatch, fault):
    from robothor.engine.spawn_limits import extend_limits, try_claim

    class Runner(RunLifecycleMixin):
        pass

    runner = Runner()
    runner.config = SimpleNamespace(manifest_dir="unused")
    runner.execute = AsyncMock(
        return_value=AgentRun(
            agent_id="worker",
            trigger_type=TriggerType.SUB_AGENT,
            status=RunStatus.COMPLETED,
            output_text="diagnosis",
        )
    )
    parent = AgentConfig(id="parent", name="Parent", can_spawn_agents=True)
    child = AgentConfig(id="worker", name="Worker", tools_allowed=["web_fetch"])
    monkeypatch.setattr("robothor.engine.config.load_agent_config", lambda *a: child)
    monkeypatch.setattr(
        "robothor.engine.spawn_release.load_child_config", AsyncMock(return_value=(child, ""))
    )
    monkeypatch.setattr("robothor.engine.dedup.try_acquire", AsyncMock(return_value=True))
    monkeypatch.setattr("robothor.engine.dedup.release", AsyncMock())
    ctx = SpawnContext(
        parent_run_id="parent-run",
        parent_agent_id="parent",
        correlation_id="job",
        nesting_depth=0,
        max_nesting_depth=1,
        allowed_agents=frozenset({"worker"}),
        fleet_release_id="a" * 64,
        spawn_limits=extend_limits((), 1),
    )
    if fault == "exhausted":
        assert try_claim(ctx.spawn_limits)
    if fault == "too_deep":
        ctx.nesting_depth = 1
    token = spawn._current_spawn_context.set(ctx)
    session = SimpleNamespace(
        run=SimpleNamespace(
            user_id="service:parent",
            user_role="sales_research_agent",
            tenant_id="tenant-a",
            id="parent-run",
        )
    )
    try:
        result = await runner._spawn_recovery_helper(
            parent,
            session,
            SimpleNamespace(
                agent_id="main" if fault == "wrong_target" else "worker", message="diagnose"
            ),
        )
    finally:
        spawn._current_spawn_context.reset(token)
    assert result is None
    runner.execute.assert_not_called()


@pytest.mark.asyncio
async def test_recovery_uses_native_admission_with_the_actual_runner_and_identity(monkeypatch):
    class Runner(RunLifecycleMixin):
        pass

    runner = Runner()
    handler = AsyncMock(return_value={"status": "completed", "output_text": "diagnosis"})
    monkeypatch.setattr(spawn, "_handle_spawn_agent", handler)
    parent = AgentConfig(id="parent", name="Parent")
    session = SimpleNamespace(
        run=SimpleNamespace(
            user_id="service:parent",
            user_role="sales_research_agent",
            tenant_id="tenant-a",
            id="parent-run",
        )
    )
    result = await runner._spawn_recovery_helper(
        parent, session, SimpleNamespace(agent_id="worker", message="diagnose")
    )
    assert result == "diagnosis"
    (args,) = handler.await_args.args
    assert args == {"agent_id": "worker", "message": "diagnose", "max_iterations": 5}
    assert handler.await_args.kwargs["_runner"] is runner
    ctx = handler.await_args.kwargs["ctx"]
    assert ctx.tenant_id == "tenant-a" and ctx.user_role == "sales_research_agent"
