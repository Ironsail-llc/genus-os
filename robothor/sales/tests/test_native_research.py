"""A real native parent dispatches real child runs; only remote responses are fixtures."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from robothor.db.connection import get_connection, tenant_scope
from robothor.sales.models import Dossier
from robothor.templates.tests.test_fleet_release import source as source
from robothor.templates.tests.test_fleet_release import spec


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ignore_requirement,citation_fault,fetch_tool",
    [
        (False, None, "web_fetch"),
        (True, None, "web_fetch"),
        (False, "no_fetch", "web_fetch"),
        (False, "excerpt", "web_fetch"),
        (False, "url", "web_fetch"),
        (False, None, "web_render"),
    ],
)
async def test_native_research_broker_uses_rbac_and_persists_one_parent_three_children(
    sales, source, tmp_path, monkeypatch, ignore_requirement, citation_fault, fetch_tool
):
    from litellm import ModelResponse

    from robothor.engine import model_breaker
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner
    from robothor.engine.task_registry import get_task_registry
    from robothor.engine.tools import dispatch
    from robothor.engine.tools.constants import GOAL_TOOLS
    from robothor.engine.tools.handlers import spawn, web, web_render
    from robothor.engine.tools.registry import ToolRegistry
    from robothor.sales.runtime import NativeStageRunner
    from robothor.sales.tests.test_research_fanout import TOPICS, fragment
    from robothor.templates.fleet_release import build_release
    from robothor.templates.fleet_store import stage_release

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            (
                Path(__file__).parents[3] / "crm/migrations/132_sales_research_delegation.sql"
            ).read_text()
        )
        cur.execute(
            (Path(__file__).parents[3] / "crm/migrations/134_web_render_permission.sql").read_text()
        )
        conn.commit()
    parent_path = source / "docs/agents/ticket-router.yaml"
    parent = yaml.safe_load(parent_path.read_text())
    parent.update(
        role="sales_research_agent",
        tools_allowed=["sales_research_parallel"],
        task_protocol=False,
        notification_inbox=False,
        shared_working_state=False,
    )
    parent["model"] = {
        "primary": "openrouter/test/model",
        "fallbacks": [],
        "response_format": "json_object",
    }
    parent["delivery"] = {"mode": "none"}
    parent["schedule"] = {"cron": "", "timeout_seconds": 30, "max_iterations": 5, "safety_cap": 8}
    parent["v2"] = {
        "can_spawn_agents": True,
        "spawn_allowed_agents": ["research-worker"],
        "max_spawn_total": 3,
        "max_spawn_batch": 3,
        "max_nesting_depth": 1,
        "max_cost_usd": 1,
        "hard_budget": True,
    }
    child = json.loads(json.dumps(parent))
    child.update(
        id="research-worker",
        name="Research worker",
        role="sales_agent",
        tools_allowed=[fetch_tool],
    )
    child["v2"] = {"can_spawn_agents": False, "max_cost_usd": 1, "hard_budget": True}
    parent_path.write_text(yaml.safe_dump(parent))
    (source / "docs/agents/research-worker.yaml").write_text(yaml.safe_dump(child))
    artifact = tmp_path / "candidate"
    document = build_release(
        source,
        artifact,
        spec(agents=["docs/agents/ticket-router.yaml", "docs/agents/research-worker.yaml"]),
    )
    stage_release(artifact, tmp_path, expected_digest=document["release_id"])

    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-not-a-credential")
    monkeypatch.setattr(model_breaker, "_BREAKER", model_breaker.ModelBreaker(on_open=None))
    monkeypatch.setattr(dispatch, "_handler_map", None)
    monkeypatch.setattr(spawn, "_spawn_semaphore", None)
    monkeypatch.setattr(spawn, "_spawn_limit_override", 3)
    fetched, provider_calls, ignored_answers = [], [], []

    async def fetch(args, ctx):
        assert ctx.tenant_id == sales.tenant
        assert ctx.user_role == "sales_research_agent"
        assert ctx.agent_id == "research-worker"
        fetched.append(args["url"])
        return {"url": args["url"], "content": "Public business information", "status": 200}

    async def quote(kwargs):
        return 2_000, kwargs

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        names = {tool["function"]["name"] for tool in kwargs.get("tools", [])}
        # The registry advertises common goal tools by default. The sales role
        # denies their execution; they are not research capabilities.
        names -= GOAL_TOOLS
        if not names:
            # Native planning/finalization helpers share the same funded scope.
            return ModelResponse(
                model=kwargs["model"],
                choices=[
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}
                ],
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "cost": 0.001,
                },
            )
        assert names in ({"sales_research_parallel"}, {fetch_tool})
        requests = []
        for message in kwargs["messages"]:
            if message.get("role") == "user" and isinstance(message.get("content"), str):
                try:
                    data = json.loads(message["content"])
                except ValueError:
                    continue
                if isinstance(data, dict) and "untrusted_business_data" in data:
                    requests.append(data)
        assert requests
        request = requests[0]
        tool_result = next((m for m in kwargs["messages"] if m.get("role") == "tool"), None)
        tool = None
        if names == {"sales_research_parallel"}:
            if tool_result is None:
                assert kwargs["tool_choice"] == {
                    "type": "function",
                    "function": {"name": "sales_research_parallel"},
                }
                if ignore_requirement:
                    ignored_answers.append(True)
                else:
                    tool = ("sales_research_parallel", {"buying_case": "network_access"})
            else:
                assert kwargs["tool_choice"] == "auto"
                if citation_fault is None:
                    assert "dossier" in json.loads(tool_result["content"]), tool_result

            content = '{"untrusted_parent_narrative": true}'
        else:
            assert kwargs["tool_choice"] == (
                {"type": "function", "function": {"name": fetch_tool}}
                if tool_result is None
                else "auto"
            )
            topic = request["topic"]
            if tool_result is None and citation_fault != "no_fetch":
                tool = (fetch_tool, {"url": "https://clinic.example.com/" + topic})
            elif tool_result is not None:
                assert "Public business information" in tool_result["content"]
            part = fragment(topic)
            if citation_fault == "excerpt":
                part.evidence[0].excerpt = "Made-up quotation"
            elif citation_fault == "url":
                part.evidence[0].url = "https://different.example.com/"
            content = part.model_dump_json()
        message = {"role": "assistant", "content": content if tool is None else None}
        if tool is not None:
            message["tool_calls"] = [
                {
                    "id": "call-" + str(len(provider_calls)),
                    "type": "function",
                    "function": {"name": tool[0], "arguments": json.dumps(tool[1])},
                }
            ]
        return ModelResponse(
            model=kwargs["model"],
            choices=[{"finish_reason": "tool_calls" if tool else "stop", "message": message}],
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "cost": 0.001,
            },
        )

    monkeypatch.setitem(
        web.HANDLERS if fetch_tool == "web_fetch" else web_render.HANDLERS, fetch_tool, fetch
    )
    monkeypatch.setattr("robothor.engine.request_budget.OpenRouterQuotes", lambda: quote)
    monkeypatch.setattr("litellm.acompletion", provider)
    engine = AgentRunner(
        EngineConfig(
            workspace=tmp_path, manifest_dir=source / "docs/agents", tenant_id=sales.tenant
        )
    )
    engine.registry = ToolRegistry()
    monkeypatch.setattr(spawn, "_runner_ref", engine)
    monkeypatch.setattr(spawn, "_engine_config", engine.config)
    context = {
        "prospect": {"id": "synthetic", "domain": "clinic.example.com"},
        "policies": [{"kind": "qualification", "data": {"buying_case": "network_access"}}],
    }
    from robothor.sales.research_recovery import ResearchRecovery

    sales.ops.enqueue("sales.research", "native-recovery", {})
    job = sales.ops.claim("sales.research", lease_seconds=360)
    recovery = ResearchRecovery(sales.ops, job, document["release_id"], "ticket-router", context)
    await recovery.load()
    with tenant_scope(sales.tenant):
        result = await NativeStageRunner().run(
            agent_id="ticket-router",
            tenant_id=sales.tenant,
            correlation_id=str(uuid4()),
            max_cost_usd=1,
            release_id=document["release_id"],
            stage="research",
            recovery=recovery,
            message=json.dumps(
                {"untrusted_business_data": context, "output_schema": Dossier.model_json_schema()}
            ),
        )
        await get_task_registry().drain(timeout=10)
    if ignore_requirement:
        assert ignored_answers == [True]
        assert result.status == "failed"
        assert result.output_text is None
        assert result.stage_provenance == {}
        assert fetched == []
        assert recovery.store.read(job, recovery.input_hash) == {}
        return
    if citation_fault:
        assert result.status == "failed"
        assert result.output_text is None
        assert result.stage_provenance == {}
        assert set(recovery.store.read(job, recovery.input_hash)) == {"plan"}
        assert len(fetched) == (0 if citation_fault == "no_fetch" else 3)
        return
    assert result.status == "completed", result.error_message
    assert len(Dossier.model_validate_json(result.output_text).evidence) == 3
    assert len([call for call in provider_calls if call.get("tools")]) == 8
    assert result.total_cost_usd == pytest.approx(len(provider_calls) * 0.001)
    assert set(fetched) == {"https://clinic.example.com/" + topic for topic in TOPICS}
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT id,parent_run_id,nesting_depth,tenant_id,user_role,status FROM agent_runs WHERE tenant_id=%s AND (id=%s OR parent_run_id=%s)",
            (sales.tenant, result.id, result.id),
        )
        rows = cur.fetchall()
    assert len(rows) == 4
    children = [row for row in rows if row["parent_run_id"]]
    assert len(children) == 3
    assert all(row["status"] == "completed" and row["nesting_depth"] == 1 for row in children)
    assert all(
        row["tenant_id"] == sales.tenant and row["user_role"] == "sales_research_agent"
        for row in rows
    )

    saved = recovery.store.read(job, recovery.input_hash)
    assert set(saved) == {"plan", *TOPICS}
    assert {saved[topic]["run_id"] for topic in TOPICS} == {str(row["id"]) for row in children}

    # Simulate losing the parent-stage result after all three topics were saved.
    # A fresh native parent must merge the saved children without fetching again.
    from robothor.sales.tests.test_research_recovery import reclaim

    next_job = reclaim(sales, job)
    resumed = ResearchRecovery(
        sales.ops, next_job, document["release_id"], "ticket-router", context
    )
    await resumed.load()
    with tenant_scope(sales.tenant):
        retry = await NativeStageRunner().run(
            agent_id="ticket-router",
            tenant_id=sales.tenant,
            correlation_id=str(next_job["id"]),
            max_cost_usd=1,
            release_id=document["release_id"],
            stage="research",
            recovery=resumed,
            message=json.dumps(
                {"untrusted_business_data": context, "output_schema": Dossier.model_json_schema()}
            ),
        )
        await get_task_registry().drain(timeout=10)
    assert retry.status == "completed", retry.error_message
    assert retry.id != result.id
    assert retry.stage_provenance == result.stage_provenance
    assert retry.output_text == result.output_text
    assert len(fetched) == 3
    with sales.ops.transaction() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM agent_runs WHERE tenant_id=%s AND parent_run_id=%s",
            (sales.tenant, retry.id),
        )
        assert cur.fetchone()["n"] == 0
