"""Delegation cannot leave the parent's reviewed fleet or use ambient drift."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from robothor.engine.config import build_system_prompt
from robothor.engine.models import AgentRun, RunStatus, SpawnContext
from robothor.engine.tools.handlers import spawn
from robothor.templates.fleet_release import build_release
from robothor.templates.fleet_store import stage_release
from robothor.templates.tests.test_fleet_release import source as source
from robothor.templates.tests.test_fleet_release import spec


@pytest.mark.parametrize("fault", [None, "drift", "missing_child", "missing_release"])
async def test_spawn_uses_verified_parent_release_without_ambient_fallback(
    source, tmp_path, monkeypatch, fault
):
    candidate = tmp_path / "candidate"
    document = build_release(source, candidate, spec())
    release = stage_release(candidate, tmp_path, expected_digest=document["release_id"])
    if fault == "drift":
        (release.path / "brain/WORKER.md").write_text("Unreviewed instructions")
    context = SpawnContext("parent-run", "research-parent", "work", nesting_depth=0)
    context.fleet_release_id = "c" * 64 if fault == "missing_release" else release.release_id
    execute = AsyncMock(return_value=AgentRun(agent_id="ticket-router", status=RunStatus.COMPLETED))
    engine = SimpleNamespace(
        config=SimpleNamespace(workspace=tmp_path, manifest_dir=source / "docs/agents"),
        execute=execute,
    )
    ambient = Mock(side_effect=AssertionError("Pinned delegation used ambient manifests"))
    monkeypatch.setattr(spawn, "get_runner", lambda: engine)
    monkeypatch.setattr("robothor.engine.config.load_agent_config_or_reason", ambient)
    monkeypatch.setattr("robothor.engine.dedup.try_acquire", AsyncMock(return_value=True))
    monkeypatch.setattr("robothor.engine.dedup.release", AsyncMock())
    token = spawn._current_spawn_context.set(context)
    try:
        result = await spawn._handle_spawn_agent(
            {
                "agent_id": "other-agent" if fault == "missing_child" else "ticket-router",
                "message": "Research",
            },
            agent_id="research-parent",
        )
    finally:
        spawn._current_spawn_context.reset(token)
    ambient.assert_not_called()
    if fault:
        assert "error" in result
        execute.assert_not_awaited()
    else:
        assert "error" not in result
        child = execute.await_args.kwargs["agent_config"]
        assert child.fleet_release_id == release.release_id
        assert execute.await_args.kwargs["spawn_context"].fleet_release_id == release.release_id
        assert "Use the approved knowledge." in build_system_prompt(child, tmp_path).full_text()


async def test_pinned_child_provider_calls_share_parent_request_allowance(
    source, tmp_path, monkeypatch
):
    from robothor.engine.request_budget import (
        RequestBudget,
        RequestBudgetError,
        bounded_completion,
        budget_scope,
    )

    candidate = tmp_path / "candidate"
    document = build_release(source, candidate, spec())
    stage_release(candidate, tmp_path, expected_digest=document["release_id"])
    context = SpawnContext(
        "parent-run",
        "research-parent",
        "work",
        nesting_depth=0,
        fleet_release_id=document["release_id"],
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def quote(kwargs):
        return 60, kwargs

    async def provider(**kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(usage={"cost": "0.000020"})

    async def execute(**kwargs):
        await bounded_completion(provider, model="example/model")
        return AgentRun(agent_id="ticket-router", status=RunStatus.COMPLETED)

    engine = SimpleNamespace(
        config=SimpleNamespace(workspace=tmp_path, manifest_dir=source / "docs/agents"),
        execute=execute,
    )
    monkeypatch.setattr(spawn, "get_runner", lambda: engine)
    monkeypatch.setattr("robothor.engine.dedup.try_acquire", AsyncMock(return_value=True))
    monkeypatch.setattr("robothor.engine.dedup.release", AsyncMock())
    budget = RequestBudget(100, quote=quote)
    token = spawn._current_spawn_context.set(context)
    first = None
    try:
        with budget_scope(budget):
            first = asyncio.create_task(
                spawn._handle_spawn_agent(
                    {"agent_id": "ticket-router", "message": "first topic"},
                    agent_id="research-parent",
                )
            )
            await asyncio.wait_for(started.wait(), 2)
            with pytest.raises(RequestBudgetError):
                await spawn._handle_spawn_agent(
                    {"agent_id": "ticket-router", "message": "second topic"},
                    agent_id="research-parent",
                )
            release.set()
            assert (await first)["status"] == "completed"
        assert budget.charged_units == 20
    finally:
        release.set()
        if first is not None:
            await first
        spawn._current_spawn_context.reset(token)
