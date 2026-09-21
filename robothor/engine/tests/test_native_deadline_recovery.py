"""Persist native interruption causes and recover them through the chat API."""

import asyncio
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import psycopg2
import pytest
from fastapi import FastAPI

from robothor.auth.deps import AuthContext
from robothor.engine import chat
from robothor.engine.models import TriggerType
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.runtime.chat_control import request_key
from robothor.engine.runtime.deadlines import RuntimeDeadlineError
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("expire", [False, True])
async def test_native_interruption_reason_survives_chat_recovery(
    engine_config, sample_agent_config, monkeypatch, expire
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, client_id = "deadline-" + uuid4().hex, str(uuid4())
    auth = AuthContext(
        tenant_id=tenant, user_id="service:" + sample_agent_config.id, role="owner", typ="user"
    )
    session_key = "web:deadline-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    engine = AgentRunner(replace(engine_config, tenant_id=tenant))
    sample_agent_config.task_protocol = False
    sample_agent_config.model_fallbacks = ["openrouter/test/fallback"]
    entered, interrupted = asyncio.Event(), asyncio.Event()
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == sample_agent_config.model_primary:
            raise ValueError("Synthetic unavailable primary")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.set()

    monkeypatch.setattr("litellm.acompletion", provider)
    tools = AsyncMock(side_effect=AssertionError("No business calls before provider completion"))
    monkeypatch.setattr(engine.registry, "execute", tools)
    deadline = datetime.now(UTC) + timedelta(seconds=2 if expire else 10)
    context = ExecutionContext(
        tenant, auth.user_id, request_key(auth, session_key, client_id), deadline=deadline
    )
    work = asyncio.create_task(
        CurrentRuntime(engine.execute).run(
            RunRequest(
                context,
                sample_agent_config.id,
                "Synthetic interrupted request",
                options={"agent_config": sample_agent_config, "trigger_type": TriggerType.EVENT},
            )
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 1.5)
        if not expire:
            work.cancel()
        with pytest.raises(RuntimeDeadlineError if expire else asyncio.CancelledError):
            await work
    finally:
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        await get_task_registry().drain(timeout=5)
    assert interrupted.is_set()
    assert calls == [sample_agent_config.model_primary, sample_agent_config.model_fallbacks[0]]
    tools.assert_not_awaited()
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id,status,error_message,completed_at,runtime_context FROM agent_runs WHERE tenant_id=%s",
            (tenant,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    run_id, status, reason, completed_at, runtime = rows[0]
    assert status == "cancelled" and completed_at is not None
    expected = "Runtime deadline expired" if expire else "Run cancelled externally"
    assert expected in reason
    assert ("Runtime deadline expired" in reason) is expire
    assert (
        runtime["request_id"] == context.request_id and runtime["deadline"] == deadline.isoformat()
    )
    monkeypatch.setattr(chat, "_runner", engine)
    monkeypatch.setattr(chat, "_config", engine.config)
    app = FastAPI()
    app.include_router(chat.router)
    caller = [auth]
    monkeypatch.setattr(chat, "_auth_context", lambda _: caller[0])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.get(
                "/chat/outcome", params={"request_id": client_id, "session_key": session_key}
            )
            assert response.status_code == 200
            body = response.json()
            assert body["terminal"] and body["state"] == "cancelled"
            assert body["run_id"] == str(run_id) and expected in body["text"]
            assert not body["verified"] and body["effects"] == []
        caller[0] = AuthContext(
            tenant_id="foreign-tenant", user_id=auth.user_id, role="owner", typ="user"
        )
        foreign = await client.get(
            "/chat/outcome", params={"request_id": client_id, "session_key": session_key}
        )
        assert foreign.json()["state"] == "not_found"
    assert calls == [sample_agent_config.model_primary, sample_agent_config.model_fallbacks[0]]
    tools.assert_not_awaited()


@pytest.mark.parametrize("suppress_cancel", [False, True])
async def test_planner_classified_chat_times_out_and_persists_its_deadline(
    engine_config, sample_agent_config, monkeypatch, suppress_cancel
):
    import json

    import litellm

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "classified-deadline-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    engine = AgentRunner(replace(engine_config, tenant_id=tenant))
    sample_agent_config.task_protocol = False
    sample_agent_config.difficulty_class = ""
    sample_agent_config.planning_enabled = True
    sample_agent_config.model_fallbacks = []
    monkeypatch.setattr("robothor.engine.runtime.action_policy.SIMPLE_ACTION_SECONDS", 0.5)
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            assert "Analyze this task" in str(kwargs["messages"])
            return litellm.ModelResponse(
                choices=[
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {
                                    "difficulty": "simple",
                                    "estimated_steps": 1,
                                    "plan": [
                                        {"step": 1, "action": "Create task", "tool": "create_task"}
                                    ],
                                    "risks": [],
                                    "success_criteria": "Stored task exists",
                                }
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ]
            )
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress_cancel:
                raise
            return litellm.ModelResponse(
                choices=[
                    {
                        "message": {"role": "assistant", "content": "Everything completed."},
                        "finish_reason": "stop",
                    }
                ]
            )
        raise AssertionError("Unreachable provider completion")

    monkeypatch.setattr("litellm.acompletion", provider)
    tools = AsyncMock(side_effect=AssertionError("Deadline must stop before business execution"))
    monkeypatch.setattr(engine.registry, "execute", tools)
    before = datetime.now(UTC)
    from contextlib import nullcontext

    with pytest.raises(RuntimeDeadlineError) if suppress_cancel else nullcontext():
        async with asyncio.timeout(3):
            run = await engine.execute(
                sample_agent_config.id,
                "Create a synthetic task",
                agent_config=sample_agent_config,
                trigger_type=TriggerType.WEBCHAT,
                tenant_id=tenant,
                user_id="operator",
                user_role="owner",
            )
            assert str(run.status) == "cancelled"
    await get_task_registry().drain(timeout=5)
    assert len(calls) == 2
    tools.assert_not_awaited()
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status,runtime_context,error_message FROM agent_runs WHERE tenant_id=%s",
            (tenant,),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        state, context, reason = rows[0]
        assert state == "cancelled"
        assert "Runtime deadline expired" in reason
        deadline = datetime.fromisoformat(context["deadline"])
        assert 0.49 <= (deadline - before).total_seconds() < 0.7


async def test_initial_chat_delivers_verified_task_after_reply_deadline(
    engine_config, sample_agent_config, monkeypatch
):
    import json

    import litellm

    from robothor.crm import dal
    from robothor.engine import chat_delivery
    from robothor.engine.llm_client import LLMClient
    from robothor.engine.runtime import effects

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "delivery-" + uuid4().hex
    auth = AuthContext(tenant_id=tenant, user_id="operator", role="owner", typ="user")
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    sample_agent_config.id = "main"
    sample_agent_config.task_protocol = False
    sample_agent_config.planning_enabled = False
    sample_agent_config.difficulty_class = "simple"
    sample_agent_config.model_fallbacks = []
    sample_agent_config.tools_allowed = ["create_task"]
    engine = AgentRunner(replace(engine_config, tenant_id=tenant))
    monkeypatch.setattr("robothor.engine.runtime.action_policy.SIMPLE_ACTION_SECONDS", 0.5)
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

    async def provider(self, messages, models, tools, on_content=None, **kwargs):
        calls.append(True)
        if len(calls) > 1:
            await asyncio.Event().wait()
        return litellm.ModelResponse(
            choices=[
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "create-once",
                                "type": "function",
                                "function": {
                                    "name": "create_task",
                                    "arguments": json.dumps(
                                        {"title": "Delivery evidence", "body": "Synthetic only"}
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
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
    session = "agent:main:delivery-" + uuid4().hex
    client_id = str(uuid4())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chat/send",
            json={
                "message": "Create one synthetic task",
                "session_key": session,
                "request_id": client_id,
            },
        )
        assert response.status_code == 200
        events = [
            json.loads(part.split("data: ", 1)[1])
            for part in response.text.split("\n\n")
            if part.startswith("event: done\n")
        ]
        assert len(events) == 1
        delivered = events[0]
        assert delivered["status"] == "cancelled"
        assert delivered["audit_outcome"] is True
        assert delivered["text"].startswith("The task was created.")
        assert "remaining work is not confirmed" in delivered["text"]
        recovered = (
            await client.get(
                "/chat/outcome", params={"session_key": session, "request_id": client_id}
            )
        ).json()
        assert recovered["text"] == delivered["text"]
        assert not recovered["verified"] and not recovered["reconciliation_pending"]
        assert chat._get_session(session).history[-1]["content"] == delivered["text"]
    await get_task_registry().drain(timeout=5)
    assert len(calls) == 2
    for caller in [
        AuthContext(tenant_id="foreign", user_id=auth.user_id, role="owner", typ="user"),
        AuthContext(tenant_id=tenant, user_id="someone-else", role="owner", typ="user"),
    ]:
        assert chat_delivery._saved_result(delivered["run_id"], caller) is None
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT body,status FROM crm_tasks WHERE tenant_id=%s", (tenant,))
        assert cur.fetchall() == [("Synthetic only", "TODO")]
        cur.execute(
            "SELECT state,count(*) FROM agent_runtime_effects WHERE tenant_id=%s GROUP BY state",
            (tenant,),
        )
        assert cur.fetchall() == [("confirmed", 1)]
