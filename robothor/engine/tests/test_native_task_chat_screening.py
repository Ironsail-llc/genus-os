"""Browser response loss after real native CRM creation recovers its receipt."""

import asyncio
import json
import os
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

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


@pytest.mark.timeout(120)
async def test_warm_chat_task_screening(engine_config, sample_agent_config, monkeypatch):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires --task-chat-screening canonical harness")
    tenant = "task-browser-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    config = replace(engine_config, tenant_id=tenant)
    installation = os.environ.get("ROBOTHOR_RUNTIME_SCREENING_INSTALLATION")
    if installation:
        from robothor.engine.config import load_agent_config

        workspace = Path(installation)
        sample_agent_config = load_agent_config(
            "main", workspace / "docs/agents", workspace=workspace, trigger_type="webchat"
        )
        assert sample_agent_config is not None
        assert "create_task" in sample_agent_config.tools_allowed
    else:
        sample_agent_config.id = "main"
        sample_agent_config.task_protocol = False
        sample_agent_config.difficulty_class = "simple"
        sample_agent_config.model_fallbacks = []
        sample_agent_config.tools_allowed = ["create_task"]
    runner = AgentRunner(config)
    auth = AuthContext(tenant_id=tenant, user_id="service:main", role="owner", typ="service")
    calls = []
    turn_calls = 0
    sample_index = 0
    planning_calls = 0

    async def provider(self, messages, models, tools, on_content=None, **kwargs):
        nonlocal turn_calls
        turn_calls += 1
        calls.append(True)
        assert turn_calls <= 2, "Unexpected model work after task creation"
        if turn_calls == 1:
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
                                    "title": f"Chat screening {sample_index}",
                                    "body": "Synthetic only",
                                }
                            ),
                        },
                    }
                ],
            }
        else:
            results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            assert results and results[-1].get("id") and not results[-1].get("error"), results
            message = {"role": "assistant", "content": "Task creation recorded."}
            if on_content:
                await on_content(message["content"])
        return litellm.ModelResponse(
            choices=[
                {"message": message, "finish_reason": "tool_calls" if turn_calls == 1 else "stop"}
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    monkeypatch.setattr(LLMClient, "_call_llm", provider)
    monkeypatch.setattr(LLMClient, "_call_llm_streaming", provider)

    async def planning_provider(**kwargs):
        nonlocal planning_calls
        assert installation and not kwargs.get("tools"), "Unexpected auxiliary provider call"
        assert "Analyze this task" in str(kwargs.get("messages"))
        planning_calls += 1
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
                                    {
                                        "step": 1,
                                        "action": "Create the synthetic task",
                                        "tool": "create_task",
                                    }
                                ],
                                "risks": [],
                                "success_criteria": "The stored task exists",
                            }
                        ),
                    },
                    "finish_reason": "stop",
                }
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    isolated_provider = AsyncMock(side_effect=planning_provider)
    monkeypatch.setattr("litellm.acompletion", isolated_provider)
    monkeypatch.setattr(
        "robothor.engine.runner.load_agent_config_or_reason",
        lambda *a, **k: (sample_agent_config, None),
    )
    monkeypatch.setattr(
        "robothor.llm.ollama.get_embeddings_batch_async", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        assert request.client.host == "127.0.0.1"
        request.state.auth = auth
        return await call_next(request)

    app.include_router(chat.router)

    import random
    import statistics
    import time

    from httpx import ASGITransport, AsyncClient

    output = os.environ.get("ROBOTHOR_RUNTIME_TASK_CHAT_SCREENING_ARTIFACT")
    with Path(output).open("x") if output else nullcontext() as stream:
        samples = []
        chat._sessions.clear()
        chat.init_chat(runner, config)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as http:
                for sample_index in range(31):
                    turn_calls = 0
                    planning_before = planning_calls
                    started = time.perf_counter()
                    sample = {"index": sample_index, "warmup": sample_index == 0}
                    try:
                        async with asyncio.timeout(10):
                            response = await http.post(
                                "/chat/send",
                                json={
                                    "message": f"Create synthetic task {sample_index}",
                                    "request_id": str(uuid4()),
                                    "session_key": f"agent:main:screen-{sample_index}",
                                },
                            )
                        sample["duration_ms"] = (time.perf_counter() - started) * 1000
                        assert (
                            response.status_code == 200
                            and "Task creation recorded." in response.text
                        )
                        assert turn_calls == 2
                        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                            cur.execute(
                                "SELECT count(*) FROM crm_tasks WHERE tenant_id=%s AND title=%s",
                                (tenant, f"Chat screening {sample_index}"),
                            )
                            assert cur.fetchone() == (1,)
                            cur.execute(
                                "SELECT count(*) FROM agent_runtime_effects WHERE tenant_id=%s AND state='confirmed'",
                                (tenant,),
                            )
                            assert cur.fetchone() == (sample_index + 1,)
                            cur.execute(
                                "SELECT status,count(*) FROM agent_runs WHERE tenant_id=%s GROUP BY status",
                                (tenant,),
                            )
                            assert cur.fetchall() == [("completed", sample_index + 1)]
                            cur.execute(
                                "SELECT runtime_context->>'deadline' FROM agent_runs WHERE tenant_id=%s ORDER BY started_at DESC LIMIT 1",
                                (tenant,),
                            )
                            sample["runtime_deadline_assigned"] = cur.fetchone()[0] is not None
                            assert sample["runtime_deadline_assigned"], (
                                "Simple action lacks a deadline"
                            )
                        sample.update(verified=True, run_status="completed", model_calls=turn_calls)
                    except Exception as exc:
                        sample.update(
                            verified=False, error_type=type(exc).__name__, model_calls=turn_calls
                        )
                        sample.setdefault("duration_ms", (time.perf_counter() - started) * 1000)
                    samples.append(sample)
                    sample["planning_calls"] = planning_calls - planning_before
                    sample["total_model_calls"] = turn_calls + sample["planning_calls"]
                    if stream:
                        stream.write(json.dumps({"sample": sample}) + "\n")
                        stream.flush()
                    print("CHAT_TASK_SAMPLE " + json.dumps(sample), flush=True)
            await get_task_registry().drain(timeout=5)
            if not installation:
                isolated_provider.assert_not_awaited()
            assert all(sample["verified"] for sample in samples), samples
            assert len(calls) == 62
            values = [sample["duration_ms"] for sample in samples if not sample["warmup"]]

            def p95(data):
                return statistics.quantiles(data, n=100, method="inclusive")[94]

            rng = random.Random(142)
            boots = sorted(p95(rng.choices(values, k=len(values))) for _ in range(2000))
            summary = {
                "p50_ms": statistics.median(values),
                "p95_ms": p95(values),
                "p95_ci95_ms": [boots[49], boots[1949]],
                "samples": 30,
                "warmups": 1,
                "model_calls": len(calls),
                "external_model_calls": 0,
                "planning_calls": planning_calls,
                "total_model_calls": len(calls) + planning_calls,
                "profile": "installation-main" if installation else "minimal",
                "tools_allowed": len(sample_agent_config.tools_allowed),
                "task_protocol": sample_agent_config.task_protocol,
                "scope": "Authenticated ASGI chat admission through native runner, real private task creation and reply. Scripted immediate model; installation config when selected, isolated workspace without production memory/instruction files. No TCP/browser/provider latency. Audit/event publication and embeddings stubbed. Verification queries and post-response background drain excluded from timing.",
            }
            if stream:
                stream.write(json.dumps({"summary": summary}) + "\n")
                stream.flush()
            print("CHAT_TASK_SCREENING " + json.dumps(summary), flush=True)
            assert summary["p95_ms"] < 2000, (
                "Local warm chat-to-task harness overhead exceeds target"
            )
        finally:
            chat._sessions.clear()
