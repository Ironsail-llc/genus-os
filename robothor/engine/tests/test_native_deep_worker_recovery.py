"""Late deep-worker results survive chat cancellation in the canonical audit store."""

import asyncio
import os
import threading
from dataclasses import replace
from unittest.mock import patch
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

    def worker(**kwargs):
        entered.set()
        assert release.wait(10)
        return {"response": "Late synthetic result", "cost_usd": 0.12}

    token = active_context.set(ExecutionContext(tenant, auth.user_id, identifier))
    with (
        patch.object(controls, "stopped", _REAL_STOPPED),
        patch("robothor.engine.rlm_tool.execute_deep_reason", side_effect=worker) as deep,
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
                assert cur.fetchone()[0]["response"] == "Late synthetic result"
            deep.assert_called_once()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await get_task_registry().drain(timeout=5)
            active_context.reset(token)
