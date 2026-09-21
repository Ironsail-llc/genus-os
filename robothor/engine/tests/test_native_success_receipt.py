"""Successful CRM effects survive a replacement worker before reply delivery."""

import os
from uuid import uuid4

import psycopg2
import pytest

from robothor.engine.runtime import ExecutionContext, effects
from robothor.engine.runtime.current import active_context
from robothor.engine.tools import dispatch

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("tool,table", [("create_note", "crm_notes"), ("create_task", "crm_tasks")])
async def test_successful_creation_returns_same_receipt_to_replacement_worker(
    monkeypatch, tool, table
):
    from robothor.crm import dal

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "success-receipt-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    ctx = ExecutionContext(tenant, "service:main", str(uuid4()))
    results = []
    token = active_context.set(ctx)
    try:
        for _ in range(2):
            results.append(  # noqa: PERF401 — sequential replacement-worker admissions
                await dispatch._execute_tool(
                    tool,
                    {"title": "Once", "body": "Synthetic"},
                    agent_id="main",
                    run_id=str(uuid4()),
                    tenant_id=tenant,
                    user_id=ctx.principal_id,
                    user_role="service",
                )
            )
    finally:
        active_context.reset(token)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (1,), "successful action was repeated by the replacement worker"
    assert results[0]["id"] == results[1]["id"]
    assert results[1]["recovered"]
    assert effects.read(ctx, results[1]["effect_id"])["state"] == "confirmed"

    # An independently authorized new request may deliberately create the same content.
    from dataclasses import replace

    token = active_context.set(replace(ctx, request_id=str(uuid4())))
    try:
        new = await dispatch._execute_tool(
            tool,
            {"title": "Once", "body": "Synthetic"},
            agent_id="main",
            run_id=str(uuid4()),
            tenant_id=tenant,
            user_id=ctx.principal_id,
            user_role="service",
        )
    finally:
        active_context.reset(token)
    assert new["id"] != results[0]["id"]
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (2,)
