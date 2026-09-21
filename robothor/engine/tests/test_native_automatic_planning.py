"""After optional planning expires, native execution still creates one real task."""

import asyncio
import json
import os
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import psycopg2
import pytest

from robothor.crm import dal
from robothor.engine.llm_client import LLMClient
from robothor.engine.models import TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import effects
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("suppress", [False, True])
async def test_expired_automatic_plan_preserves_native_execution(
    engine_config, sample_agent_config, monkeypatch, suppress
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "auto-plan-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    sample_agent_config.planning_enabled = False
    sample_agent_config.planning_model = ""
    sample_agent_config.difficulty_class = ""
    sample_agent_config.task_protocol = False
    sample_agent_config.tools_allowed = ["create_task"]
    sample_agent_config.model_fallbacks = ["openrouter/test/fallback"]
    engine = AgentRunner(replace(engine_config, tenant_id=tenant))
    monkeypatch.setattr("robothor.engine.runtime.automatic_planning.AUTOMATIC_PLAN_SECONDS", 0.02)
    monkeypatch.setattr("robothor.engine.runtime.action_policy.SIMPLE_ACTION_SECONDS", 1)
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    monkeypatch.setattr("robothor.goals.events.capture", lambda *a, **k: None)
    monkeypatch.setattr(
        "robothor.llm.ollama.get_embeddings_batch_async", AsyncMock(return_value=[])
    )
    planning = []
    execution = []
    finished = []

    async def planner(**kwargs):
        planning.append(kwargs["model"])
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress:
                raise
            return litellm.ModelResponse(
                choices=[
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"difficulty": "complex", "plan": [], "estimated_steps": 10}
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            )
        finally:
            finished.append(True)

    async def provider(self, messages, models, tools, on_content=None, **kwargs):
        execution.append(True)
        if len(execution) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "one",
                        "type": "function",
                        "function": {
                            "name": "create_task",
                            "arguments": json.dumps(
                                {"title": "After automatic planning", "body": "Synthetic"}
                            ),
                        },
                    }
                ],
            }
        else:
            assert len(execution) == 2
            message = {
                "role": "assistant",
                "content": "The task was created; its work remains TODO.",
            }
        return litellm.ModelResponse(
            choices=[
                {
                    "message": message,
                    "finish_reason": "tool_calls" if len(execution) == 1 else "stop",
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    monkeypatch.setattr("litellm.acompletion", planner)
    monkeypatch.setattr(LLMClient, "_call_llm", provider)
    monkeypatch.setattr(LLMClient, "_call_llm_streaming", provider)
    # Enough context triggers the existing automatic complexity heuristic.
    async with asyncio.timeout(3):
        run = await engine.execute(
            sample_agent_config.id,
            "Create one task. " + "Synthetic context. " * 30,
            agent_config=sample_agent_config,
            trigger_type=TriggerType.WEBCHAT,
            tenant_id=tenant,
            user_id="operator",
            user_role="owner",
        )
    await get_task_registry().drain(timeout=5)
    assert str(run.status) == "completed"
    assert planning == [sample_agent_config.model_primary] and finished == [True]
    assert len(execution) == 2
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT body,status FROM crm_tasks WHERE tenant_id=%s", (tenant,))
        assert cur.fetchall() == [("Synthetic", "TODO")]
        cur.execute(
            "SELECT state,count(*) FROM agent_runtime_effects WHERE tenant_id=%s GROUP BY state",
            (tenant,),
        )
        assert cur.fetchall() == [("confirmed", 1)]
