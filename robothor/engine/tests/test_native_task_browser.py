"""Browser response loss after real native CRM creation recovers its receipt."""

import asyncio
import json
import os
import signal
import socket
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import litellm
import psycopg2
import pytest
import uvicorn
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
async def test_task_receipt_recovers_in_browser(engine_config, sample_agent_config, monkeypatch):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires --chat-browser canonical harness and freshly built app")
    tenant = "task-browser-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    config = replace(engine_config, tenant_id=tenant)
    sample_agent_config.id = "main"
    sample_agent_config.task_protocol = False
    sample_agent_config.difficulty_class = "simple"
    sample_agent_config.model_fallbacks = []
    sample_agent_config.tools_allowed = ["create_task"]
    runner = AgentRunner(config)
    auth = AuthContext(tenant_id=tenant, user_id="service:main", role="owner", typ="service")
    calls = []

    async def provider(self, messages, models, tools, on_content=None, **kwargs):
        calls.append(True)
        assert len(calls) <= 2, "Unexpected model work after task creation"
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
                                {"title": "Browser task", "body": "Synthetic only"}
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
                {"message": message, "finish_reason": "tool_calls" if len(calls) == 1 else "stop"}
            ],
            usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        )

    monkeypatch.setattr(LLMClient, "_call_llm", provider)
    monkeypatch.setattr(LLMClient, "_call_llm_streaming", provider)
    forbidden = AsyncMock(side_effect=AssertionError("External models forbidden"))
    monkeypatch.setattr("litellm.acompletion", forbidden)
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
    engine_socket = socket.socket()
    engine_socket.bind(("127.0.0.1", 0))
    engine_socket.listen()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        ui_port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    server_task = browser = None
    root = Path(__file__).resolve().parents[3]
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "PORT": str(ui_port),
        "HOSTNAME": "127.0.0.1",
        "ROBOTHOR_ENGINE_URL": f"http://127.0.0.1:{engine_socket.getsockname()[1]}",
        "BRIDGE_URL": "http://127.0.0.1:59999",
        "GENUS_ENVIRONMENT": "test",
        "GENUS_INSECURE_DEV_MODE": "true",
        "AUTH_TRUST_HOST": "true",
        "AUTH_SECRET": "isolated-task-browser-secret",
        "AUTH_OIDC_ISSUER": "https://idp.playwright.invalid",
        "AUTH_OIDC_CLIENT_ID": "test",
        "AUTH_OIDC_CLIENT_SECRET": "test",
        "GENUS_BRIDGE_SSO_SECRET": "test",
    }
    log_path = config.workspace / "task-browser.log"
    try:
        chat._sessions.clear()
        chat.init_chat(runner, config)
        server_task = asyncio.create_task(server.serve(sockets=[engine_socket]))
        while not server.started:
            if server_task.done():
                await server_task
            await asyncio.sleep(0.01)
        with log_path.open("wb") as log:
            browser = await asyncio.create_subprocess_exec(
                "node",
                "scripts/runtime-task-recovery.mjs",
                cwd=root / "app",
                env=env,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            async with asyncio.timeout(90):
                await browser.wait()
        output = log_path.read_text()
        assert browser.returncode == 0, output
        marker = next(line for line in output.splitlines() if line.startswith("TASK_BROWSER "))
        await get_task_registry().drain(timeout=5)
        assert len(calls) == 2
        forbidden.assert_not_awaited()
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM crm_tasks WHERE tenant_id=%s", (tenant,))
            assert cur.fetchone() == (1,)
            cur.execute(
                "SELECT state FROM agent_runtime_effects WHERE tenant_id=%s AND tool_name='create_task'",
                (tenant,),
            )
            assert cur.fetchall() == [("confirmed",)]
            cur.execute("SELECT count(*) FROM agent_runs WHERE tenant_id=%s", (tenant,))
            assert cur.fetchone() == (1,)
        print(marker, flush=True)
    finally:
        if browser is not None:
            with suppress(ProcessLookupError):
                os.killpg(browser.pid, signal.SIGKILL)
            await browser.wait()
        server.should_exit = True
        if server_task is not None:
            await asyncio.wait_for(server_task, timeout=10)
        engine_socket.close()
        chat._sessions.clear()
