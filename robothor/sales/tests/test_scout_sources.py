"""A scout must actually search; candidate URLs cannot come from invented tool output."""

import json
from types import SimpleNamespace

import pytest

from robothor.operations.store import Conflict
from robothor.sales.scout_sources import ScoutSources


def candidate(**changes):
    return json.dumps(
        {
            "companies": [
                {
                    "name": "Example Clinic",
                    "website": "https://clinic.example.com",
                    "source_url": "https://clinic.example.com/services",
                    "reason": "Provider business services",
                    **changes,
                }
            ]
        }
    )


def observed(sources, run="run-1", tenant="test", result=None):
    sources.observe(
        "web_search",
        {"query": "provider businesses"},
        result
        if result is not None
        else {
            "results": [{"url": "https://clinic.example.com/services", "title": "Example Clinic"}],
            "provider": "searxng",
        },
        SimpleNamespace(tenant_id=tenant, agent_id="scout", run_id=run),
    )


def test_source_proof_requires_actual_owned_search_and_validates_domains():
    sources = ScoutSources("test", "scout", 1)
    with pytest.raises(Conflict):
        sources.attest("run-1", candidate())
    observed(sources)
    batch, proof = sources.attest("run-1", candidate())
    assert len(batch.companies) == 1
    assert proof["run_id"] == "run-1"
    assert proof["observations"][0]["urls"] == ["https://clinic.example.com/services"]
    for change in (
        {"source_url": "https://invented.example.com"},
        {"website": "https://invented.example.com"},
    ):
        with pytest.raises(Conflict):
            sources.attest("run-1", candidate(**change))


@pytest.mark.parametrize("kind", ["foreign_run", "foreign_tenant", "failed", "malformed"])
def test_unowned_or_failed_search_does_not_satisfy_requirement(kind):
    sources = ScoutSources("test", "scout", 1)
    observed(
        sources,
        run="other" if kind == "foreign_run" else "run-1",
        tenant="other" if kind == "foreign_tenant" else "test",
        result={"error": "Unavailable"}
        if kind == "failed"
        else {"results": "invented"}
        if kind == "malformed"
        else None,
    )
    with pytest.raises(Conflict):
        sources.attest("run-1", candidate())


def test_real_empty_search_allows_empty_batch_but_not_fabricated_candidates():
    sources = ScoutSources("test", "scout", 1)
    observed(sources, result={"results": [], "provider": "searxng"})
    assert sources.attest("run-1", '{"companies": []}')[0].companies == []
    with pytest.raises(Conflict):
        sources.attest("run-1", candidate())


def test_rejecting_observed_results_requires_an_auditable_explanation():
    sources = ScoutSources("test", "scout", 1)
    observed(sources)
    with pytest.raises(Conflict, match="explain"):
        sources.attest("run-1", '{"companies": []}')
    batch, proof = sources.attest(
        "run-1",
        json.dumps(
            {
                "companies": [],
                "empty_reason": "The listed business is outside the assigned geography; no local practice was established.",
            }
        ),
    )
    assert batch.empty_reason
    from robothor.operations.store import digest

    assert proof["output_hash"] == digest(batch.model_dump(mode="json"))


def test_degraded_search_requires_bounded_distinct_query_refinement():
    sources = ScoutSources("test", "scout", 1)
    ctx = SimpleNamespace(tenant_id="test", agent_id="scout", run_id="run-1")
    for query in ("TRT clinics", " TRT   clinics ", "TRT Florida"):
        sources.observe(
            "web_search", {"query": query}, {"results": [], "degraded": "low relevance"}, ctx
        )
        with pytest.raises(Conflict, match="Refine"):
            sources.attest("run-1", '{"companies": []}')
    sources.observe(
        "web_search",
        {"query": "testosterone clinic Texas"},
        {"results": [], "degraded": "low relevance"},
        ctx,
    )
    assert sources.attest("run-1", '{"companies": []}')[0].companies == []


def test_useful_search_can_finish_before_retry_limit():
    sources = ScoutSources("test", "scout", 1)
    observed(sources, result={"results": [], "degraded": "low relevance"})
    observed(sources)
    assert sources.attest("run-1", candidate())[0].companies


def test_batch_limit_and_duplicate_domain_checked_before_native_completion():
    sources = ScoutSources("test", "scout", 1)
    observed(sources)
    data = json.loads(candidate())
    data["companies"] *= 2
    with pytest.raises(Conflict):
        sources.attest("run-1", json.dumps(data))


@pytest.mark.asyncio
async def test_native_scout_requires_tool_call_before_schema_and_preserves_proof(monkeypatch):
    from robothor.engine.models import AgentConfig
    from robothor.engine.required_tool import tool_choice
    from robothor.engine.response_schema import response_format
    from robothor.engine.tool_observation import observe_tool_result
    from robothor.sales.runtime import NativeStageRunner

    config = AgentConfig(
        id="scout", name="Scout", tools_allowed=["web_search", "web_fetch"], max_cost_usd=1
    )
    tools = [{"type": "function", "function": {"name": "web_search"}}]

    async def execute(**kwargs):
        assert tool_choice(tools) == {"type": "function", "function": {"name": "web_search"}}
        assert response_format() is None
        ctx = SimpleNamespace(tenant_id="test", agent_id="scout", run_id="run-1")
        observe_tool_result(
            "web_search",
            {"query": "provider businesses"},
            {"results": [{"url": "https://clinic.example.com/services"}]},
            ctx,
        )
        assert tool_choice(tools) == "auto"
        assert response_format()["json_schema"]["name"] == "candidate_batch"
        return SimpleNamespace(
            id="run-1", status="completed", total_cost_usd=0, output_text=candidate()
        )

    monkeypatch.setattr("robothor.engine.config.load_agent_config", lambda *a: config)
    monkeypatch.setattr(
        "robothor.engine.tools.handlers.spawn.get_runner",
        lambda: SimpleNamespace(config=SimpleNamespace(manifest_dir="unused"), execute=execute),
    )
    result = await NativeStageRunner().run(
        agent_id="scout",
        tenant_id="test",
        correlation_id="job",
        max_cost_usd=1,
        stage="scout",
        message=json.dumps({"untrusted_business_data": {"max_companies": 1}}),
    )
    assert result.stage_provenance["discovery_sources"]["run_id"] == "run-1"
    assert tool_choice(tools) == "auto"


@pytest.mark.asyncio
async def test_scout_commit_keeps_native_source_receipt(sales):
    from robothor.sales.stages import ScoutWorker
    from robothor.sales.tests.test_runtime import RunnerStub

    class ObservedRunner(RunnerStub):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            result.stage_provenance = {"discovery_sources": {"run_id": "run-1", "fixture": True}}
            return result

    sales.configure(
        {
            "agents": {"scout": "scout"},
            "monthly_limit_units": 2000000,
            "daily_limit_units": 2000000,
        },
        "operator:test",
    )
    job_id = sales.ops.enqueue(
        "sales.scout", "one", {"segment": {"query": "provider businesses"}, "max_companies": 1}
    )
    assert await ScoutWorker(sales, ObservedRunner(json.loads(candidate()))).tick()
    receipt = sales.ops.get_job(job_id)["result"]
    assert receipt["provenance"]["discovery_sources"]["run_id"] == "run-1"
