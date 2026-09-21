"""Native workflow identity and deterministic discovery admission."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from robothor.operations.store import Conflict
from robothor.sales.models import QualificationPolicy
from robothor.sales.queue import DiscoveryPlanner, QueueDriver

NY = ZoneInfo("America/New_York")


def _next_monday_at_0300() -> datetime:
    """A weekday inside the discovery window that is always still ahead of us.

    `DiscoveryPlanner` stamps each queued job's deadline from THIS clock — that
    local day's `discovery_end_hour` — but `OperationStore.claim` compares the
    deadline against the DATABASE's `now()`. A hard-coded calendar date is
    therefore a fuse, not a fixture: `datetime(2026, 9, 21, 7)` made every job
    this module queues expire at 07:00 New York on 2026-09-21, and
    `test_request_allocates_only_target_slots_and_pause_prevents_commit` began
    failing the moment that hour passed — on a branch whose only change was
    being merged a day later. Anchoring to the NEXT Monday keeps the weekday
    deterministic (the planner refuses Saturday and Sunday) and keeps the
    deadline in the future on every day the suite is ever run.
    """
    today = datetime.now(NY).date()
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return datetime.combine(monday, time(3), tzinfo=NY).astimezone(UTC)


NOW = _next_monday_at_0300()  # Monday 03:00 New York, always in the future

#: Where the planner puts a job's deadline: local midnight plus the default
#: `discovery_end_hour` (7). Derived rather than written as a UTC hour so the
#: assertion survives the EDT/EST boundary.
WINDOW_END = (NOW.astimezone(NY).replace(hour=7) + timedelta(0)).astimezone(UTC)


def configure(sales, **changes):
    sales.publish_policy(
        QualificationPolicy(
            version="v1",
            buying_case="network_access",
            required=["prescribing"],
            weights={"prescribing": 100},
            threshold=80,
        ),
        "operator:test",
    )
    sales.configure(
        {
            "research_enabled": True,
            "timezone": "America/New_York",
            "daily_limit_units": 20_000_000,
            "monthly_limit_units": 500_000_000,
            "agents": {"scout": "scout-agent"},
            "active_policy_versions": {"network_access": "v1"},
            "discovery_segments": [
                {
                    "id": "clinic",
                    "buying_case": "network_access",
                    "query": "Regional widget distributors",
                },
                {
                    "id": "wellness",
                    "buying_case": "network_access",
                    "query": "US physician led wellness practices",
                },
            ],
            **changes,
        },
        "operator:test",
    )


def scouts(sales):
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT * FROM operation_jobs WHERE tenant_id=%s AND kind='sales.scout' ORDER BY dedup_key",
            (sales.tenant,),
        )
        return list(cur.fetchall())


def test_concurrent_planners_queue_one_bounded_daily_plan(sales):
    configure(sales, discovery_daily_limit=30)
    planner = DiscoveryPlanner(sales, clock=lambda: NOW)
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda _: planner.plan(), range(4)))
    assert sum(outcomes) == 1
    jobs = scouts(sales)
    assert [j["payload"]["max_companies"] for j in jobs] == [20, 10]
    assert {j["payload"]["segment"]["id"] for j in jobs} == {"clinic", "wellness"}
    assert all(j["deadline"] <= WINDOW_END for j in jobs)
    assert not planner.plan()


def test_planner_respects_backlog_and_does_not_renew_daily_plan_after_restart(sales):
    configure(sales, review_backlog_limit=2)
    sales.discover("Existing", "https://example.com", "https://directory.example.com")
    assert DiscoveryPlanner(sales, clock=lambda: NOW).plan()
    assert scouts(sales)[0]["payload"]["max_companies"] == 1
    assert not DiscoveryPlanner(sales, clock=lambda: NOW).plan()
    assert len(scouts(sales)) == 1


@pytest.mark.parametrize(
    "at",
    [
        NOW.astimezone(NY).replace(hour=14).astimezone(UTC),  # past the window
        NOW - timedelta(days=1),  # the Sunday before it
    ],
)
def test_discovery_is_not_queued_outside_weekday_research_window(sales, at):
    configure(sales)
    assert not DiscoveryPlanner(sales, clock=lambda: at).plan()
    assert scouts(sales) == []


@pytest.mark.asyncio
async def test_queue_requires_exact_configured_workflow_binding(sales):
    configure(sales)
    driver = QueueDriver(sales)
    with pytest.raises(Conflict):
        await driver.tick("research", "other-workflow")
    sales.configure({"workflow_bindings": {"research": "research-workflow"}}, "operator:test")
    with pytest.raises(Conflict):
        await driver.tick("research", "other-workflow")


@pytest.mark.asyncio
async def test_control_work_is_independent_of_slow_research_and_all_switches(sales, monkeypatch):
    from robothor.sales import queue

    sales.configure(
        {
            "research_enabled": False,
            "sending_enabled": False,
            "workflow_bindings": {"stop": "stop-workflow", "research": "research-workflow"},
        },
        "operator:test",
    )
    started, release = asyncio.Event(), asyncio.Event()

    async def research():
        started.set()
        await release.wait()
        return True

    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(queue, "ResearchWorker", lambda service: SimpleNamespace(tick=research))
    monkeypatch.setattr(queue, "StopWorker", lambda service: SimpleNamespace(tick=stop))
    driver = QueueDriver(sales)
    task = asyncio.create_task(driver.tick("research", "research-workflow"))
    await started.wait()
    assert await driver.tick("stop", "stop-workflow") == {"stage": "stop", "worked": True}
    stop.assert_awaited_once()
    release.set()
    await task


@pytest.mark.asyncio
async def test_maintenance_exclusion_blocks_queue_admission(sales, monkeypatch):
    from robothor.operations.gates import gate
    from robothor.sales import queue

    sales.configure({"workflow_bindings": {"stop": "stop-workflow"}}, "operator:test")
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(queue, "StopWorker", lambda service: SimpleNamespace(tick=stop))
    with gate(sales.ops, "sales-fleet"):
        with pytest.raises(Conflict, match="gate"):
            await QueueDriver(sales).tick("stop", "stop-workflow")
    stop.assert_not_awaited()
    assert (await QueueDriver(sales).tick("stop", "stop-workflow"))["worked"]


@pytest.mark.asyncio
async def test_running_queue_work_refuses_maintenance_until_completion(sales, monkeypatch):
    from robothor.operations.gates import gate
    from robothor.sales import queue

    sales.configure({"workflow_bindings": {"research": "research-workflow"}}, "operator:test")
    started, finish = asyncio.Event(), asyncio.Event()

    async def research():
        started.set()
        await finish.wait()
        return True

    monkeypatch.setattr(queue, "ResearchWorker", lambda service: SimpleNamespace(tick=research))
    task = asyncio.create_task(QueueDriver(sales).tick("research", "research-workflow"))
    await asyncio.wait_for(started.wait(), 2)
    try:
        with pytest.raises(Conflict, match="gate"):
            with gate(sales.ops, "sales-fleet"):
                pytest.fail("maintenance entered while research was active")
    finally:
        finish.set()
        await task
    with gate(sales.ops, "sales-fleet"):
        pass


def test_partial_settings_are_validated_against_current_configuration(sales):
    from pydantic import ValidationError

    sales.configure({"discovery_start_hour": 8, "discovery_end_hour": 10}, "operator:test")
    sales.configure({"discovery_start_hour": 9}, "operator:test")
    with pytest.raises(ValidationError):
        sales.configure({"discovery_end_hour": 8}, "operator:test")
    assert sales.settings()["discovery_end_hour"] == 10


@pytest.mark.asyncio
async def test_scout_checkpoint_reuses_paid_output_after_domain_commit_failure(sales, monkeypatch):
    from robothor.sales.stages import ScoutWorker
    from robothor.sales.tests.test_runtime import RunnerStub

    configure(sales)
    output = {
        "companies": [
            {
                "name": "Example",
                "website": "https://example.com",
                "source_url": "https://directory.example.com",
                "reason": "Prescribing practice",
            }
        ]
    }
    runner = RunnerStub(output)
    worker = ScoutWorker(sales, runner)
    job = sales.ops.enqueue(
        "sales.scout", "checkpoint", {"segment": "Example segment", "max_companies": 1}
    )
    real_commit = worker.commit

    def fail(*args):
        raise Conflict("Temporary domain conflict")

    monkeypatch.setattr(worker, "commit", fail)
    assert await worker.tick()
    assert sales.ops.get_job(job)["status"] == "pending"
    assert sales.overview()["prospects"] == []
    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE operation_jobs SET available_at=now() WHERE tenant_id=%s AND id=%s",
            (sales.tenant, job),
        )
    monkeypatch.setattr(worker, "commit", real_commit)
    assert await worker.tick()
    assert sales.ops.get_job(job)["status"] == "completed"
    assert len(runner.calls) == 1
    assert len(sales.overview()["prospects"]) == 1


@pytest.mark.asyncio
async def test_scout_cannot_exceed_its_planned_batch(sales):
    from robothor.sales.stages import ScoutWorker
    from robothor.sales.tests.test_runtime import RunnerStub

    configure(sales)
    runner = RunnerStub(
        {
            "companies": [
                {
                    "name": "Example One",
                    "website": "https://one.example.com",
                    "source_url": "https://directory.example.com",
                    "reason": "Prescribing",
                },
                {
                    "name": "Example Two",
                    "website": "https://two.example.com",
                    "source_url": "https://directory.example.com",
                    "reason": "Prescribing",
                },
            ]
        }
    )
    sales.ops.enqueue("sales.scout", "one-only", {"segment": "Example", "max_companies": 1})
    assert await ScoutWorker(sales, runner).tick()
    assert sales.overview()["prospects"] == []


@pytest.mark.asyncio
async def test_native_workflow_executes_registry_worker_and_persists_receipts(
    sales, monkeypatch, tmp_path
):
    from robothor.engine.models import RunStatus
    from robothor.engine.tools.registry import ToolRegistry
    from robothor.engine.workflow import WorkflowEngine, parse_workflow
    from robothor.sales import queue
    from robothor.sales.models import QualificationPolicy
    from robothor.sales.runtime import ResearchWorker
    from robothor.sales.tests.test_runtime import RunnerStub

    p = sales.discover(
        "Example Clinic", "https://clinic.example.com", "https://directory.example.com"
    )
    output = {
        "buying_case": "network_access",
        "criteria": {"prescribing": ["services"]},
        "evidence": [
            {
                "id": "services",
                "field": "prescribing",
                "value": True,
                "url": "https://clinic.example.com/services",
                "excerpt": "Prescription services",
                "retrieved_at": datetime.now(UTC).isoformat(),
            }
        ],
    }
    model = RunnerStub(output)
    monkeypatch.setattr(queue, "ResearchWorker", lambda service: ResearchWorker(service, model))
    binding = {stage: "test-" + stage + "-" + sales.tenant for stage in ("research", "qualify")}
    sales.publish_policy(
        QualificationPolicy(
            version="v1",
            buying_case="network_access",
            required=["prescribing"],
            weights={"prescribing": 100},
            threshold=80,
        ),
        "operator:test",
    )
    sales.configure(
        {
            "agents": {"research": "research-agent"},
            "daily_limit_units": 20_000_000,
            "monthly_limit_units": 500_000_000,
            "workflow_bindings": binding,
            "active_policy_versions": {"network_access": "v1"},
        },
        "operator:test",
    )
    engine = WorkflowEngine(
        SimpleNamespace(tenant_id=sales.tenant, workspace=tmp_path),
        SimpleNamespace(registry=ToolRegistry()),
    )
    monkeypatch.setattr(engine, "_notify_run_failure", lambda run: None)
    for stage, workflow_id in binding.items():
        engine._workflows[workflow_id] = parse_workflow(
            {
                "id": workflow_id,
                "timeout_seconds": 360,
                "steps": [
                    {
                        "id": "pump",
                        "type": "tool",
                        "tool_name": "sales_process_queue",
                        "tool_args": {"stage": stage},
                        "tool_timeout_seconds": 330,
                    }
                ],
            }
        )
        run = await engine.execute(workflow_id, trigger_type="cron")
        assert run.status == RunStatus.COMPLETED, run.error_message
        with sales.ops.transaction() as cur:
            cur.execute(
                "SELECT status,steps_completed FROM workflow_runs WHERE id=%s AND tenant_id=%s",
                (run.id, sales.tenant),
            )
            saved = cur.fetchone()
            assert saved["status"] == "completed" and saved["steps_completed"] == 1
    assert len(model.calls) == 1
    assert sales.get(p["id"])["status"] == "qualified"
    assert sales.ops.claim_action() is None


# Marked by hand: this one builds its own `Sales` rather than taking the
# `sales` fixture, because it races the FIRST creation of the settings row
# and needs a tenant that has none. The conftest marks by fixture, so it
# cannot see this one.
@pytest.mark.integration
def test_concurrent_first_configuration_preserves_both_operator_changes(monkeypatch):
    import time
    from uuid import uuid4

    from robothor.sales.models import SalesSettings
    from robothor.sales.service import Sales

    sales = Sales("test-" + uuid4().hex)
    validate = SalesSettings.model_validate

    def slow_validate(value, **kwargs):
        time.sleep(0.03)  # Hold the first-create race open after reading the old row.
        return validate(value, **kwargs)

    monkeypatch.setattr(SalesSettings, "model_validate", slow_validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda config: sales.configure(config, "operator:test"),
                [{"research_enabled": True}, {"outcomes_enabled": True}],
            )
        )
    assert sales.settings()["research_enabled"] is True
    assert sales.settings()["outcomes_enabled"] is True
