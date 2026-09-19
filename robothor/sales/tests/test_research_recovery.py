"""Recover a failed native research bundle without repeating completed paid topics."""

import asyncio
import json

import pytest

from robothor.operations.store import Conflict
from robothor.sales.tests.test_research_fanout import TOPICS, context, response, tool_context


def job_for(sales):
    sales.ops.enqueue("sales.research", "recovery", {})
    return sales.ops.claim("sales.research", lease_seconds=360)


async def bind(sales, job, *, release="a" * 64, data=None):
    from robothor.sales.research_fanout import ResearchFanout
    from robothor.sales.research_recovery import ResearchRecovery

    data = data or context()
    recovery = ResearchRecovery(sales.ops, job, release, "researcher", data)
    await recovery.load()
    fanout = ResearchFanout("researcher", "research-worker", sales.tenant, data)
    await recovery.bind(fanout, release_id=release)
    return fanout


def reclaim(sales, job):
    sales.ops.defer(job["id"], job["lease_token"], "retry", delay_seconds=1)
    with sales.ops.transaction() as cur:
        cur.execute("UPDATE operation_jobs SET available_at=now() WHERE id=%s", (job["id"],))
    return sales.ops.claim("sales.research", lease_seconds=360)


@pytest.mark.asyncio
async def test_only_missing_topic_runs_after_failed_bundle_and_reclaim(sales, monkeypatch):
    attempts = []

    async def spawn(args, ctx, *, _on_result):
        topics = [json.loads(s["message"])["topic"] for s in args["agents"]]
        attempts.append(topics)
        result = response(args["agents"])
        for i, item in enumerate(result["results"]):
            if len(attempts) == 1 and topics[i] == TOPICS[-1]:
                item.update(status="failed", error="retrieval failed")
            else:
                await _on_result(i, item)
        return result

    monkeypatch.setattr("robothor.engine.tools.handlers.spawn._handle_spawn_agents", spawn)
    job = job_for(sales)
    first = await bind(sales, job)
    ctx = tool_context(tenant_id=sales.tenant)
    with pytest.raises(Conflict):
        await first.run("network_access", ctx)
    assert first.dossier is None
    second = await bind(sales, reclaim(sales, job))
    output = await second.run("network_access", ctx)
    assert attempts == [list(TOPICS), [TOPICS[-1]]]
    assert len(output["dossier"]["evidence"]) == 3
    assert set(output["provenance"]["children"]) == set(TOPICS)
    assert output["provenance"]["children"]["services"]["run_id"] == "run-services"


@pytest.mark.asyncio
async def test_partial_work_survives_cancellation_and_changed_inputs_refuse_reuse(
    sales, monkeypatch
):
    saved = asyncio.Event()

    async def spawn(args, ctx, *, _on_result):
        await _on_result(0, response(args["agents"])["results"][0])
        saved.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("robothor.engine.tools.handlers.spawn._handle_spawn_agents", spawn)
    job = job_for(sales)
    first = await bind(sales, job)
    task = asyncio.create_task(first.run("network_access", tool_context(tenant_id=sales.tenant)))
    await asyncio.wait_for(saved.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    new_job = reclaim(sales, job)
    with pytest.raises(Conflict):
        await bind(sales, new_job, release="b" * 64)
    changed = context()
    changed["prospect"]["domain"] = "different.example.com"
    with pytest.raises(Conflict):
        await bind(sales, new_job, data=changed)
    resumed = await bind(sales, new_job)
    assert set(resumed.parts) == {"services"}
    assert resumed.buying_case == "network_access"


@pytest.mark.asyncio
async def test_expired_worker_cannot_save_a_completed_child(sales):
    job = job_for(sales)
    fanout = await bind(sales, job)
    fanout.buying_case = "network_access"
    await fanout.recovery.begin("network_access")
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET lease_until=now()-interval '1 second' WHERE id=%s",
            (job["id"],),
        )
    item = response(
        [{"agent_id": "research-worker", "message": json.dumps({"topic": "services"})}]
    )["results"][0]
    with pytest.raises(Conflict):
        await fanout.record("services", item)
    assert fanout.parts == {}


@pytest.mark.asyncio
async def test_stage_worker_passes_recovery_and_refuses_changed_inputs_before_spending(sales):
    from robothor.sales.models import Dossier, SalesSettings
    from robothor.sales.runtime import ResearchWorker
    from robothor.sales.tests.test_research_fanout import fragment
    from robothor.sales.tests.test_runtime import RunnerStub

    sales.configure(
        {
            "agents": {"research": "researcher"},
            "monthly_limit_units": 10000000,
            "daily_limit_units": 5000000,
        },
        "operator:test",
    )
    settings = SalesSettings.model_validate(sales.settings())
    job = job_for(sales)

    class PartialRunner(RunnerStub):
        async def run(self, **kwargs):
            recovery = kwargs["recovery"]
            from robothor.sales.research_fanout import ResearchFanout

            fanout = ResearchFanout("researcher", "research-worker", sales.tenant, context())
            await recovery.bind(fanout, release_id=kwargs["release_id"])
            await recovery.begin("network_access")
            raise Conflict("Interrupted run")

    with pytest.raises(Conflict):
        await ResearchWorker(sales, PartialRunner({}))._generate(
            job, settings, "research", Dossier, context(), "Research"
        )
    budgets_before = sales.overview()["budgets"]
    changed = context()
    changed["prospect"]["version"] = 2
    runner = RunnerStub(fragment("services").model_dump(mode="json"))
    with pytest.raises(Conflict, match="inputs"):
        await ResearchWorker(sales, runner)._generate(
            reclaim(sales, job), settings, "research", Dossier, changed, "Research"
        )
    assert not runner.calls
    assert sales.overview()["budgets"] == budgets_before


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["tenant", "agent", "context", "release"])
async def test_recovery_cannot_bind_to_another_native_stage(sales, change):
    from robothor.sales.research_fanout import ResearchFanout
    from robothor.sales.research_recovery import ResearchRecovery

    job = job_for(sales)
    recovery = ResearchRecovery(sales.ops, job, "a" * 64, "researcher", context())
    await recovery.load()
    tenant = "other" if change == "tenant" else sales.tenant
    agent = "other" if change == "agent" else "researcher"
    data = context()
    if change == "context":
        data["prospect"]["domain"] = "other.example.com"
    fanout = ResearchFanout(agent, "research-worker", tenant, data)
    with pytest.raises(Conflict):
        await recovery.bind(fanout, release_id="b" * 64 if change == "release" else "a" * 64)
