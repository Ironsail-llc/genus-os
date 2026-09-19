"""Native stage admission pins and bounds the one fixed research bundle."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.models import AgentConfig, RunStatus
from robothor.operations.store import Conflict
from robothor.sales.models import Dossier
from robothor.sales.runtime import NativeStageRunner
from robothor.sales.tests.test_research_fanout import context, response, tool_context


@pytest.fixture
def fleet(monkeypatch, tmp_path):
    parent = AgentConfig(
        id="researcher",
        name="Researcher",
        service_role="sales_research_agent",
        tools_allowed=["sales_research_parallel"],
        can_spawn_agents=True,
        spawn_allowed_agents=["research-worker"],
        max_spawn_total=3,
        max_spawn_batch=3,
        max_nesting_depth=1,
        fleet_release_id="a" * 64,
    )
    child = AgentConfig(
        id="research-worker",
        name="Worker",
        service_role="sales_agent",
        tools_allowed=["web_search", "web_fetch"],
        hard_budget=True,
        max_cost_usd=1,
        timeout_seconds=180,
        safety_cap=12,
        fleet_release_id="a" * 64,
    )
    configs = {parent.id: parent, child.id: child}
    snapshot = SimpleNamespace(agent=lambda name: deepcopy(configs[name]))
    monkeypatch.setattr(
        "robothor.templates.fleet_snapshot.load_snapshot", lambda *a, **kw: snapshot
    )
    monkeypatch.setattr("robothor.engine.config.load_agent_config", lambda *a: deepcopy(parent))
    engine = SimpleNamespace(
        config=SimpleNamespace(workspace=tmp_path, manifest_dir=tmp_path), execute=AsyncMock()
    )
    monkeypatch.setattr("robothor.engine.tools.handlers.spawn.get_runner", lambda: engine)
    return parent, child, engine


async def run(release_id="a" * 64, stage="research"):
    return await NativeStageRunner().run(
        agent_id="researcher",
        tenant_id="tenant-a",
        correlation_id="job",
        max_cost_usd=1,
        release_id=release_id,
        stage=stage,
        message=json.dumps(
            {"untrusted_business_data": context(), "output_schema": Dossier.model_json_schema()}
        ),
    )


@pytest.mark.asyncio
async def test_native_parent_commits_only_the_actual_child_merge_and_closes_scope(
    fleet, monkeypatch
):
    from robothor.engine.tools.handlers.sales import HANDLERS
    from robothor.sales.research_fanout import delegate_research

    parent, child, engine = fleet

    async def spawn(args, ctx):
        return response(args["agents"])

    native = AsyncMock(side_effect=spawn)
    monkeypatch.setattr("robothor.engine.tools.handlers.spawn._handle_spawn_agents", native)

    async def execute(**kwargs):
        assert kwargs["agent_config"].max_spawn_total == 3
        assert kwargs["agent_config"].fleet_release_id == "a" * 64
        tool = await HANDLERS["sales_research_parallel"](
            {"buying_case": "network_access"}, tool_context()
        )
        assert "dossier" in tool
        return SimpleNamespace(
            id="parent-run",
            status=RunStatus.COMPLETED,
            total_cost_usd=0,
            output_text='{"invented": "parent narrative is not committed"}',
        )

    engine.execute.side_effect = execute
    result = await run()
    dossier = Dossier.model_validate_json(result.output_text)
    assert len(dossier.evidence) == 3
    assert len(result.stage_provenance["children"]) == 3
    assert native.await_count == 1
    with pytest.raises(Conflict):
        await delegate_research("network_access", tool_context())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "unversioned",
        "other_stage",
        "no_total",
        "too_many",
        "deep",
        "unbounded_batch",
        "generic_spawn",
        "missing_target",
        "wrong_role",
        "child_write",
        "child_spawn",
        "child_continuous",
        "child_downstream",
    ],
)
async def test_unsafe_delegation_refused_before_any_native_run(fleet, fault):
    parent, child, engine = fleet
    if fault == "no_total":
        parent.max_spawn_total = 0
    if fault == "too_many":
        parent.max_spawn_total = 4
    if fault == "deep":
        parent.max_nesting_depth = 2
    if fault == "unbounded_batch":
        parent.max_spawn_batch = 0
    if fault == "generic_spawn":
        parent.tools_allowed.append("spawn_agent")
    if fault == "missing_target":
        parent.spawn_allowed_agents = []
    if fault == "wrong_role":
        parent.service_role = "service"
    if fault == "child_write":
        child.tools_allowed.append("exec")
    if fault == "child_spawn":
        child.can_spawn_agents = True
    if fault == "child_continuous":
        child.continuous = True
    if fault == "child_downstream":
        child.downstream_agents = ["other"]
    with pytest.raises(Conflict):
        await run(
            release_id=None if fault == "unversioned" else "a" * 64,
            stage="draft" if fault == "other_stage" else "research",
        )
    engine.execute.assert_not_called()


@pytest.mark.asyncio
async def test_parent_success_without_completed_children_is_not_a_successful_stage(fleet):
    parent, child, engine = fleet
    engine.execute.return_value = SimpleNamespace(
        id="parent-run", status=RunStatus.COMPLETED, total_cost_usd=0.03, output_text="{}"
    )
    result = await run()
    assert result.status == RunStatus.FAILED
    assert result.total_cost_usd == 0.03
    assert not result.output_text
