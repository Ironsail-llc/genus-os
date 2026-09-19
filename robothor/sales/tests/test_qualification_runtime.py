"""Configured fleets cannot bypass the independent, version-bound qualifier."""

import json

import pytest

from robothor.operations.store import Conflict
from robothor.sales.models import Dossier
from robothor.sales.runtime import ResearchWorker
from robothor.sales.tests.test_runtime import RunnerStub
from robothor.sales.tests.test_service import researched


def prepared(sales):
    original = researched(sales)
    first = sales.ops.claim("sales.qualify")
    sales.ops.complete(first["id"], first["lease_token"], {})
    p = sales.discover("Other Clinic", "https://other.example.com", "https://directory.example.com")
    sales.research(
        p["id"], Dossier.model_validate(sales.get(original["id"])["dossier"]), expected_version=0
    )
    sales.configure(
        {
            "agents": {"qualify": "qualifier-agent"},
            "active_policy_versions": {"network_access": "1"},
            "monthly_limit_units": 10_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )
    return p


def output(status="unknown"):
    return {
        "criteria": {
            "prescribing": {
                "status": status,
                "evidence_ids": ["service"],
                "explanation": "Insufficient detail about prescription care.",
            }
        },
        "research_gaps": ["Find explicit care description."],
    }


def test_configured_qualifier_cannot_be_bypassed_by_direct_scoring(sales):
    p = prepared(sales)
    with pytest.raises(Conflict, match="assessment"):
        sales.qualify(p["id"], "1")


def test_enabling_assessor_does_not_grandfather_old_boolean_qualification(sales):
    p = researched(sales)
    sales.configure({"agents": {"qualify": "qualifier-agent"}}, "operator:test")
    with pytest.raises(Conflict, match="assessment"):
        sales.accept(
            p["id"], True, "operator:test", expected_version=1, expected_policy_version="1"
        )
    with pytest.raises(Conflict, match="assessment"):
        sales.add_contact(
            p["id"],
            {
                "name": "Alice",
                "role": "Owner",
                "email": "alice@example.com",
                "source_url": "https://clinic.example.com/team",
            },
        )


@pytest.mark.asyncio
async def test_native_qualifier_decides_before_code_scores_and_preserves_original(sales):
    p = prepared(sales)
    original = sales.get(p["id"])["dossier"]
    runner = RunnerStub(output())
    assert await ResearchWorker(sales, runner).qualify_tick()
    assert len(runner.calls) == 1
    assert runner.calls[0]["stage"] == "qualify"
    context = json.loads(runner.calls[0]["message"])["untrusted_business_data"]
    assert "qualification" not in context
    assert "value" not in context["evidence"][0]
    current = sales.get(p["id"])
    assert current["qualification"]["decision"] == "needs_research"
    assert current["qualification"]["score"] == 0
    assert current["qualification"]["assessment"]["criteria"] == output()["criteria"]
    assert current["qualification"]["assessment_receipt"]["run_id"] == "run-1"
    assert current["dossier"] == original
    assert current["status"] == "needs_research"


@pytest.mark.asyncio
async def test_supported_assessment_allows_human_review_but_does_not_accept_automatically(sales):
    p = prepared(sales)
    assert await ResearchWorker(sales, RunnerStub(output("supported"))).qualify_tick()
    assert sales.get(p["id"])["status"] == "qualified"
    sales.require_assessment(p["id"])
    sales.accept(p["id"], True, "operator:test", expected_version=1, expected_policy_version="1")
    assert sales.get(p["id"])["status"] == "accepted"
    assert sales.ops.claim_action() is None


@pytest.mark.parametrize("mutation", ["foreign", "lease", "context"])
@pytest.mark.asyncio
async def test_assessment_commit_reads_fenced_database_checkpoint(sales, monkeypatch, mutation):
    from uuid import uuid4

    from robothor.sales.models import SalesSettings

    p = prepared(sales)
    worker = ResearchWorker(sales, RunnerStub(output("supported")))
    captured = {}

    def stop(job, settings):
        captured.update(job)
        raise Conflict("Simulated interruption")

    monkeypatch.setattr(worker, "_qualify", stop)
    await worker.qualify_tick()
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND id=%s",
            (sales.tenant, captured["id"]),
        )
    job = sales.ops.claim("sales.qualify", lease_seconds=360)
    if mutation == "foreign":
        with sales.ops.transaction() as cur:
            cur.execute(
                "UPDATE operation_jobs SET payload=jsonb_set(payload,'{prospect_id}',%s::jsonb) WHERE tenant_id=%s AND id=%s",
                (json.dumps(str(uuid4())), sales.tenant, job["id"]),
            )
    elif mutation == "lease":
        job["lease_token"] = str(uuid4())
    else:
        with sales.ops.transaction() as cur:
            cur.execute(
                "UPDATE operation_jobs SET result=jsonb_set(result,'{context,binding,dossier_hash}','\"changed\"') WHERE tenant_id=%s AND id=%s",
                (sales.tenant, job["id"]),
            )
    # Supplying a previously valid in-memory result cannot replace the stored proof.
    with pytest.raises(Conflict):
        ResearchWorker(sales)._qualify(job, SalesSettings.model_validate(sales.settings()))
    assert sales.get(p["id"])["qualification"] is None


@pytest.mark.parametrize("change", ["dossier", "policy", "disabled", "agent"])
@pytest.mark.asyncio
async def test_stale_assessment_cannot_commit(sales, change):
    p = prepared(sales)

    class RacingRunner(RunnerStub):
        async def run(self, **kwargs):
            if change == "dossier":
                sales.research(
                    p["id"],
                    Dossier.model_validate(sales.get(p["id"])["dossier"]),
                    expected_version=1,
                )
            else:
                mutation = {
                    "policy": {"active_policy_versions": {}},
                    "disabled": {"research_enabled": False},
                    "agent": {"agents": {"qualify": "different-agent"}},
                }[change]
                sales.configure(mutation, "operator:test")
            return await super().run(**kwargs)

    assert await ResearchWorker(sales, RacingRunner(output("supported"))).qualify_tick()
    assert sales.get(p["id"])["qualification"] is None


@pytest.mark.asyncio
async def test_saved_assessment_resumes_without_a_second_paid_call(sales, monkeypatch):
    p = prepared(sales)
    runner = RunnerStub(output())
    worker = ResearchWorker(sales, runner)
    commit = worker._qualify

    def interrupted(*args):
        raise Conflict("Simulated restart after checkpoint")

    monkeypatch.setattr(worker, "_qualify", interrupted)
    assert await worker.qualify_tick()
    assert len(runner.calls) == 1
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND kind='sales.qualify'",
            (sales.tenant,),
        )
    monkeypatch.setattr(worker, "_qualify", commit)
    assert await worker.qualify_tick()
    assert len(runner.calls) == 1
    assert sales.get(p["id"])["qualification"]["score"] == 0


@pytest.mark.parametrize("bad", [{"score": 100}, {"criteria": {}, "research_gaps": []}])
@pytest.mark.asyncio
async def test_bad_assessment_never_falls_back_to_researcher_booleans(sales, bad):
    p = prepared(sales)
    assert await ResearchWorker(sales, RunnerStub(bad)).qualify_tick()
    assert sales.get(p["id"])["qualification"] is None


@pytest.mark.asyncio
async def test_no_budget_does_not_invoke_qualifier_or_fall_back(sales):
    p = prepared(sales)
    sales.configure({"daily_limit_units": 0}, "operator:test")
    runner = RunnerStub(output("supported"))
    assert await ResearchWorker(sales, runner).qualify_tick()
    assert runner.calls == []
    assert sales.get(p["id"])["qualification"] is None


@pytest.mark.asyncio
async def test_native_stage_installs_trusted_assessment_schema_and_validation(monkeypatch):
    from types import SimpleNamespace

    from robothor.engine.models import AgentConfig
    from robothor.engine.output_validation import validated_completion
    from robothor.engine.response_schema import response_format
    from robothor.sales.qualification import assessment_context
    from robothor.sales.runtime import NativeStageRunner
    from robothor.sales.tests.test_qualification_assessment import inputs

    dossier, policy = inputs()
    context = assessment_context(dossier, policy)
    config = AgentConfig(
        id="qualifier", name="Qualifier", tools_allowed=["sales_get_context"], max_cost_usd=1
    )

    async def execute(**kwargs):
        assert response_format()["json_schema"]["name"] == "qualification_assessment"
        session = SimpleNamespace(
            run=SimpleNamespace(),
            fail=lambda error: ("failed", error),
            complete=lambda value: ("completed", value),
        )
        assert validated_completion(session, '{"score": 100}')[0] == "failed"
        bad = {"criteria": {}, "research_gaps": []}
        assert validated_completion(session, json.dumps(bad))[0] == "failed"
        good = {
            "criteria": {
                "prescribing": {
                    "status": "unknown",
                    "evidence_ids": ["menu"],
                    "explanation": "Missing explicit care",
                }
            },
            "research_gaps": [],
        }
        assert validated_completion(session, json.dumps(good))[0] == "completed"
        return SimpleNamespace(
            id="run-1", status="completed", total_cost_usd=0, output_text=json.dumps(good)
        )

    monkeypatch.setattr("robothor.engine.config.load_agent_config", lambda *a: config)
    monkeypatch.setattr(
        "robothor.engine.tools.handlers.spawn.get_runner",
        lambda: SimpleNamespace(config=SimpleNamespace(manifest_dir="unused"), execute=execute),
    )
    await NativeStageRunner().run(
        agent_id="qualifier",
        tenant_id="test",
        correlation_id="job",
        max_cost_usd=1,
        stage="qualify",
        message=json.dumps({"untrusted_business_data": context}),
    )
    assert response_format() is None
