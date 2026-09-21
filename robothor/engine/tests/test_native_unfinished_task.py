"""A completed worker turn must retain its explicitly unfinished parent task."""

import json
import os
from dataclasses import replace
from uuid import uuid4

import litellm
import psycopg2
import pytest

from robothor.crm import dal
from robothor.engine.models import RunStatus, SpawnContext, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import effects
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("unfinished", [True, False])
@pytest.mark.parametrize("tenant_kind", ["default", "isolated"])
async def test_worker_completion_keeps_known_unfinished_task_open(
    engine_config, sample_agent_config, monkeypatch, tenant_kind, unfinished
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "default" if tenant_kind == "default" else "unfinished-" + uuid4().hex
    parent_run = str(uuid4())
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
            (tenant, tenant),
        )
        cur.execute(
            "INSERT INTO agent_runs(id,tenant_id,agent_id,trigger_type,status) VALUES (%s,%s,'main','manual','running')",
            (parent_run, tenant),
        )
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    monkeypatch.setenv("ROBOTHOR_TODO_ESCALATE_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_TODO_PROMOTE_SUBTASKS_ENABLED", "0")
    # Known unfinished items must stay open even without a model-based verifier.
    monkeypatch.setenv("ROBOTHOR_RUN_VERIFICATION_ENABLED", "0")
    parent = dal.create_task(
        title="Synthetic unfinished work",
        body="Two steps",
        status="IN_PROGRESS",
        assigned_to_agent="main",
        created_by_agent="main",
        tenant_id=tenant,
    )
    assert isinstance(parent, str)
    sample_agent_config.id = "main"
    sample_agent_config.task_protocol = True
    sample_agent_config.todo_list_enabled = True
    sample_agent_config.auto_task = False
    sample_agent_config.tools_allowed = ["todo_write"]
    sample_agent_config.model_fallbacks = []
    runner = AgentRunner(replace(engine_config, tenant_id=tenant))
    calls = []

    async def provider(**kwargs):
        calls.append(True)
        assert len(calls) <= 2
        if len(calls) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "record-progress",
                        "type": "function",
                        "function": {
                            "name": "todo_write",
                            "arguments": json.dumps(
                                {
                                    "todos": [
                                        {
                                            "content": "Check first item",
                                            "status": "completed",
                                            "activeForm": "Checking first item",
                                        },
                                        {
                                            "content": "Check remaining item",
                                            "status": "pending" if unfinished else "completed",
                                            "activeForm": "Checking remaining item",
                                        },
                                    ]
                                }
                            ),
                        },
                    }
                ],
            }
        else:
            message = {
                "role": "assistant",
                "content": (
                    "The first item is done. The remaining item still needs checking."
                    if unfinished
                    else "Both items are done."
                ),
            }
        return litellm.ModelResponse(
            choices=[
                {"message": message, "finish_reason": "tool_calls" if len(calls) == 1 else "stop"}
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    monkeypatch.setattr("litellm.acompletion", provider)
    run = await runner.execute(
        "main",
        "Record progress on the two items",
        agent_config=sample_agent_config,
        trigger_type=TriggerType.SUB_AGENT,
        tenant_id=tenant,
        spawn_context=SpawnContext(
            parent_run_id=parent_run,
            parent_agent_id="main",
            correlation_id=str(uuid4()),
            nesting_depth=1,
            parent_task_id=parent,
            user_id="service:main",
            user_role="service",
        ),
    )
    await get_task_registry().drain(timeout=5)
    assert run.status == RunStatus.COMPLETED, run.error_message
    assert len(calls) == 2
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,next_action FROM crm_tasks WHERE id=%s AND tenant_id=%s",
            (parent, tenant),
        )
        status, next_action = cur.fetchone()
    if unfinished:
        assert status != "DONE", "Finishing a turn closed a task with a known pending item"
        assert next_action == "Continue: Check remaining item"
    else:
        assert status == "DONE"
