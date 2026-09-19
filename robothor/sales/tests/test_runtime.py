"""Durable research execution through the native agent-runner boundary."""

import json
from types import SimpleNamespace

import pytest

from robothor.sales.runtime import DraftWorker, ResearchWorker
from robothor.sales.tests.test_service import researched


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
