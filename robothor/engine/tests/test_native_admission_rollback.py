"""A new compatible-native admission must preserve older stopped/uncertain work."""

import os
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import litellm
import psycopg2
import pytest

from robothor.engine.models import RunStatus, TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_new_native_admission_preserves_old_operation_and_stop(
    engine_config, sample_agent_config
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "rollback-" + uuid4().hex
    old_run, operation, goal, approval = [str(uuid4()) for _ in range(4)]
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
        cur.execute(
            "INSERT INTO chat_approval_receipts(tenant_id,request_id,session_key,plan_id,plan_state) VALUES (%s,%s,'web:main',%s,'{\"status\":\"approved\"}')",
            (tenant, approval, str(uuid4())),
        )
        cur.execute(
            "INSERT INTO agent_runtime_request_stops(tenant_id,request_id,note) VALUES (%s,%s,'Keep approval stopped')",
            (tenant, approval),
        )
        cur.execute(
            "INSERT INTO agent_runs(id,tenant_id,agent_id,trigger_type,status,error_message) VALUES (%s,%s,'main','event','cancelled','Explicit stop')",
            (old_run, tenant),
        )
        cur.execute(
            "INSERT INTO agent_runtime_controls(tenant_id,run_id,action,note) VALUES (%s,%s,'cancel','Keep stopped')",
            (tenant, old_run),
        )
        cur.execute(
            'INSERT INTO agent_run_checkpoints(run_id,step_number,messages,schema_version) VALUES (%s,1,\'[{"role":"user","content":"Prior work"}]\',1)',
            (old_run,),
        )
        cur.execute(
            "INSERT INTO pursuit_goals(tenant_id,id,status,data) VALUES (%s,%s,'paused','{\"tokens_used\":123}')",
            (tenant, goal),
        )
        cur.execute(
            "INSERT INTO calendar_operations(id,tenant_id,user_id,agent_id,calendar_id,event_id,arguments,status,result) VALUES (%s,%s,'operator','main','fixture','fixture','{}','executing','{\"verification\":\"unverified\"}')",
            (operation, tenant),
        )

    def snapshot():
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            result = []
            for query, parameters in [
                (
                    "SELECT to_jsonb(r) FROM chat_approval_receipts r WHERE tenant_id=%s AND request_id=%s",
                    (tenant, approval),
                ),
                (
                    "SELECT to_jsonb(r) FROM agent_runtime_request_stops r WHERE tenant_id=%s AND request_id=%s",
                    (tenant, approval),
                ),
                ("SELECT to_jsonb(r) FROM agent_runs r WHERE id=%s", (old_run,)),
                ("SELECT to_jsonb(r) FROM agent_runtime_controls r WHERE run_id=%s", (old_run,)),
                ("SELECT to_jsonb(r) FROM agent_run_checkpoints r WHERE run_id=%s", (old_run,)),
                ("SELECT to_jsonb(r) FROM pursuit_goals r WHERE id=%s", (goal,)),
                ("SELECT to_jsonb(r) FROM calendar_operations r WHERE id=%s", (operation,)),
            ]:
                cur.execute(query, parameters)
                result.append(cur.fetchall())
            return result

    before = snapshot()
    engine_config = replace(engine_config, tenant_id=tenant)
    sample_agent_config.task_protocol = False
    sample_agent_config.difficulty_class = "simple"
    sample_agent_config.model_fallbacks = []
    runner = AgentRunner(engine_config)
    response = litellm.ModelResponse(
        model=sample_agent_config.model_primary,
        choices=[
            {
                "message": {"role": "assistant", "content": "New request received."},
                "finish_reason": "stop",
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )
    with (
        patch("litellm.acompletion", new_callable=AsyncMock, return_value=response) as provider,
        patch.object(runner.registry, "execute", new_callable=AsyncMock) as tools,
    ):
        new = await runner.execute(
            sample_agent_config.id,
            "Acknowledge this new request",
            agent_config=sample_agent_config,
            trigger_type=TriggerType.EVENT,
            tenant_id=tenant,
        )
        await get_task_registry().drain(timeout=5)
    assert new.status == RunStatus.COMPLETED and new.id != old_run
    assert provider.await_count == 1
    tools.assert_not_awaited()
    assert snapshot() == before
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT status,runtime_context FROM agent_runs WHERE id=%s", (new.id,))
        status, context = cur.fetchone()
        assert status == "completed" and context["runtime_id"] == "current"
