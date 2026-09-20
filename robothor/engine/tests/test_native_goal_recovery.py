"""Native runner recovery against the disposable, fully migrated goal store."""

import json
import os
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

import litellm
import psycopg2
import pytest

from robothor.engine.runner import AgentRunner
from robothor.engine.task_registry import get_task_registry
from robothor.goals import store
from robothor.goals.controller import GoalController
from robothor.goals.model import CreateGoal

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_native_goal_recovery_reconciles_before_waiting(engine_config, sample_agent_config):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires the disposable canonical migration harness")
    tenant = "native-recovery-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    engine_config = replace(engine_config, tenant_id=tenant)
    sample_agent_config.id = "main"
    sample_agent_config.task_protocol = False
    sample_agent_config.difficulty_class = "simple"
    sample_agent_config.tools_allowed = ["get_pursuit_goal", "update_pursuit_goal"]
    sample_agent_config.model_fallbacks = []
    store.set_enabled(tenant, True, "operator")
    goal = store.create(
        tenant,
        CreateGoal(
            objective="Recover synthetic work",
            success_criteria=["Reply received"],
            token_budget=1000000,
        ),
        "operator",
    )
    _, old_attempt = store.claim(tenant)
    store.heartbeat(tenant, goal["id"], old_attempt, tokens=30)
    with store.transaction() as cur:
        cur.execute(
            "UPDATE pursuit_goals SET lease_until=now()-interval '1 minute' WHERE tenant_id=%s",
            (tenant,),
        )
    recovered, attempt = store.claim(tenant)
    assert recovered["recovery_required"] and attempt != old_attempt
    calls = []

    async def provider(**kwargs):
        state = store.control(tenant, goal["id"])
        index = len(calls)
        calls.append(kwargs)
        assert index < 3, "native runner continued after the goal yielded"
        action = "reconciled" if index == 1 else "wait"
        if index < 2:
            assert state["recovery_required"]
        else:
            assert not state["recovery_required"]
        args = {
            "goal_id": goal["id"],
            "version": state["version"],
            "action": action,
            "note": "Synthetic audit checked"
            if action == "reconciled"
            else "Await synthetic reply",
        }
        if action == "wait":
            args["event_type"] = "fixture.reply"
        return litellm.ModelResponse(
            model=sample_agent_config.model_primary,
            choices=[
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"recovery_{index}",
                                "type": "function",
                                "function": {
                                    "name": "update_pursuit_goal",
                                    "arguments": json.dumps(args),
                                },
                            }
                        ],
                    },
                }
            ],
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )

    with (
        patch(
            "robothor.engine.config.load_agent_config_or_broken", return_value=sample_agent_config
        ),
        patch("litellm.acompletion", side_effect=provider),
    ):
        controller = GoalController(AgentRunner(engine_config), engine_config)
        await controller.execute(recovered, attempt)
        with patch("robothor.goals.events.capture"):
            await controller.tick()
        await get_task_registry().drain(timeout=5)
    result = store.get(tenant, goal["id"])
    assert len(calls) == 3
    assert result["status"] == "waiting", result
    assert not result["recovery_required"]
    assert result["tokens_used"] == 480
    store.finish(tenant, goal["id"], old_attempt, tokens=30)
    assert store.get(tenant, goal["id"])["tokens_used"] == 480
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT status,runtime_context FROM agent_runs WHERE tenant_id=%s", (tenant,))
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "completed"
        assert rows[0][1]["goal_id"] == goal["id"]
        assert rows[0][1]["attempt_id"] == attempt

        cur.execute(
            "SELECT s.tool_input,s.tool_output FROM agent_run_steps s JOIN agent_runs r ON r.id=s.run_id WHERE r.tenant_id=%s AND s.tool_name='update_pursuit_goal' ORDER BY s.step_number",
            (tenant,),
        )
        tools = cur.fetchall()
        assert [args["action"] for args, _ in tools] == ["wait", "reconciled", "wait"]
        assert "inspect previous run results" in json.dumps(tools[0][1])
        assert tools[1][1]["goal"]["recovery_required"] is False
        assert tools[2][1]["goal"]["status"] == "waiting"
