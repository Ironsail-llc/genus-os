"""Chat polls reconcile real committed CRM writes after initial readback failure."""

import os
from unittest.mock import AsyncMock
from uuid import uuid4

import psycopg2
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from robothor.auth.deps import AuthContext
from robothor.crm import dal
from robothor.engine import chat
from robothor.engine.runtime import ExecutionContext, effects, note_recovery, task_recovery
from robothor.engine.runtime.chat_control import request_key
from robothor.engine.runtime.current import active_context
from robothor.engine.tools import dispatch

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("tool,table", [("create_task", "crm_tasks"), ("create_note", "crm_notes")])
async def test_chat_recovers_committed_write_without_waiting_for_cleanup(monkeypatch, tool, table):
    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, run, client = "chat-readback-" + uuid4().hex, str(uuid4()), str(uuid4())
    auth = AuthContext(tenant_id=tenant, user_id="service:main", role="owner", typ="user")
    key = request_key(auth, "web:main", client)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
        cur.execute(
            """INSERT INTO agent_runs(id,tenant_id,agent_id,user_id,correlation_id,trigger_type,status)
               VALUES (%s,%s,'main',%s,%s,'manual','failed')""",
            (run, tenant, auth.user_id, key),
        )
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    recovery = task_recovery if tool == "create_task" else note_recovery
    real_create, real_verify = getattr(dal, tool), recovery.verify
    writes = []

    def lose_response(*args, **kwargs):
        identifier = real_create(*args, **kwargs)
        assert isinstance(identifier, str)
        writes.append(identifier)
        raise OSError("Synthetic lost response after the committed CRM write")

    monkeypatch.setattr(dal, tool, lose_response)
    monkeypatch.setattr(recovery, "verify", lambda _: effects.Verification("unknown"))
    ctx = ExecutionContext(tenant, auth.user_id, key)
    token = active_context.set(ctx)
    try:
        result = await dispatch._execute_tool(
            tool,
            {"title": "Synthetic readback", "body": "Once"},
            agent_id="main",
            run_id=run,
            tenant_id=tenant,
            user_id=auth.user_id,
            user_role="service",
        )
    finally:
        active_context.reset(token)
    assert result["outcome_unknown"] and not result["retryable"]
    monkeypatch.setattr(recovery, "verify", real_verify)
    monkeypatch.setattr(chat, "_sessions", {})
    forbidden = AsyncMock(side_effect=AssertionError("Recovery cannot execute another action"))
    monkeypatch.setattr(dispatch, "_execute_tool", forbidden)
    monkeypatch.setattr("litellm.acompletion", forbidden)
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = auth
        return await call_next(request)

    app.include_router(chat.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        first = await http.get(
            "/chat/outcome", params={"request_id": client, "session_key": "web:main"}
        )
        assert first.status_code == 200 and first.json()["reconciliation_pending"]
        for _ in range(2):
            response = await http.get(
                "/chat/outcome", params={"request_id": client, "session_key": "web:main"}
            )
            outcome = response.json()
            assert outcome["state"] == "failed" and not outcome["verified"]
            assert not outcome["reconciliation_pending"] and outcome["effects"][0]["verified"]
            assert "without creating another" in outcome["text"]
    assert effects.read(ctx, result["effect_id"])["state"] == "confirmed"
    assert len(writes) == 1
    forbidden.assert_not_awaited()
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (1,)


async def test_permitted_retry_has_one_task_and_preserves_both_audit_attempts(monkeypatch):
    from robothor.engine.chat_recovery import read_outcome
    from robothor.engine.runtime.chat_effect_recovery import reconcile_record

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, client = "chat-retry-" + uuid4().hex, str(uuid4())
    root, first_run, retry_run = (str(uuid4()) for _ in range(3))
    auth = AuthContext(tenant_id=tenant, user_id="service:main", role="owner", typ="user")
    key = request_key(auth, "web:main", client)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
        for run, parent, status in [
            (root, None, "running"),
            (first_run, root, "failed"),
            (retry_run, root, "running"),
        ]:
            cur.execute(
                """INSERT INTO agent_runs(id,tenant_id,agent_id,user_id,correlation_id,parent_run_id,trigger_type,status)
                   VALUES (%s,%s,'main',%s,%s,%s,'manual',%s)""",
                (run, tenant, auth.user_id, key, parent, status),
            )
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    ctx = ExecutionContext(tenant, auth.user_id, key)
    args = {"title": "Synthetic permitted retry", "body": "Once"}
    first = effects.begin(ctx, first_run, "main", "create_task", args)
    reconcile_record(first["id"], auth)
    assert effects.read(ctx, first["id"])["state"] == "not_applied"
    token = active_context.set(ctx)
    try:
        created = await dispatch._execute_tool(
            "create_task",
            args,
            agent_id="main",
            run_id=retry_run,
            tenant_id=tenant,
            user_id=auth.user_id,
            user_role="service",
        )
    finally:
        active_context.reset(token)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM crm_tasks WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (1,)
        cur.execute(
            "UPDATE agent_runs SET status='completed',verified_status='verified',output_text='Task created.' WHERE id IN (%s,%s)",
            (root, retry_run),
        )
    forbidden = AsyncMock(side_effect=AssertionError("Reading audit results cannot execute work"))
    monkeypatch.setattr(dispatch, "_execute_tool", forbidden)
    monkeypatch.setattr("litellm.acompletion", forbidden)
    for _ in range(2):
        result = read_outcome(auth, "web:main", client)
        assert result["verified"] and not result["reconciliation_pending"]
        assert "Task created." in result["text"]
        assert len(result["effects"]) == 2
        old = next(row for row in result["effects"] if row["operation_id"] == first["id"])
        assert old["status"] == "not_applied" and old["superseded_by"] == created["effect_id"]
    assert effects.read(ctx, first["id"])["state"] == "not_applied"
    forbidden.assert_not_awaited()
