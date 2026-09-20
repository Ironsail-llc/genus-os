from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.models import AgentRun, RunStatus
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.runtime.budget import SharedBudget
from robothor.engine.runtime.contracts import StateEnvelope
from robothor.engine.runtime.current import active_context


def request(**kw):
    return RunRequest(ExecutionContext("tenant", "operator", "request"), "main", "Go", **kw)


async def test_current_preserves_native_arguments_and_does_not_claim_business_success():
    async def execute(*args, **kw):
        assert active_context.get().tenant_id == kw["tenant_id"] == "tenant"
        return AgentRun(status=RunStatus.COMPLETED)

    execute = AsyncMock(side_effect=execute)
    events = AsyncMock()
    result = await CurrentRuntime(execute).run(request(options={"readonly_mode": True}), events)
    assert result.usage.model_calls == 0 and not result.verified
    assert not result.unresolved
    assert execute.call_args.kwargs["readonly_mode"] is True
    assert events.call_args.args[0].phase == "accepted"
    assert active_context.get() is None


async def test_tenant_and_goal_binding_fail_before_execution():
    execute = AsyncMock()
    runtime = CurrentRuntime(execute)
    with pytest.raises(ValueError, match="tenant"):
        await runtime.run(request(options={"tenant_id": "another"}))
    with pytest.raises(ValueError, match="lease"):
        await runtime.run(
            RunRequest(
                ExecutionContext("tenant", "operator", "req", goal_id="g", attempt_id="a"),
                "main",
                "Go",
            )
        )
    execute.assert_not_called()


async def test_incompatible_or_missing_checkpoint_never_reexecutes():
    execute = AsyncMock()
    runtime = CurrentRuntime(execute)
    with pytest.raises(ValueError, match="incompatible"):
        await runtime.run(request(resume_from="old", checkpoint=StateEnvelope(runtime_id="other")))
    with patch("robothor.engine.checkpoint.CheckpointManager.load_latest", return_value=None):
        with pytest.raises(ValueError, match="refusing"):
            await runtime.run(request(resume_from="old", checkpoint=StateEnvelope()))
    execute.assert_not_called()


async def test_delegated_run_preserves_goal_parent_separately_from_run_parent():
    from types import SimpleNamespace

    from robothor.engine.runtime.activity import current
    from robothor.engine.runtime.current import runtime_entrypoint
    from robothor.goals.runtime import Binding, binding

    class Runner:
        config = SimpleNamespace(tenant_id="tenant")

        @runtime_entrypoint
        async def execute(
            self, agent_id, message, tenant_id=None, correlation_id=None, spawn_context=None
        ):
            context = active_context.get()
            assert context.parent_id == "parent-run"
            assert context.parent_goal_id == "parent-goal"
            assert current.get().parent_goal_id == "parent-goal"
            return AgentRun(status=RunStatus.COMPLETED)

    token = binding.set(Binding("tenant", "child-goal", "attempt"))
    try:
        await CurrentRuntime(Runner().execute).run(
            RunRequest(
                ExecutionContext(
                    "tenant",
                    "operator",
                    "request",
                    goal_id="child-goal",
                    attempt_id="attempt",
                    parent_goal_id="parent-goal",
                ),
                "main",
                "Go",
                {"spawn_context": SimpleNamespace(parent_run_id="parent-run")},
            )
        )
    finally:
        binding.reset(token)


def test_concurrent_reservations_cannot_spend_same_remainder():
    budget = SharedBudget(100)

    def reserve(i):
        try:
            budget.reserve(str(i), 60)
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=20) as workers:
        assert sum(workers.map(reserve, range(20))) == 1
    assert budget.charged == 60


def test_usage_is_idempotent_unknown_stays_charged_and_overruns_are_visible():
    budget = SharedBudget(100)
    budget.reserve("one", 60)
    budget.settle("one", None)
    assert budget.charged == 60
    budget.settle("one", 30)
    budget.settle("one", 30)
    assert budget.charged == 30
    with pytest.raises(ValueError, match="redispatch"):
        budget.reserve("one", 30)
    budget.reserve("two", 50)
    with pytest.raises(ValueError, match="exceeded"):
        budget.settle("two", 90)
    assert budget.charged == 120


async def test_goal_provider_attempts_share_reservations_and_unknown_usage_is_retained(monkeypatch):
    monkeypatch.setattr("robothor.goals.store.reserve_provider_usage", lambda *args: None)
    import asyncio

    from robothor.engine.request_budget import RequestBudgetError, bounded_completion
    from robothor.goals.runtime import Binding, binding

    current = Binding("tenant", "goal", "attempt", 2000)
    token = binding.set(current)
    started, finish = asyncio.Event(), asyncio.Event()

    async def provider(**kwargs):
        started.set()
        await finish.wait()
        return {"usage": {"total_tokens": 100}}

    try:
        first = asyncio.create_task(
            bounded_completion(
                provider, messages=[{"role": "user", "content": "Hi"}], max_tokens=1400
            )
        )
        await started.wait()
        with pytest.raises(RequestBudgetError):
            await bounded_completion(
                provider, messages=[{"role": "user", "content": "Hi"}], max_tokens=1400
            )
        finish.set()
        await first
        assert current.provider_budget.charged == 100
        await bounded_completion(AsyncMock(return_value={}), messages=[], max_tokens=1000)
        assert current.provider_budget.charged > 1000
    finally:
        binding.reset(token)


async def test_local_stop_interrupts_inflight_provider_and_cleans_activity():
    import asyncio
    from types import SimpleNamespace

    from robothor.engine.runtime.activity import register, runs, stop_local

    started = asyncio.Event()
    interrupted = []
    run = AgentRun(tenant_id="tenant")

    async def execute(**kwargs):
        register(SimpleNamespace(run=run, run_id=run.id, interrupt=interrupted.append))
        started.set()
        await asyncio.Event().wait()

    work = asyncio.create_task(CurrentRuntime(execute).run(request()))
    await started.wait()
    await asyncio.to_thread(stop_local, "tenant", run.id, "Operator stopped execution")
    with pytest.raises(asyncio.CancelledError):
        await work
    assert interrupted == ["Operator stopped execution"]
    assert runs("tenant") == []


async def test_durable_stop_denies_next_provider_or_fallback(monkeypatch):
    from types import SimpleNamespace

    from robothor.engine.request_budget import RequestBudgetError, bounded_completion
    from robothor.engine.runtime.activity import register

    provider = AsyncMock()
    monkeypatch.setattr("robothor.engine.runtime.controls.stopped", lambda *args: True)

    async def execute(**kwargs):
        run = AgentRun(tenant_id="tenant")
        register(SimpleNamespace(run=run, run_id=run.id))
        with pytest.raises(RequestBudgetError, match="Durable stop"):
            await bounded_completion(provider, messages=[])
        return run

    await CurrentRuntime(execute).run(request())
    provider.assert_not_called()
