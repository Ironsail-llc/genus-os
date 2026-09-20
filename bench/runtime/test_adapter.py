"""Shared request/result contracts through the installed experimental frameworks."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("deepagents")

from bench.runtime.adapter import CandidateRuntime
from bench.runtime.candidates import FixtureGateway
from bench.runtime.test_candidate_boundaries import candidate
from robothor.engine.models import AgentRun
from robothor.engine.runtime.contracts import ExecutionContext, RunRequest, StateEnvelope
from robothor.engine.runtime.current import active_context


class Host:
    def __init__(self):
        self.gateway = FixtureGateway("fixture")
        self.prepared = []
        self.results = []
        self.controls = []

    async def prepare(self, request, identity):
        self.prepared.append((request, identity))
        c = request.context
        self.run = AgentRun(
            tenant_id=c.tenant_id,
            user_id=c.principal_id,
            correlation_id=c.request_id,
            parent_run_id=c.parent_id,
            agent_id=request.agent_id,
            started_at=datetime.now(UTC),
        )
        return self.run, self.gateway

    async def bind_candidate(self, request, candidate):
        return candidate

    async def finish(self, result):
        self.results.append(result)

    async def control(self, tenant, run_id, action, note):
        if (tenant, run_id) != (self.run.tenant_id, self.run.id):
            raise ValueError("run not found in tenant")
        self.controls.append(action)
        self.gateway.stopped = True
        return {"status": "stopping"}


def request(**kwargs):
    return RunRequest(
        ExecutionContext("fixture", "operator", "request", **kwargs),
        "synthetic",
        "Store report=delivered",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_framework_returns_shared_result_with_host_identity(name):
    host, calls, events = Host(), [], []
    runtime = CandidateRuntime(
        candidate(name, [("record", {"key": "report", "value": "delivered"})], calls), host
    )

    async def on_event(event):
        events.append(event)

    result = await runtime.run(request(parent_id="parent"), on_event)
    assert result.verified and not result.unresolved
    assert str(result.run.status) == "completed"
    assert result.runtime == host.prepared[0][1] == runtime.identity
    assert result.runtime.runtime_id == name
    assert result.usage.model_calls == 1
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 3
    assert result.usage.cost_usd is None
    assert result.run.parent_run_id == "parent"
    assert host.results == [result] and host.gateway.writes == 1
    assert [e.phase for e in events] == ["accepted", "completed"]
    assert active_context.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
@pytest.mark.parametrize("outcome", ["cancel", "timeout", "error", "unverified"])
async def test_unsuccessful_execution_is_persisted_without_false_completion(name, outcome):
    host, entered = Host(), asyncio.Event()
    adapter = candidate(name, [], [])

    async def execution(*args, **kwargs):
        entered.set()
        if outcome == "error":
            raise RuntimeError("synthetic provider failure")
        if outcome == "unverified":
            return {"verified": True}  # Framework claim alone is insufficient.
        await asyncio.Event().wait()
        return {}

    adapter.run = execution
    runtime = CandidateRuntime(adapter, host)
    deadline = datetime.now(UTC) + timedelta(seconds=0.05 if outcome == "timeout" else 5)
    task = asyncio.create_task(runtime.run(request(deadline=deadline)))
    await asyncio.wait_for(entered.wait(), 2)
    if outcome == "cancel":
        with pytest.raises(ValueError, match="tenant"):
            await runtime.control("wrong", host.run.id, "cancel")
        assert not task.done()
        assert await runtime.control("fixture", host.run.id, "cancel") == {"status": "stopping"}
    result = await asyncio.wait_for(task, 2)
    assert not result.verified and result.unresolved
    expected = {"cancel": "cancelled", "timeout": "timeout"}.get(outcome, "failed")
    assert str(result.run.status) == expected
    assert result.usage.cost_usd is None and result.usage.model_calls is None
    assert host.results == [result] and host.gateway.writes == 0
    assert not runtime._active


@pytest.mark.asyncio
async def test_resume_refused_before_admission_or_model_work():
    host, calls = Host(), []
    runtime = CandidateRuntime(candidate("pydantic-ai", [], calls), host)
    original = request()
    resume = RunRequest(
        original.context,
        original.agent_id,
        original.message,
        resume_from="old-run",
        checkpoint=StateEnvelope(),
    )
    with pytest.raises(ValueError, match="replay denied"):
        await runtime.run(resume)
    assert not host.prepared and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["persistence", "identity", "deadline"])
async def test_failed_host_admission_never_starts_model(denial):
    host, calls = Host(), []
    original = host.prepare

    async def prepare(req, identity):
        if denial == "persistence":
            raise ValueError("host persistence unavailable")
        run, gateway = await original(req, identity)
        run.tenant_id = "wrong"
        return run, gateway

    host.prepare = prepare
    runtime = CandidateRuntime(candidate("pydantic-ai", [], calls), host)
    req = (
        request(deadline=datetime.now(UTC) - timedelta(seconds=1))
        if denial == "deadline"
        else request()
    )
    with pytest.raises((ValueError, TimeoutError)):
        await runtime.run(req)
    assert not calls and host.gateway.writes == 0
    assert active_context.get() is None


@pytest.mark.asyncio
async def test_progress_delivery_failure_does_not_repeat_completed_action():
    host, calls = Host(), []
    runtime = CandidateRuntime(
        candidate("pydantic-ai", [("record", {"key": "report", "value": "delivered"})], calls), host
    )

    async def broken_delivery(event):
        raise RuntimeError("synthetic delivery error")

    result = await runtime.run(request(), broken_delivery)
    assert result.verified and len(calls) == host.gateway.writes == 1
    assert host.results == [result]
