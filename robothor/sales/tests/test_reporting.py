"""Measured feedback survives restarts without granting policy or send authority."""

import json
from types import SimpleNamespace

import pytest

from robothor.operations.store import Conflict
from robothor.sales.tests.test_guards import prepared


def reports(sales):
    from robothor.sales.reporting import Reports

    return Reports(sales)


def test_snapshot_separates_observed_funnel_actual_cost_and_unsettled_reservations(sales):
    p = prepared(sales)
    sales.ops.set_budget("sales:month:test", 1000)
    sales.ops.set_budget("sales:day:test", 1000)
    ids = sales.ops.reserve_many(
        {"sales:month:test": 1000, "sales:day:test": 1000}, "same-cost", 100
    )
    for reservation in ids:
        sales.ops.settle(reservation, 30)
    sales.ops.reserve_many({"sales:month:test": 1000, "sales:day:test": 1000}, "held-cost", 50)
    data = reports(sales).capture()
    assert data["supporting_counts"]["observed_businesses"] == 1
    assert data["supporting_counts"]["accepted_businesses"] == 1
    assert data["costs"]["actual_units"] == 30
    assert data["costs"]["reserved_units"] == 50
    assert data["supporting_counts"]["first_fulfilled"] == 0
    assert data["supporting_counts"]["retained_30_day"] is None
    assert data["cohorts"][0]["buying_case"] == "network_access"
    assert str(p["id"]) not in json.dumps(data)  # Aggregate-only agent dataset.


@pytest.mark.asyncio
async def test_native_analyst_report_is_persisted_and_does_not_activate_proposals(sales):
    from robothor.sales.reporting import AnalystWorker

    prepared(sales)
    sales.configure(
        {
            "agents": {"analyst": "analyst-agent"},
            "monthly_limit_units": 5_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )

    class Runner:
        async def run(self, **kwargs):
            self.request = kwargs
            context = json.loads(kwargs["message"])["untrusted_business_data"]
            return SimpleNamespace(
                id="analysis-run",
                status="completed",
                total_cost_usd=0.02,
                output_text=json.dumps(
                    {
                        "cohort_summary": "One observed business; conversion is not yet measured.",
                        "limitations": context["dataset"]["limitations"],
                        "proposed_changes": [
                            {
                                "proposal": "Review evidence coverage before increasing volume",
                                "metric_paths": ["supporting_counts.observed_businesses"],
                            }
                        ],
                        "supporting_counts": context["dataset"]["supporting_counts"],
                    }
                ),
            )

    runner = Runner()
    worker = AnalystWorker(sales, runner=runner)
    before = sales.settings()
    assert await worker.tick()
    report = reports(sales).latest()
    assert report["analysis"]["proposed_changes"]
    assert report["dataset_hash"]
    assert runner.request["agent_id"] == "analyst-agent"
    assert sales.settings() == before
    assert sales.ops.claim_action() is None
    assert not await worker.tick()  # At most one automatic report per week.


def test_analyst_cannot_change_counts_or_activate_settings(sales):
    from robothor.sales.reporting import Analysis, validate_analysis

    data = reports(sales).capture()
    output = {
        "cohort_summary": "No observed customers",
        "limitations": ["No history"],
        "proposed_changes": [],
        "supporting_counts": data["supporting_counts"] | {"first_fulfilled": 99},
    }
    with pytest.raises(Conflict):
        validate_analysis(Analysis.model_validate(output), data)
    with pytest.raises(ValueError):
        Analysis.model_validate(output | {"active_policy": "v2"})


def test_disabled_analysis_never_queues_or_spends_and_reports_are_tenant_scoped(sales):
    sales.configure(
        {"research_enabled": False, "agents": {"analyst": "analyst-agent"}}, "operator:test"
    )
    assert reports(sales).plan() is None
    assert reports(sales).latest() is None
    assert sales.overview()["budgets"] == []


def test_analyst_stage_is_bound_to_the_native_queue_contract():
    from robothor.sales.models import SalesSettings
    from robothor.sales.tool_schemas import QueueArgs

    assert QueueArgs(stage="analyst").stage == "analyst"
    assert (
        SalesSettings(workflow_bindings={"analyst": "sales-analysis"}).workflow_bindings["analyst"]
        == "sales-analysis"
    )


@pytest.mark.asyncio
async def test_analyst_checkpoint_recovery_reuses_the_measured_dataset_without_rebuying(sales):
    from robothor.sales.models import SalesSettings
    from robothor.sales.reporting import AnalystWorker
    from robothor.sales.tests.test_runtime import RunnerStub

    prepared(sales)
    sales.configure(
        {
            "agents": {"analyst": "analyst-agent"},
            "monthly_limit_units": 5_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )
    reports(sales).plan()
    job = sales.ops.claim("sales.analyst", lease_seconds=360)
    dataset = job["payload"]["dataset"]
    from robothor.sales.reporting import Analysis

    response = {
        "cohort_summary": "One observed practice",
        "limitations": dataset["limitations"],
        "proposed_changes": [],
        "supporting_counts": dataset["supporting_counts"],
    }
    runner = RunnerStub(response)
    worker = AnalystWorker(sales, runner=runner)
    await worker._generate(
        job,
        SalesSettings.model_validate(sales.settings()),
        "analyst",
        Analysis,
        {"dataset": dataset},
        "Analyze observed counts",
    )
    sales.ops.defer(job["id"], job["lease_token"], "Restart after checkpoint", delay_seconds=1)
    with sales.ops.transaction() as cur:
        cur.execute("UPDATE operation_jobs SET available_at=now() WHERE id=%s", (job["id"],))
    assert await AnalystWorker(sales, runner=runner).tick()
    assert len(runner.calls) == 1
    assert reports(sales).latest()["dataset"] == dataset
    from robothor.sales.service import Sales

    assert reports(Sales("another-tenant")).latest() is None


def test_native_analyst_manifest_admission_uses_report_permissions():
    from robothor.engine.models import AgentConfig
    from robothor.sales.research_manifest import prepare_research

    config = AgentConfig(
        id="analyst",
        name="Analyst",
        service_role="sales_analyst",
        tools_allowed=["sales_get_report"],
        can_spawn_agents=False,
    )
    assert prepare_research(config, None, "analyst", "fixture", "{}") == (None, "{}")
    config.tools_allowed = ["web_fetch"]
    with pytest.raises(Conflict):
        prepare_research(config, None, "analyst", "fixture", "{}")
