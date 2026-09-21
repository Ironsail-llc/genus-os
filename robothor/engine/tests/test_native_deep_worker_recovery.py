"""Late deep-worker results survive chat cancellation in the canonical audit store."""

import asyncio
import os
import threading
from dataclasses import replace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import psycopg2
import pytest

from robothor.auth.deps import AuthContext
from robothor.engine.chat_recovery import read_outcome
from robothor.engine.runner import AgentRunner
from robothor.engine.runtime import controls
from robothor.engine.runtime.chat_control import request_key
from robothor.engine.runtime.contracts import ExecutionContext
from robothor.engine.runtime.current import active_context
from robothor.engine.task_registry import get_task_registry

pytestmark = pytest.mark.integration
_REAL_STOPPED = controls.stopped


async def test_native_deep_stop_retains_pending_and_late_evidence(engine_config):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, client = "deep-worker-" + uuid4().hex, str(uuid4())
    auth = AuthContext(tenant_id=tenant, user_id="operator", role="owner", typ="user")
    identifier = request_key(auth, "web:main", client)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    runner = AgentRunner(replace(engine_config, tenant_id=tenant))
    entered, release = threading.Event(), threading.Event()

    late_effect = MagicMock(return_value="Unexpected late effect")

    def worker(**kwargs):
        from robothor.engine.rlm_tool import _build_custom_tools
        from robothor.engine.runtime.provider_budget import DurableStopError

        execute = _build_custom_tools(str(engine_config.workspace))["exec_shell"]["tool"]
        entered.set()
        assert release.wait(10)
        denied = False
        try:
            execute("synthetic command")
        except DurableStopError:
            denied = True
        return {"response": "Late synthetic result", "cost_usd": 0.12, "late_tool_denied": denied}

    token = active_context.set(ExecutionContext(tenant, auth.user_id, identifier))
    with (
        patch.object(controls, "stopped", _REAL_STOPPED),
        patch("robothor.engine.rlm_tool.execute_deep_reason", side_effect=worker) as deep,
        patch("robothor.engine.rlm_tool._make_exec_fn", return_value=late_effect),
    ):
        task = asyncio.create_task(
            runner.execute_deep(
                "Synthetic analysis", tenant_id=tenant, user_id=auth.user_id, user_role=auth.role
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.to_thread(
                controls.issue_request, tenant, identifier, "Operator stopped chat"
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            pending = await asyncio.to_thread(read_outcome, auth, "web:main", client)
            assert not pending["terminal"] and pending["state"] == "stopping"
            release.set()
            await get_task_registry().drain(timeout=5)
            final = await asyncio.to_thread(read_outcome, auth, "web:main", client)
            assert final["terminal"] and final["state"] == "cancelled"
            assert "returned result is recorded" in final["text"]
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT total_cost_usd FROM agent_runs WHERE id=%s AND tenant_id=%s",
                    (final["run_id"], tenant),
                )
                assert float(cur.fetchone()[0]) == 0.12
                cur.execute(
                    "SELECT tool_output FROM agent_run_steps WHERE run_id=%s AND tool_name='deep_reason'",
                    (final["run_id"],),
                )
                evidence = cur.fetchone()[0]
                assert evidence["response"] == "Late synthetic result"
                assert evidence["late_tool_denied"]
            deep.assert_called_once()
            late_effect.assert_not_called()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await get_task_registry().drain(timeout=5)
            active_context.reset(token)


async def test_approved_deep_chat_stop_reconciles_late_worker_result(engine_config, monkeypatch):
    import json
    from datetime import UTC, datetime

    import httpx
    from fastapi import FastAPI

    from robothor.engine import chat
    from robothor.engine.chat_store import save_plan_state_async
    from robothor.engine.models import PlanState

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, client_id = "deep-chat-" + uuid4().hex, str(uuid4())
    session_key = "agent:main:deep-" + uuid4().hex
    auth = AuthContext(tenant_id=tenant, user_id="operator", role="owner", typ="user")
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    runner = AgentRunner(replace(engine_config, tenant_id=tenant))
    monkeypatch.setattr(chat, "_runner", runner)
    monkeypatch.setattr(chat, "_config", runner.config)
    monkeypatch.setattr(chat, "_auth_context", lambda _: auth)
    monkeypatch.setattr(controls, "stopped", _REAL_STOPPED)
    entered, release = threading.Event(), threading.Event()
    late_effect = MagicMock(side_effect=AssertionError("No new effects after durable stop"))

    def worker(**kwargs):
        from robothor.engine.rlm_tool import _build_custom_tools
        from robothor.engine.runtime.provider_budget import DurableStopError

        execute = _build_custom_tools(str(engine_config.workspace))["exec_shell"]["tool"]
        entered.set()
        assert release.wait(15), "Test must release the worker"
        with pytest.raises(DurableStopError):
            execute("synthetic command")
        return {"response": "Late synthetic analysis", "cost_usd": 0.12}

    deep = MagicMock(side_effect=worker)
    monkeypatch.setattr("robothor.engine.rlm_tool.execute_deep_reason", deep)
    monkeypatch.setattr("robothor.engine.rlm_tool._make_exec_fn", lambda *a, **k: late_effect)
    plan = PlanState(
        plan_id=str(uuid4()),
        plan_text="Analyze synthetic data",
        original_message="Synthetic analysis",
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
        deep_plan=True,
    )
    await save_plan_state_async(
        session_key, chat._plan_to_dict(plan), tenant_id=tenant, strict=True
    )
    chat._get_session(session_key).active_plan = plan
    app = FastAPI()
    app.include_router(chat.router)
    payload = {"session_key": session_key, "request_id": client_id, "plan_id": plan.plan_id}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        request = asyncio.create_task(client.post("/chat/plan/approve", json=payload))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            stop = (await client.post("/chat/abort", json=payload)).json()
            assert stop["durable_stopped"] and stop["aborted"]
            response = await asyncio.wait_for(request, 2)
            assert response.status_code == 200
            done = [
                json.loads(part.split("data: ", 1)[1])
                for part in response.text.split("\n\n")
                if part.startswith("event: done\n")
            ]
            assert len(done) == 1 and done[0]["aborted"] is True
            assert not done[0].get("audit_outcome")
            params = {"session_key": session_key, "request_id": client_id}
            pending = (await client.get("/chat/outcome", params=params)).json()
            assert pending["state"] == "stopping" and not pending["terminal"]
            assert "Waiting for the original worker" in pending["text"]
            repeated = await client.post("/chat/plan/approve", json=payload)
            assert repeated.status_code == 409 and repeated.json()["request_admitted"] is True
            release.set()
            await get_task_registry().drain(timeout=5)
            final = (await client.get("/chat/outcome", params=params)).json()
            assert final["terminal"] and final["state"] == "cancelled"
            assert not final["verified"] and "returned result is recorded" in final["text"]
            with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id,total_cost_usd FROM agent_runs WHERE tenant_id=%s", (tenant,)
                )
                rows = cur.fetchall()
                assert len(rows) == 1 and str(rows[0][0]) == final["run_id"]
                assert float(rows[0][1]) == 0.12
                cur.execute(
                    "SELECT tool_output FROM agent_run_steps WHERE run_id=%s AND tool_name='deep_reason'",
                    (final["run_id"],),
                )
                assert cur.fetchone()[0]["response"] == "Late synthetic analysis"
            deep.assert_called_once()
            late_effect.assert_not_called()
        finally:
            release.set()
            if not request.done():
                request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            await get_task_registry().drain(timeout=5)
