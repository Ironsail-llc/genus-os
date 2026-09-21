"""Real browser/Next/chat/native-runner draft recovery on the private canonical DB."""

import asyncio
import json
import os
import signal
import socket
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import litellm
import psycopg2
import pytest
import uvicorn
from fastapi import FastAPI, Request

from robothor.auth.deps import AuthContext
from robothor.engine import chat
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import controls
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration
_REAL_STOPPED = controls.stopped


@pytest.mark.asyncio
@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    "approval_case", ["none", "status_first", "distinct", "same", "early_stop", "early_deep_stop"]
)
async def test_saved_plan_recovers_through_browser_and_native_engine(
    engine_config, sample_agent_config, approval_case
):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires --chat-browser canonical harness and freshly built app")
    approve_twice = approval_case in {"distinct", "same"}
    tenant = "plan-browser-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    config = replace(engine_config, tenant_id=tenant)
    sample_agent_config.id = "main"
    sample_agent_config.task_protocol = False
    sample_agent_config.difficulty_class = "simple"
    sample_agent_config.model_fallbacks = []
    runner = AgentRunner(config)
    auth = AuthContext(tenant_id=tenant, user_id="service:main", role="owner", typ="service")
    calls = []

    async def provider(**kwargs):
        alignment = any(
            "Return exactly ALIGNED" in str(message.get("content", ""))
            for message in kwargs["messages"]
        )
        execution = len(calls) >= 3
        calls.append("execution" if execution else "alignment" if alignment else "draft")
        content = "ALIGNED" if alignment else "Inspect the synthetic task record[PLAN_READY]"
        if execution:
            content = "Synthetic task review complete."
            await asyncio.sleep(0.1)
        if not kwargs.get("stream"):
            return litellm.ModelResponse(
                model=kwargs["model"],
                choices=[
                    {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            )

        async def chunks():
            yield litellm.ModelResponse(
                model=kwargs["model"],
                stream=True,
                choices=[
                    {"delta": {"role": "assistant", "content": content}, "finish_reason": None}
                ],
            )
            yield litellm.ModelResponse(
                model=kwargs["model"],
                stream=True,
                choices=[{"delta": {}, "finish_reason": "stop"}],
                usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            )

        return chunks()

    release_admission = asyncio.Event()
    stop_recorded = False
    admit = chat.admit_plan

    async def gated_admit(*args, **kwargs):
        result = await admit(*args, **kwargs)
        if approval_case in {"early_stop", "early_deep_stop"} and result[0] is not None:
            await release_admission.wait()
        return result

    app = FastAPI()
    cache_reset = False

    @app.middleware("http")
    async def synthetic_identity(request: Request, call_next):
        nonlocal cache_reset, stop_recorded
        assert request.client.host == "127.0.0.1"
        request.state.auth = auth
        if (
            approval_case == "status_first"
            and request.url.path == "/chat/plan/status"
            and len(calls) == 3
            and not cache_reset
        ):
            # Simulate lost process-local sessions; the canonical DB remains intact.
            chat._sessions.clear()
            cache_reset = True
        response = await call_next(request)
        if request.url.path == "/chat/abort" and response.status_code == 200:
            stop_recorded = True
        if request.url.path == "/chat/outcome" and stop_recorded:
            release_admission.set()
        return response

    app.include_router(chat.router)
    engine_socket = socket.socket()
    engine_socket.bind(("127.0.0.1", 0))
    engine_socket.listen()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        ui_port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    server_task = None
    browser = None
    browser_log = (config.workspace / "plan-browser.log").open("wb")
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
        "AUTH_SECRET": "isolated-plan-browser-test-secret-never-used-in-production",
        "AUTH_OIDC_ISSUER": "https://idp.playwright.invalid",
        "AUTH_OIDC_CLIENT_ID": "test",
        "AUTH_OIDC_CLIENT_SECRET": "test",
        "GENUS_BRIDGE_SSO_SECRET": "test",
        "RUNTIME_APPROVAL_TEST": approval_case,
    }
    try:
        with (
            patch("litellm.acompletion", side_effect=provider),
            patch.object(chat, "admit_plan", side_effect=gated_admit),
            patch.object(controls, "stopped", _REAL_STOPPED),
            patch(
                "robothor.engine.rlm_tool.execute_deep_reason",
                return_value={"response": "Unexpected synthetic deep execution"},
            ) as deep_worker,
            patch(
                "robothor.engine.runner.load_agent_config_or_reason",
                return_value=(sample_agent_config, None),
            ),
            patch(
                "robothor.llm.ollama.get_embeddings_batch_async",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(runner.registry, "execute", new_callable=AsyncMock) as tools,
        ):
            chat._sessions.clear()
            chat.init_chat(runner, config)
            server_task = asyncio.create_task(server.serve(sockets=[engine_socket]))
            while not server.started:
                if server_task.done():
                    await server_task
                await asyncio.sleep(0.01)
            browser = await asyncio.create_subprocess_exec(
                "node",
                "scripts/runtime-plan-recovery.mjs",
                cwd=root / "app",
                env=env,
                stdout=browser_log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            async with asyncio.timeout(90):
                await browser.wait()
            browser_log.flush()
            output = (config.workspace / "plan-browser.log").read_bytes()
            assert browser.returncode == 0, output.decode()
            marker = next(
                line for line in output.decode().splitlines() if line.startswith("PLAN_BROWSER ")
            )
            report = json.loads(marker.removeprefix("PLAN_BROWSER "))
            assert report["restored"] and report["starts"] == 1
            assert cache_reset is (approval_case == "status_first")
            assert report["approvals"] == (
                3
                if approval_case == "same"
                else 2
                if approve_twice
                else 1
                if approval_case in {"early_stop", "early_deep_stop"}
                else 0
            )
            await get_task_registry().drain(timeout=5)
            assert calls == ["draft", "draft", "alignment"] + (
                ["execution"] if approve_twice else []
            )
            tools.assert_not_awaited()
            deep_worker.assert_not_called()
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("SELECT id,status FROM agent_runs WHERE tenant_id=%s", (tenant,))
                runs = cur.fetchall()
                assert len(runs) == (
                    2 if approve_twice or approval_case in {"early_stop", "early_deep_stop"} else 1
                )
                if approval_case in {"early_stop", "early_deep_stop"}:
                    assert sorted(status for _, status in runs) == ["cancelled", "completed"]
                    assert report["early_stop"]["durable_stop"]
                else:
                    assert all(status == "completed" for _, status in runs)
                cur.execute(
                    "SELECT plan_state FROM chat_sessions WHERE tenant_id=%s AND plan_state IS NOT NULL",
                    (tenant,),
                )
                plans = cur.fetchall()
                if approve_twice or approval_case in {"early_stop", "early_deep_stop"}:
                    assert not plans or plans[0][0]["status"] == "approved"
                else:
                    ((plan,),) = plans
                    assert (
                        plan["exploration_run_id"] == str(runs[0][0])
                        and plan["status"] == "pending"
                    )
            print(marker, flush=True)
    finally:
        if browser is not None:
            with suppress(ProcessLookupError):
                os.killpg(browser.pid, signal.SIGKILL)
            await browser.wait()
        server.should_exit = True
        if server_task is not None:
            await asyncio.wait_for(server_task, timeout=10)
        browser_log.close()
        engine_socket.close()
        chat._sessions.clear()
