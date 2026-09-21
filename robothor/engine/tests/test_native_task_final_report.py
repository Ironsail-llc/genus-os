"""One native creation can deliver verified facts without a second model turn."""

import asyncio
import json
import os
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import litellm
import psycopg2
import pytest
from fastapi import FastAPI

from robothor.auth.deps import AuthContext
from robothor.crm import dal
from robothor.engine import chat
from robothor.engine.llm_client import LLMClient
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import effects
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("compound", [False, True])
@pytest.mark.parametrize("final_report", [False, True])
async def test_native_task_report_delivers_and_recovers_without_extra_model(
    engine_config, sample_agent_config, monkeypatch, final_report, compound
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "task-report-" + uuid4().hex
    auth = AuthContext(tenant_id=tenant, user_id="operator", role="owner", typ="user")
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    sample_agent_config.id = "main"
    sample_agent_config.task_protocol = True
    sample_agent_config.planning_enabled = False
    sample_agent_config.difficulty_class = "simple"
    sample_agent_config.model_fallbacks = []
    sample_agent_config.tools_allowed = ["create_task"]
    engine = AgentRunner(replace(engine_config, tenant_id=tenant))
    monkeypatch.setattr(
        "robothor.engine.runner.load_agent_config_or_reason",
        lambda *a, **k: (sample_agent_config, None),
    )
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    monkeypatch.setattr("robothor.goals.events.capture", lambda *a, **k: None)
    monkeypatch.setattr(
        "robothor.llm.ollama.get_embeddings_batch_async", AsyncMock(return_value=[])
    )
    calls = []
    expected_calls = 1 if final_report and not compound else 2

    async def provider(self, messages, models, tools, on_content=None, **kwargs):
        calls.append(True)
        assert len(calls) <= expected_calls, "Unnecessary model after verified final report"
        message = {"role": "assistant", "content": "Created task: One. Status: TODO."}
        if compound:
            message["content"] += " Separately, 17 times 19 is 323."
        if len(calls) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "create-once",
                        "type": "function",
                        "function": {
                            "name": "create_task",
                            "arguments": json.dumps(
                                {
                                    "title": "One",
                                    "body": "Synthetic only",
                                    "finalReport": final_report,
                                }
                            ),
                        },
                    }
                ],
            }
        return litellm.ModelResponse(
            choices=[
                {"message": message, "finish_reason": "tool_calls" if len(calls) == 1 else "stop"}
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    monkeypatch.setattr(LLMClient, "_call_llm", provider)
    monkeypatch.setattr(LLMClient, "_call_llm_streaming", provider)
    monkeypatch.setattr(chat, "_runner", engine)
    monkeypatch.setattr(chat, "_config", engine.config)
    monkeypatch.setattr(chat, "_auth_context", lambda _: auth)
    app = FastAPI()
    app.include_router(chat.router)
    session = "agent:main:task-report-" + uuid4().hex
    client_id = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chat/send",
            json={
                "message": (
                    'Create one task titled "One" with description "Synthetic only".'
                    + (" Also calculate 17 times 19 separately in your reply." if compound else "")
                ),
                "session_key": session,
                "request_id": client_id,
            },
        )
        assert response.status_code == 200
        done = [
            json.loads(part.split("data: ", 1)[1])
            for part in response.text.split("\n\n")
            if part.startswith("event: done\n")
        ]
        assert len(done) == 1 and done[0]["status"] == "completed"
        assert "Created task:" in done[0]["text"] and "Status: TODO" in done[0]["text"]
        if compound:
            assert "323" in done[0]["text"], "Task-only report dropped the additional obligation"
        # Terminal delivery can precede the asynchronous run-record write. The
        # reconnect client polls that original identity, never re-executes it.
        async with asyncio.timeout(2):
            while True:
                recovered = (
                    await client.get(
                        "/chat/outcome", params={"session_key": session, "request_id": client_id}
                    )
                ).json()
                if recovered.get("terminal"):
                    break
                await asyncio.sleep(0.01)
        assert recovered["text"].startswith(done[0]["text"])
        assert "without creating another task" in recovered["text"]
        assert chat._get_session(session).history[-1]["content"] == done[0]["text"]
    await get_task_registry().drain(timeout=5)
    assert len(calls) == expected_calls
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT title,body,status FROM crm_tasks WHERE tenant_id=%s", (tenant,))
        assert cur.fetchall() == [("One", "Synthetic only", "TODO")]
        cur.execute(
            "SELECT state,count(*) FROM agent_runtime_effects WHERE tenant_id=%s GROUP BY state",
            (tenant,),
        )
        assert cur.fetchall() == [("confirmed", 1)]
        cur.execute(
            "SELECT tool_output FROM agent_run_steps WHERE run_id=%s AND step_type='checkpoint' AND tool_name='create_task'",
            (recovered["run_id"],),
        )
        evidence = cur.fetchall()
        assert len(evidence) == int(final_report and not compound)
        if final_report and not compound:
            assert evidence[0][0]["origin"] == "trusted_task_report"
