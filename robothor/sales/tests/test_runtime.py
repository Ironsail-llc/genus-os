"""Durable research execution through the native agent-runner boundary."""

import json
from types import SimpleNamespace

import pytest

from robothor.sales.runtime import DraftWorker, ResearchWorker
from robothor.sales.tests.test_service import researched


@pytest.mark.parametrize("change", [{"daily_limit_units": 0}, {"research_enabled": False}])
def test_admission_rejects_changed_settings_without_restoring_old_limits(sales, change):
    from robothor.operations.store import Conflict
    from robothor.sales.models import SalesSettings

    sales.configure(
        {
            "research_enabled": True,
            "monthly_limit_units": 10_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )
    stale = SalesSettings.model_validate(sales.settings())
    sales.ops.enqueue("sales.research", "budget-race", {})
    job = sales.ops.claim("sales.research")
    sales.configure(change, "operator:test")
    with pytest.raises(Conflict):
        ResearchWorker(sales)._reserve(stale, job)
    assert all(b["reserved_units"] == 0 for b in sales.overview()["budgets"])


def test_expired_worker_cannot_reserve_money(sales):
    from robothor.operations.store import Conflict
    from robothor.sales.models import SalesSettings

    sales.configure(
        {
            "research_enabled": True,
            "monthly_limit_units": 10_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )
    sales.ops.enqueue("sales.research", "expired", {})
    job = sales.ops.claim("sales.research")
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET lease_until=now()-interval '1 second' WHERE tenant_id=%s AND id=%s",
            (sales.tenant, job["id"]),
        )
    with pytest.raises(Conflict):
        ResearchWorker(sales)._reserve(SalesSettings.model_validate(sales.settings()), job)
    assert sales.overview()["budgets"] == []


class RunnerStub:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            id="run-1", status="completed", total_cost_usd=0.02, output_text=json.dumps(self.result)
        )


@pytest.mark.asyncio
async def test_research_commits_dossier_and_job_completion_together(sales):
    existing = researched(sales)
    p = sales.discover(
        "Another Clinic", "https://another.example.com", "https://directory.example.com"
    )
    runner = RunnerStub(sales.get(existing["id"])["dossier"])
    sales.configure(
        {
            "monthly_limit_units": 500_000_000,
            "daily_limit_units": 20_000_000,
            "agents": {"research": "research-agent"},
            "active_policy_versions": {"network_access": "1"},
        },
        "operator:test",
    )
    # Retire the helper's already researched initial job.
    first = sales.ops.claim("sales.research")
    sales.ops.complete(first["id"], first["lease_token"], {"already_researched": True})
    worker = ResearchWorker(sales, runner)
    assert await worker.tick() is True
    assert sales.get(p["id"])["status"] == "researched"
    assert len(runner.calls) == 1
    assert runner.calls[0]["tenant_id"] == sales.tenant
    assert runner.calls[0]["max_cost_usd"] > 0
    assert await worker.qualify_tick() is True
    assert await worker.qualify_tick() is True
    assert sales.get(p["id"])["qualification"]["score"] == 100
    assert await worker.tick() is False


@pytest.mark.asyncio
async def test_missing_budget_never_invokes_an_agent(sales):
    sales.discover("Example", "https://example.com", "https://directory.example.com")
    runner = RunnerStub({})
    sales.configure({"agents": {"research": "research-agent"}}, "operator:test")
    assert await ResearchWorker(sales, runner).tick() is True
    assert runner.calls == []


@pytest.mark.asyncio
async def test_research_completion_retains_child_merge_provenance(sales):
    existing = researched(sales)
    provenance = {
        "version": 1,
        "children": {"services": {"run_id": "child-run"}},
        "merged_hash": "a" * 64,
    }

    class DelegatingRunner(RunnerStub):
        async def run(self, **kwargs):
            result = await super().run(**kwargs)
            result.stage_provenance = provenance
            return result

    runner = DelegatingRunner(sales.get(existing["id"])["dossier"])
    sales.configure(
        {
            "monthly_limit_units": 500_000_000,
            "daily_limit_units": 20_000_000,
            "agents": {"research": "researcher"},
        },
        "operator:test",
    )
    first = sales.ops.claim("sales.research")
    sales.ops.complete(first["id"], first["lease_token"], {})
    prospect = sales.discover("Other", "https://other.example.com", "https://directory.example.com")
    assert await ResearchWorker(sales, runner).tick()
    assert runner.calls[0]["stage"] == "research"
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT result FROM operation_jobs WHERE tenant_id=%s AND kind='sales.research' AND payload->>'prospect_id'=%s AND status='completed'",
            (sales.tenant, str(prospect["id"])),
        )
        assert cur.fetchone()["result"]["provenance"] == provenance


@pytest.mark.asyncio
async def test_malformed_agent_output_cannot_change_company_state(sales):
    p = sales.discover("Example", "https://example.com", "https://directory.example.com")
    sales.configure(
        {
            "agents": {"research": "research-agent"},
            "monthly_limit_units": 10_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )
    runner = RunnerStub({"approve": True, "send_to": "attacker@example.com", "score": 100})
    assert await ResearchWorker(sales, runner).tick() is True
    assert sales.get(p["id"])["status"] == "discovered"
    assert sales.ops.claim_action() is None


@pytest.mark.asyncio
async def test_sdr_output_creates_one_reviewable_draft_and_never_approves(sales):
    from robothor.sales.tests.test_guards import prepared

    p = prepared(sales)
    sales.configure(
        {
            "agents": {"draft": "sdr-agent"},
            "monthly_limit_units": 10_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )
    output = {
        "recipient": "alice@example.com",
        "sender": "sales@example.com",
        "subject": "Workflow question",
        "body": "Access participating pharmacies.\nTEST POSTAL ADDRESS\nhttps://example.com/unsubscribe",
        "claim_ids": ["access"],
        "knowledge_version": "v1",
        "evidence_ids": ["service"],
    }
    job = sales.ops.enqueue("sales.draft", p["id"], {"prospect_id": p["id"]})
    worker = DraftWorker(sales, RunnerStub(output))
    assert await worker.tick() is True
    result = sales.ops.get_job(job)
    assert result["status"] == "completed"
    assert len(sales.overview()["actions"]) == 1
    assert sales.overview()["actions"][0]["status"] == "review"
    assert sales.ops.claim_action(kind="sales.email") is None
    assert await worker.tick() is False


@pytest.mark.asyncio
async def test_sdr_cannot_commit_against_changed_conversation(sales):
    from robothor.sales.tests.test_guards import prepared

    p = prepared(sales)
    sales.configure(
        {
            "agents": {"draft": "sdr-agent"},
            "monthly_limit_units": 10_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )

    class ReplyDuringRun(RunnerStub):
        async def run(self, **kwargs):
            from datetime import UTC, datetime

            sales.record_message(
                {
                    "prospect_id": p["id"],
                    "provider_id": "reply-race",
                    "direction": "inbound",
                    "sender": "alice@example.com",
                    "recipient": "sales@example.com",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "subject": "Wait",
                    "body": "A new question",
                }
            )
            return await super().run(**kwargs)

    output = {
        "recipient": "alice@example.com",
        "sender": "sales@example.com",
        "subject": "Old idea",
        "body": "Access participating pharmacies.",
        "claim_ids": ["access"],
        "knowledge_version": "v1",
        "evidence_ids": ["service"],
    }
    sales.ops.enqueue("sales.draft", p["id"], {"prospect_id": p["id"]})
    assert await DraftWorker(sales, ReplyDuringRun(output)).tick() is True
    assert sales.overview()["actions"] == []


@pytest.mark.asyncio
async def test_native_runner_funds_main_and_helper_requests_and_reports_both(monkeypatch):
    from robothor.engine.models import AgentConfig
    from robothor.engine.request_budget import bounded_completion
    from robothor.sales.runtime import NativeStageRunner

    config = AgentConfig(
        id="research-agent", name="Research", tools_allowed=["web_fetch"], max_cost_usd=1
    )

    async def quote(kwargs):
        return 100_000, kwargs

    async def provider(**kwargs):
        return SimpleNamespace(usage={"cost": "0.02"})

    async def execute(**kwargs):
        await bounded_completion(provider, model="example/model")
        await bounded_completion(provider, model="example/model")
        return SimpleNamespace(total_cost_usd=0.02)

    monkeypatch.setattr("robothor.engine.request_budget.OpenRouterQuotes", lambda: quote)
    monkeypatch.setattr("robothor.engine.config.load_agent_config", lambda *args: config)
    monkeypatch.setattr(
        "robothor.engine.tools.handlers.spawn.get_runner",
        lambda: SimpleNamespace(config=SimpleNamespace(manifest_dir="unused"), execute=execute),
    )
    result = await NativeStageRunner().run(
        agent_id=config.id,
        tenant_id="test",
        message="Research",
        correlation_id="job",
        max_cost_usd=1,
    )
    assert result.total_cost_usd == 0.04


@pytest.mark.asyncio
async def test_status_write_allowlist_requires_active_guardrail(monkeypatch):
    from unittest.mock import AsyncMock

    from robothor.engine.models import AgentConfig
    from robothor.operations.store import Conflict
    from robothor.sales.runtime import NativeStageRunner

    config = AgentConfig(
        id="research-agent",
        name="Research",
        tools_allowed=["write_file"],
        write_path_allowlist=["brain/status/*"],
    )
    execute = AsyncMock()
    monkeypatch.setattr("robothor.engine.config.load_agent_config", lambda *args: config)
    monkeypatch.setattr(
        "robothor.engine.tools.handlers.spawn.get_runner",
        lambda: SimpleNamespace(config=SimpleNamespace(manifest_dir="unused"), execute=execute),
    )
    with pytest.raises(Conflict):
        await NativeStageRunner().run(
            agent_id=config.id,
            tenant_id="test",
            message="Research",
            correlation_id="job",
            max_cost_usd=1,
        )
    execute.assert_not_called()
