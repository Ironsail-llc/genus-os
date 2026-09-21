"""A final goal report survives delivery loss as an original-request audit result."""

import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import psycopg2
import pytest
from fastapi import FastAPI
from litellm import ModelResponse

from bench.runtime.uat_server import seed_unfinished_work
from robothor.auth.deps import AuthContext
from robothor.engine import chat
from robothor.engine.models import TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.runtime.chat_control import request_key
from robothor.engine.task_registry import get_task_registry
from robothor.goals import store
from robothor.goals.model import GoalUpdate
from robothor.identity import IdentityContext

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("compound", [False, True])
@pytest.mark.asyncio
async def test_native_goal_report_is_audited_and_recovered_without_reexecution(
    engine_config, sample_agent_config, monkeypatch, compound
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, client_id = "report-" + uuid4().hex, str(uuid4())
    session_key = "web:report-" + uuid4().hex
    auth = AuthContext(tenant_id=tenant, user_id="operator", role="owner", typ="user")
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    store.set_enabled(tenant, False, "operator")
    seed_unfinished_work(tenant)
    (goal,) = store.list_goals(tenant)
    engine = AgentRunner(replace(engine_config, tenant_id=tenant))
    config = replace(
        sample_agent_config, id="main", task_protocol=False, tools_allowed=["report_pursuit_goal"]
    )
    response = ModelResponse(
        model=config.model_primary,
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "report",
                            "type": "function",
                            "function": {
                                "name": "report_pursuit_goal",
                                "arguments": json.dumps({"goal_id": goal["id"]}),
                            },
                        }
                    ],
                },
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )

    async def produce(**kwargs):
        if provider.await_count == 1:
            return response
        assert compound and provider.await_count == 2
        facts = json.loads(
            next(m["content"] for m in reversed(kwargs["messages"]) if m["role"] == "tool")
        )
        assert facts["report_prepared"] is False
        from robothor.goals.presentation import render_goal_progress

        factual_text = render_goal_progress(
            facts["goal"], execution_enabled=facts["execution_enabled"]
        )
        return ModelResponse(
            model=config.model_primary,
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": factual_text + "\nSeparately, 17 × 19 = 323.",
                    },
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    provider = AsyncMock(side_effect=produce)
    monkeypatch.setattr("litellm.acompletion", provider)
    context = ExecutionContext(
        tenant,
        auth.user_id,
        request_key(auth, session_key, client_id),
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    result = await CurrentRuntime(engine.execute).run(
        RunRequest(
            context,
            "main",
            f"Report progress for goal {goal['id']}."
            + (" Also calculate 17 times 19 separately." if compound else ""),
            options={
                "agent_config": config,
                "trigger_type": TriggerType.WEBCHAT,
                "user_id": auth.user_id,
                "user_role": auth.role,
                "identity": IdentityContext(tenant, "webchat", auth.user_id, True, role="owner"),
            },
        )
    )
    await get_task_registry().drain(timeout=5)
    assert str(result.run.status) == "completed"
    text = result.run.output_text
    assert "The goal is not complete" in text and "scheduled reviews will not run" in text
    assert provider.await_count == (2 if compound else 1)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,output_text FROM agent_runs WHERE tenant_id=%s AND id=%s",
            (tenant, result.run.id),
        )
        assert cur.fetchone() == ("completed", text)
        cur.execute(
            "SELECT step_type,tool_output FROM agent_run_steps WHERE run_id=%s AND tool_name='report_pursuit_goal' ORDER BY step_number",
            (result.run.id,),
        )
        rows = cur.fetchall()
    tool = next(output for kind, output in rows if kind == "tool_call")
    checkpoints = [output for kind, output in rows if kind == "checkpoint"]
    assert tool["goal"]["id"] == goal["id"] and tool["goal"]["status"] == "waiting"
    assert sorted(task["status"] for task in tool["goal"]["tasks"]) == ["DONE", "TODO"]
    assert checkpoints == ([] if compound else [{"origin": "trusted_goal_report", "output": text}])
    if compound:
        assert "323" in text
    # Later state must not replace the historical report or cause its action to repeat.
    store.update(
        tenant,
        goal["id"],
        GoalUpdate(action="pause", version=goal["version"]),
        "operator",
        operator=True,
    )
    monkeypatch.setattr(chat, "_runner", engine)
    monkeypatch.setattr(chat, "_config", engine.config)
    caller = [auth]
    monkeypatch.setattr(chat, "_auth_context", lambda _: caller[0])
    app = FastAPI()
    app.include_router(chat.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            recovered = await client.get(
                "/chat/outcome", params={"request_id": client_id, "session_key": session_key}
            )
            assert recovered.status_code == 200
            body = recovered.json()
            assert body["terminal"] and body["run_id"] == result.run.id, body
            assert body["text"] == text
        caller[0] = AuthContext(tenant_id="foreign", user_id=auth.user_id, role="owner", typ="user")
        foreign = await client.get(
            "/chat/outcome", params={"request_id": client_id, "session_key": session_key}
        )
        assert foreign.json()["state"] == "not_found"
    assert provider.await_count == (2 if compound else 1)
    assert store.get(tenant, goal["id"])["status"] == "paused"
