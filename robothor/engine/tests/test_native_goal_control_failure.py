"""Saved goal controls remain visible when the next model turn cannot answer."""

import json
import os
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import psycopg2
import pytest

from bench.runtime.uat_server import seed_unfinished_work
from robothor.auth.deps import AuthContext
from robothor.engine.chat_delivery import final_result
from robothor.engine.llm_client import LLMClient
from robothor.engine.models import TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.task_registry import get_task_registry
from robothor.goals import store
from robothor.goals.model import CreateGoal, GoalUpdate

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("action,status", [("pause", "paused"), ("cancel", "canceled")])
@pytest.mark.parametrize("deferred", [False, True])
async def test_saved_family_control_is_reported_after_model_exhaustion(
    engine_config,
    sample_agent_config,
    monkeypatch,
    deferred,
    action,
    status,
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "goal-pause-failure-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    store.set_enabled(tenant, False, "operator")
    seed_unfinished_work(tenant, kind="long")
    (goal,) = store.list_goals(tenant)
    child = store.create(
        tenant,
        CreateGoal(
            objective="Synthetic unfinished child",
            parent_goal_id=goal["id"],
            success_criteria=["Synthetic check complete"],
        ),
        "operator",
    )
    goal = store.get(tenant, goal["id"])
    monkeypatch.setenv("ROBOTHOR_RIP_16_ENABLED", "1" if deferred else "0")
    config = replace(
        sample_agent_config,
        id="main",
        task_protocol=False,
        planning_enabled=False,
        difficulty_class="simple",
        model_fallbacks=[],
        tools_allowed=[] if deferred else ["update_pursuit_goal"],
    )
    engine = AgentRunner(replace(engine_config, tenant_id=tenant))
    assert engine.registry.should_defer(config) is deferred
    advertised = {tool["function"]["name"] for tool in engine.registry.build_for_agent(config)}
    assert "update_pursuit_goal" in advertised
    if deferred:
        assert {"get_pursuit_goal", "report_pursuit_goal", "tool_call"} <= advertised
    calls = []

    async def provider(self, messages, models, tools, **kwargs):
        calls.append(True)
        assert len(calls) <= 2
        if len(calls) == 2:
            return None
        return litellm.ModelResponse(
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "pause",
                                "type": "function",
                                "function": {
                                    "name": "update_pursuit_goal",
                                    "arguments": json.dumps(
                                        {
                                            "goal_id": goal["id"],
                                            "version": goal["version"],
                                            "action": action,
                                        }
                                    ),
                                },
                            }
                        ],
                    },
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    monkeypatch.setattr(LLMClient, "_call_llm", provider)
    monkeypatch.setattr(LLMClient, "_call_llm_streaming", provider)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    monkeypatch.setattr("robothor.goals.events.capture", lambda *a, **k: None)
    monkeypatch.setattr(
        "litellm.acompletion", AsyncMock(side_effect=AssertionError("No live provider"))
    )
    from robothor.engine.runtime.chat_control import request_key

    auth = AuthContext(tenant_id=tenant, user_id="operator", role="owner", typ="user")
    client_id = str(uuid4())
    run = await engine.execute(
        "main",
        f"{action.capitalize()} goal {goal['id']}.",
        agent_config=config,
        correlation_id=request_key(auth, "synthetic-control", client_id),
        trigger_type=TriggerType.WEBCHAT,
        tenant_id=tenant,
        user_id="operator",
        user_role="owner",
    )
    await get_task_registry().drain(timeout=5)
    assert str(run.status) == "failed" and len(calls) == 2
    paused = store.get(tenant, goal["id"])
    assert paused["status"] == store.get(tenant, child["id"])["status"] == status
    assert sorted(task["status"] for task in paused["tasks"]) == ["DONE", "TODO"]
    result = await final_result(
        run, AuthContext(tenant_id=tenant, user_id="operator", role="owner", typ="user")
    )
    assert status in result["text"].lower(), result
    assert result.get("audit_outcome") is True
    assert len(calls) == 2

    # Repeated audit reads do not repeat controls or call a provider.
    from psycopg2.extras import RealDictCursor

    from robothor.engine.chat_goal_receipts import family_goal_receipts

    auth = AuthContext(tenant_id=tenant, user_id="operator", role="owner", typ="user")

    def receipts(who=auth):
        with psycopg2.connect(dsn) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            return family_goal_receipts(cur, {"id": run.id}, who)

    saved = receipts()
    assert {r["goal_id"] for r in saved} == {goal["id"], child["id"]}
    assert all(r["status"] == status and r["action"] == action for r in saved)
    assert (
        receipts(AuthContext(tenant_id=tenant, user_id="another", role="owner", typ="user")) == []
    )
    assert (
        receipts(AuthContext(tenant_id="another", user_id="operator", role="owner", typ="user"))
        == []
    )
    assert store.control_origin.get() is None
    if action == "pause":
        store.update(
            tenant,
            goal["id"],
            GoalUpdate(action="resume", version=paused["version"]),
            "operator",
            operator=True,
        )
        assert store.get(tenant, goal["id"])["status"] == "queued"
    assert receipts() == saved  # A later control cannot rewrite this request's evidence.
    repeated = await final_result(run, auth)
    assert repeated["text"] == result["text"]
    assert len(calls) == 2

    from robothor.engine.chat_recovery import read_outcome
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.goals.tools import update_goal

    recovered = read_outcome(auth, "synthetic-control", client_id)
    assert recovered["text"] == result["text"]
    assert recovered["effects"] == saved
    assert recovered["verified"] is False  # The interrupted request is not upgraded.
    with pytest.raises(ValueError):
        await update_goal(
            {"goal_id": goal["id"], "action": action, "version": goal["version"]},
            ToolContext(
                tenant_id=tenant,
                agent_id="main",
                user_id="operator",
                user_role="owner",
                run_id=str(run.id),
            ),
        )
    assert store.control_origin.get() is None
    assert receipts() == saved  # A rejected stale control has no committed receipt.
