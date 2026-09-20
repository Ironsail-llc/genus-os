"""Existing goal controller executes candidates through durable runtime services."""

from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("deepagents")

from bench.runtime import test_store_host
from bench.runtime.adapter import CandidateRuntime
from bench.runtime.candidates import FixtureGateway
from bench.runtime.store_host import StoreHost
from bench.runtime.test_candidate_boundaries import candidate
from robothor.goals import store
from robothor.goals.controller import GoalController
from robothor.goals.tests import test_store

private_database = test_store_host.private_database
database = test_store_host.database


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_name", ["pydantic-ai", "deepagents"])
@pytest.mark.parametrize("provider_error", [False, True])
@pytest.mark.parametrize("child", [False, True])
@pytest.mark.parametrize("token_budget", [100, None])
async def test_goal_controller_charges_candidate_success_and_uncertain_failure(
    database, monkeypatch, runtime_name, provider_error, child, token_budget
):
    tenant, connect = database
    monkeypatch.setattr(store, "get_connection", connect)
    store.set_enabled(tenant, True, "operator")
    parent = test_store.create(tenant, token_budget=token_budget, kind="long") if child else None
    goal = test_store.create(
        tenant,
        token_budget=token_budget,
        parent_goal_id=parent["id"] if parent else None,
        priority=10,
    )
    claimed, attempt = store.claim(tenant)
    gateway, calls = FixtureGateway(tenant), []

    async def factory(request):
        assert request.context.principal_id == "service:main"
        assert request.context.goal_id == goal["id"]
        return gateway

    def failure():
        if provider_error:
            raise RuntimeError("synthetic provider error")

    adapter = candidate(
        runtime_name, [("record", {"key": "report", "value": "delivered"})], calls, failure
    )
    if runtime_name == "deepagents":
        for callback in adapter.model.callbacks:
            callback.raise_error = True
    runtime = CandidateRuntime(adapter, StoreHost(factory, request_token_bound=20))
    monkeypatch.setattr(
        "robothor.engine.config.load_agent_config_or_broken", lambda *a: SimpleNamespace()
    )
    controller = GoalController(
        None, SimpleNamespace(tenant_id=tenant, manifest_dir="unused"), runtime=runtime
    )
    await controller.execute(claimed, attempt)
    assert len(calls) == 1
    assert gateway.writes == (0 if provider_error else 1)
    saved = store.get(tenant, goal["id"])
    assert saved["tokens_used"] == (20 if provider_error else 13)
    assert saved["status"] != "complete"  # A successful fixture action is not all goal criteria.
    (run,) = test_store_host.rows(connect, tenant)
    assert run["runtime_context"]["goal_id"] == goal["id"]
    assert run["runtime_context"]["attempt_id"] == attempt
    assert run["runtime_context"]["budget_id"] == (parent["id"] if parent else goal["id"])
    if parent:
        assert store.get(tenant, parent["id"])["tokens_used"] == saved["tokens_used"]
    assert run["trigger_type"] == "cron"
    assert run["trigger_detail"] == f"goal:{goal['id']}"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tokens,run_id FROM pursuit_goal_attempts WHERE tenant_id=%s AND id=%s",
            (tenant, attempt),
        )
        tokens, run_id = cur.fetchone()
        assert tokens == saved["tokens_used"] and str(run_id) == str(run["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial", ["missing-binding", "wrong-budget", "wrong-parent", "missing-bound"]
)
async def test_goal_identity_and_bound_are_required_before_candidate_admission(
    database, monkeypatch, denial
):
    from dataclasses import replace

    from robothor.engine.runtime.contracts import ExecutionContext, RunRequest
    from robothor.goals.runtime import Binding, binding

    tenant, connect = database
    monkeypatch.setattr(store, "get_connection", connect)
    store.set_enabled(tenant, True, "operator")
    goal = test_store.create(tenant, token_budget=100)
    _, attempt = store.claim(tenant)
    gateway, calls = FixtureGateway(tenant), []

    async def factory(request):
        return gateway

    adapter = candidate("pydantic-ai", [("record", {"key": "report", "value": "delivered"})], calls)
    runtime = CandidateRuntime(
        adapter, StoreHost(factory, request_token_bound=None if denial == "missing-bound" else 20)
    )
    context = ExecutionContext(
        tenant, "operator", attempt, goal_id=goal["id"], attempt_id=attempt, budget_id=goal["id"]
    )
    if denial == "wrong-budget":
        context = replace(context, budget_id="unrelated")
    if denial == "wrong-parent":
        context = replace(context, parent_goal_id="unrelated")
    token = binding.set(
        None if denial == "missing-binding" else Binding(tenant, goal["id"], attempt, 100)
    )
    try:
        with pytest.raises(ValueError, match="binding|identity|bound"):
            await runtime.run(RunRequest(context, "synthetic", "record report=delivered"))
    finally:
        binding.reset(token)
    assert not calls and gateway.writes == 0
    assert test_store_host.rows(connect, tenant) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_name", ["pydantic-ai", "deepagents"])
async def test_native_and_candidate_provider_calls_share_one_durable_allowance(
    database, monkeypatch, runtime_name
):
    import asyncio

    from bench.runtime.goal_binding import bind_candidate, bind_goal
    from bench.runtime.goal_gateway import GoalGateway
    from robothor.engine.runtime.contracts import ExecutionContext
    from robothor.engine.runtime.provider_budget import goal_completion
    from robothor.goals.runtime import Binding, binding

    tenant, connect = database
    monkeypatch.setattr(store, "get_connection", connect)
    store.set_enabled(tenant, True, "operator")
    goal = test_store.create(tenant, token_budget=1000)
    _, attempt = store.claim(tenant)
    context = ExecutionContext(
        tenant, "operator", attempt, goal_id=goal["id"], attempt_id=attempt, budget_id=goal["id"]
    )
    token = binding.set(Binding(tenant, goal["id"], attempt, 1000))
    started, release = asyncio.Event(), asyncio.Event()
    native_task = None
    try:
        ledger = await bind_goal(context, 100)
        calls = []
        adapter = candidate(
            runtime_name, [("record", {"key": "report", "value": "delivered"})], calls
        )
        bound = bind_candidate(context, adapter, 100)
        gateway = FixtureGateway(tenant)

        async def native_provider(**kwargs):
            started.set()
            await release.wait()
            return {"usage": {"total_tokens": 100}}

        native_task = asyncio.create_task(
            goal_completion(native_provider, {"messages": [], "max_tokens": 2000}, ledger)
        )
        await asyncio.wait_for(started.wait(), 2)
        assert ledger.charged == 1000
        with pytest.raises(ValueError, match="budget"):
            await bound.run(GoalGateway(gateway, ledger), tenant=tenant)
        assert not calls and gateway.writes == 0
        release.set()
        await native_task
        assert ledger.charged == 100
        result = await bound.run(GoalGateway(gateway, ledger), tenant=tenant)
        assert result["verified"] and len(calls) == gateway.writes == 1
        assert ledger.charged == 113
    finally:
        release.set()
        if native_task:
            await native_task
        binding.reset(token)
