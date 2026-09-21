"""Pinned fleet selection reaches the existing native runner, without fallback."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from robothor.templates.tests.test_fleet_release import source as source_fixture
from robothor.templates.tests.test_fleet_release import spec

source = source_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", [False, True])
async def test_native_sales_run_uses_the_selected_verified_release(
    source, tmp_path, monkeypatch, drift
):
    from robothor.engine.config import build_system_prompt
    from robothor.operations.store import Conflict
    from robothor.sales.runtime import NativeStageRunner
    from robothor.templates.fleet_release import build_release

    agent = source / "docs/agents/ticket-router.yaml"
    raw = yaml.safe_load(agent.read_text())
    raw["tools_allowed"] = ["web_fetch"]
    agent.write_text(yaml.safe_dump(raw))
    candidate = tmp_path / "candidate"
    receipt = build_release(source, candidate, spec())
    store = tmp_path / ".robothor/fleet-releases"
    store.mkdir(parents=True)
    artifact = store / receipt["release_id"]
    candidate.rename(artifact)
    if drift:
        (artifact / "brain/WORKER.md").write_text("Changed")
    execute = AsyncMock(return_value=SimpleNamespace(total_cost_usd=0))
    engine = SimpleNamespace(
        config=SimpleNamespace(workspace=tmp_path, manifest_dir=source / "docs/agents"),
        execute=execute,
    )
    monkeypatch.setattr("robothor.engine.tools.handlers.spawn.get_runner", lambda: engine)

    async def run():
        return await NativeStageRunner().run(
            agent_id="ticket-router",
            tenant_id="test",
            message="Research",
            correlation_id="job",
            max_cost_usd=1,
            release_id=receipt["release_id"],
        )

    if drift:
        with pytest.raises(Conflict, match="release"):
            await run()
        execute.assert_not_awaited()
    else:
        await run()
        bounded = execute.await_args.kwargs["agent_config"]
        assert bounded.fleet_release_id == receipt["release_id"]
        assert "Use the approved knowledge." in build_system_prompt(bounded, tmp_path).full_text()
        assert bounded.max_cost_usd <= 1


def test_settings_accept_only_an_explicit_sha256_release():
    from pydantic import ValidationError

    from robothor.sales.models import SalesSettings

    assert SalesSettings(fleet_release_id="a" * 64).fleet_release_id == "a" * 64
    for value in ("../escape", "", 123, "a" * 63):
        with pytest.raises(ValidationError):
            SalesSettings(fleet_release_id=value)


@pytest.mark.asyncio
async def test_research_admission_passes_release_and_checkpoints_its_provenance(sales):
    from robothor.sales.models import Dossier, SalesSettings
    from robothor.sales.runtime import ResearchWorker
    from robothor.sales.tests.test_runtime import RunnerStub
    from robothor.sales.tests.test_service import researched

    prospect = researched(sales)
    sales.configure(
        {
            "agents": {"research": "research-agent"},
            "monthly_limit_units": 10_000_000,
            "daily_limit_units": 5_000_000,
        },
        "operator:test",
    )
    # Seed the selected state for this worker-only test. Deployment admission is
    # exercised separately with real artifacts in test_deployment.py.
    from psycopg2.extras import Json

    with sales.ops.transaction() as cur:
        cur.execute(
            "UPDATE sales_settings SET config=config || %s,revision=revision+1 WHERE tenant_id=%s",
            (Json({"fleet_release_id": "a" * 64}), sales.tenant),
        )
    job = sales.ops.claim("sales.research", lease_seconds=360)
    runner = RunnerStub(sales.get(prospect["id"])["dossier"])
    settings = SalesSettings.model_validate(sales.settings())
    await ResearchWorker(sales, runner)._generate(
        job, settings, "research", Dossier, {}, "Research"
    )
    assert runner.calls[0]["release_id"] == "a" * 64
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT result FROM operation_jobs WHERE tenant_id=%s AND id=%s",
            (sales.tenant, job["id"]),
        )
        assert cur.fetchone()["result"]["fleet_release_id"] == "a" * 64
