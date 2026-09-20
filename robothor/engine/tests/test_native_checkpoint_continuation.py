"""Native continuation retains saved conversation and the original task."""

import json
import os
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import litellm
import psycopg2
import pytest
from psycopg2.extras import Json

from robothor.auth.deps import AuthContext
from robothor.engine.chat_recovery import read_outcome
from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime.chat_control import request_key
from robothor.engine.task_context import install_context, make_context, read_context
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_native_checkpoint_continues_saved_conversation(engine_config, sample_agent_config):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, original = "continuation-" + uuid4().hex, str(uuid4())
    client = str(uuid4())
    auth = AuthContext(
        tenant_id=tenant, user_id="service:" + sample_agent_config.id, role="owner", typ="user"
    )
    messages = [
        {"role": "system", "content": "Synthetic task instructions"},
        {"role": "user", "content": "Summarize the saved task status"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "saved_read",
                    "type": "function",
                    "function": {"name": "list_tasks", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "saved_read",
            "content": json.dumps({"tasks": [{"title": "Synthetic task", "status": "DONE"}]}),
        },
    ]
    install_context(messages, make_context("Summarize the saved task status", [], run_id=original))
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
        cur.execute(
            "INSERT INTO agent_runs(id,tenant_id,agent_id,user_id,correlation_id,trigger_type,status,error_message) VALUES (%s,%s,%s,%s,%s,'event','cancelled','daemon_restart')",
            (
                original,
                tenant,
                sample_agent_config.id,
                auth.user_id,
                request_key(auth, "web:main", client),
            ),
        )
        cur.execute(
            "INSERT INTO agent_run_checkpoints(run_id,step_number,messages,schema_version) VALUES (%s,2,%s,1)",
            (original, Json(messages)),
        )
    engine_config = replace(engine_config, tenant_id=tenant)
    sample_agent_config.task_protocol = False
    sample_agent_config.difficulty_class = "simple"
    sample_agent_config.model_fallbacks = []
    runner = AgentRunner(engine_config)
    seen = []

    async def provider(**kwargs):
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT task_text FROM agent_runs WHERE tenant_id=%s AND id<>%s", (tenant, original)
            )
            assert cur.fetchall() == [("Summarize the saved task status",)]
        conversation = kwargs["messages"]
        assert read_context(conversation)["request"] == "Summarize the saved task status"
        assert any(
            m.get("role") == "tool"
            and m.get("tool_call_id") == "saved_read"
            and "Synthetic task" in m["content"]
            for m in conversation
        )
        assert any(
            m.get("role") == "assistant" and m.get("tool_calls", [{}])[0].get("id") == "saved_read"
            for m in conversation
            if m.get("tool_calls")
        )
        seen.append(conversation)
        return litellm.ModelResponse(
            model=sample_agent_config.model_primary,
            choices=[
                {
                    "message": {"role": "assistant", "content": "The saved task is done."},
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    with (
        patch("litellm.acompletion", side_effect=provider),
        patch.object(runner.registry, "execute", new_callable=AsyncMock) as tools,
    ):
        resumed = await runner.execute(
            sample_agent_config.id,
            "Resume from checkpoint",
            agent_config=sample_agent_config,
            trigger_type=TriggerType.EVENT,
            tenant_id=tenant,
            resume_from_run_id=original,
        )
        await get_task_registry().drain(timeout=5)
    assert resumed.status == RunStatus.COMPLETED, resumed.error_message
    assert resumed.task_text == "Summarize the saved task status"
    assert len(seen) == 1
    tools.assert_not_awaited()
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT messages FROM agent_run_checkpoints WHERE run_id=%s", (original,))
        assert cur.fetchone()[0] == messages
        cur.execute(
            "SELECT status,task_text,runtime_context FROM agent_runs WHERE id=%s", (resumed.id,)
        )
        status, task, runtime = cur.fetchone()
        assert status == "completed" and task == "Summarize the saved task status"
        assert runtime["tenant_id"] == tenant and runtime["runtime_id"] == "current"

        assert runtime["resume_from_run_id"] == original

    outcome = read_outcome(auth, "web:main", client)
    assert outcome["run_id"] == resumed.id and outcome["state"] == "completed"
    assert outcome["text"] == "The saved task is done."

    from robothor.engine.daemon import _resume_scan

    assert original not in {candidate.run_id for candidate in _resume_scan(tenant)}
