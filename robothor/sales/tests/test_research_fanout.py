"""Fixed-topic research uses native children and a lossless evidence merge."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from robothor.engine.tools.dispatch import ToolContext
from robothor.operations.store import Conflict
from robothor.sales.models import Dossier, QualificationPolicy

TOPICS = ("services", "providers_locations", "ownership_signals")


def fragment(topic, value=True):
    return Dossier.model_validate(
        {
            "buying_case": "network_access",
            "evidence": [
                {
                    "id": "e1",
                    "field": "prescribing",
                    "value": value,
                    "url": "https://clinic.example.com/" + topic,
                    "excerpt": "Public business information",
                    "retrieved_at": "2026-09-19T00:00:00Z",
                }
            ],
            "criteria": {"prescribing": ["e1"]},
            "summary": topic,
            "unanswered": ["Unknown: " + topic],
        }
    )


def test_merge_namespaces_ids_and_preserves_contradictions_independent_of_finish_order():
    from robothor.sales.research_fanout import merge_fragments

    fragments = {t: fragment(t, t != "ownership_signals") for t in TOPICS}
    result = merge_fragments(fragments, "network_access")
    assert result == merge_fragments(dict(reversed(list(fragments.items()))), "network_access")
    assert len({e.id for e in result.evidence}) == 3
    assert len(result.criteria["prescribing"]) == 3
    assert {e.id for e in result.evidence} == set(result.criteria["prescribing"])
    assert [e.value for e in result.evidence] == [True, True, False]
    assert all(e.excerpt == "Public business information" for e in result.evidence)
    policy = QualificationPolicy(
        version="v1",
        buying_case="network_access",
        required=["prescribing"],
        weights={"prescribing": 100},
        threshold=85,
    )
    from datetime import UTC, datetime

    assert (
        policy.evaluate(result, datetime(2026, 9, 19, 1, tzinfo=UTC))["decision"]
        == "needs_research"
    )


@pytest.mark.parametrize("change", ["missing", "extra", "buying_case", "too_many"])
def test_incomplete_or_mismatched_fragments_cannot_become_a_dossier(change):
    from robothor.sales.research_fanout import merge_fragments

    parts = {t: fragment(t) for t in TOPICS}
    if change == "missing":
        del parts["services"]
    elif change == "extra":
        parts["unplanned"] = fragment("unplanned")
    elif change == "buying_case":
        parts["services"].buying_case = "different"
    else:
        for part in parts.values():
            part.evidence = [
                part.evidence[0].model_copy(update={"id": f"e{i}"}) for i in range(100)
            ]
    with pytest.raises((Conflict, ValueError)):
        merge_fragments(parts, "network_access")


def context():
    return {
        "prospect": {"id": "prospect-1", "domain": "clinic.example.com"},
        "policies": [{"kind": "qualification", "data": {"buying_case": "network_access"}}],
    }


def tool_context(**changes):
    return ToolContext(
        **(
            {
                "agent_id": "researcher",
                "tenant_id": "tenant-a",
                "user_id": "service:researcher",
                "user_role": "sales_research_agent",
            }
            | changes
        )
    )


def response(specs):
    return {
        "results": [
            {
                "agent_id": s["agent_id"],
                "run_id": "run-" + json.loads(s["message"])["topic"],
                "status": "completed",
                "output_text": fragment(json.loads(s["message"])["topic"]).model_dump_json(),
            }
            for s in specs
        ]
    }


@pytest.mark.asyncio
async def test_fanout_dispatches_fixed_topics_once_using_trusted_context(monkeypatch):
    from robothor.sales.research_fanout import ResearchFanout

    async def spawn(args, ctx):
        assert ctx.tenant_id == "tenant-a"
        assert len(args["agents"]) == 3
        for spec in args["agents"]:
            assert spec["agent_id"] == "research-worker"
            payload = json.loads(spec["message"])
            assert payload["untrusted_business_data"] == context()
            assert payload["buying_case"] == "network_access"
            assert payload["output_schema"] == Dossier.model_json_schema()
        return response(args["agents"])

    native = AsyncMock(side_effect=spawn)
    monkeypatch.setattr("robothor.engine.tools.handlers.spawn._handle_spawn_agents", native)
    fanout = ResearchFanout("researcher", "research-worker", "tenant-a", context())
    result = await fanout.run("network_access", tool_context())
    again = await fanout.run("network_access", tool_context())
    assert result == again
    assert native.await_count == 1
    assert Dossier.model_validate(result["dossier"]) == fanout.dossier
    assert set(fanout.provenance["children"]) == set(TOPICS)
    assert all(v["run_id"].startswith("run-") for v in fanout.provenance["children"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "tenant-b"},
        {"agent_id": "other"},
        {"is_benchmark": True},
        {"user_role": "owner"},
    ],
)
async def test_fanout_cannot_be_reused_by_another_identity(monkeypatch, change):
    from robothor.sales.research_fanout import ResearchFanout

    native = AsyncMock()
    monkeypatch.setattr("robothor.engine.tools.handlers.spawn._handle_spawn_agents", native)
    fanout = ResearchFanout("researcher", "research-worker", "tenant-a", context())
    with pytest.raises(Conflict):
        await fanout.run("network_access", tool_context(**change))
    native.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_or_failed_fanout_never_restarts_children(monkeypatch):
    from robothor.sales.research_fanout import ResearchFanout

    native = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("robothor.engine.tools.handlers.spawn._handle_spawn_agents", native)
    fanout = ResearchFanout("researcher", "research-worker", "tenant-a", context())
    with pytest.raises(asyncio.CancelledError):
        await fanout.run("network_access", tool_context())
    with pytest.raises(Conflict):
        await fanout.run("network_access", tool_context())
    assert native.await_count == 1
    assert fanout.dossier is None


@pytest.mark.asyncio
async def test_failed_child_and_unapproved_case_do_not_produce_qualifying_evidence(monkeypatch):
    from robothor.sales.research_fanout import ResearchFanout

    native = AsyncMock(return_value={"results": [{"status": "failed"}]})
    monkeypatch.setattr("robothor.engine.tools.handlers.spawn._handle_spawn_agents", native)
    fanout = ResearchFanout("researcher", "research-worker", "tenant-a", context())
    with pytest.raises(Conflict):
        await fanout.run("unapproved", tool_context())
    native.assert_not_called()
    with pytest.raises(Conflict):
        await fanout.run("network_access", tool_context())
    assert fanout.dossier is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_actual_native_batch_shares_budget_identity_and_cancellation(monkeypatch, cancel):
    from types import SimpleNamespace

    from robothor.engine.models import AgentConfig, AgentRun, RunStatus, SpawnContext, TriggerType
    from robothor.engine.request_budget import RequestBudget, bounded_completion, budget_scope
    from robothor.engine.spawn_limits import extend_limits
    from robothor.engine.tools.handlers import spawn
    from robothor.sales.research_fanout import ResearchFanout, research_scope

    started, stopped = [], []
    all_started, finish = asyncio.Event(), asyncio.Event()

    async def quote(kwargs):
        return 100_000, kwargs

    async def provider(**kwargs):
        started.append(kwargs["model"])
        if len(started) == 3:
            all_started.set()
        try:
            await finish.wait()
            return SimpleNamespace(usage={"cost": "0.01"})
        finally:
            stopped.append(kwargs["model"])

    async def execute(**kwargs):
        assert kwargs["tenant_id"] == "tenant-a"
        assert kwargs["user_role"] == "sales_research_agent"
        assert kwargs["spawn_context"].fleet_release_id == "a" * 64
        topic = json.loads(kwargs["message"])["topic"]
        await bounded_completion(provider, model="example/" + topic)
        return AgentRun(
            agent_id="research-worker",
            trigger_type=TriggerType.SUB_AGENT,
            status=RunStatus.COMPLETED,
            output_text=fragment(topic).model_dump_json(),
            total_cost_usd=0.01,
        )

    monkeypatch.setattr("robothor.engine.request_budget.OpenRouterQuotes", lambda: quote)
    monkeypatch.setattr(
        spawn, "get_runner", lambda: SimpleNamespace(config=SimpleNamespace(), execute=execute)
    )
    monkeypatch.setattr(spawn, "_spawn_semaphore", None)
    monkeypatch.setattr(spawn, "_spawn_limit_override", 3)
    monkeypatch.setattr(
        "robothor.engine.spawn_release.load_child_config",
        AsyncMock(
            side_effect=lambda *a: (
                AgentConfig(id="research-worker", name="Worker", tools_allowed=["web_fetch"]),
                "",
            )
        ),
    )
    monkeypatch.setattr("robothor.engine.dedup.try_acquire", AsyncMock(return_value=True))
    monkeypatch.setattr("robothor.engine.dedup.release", AsyncMock())
    spawn_ctx = SpawnContext(
        parent_run_id="parent",
        parent_agent_id="researcher",
        correlation_id="job",
        nesting_depth=0,
        max_nesting_depth=1,
        max_spawn_batch=3,
        allowed_agents=frozenset({"research-worker"}),
        fleet_release_id="a" * 64,
        spawn_limits=extend_limits((), 3),
    )
    token = spawn._current_spawn_context.set(spawn_ctx)
    budget = RequestBudget(300_000)
    fanout = ResearchFanout("researcher", "research-worker", "tenant-a", context())
    try:
        with budget_scope(budget), research_scope(fanout):
            task = asyncio.create_task(fanout.run("network_access", tool_context()))
            await asyncio.wait_for(all_started.wait(), 2)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                finish.set()
                assert "dossier" in await task
        assert len(stopped) == 3
        assert budget.charged_units == (300_000 if cancel else 30_000)
        assert fanout.closed
        assert (fanout.dossier is None) == cancel
    finally:
        spawn._current_spawn_context.reset(token)
