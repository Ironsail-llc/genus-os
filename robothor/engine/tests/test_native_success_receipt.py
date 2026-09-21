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
        for attempt in range(2):
            results.append(  # noqa: PERF401 — sequential replacement-worker admissions
                await dispatch._execute_tool(
                    tool,
                    {
                        "title": "Once",
                        "body": "Synthetic",
                        **({"finalReport": True} if tool == "create_task" and attempt == 0 else {}),
                    },
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
    assert results[0]["recovered"] is False
    assert results[1]["recovered"]
    if tool == "create_task":
        for result in results:
            assert result["body"] == "Synthetic" and result["status"] == "TODO"
            assert result["verification"] == "verified"
            assert result["verification_scope"] == "stored_task_snapshot"
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


@pytest.mark.parametrize("tool,table", [("create_note", "crm_notes"), ("create_task", "crm_tasks")])
@pytest.mark.parametrize("reported", [False, True])
def test_saved_success_receipt_survives_real_worker_exit(tool, table, reported):
    import json
    import subprocess
    import sys

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "receipt-crash-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    settings = {
        "tenant": tenant,
        "request": str(uuid4()),
        "tool": tool,
        "phase": "write",
        "reported": reported,
    }
    # Subprocesses receive private DB configuration only, not model/provider credentials.
    env = {key: value for key, value in os.environ.items() if key.startswith("ROBOTHOR_DB_")}
    env.update(PATH=os.environ["PATH"], ROBOTHOR_TEST_DB_DSN=dsn)

    def worker():
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-s", __file__ + "::test_success_crash_worker"],
            env={**env, "ROBOTHOR_SUCCESS_CRASH_FIXTURE": json.dumps(settings)},
            capture_output=True,
            text=True,
            timeout=30,
        )

    first = worker()
    assert first.returncode == 76, first.stdout + first.stderr
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state,resolution FROM agent_runtime_effects WHERE tenant_id=%s", (tenant,)
        )
        rows = cur.fetchall()
        assert len(rows) == 1 and rows[0][0] == ("finished" if reported else "confirmed")
        if reported:
            assert rows[0][1]["source"] == "tool_response" and not rows[0][1].get("reference")
        original_id = rows[0][1]["result"]["id"]
    settings["phase"] = "recover"
    second = worker()
    assert second.returncode == 0, second.stdout + second.stderr
    receipt = json.loads(
        next(
            line.removeprefix("SAVED_RECEIPT ")
            for line in second.stdout.splitlines()
            if line.startswith("SAVED_RECEIPT ")
        )
    )
    assert receipt["id"] == original_id and receipt["recovered"]
    if reported:
        assert receipt["verification"] == "reported"
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (1,)


async def test_success_crash_worker(monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from robothor.crm import dal

    settings = os.environ.get("ROBOTHOR_SUCCESS_CRASH_FIXTURE")
    if not settings:
        pytest.skip("subprocess-only worker")
    assert "host=/tmp/runtime-migrated-" in os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    fixture = json.loads(settings)
    assert fixture["tool"] in {"create_note", "create_task"}
    assert fixture["phase"] in {"write", "recover"}
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    model = AsyncMock(side_effect=AssertionError("No model required for saved receipt recovery"))
    monkeypatch.setattr("litellm.acompletion", model)
    if fixture.get("reported") and fixture["phase"] == "write":
        import asyncio

        async def generic_handler(args, tool_context):
            # Synthetic provider uses real private CRM storage, without the native
            # handler's reserved-ID/independent-readback opt-in.
            identifier = await asyncio.to_thread(
                getattr(dal, fixture["tool"]), **args, tenant_id=tool_context.tenant_id
            )
            assert isinstance(identifier, str)
            return {"id": identifier, "status": "accepted"}

        monkeypatch.setattr(dispatch, "_get_handlers", lambda: {fixture["tool"]: generic_handler})
    if fixture["phase"] == "recover":

        async def forbidden(*args, **kwargs):
            raise AssertionError("Replacement worker attempted a second write")

        monkeypatch.setattr(dispatch, "_dispatch_admitted", forbidden)
    ctx = ExecutionContext(fixture["tenant"], "service:main", fixture["request"])
    token = active_context.set(ctx)
    try:
        result = await dispatch._execute_tool(
            fixture["tool"],
            {"title": "Once", "body": "Synthetic"},
            agent_id="main",
            run_id=str(uuid4()),
            tenant_id=ctx.tenant_id,
            user_id=ctx.principal_id,
            user_role="service",
        )
        assert not result.get("error"), result
        model.assert_not_awaited()
        if fixture["phase"] == "write":
            os._exit(76)  # Lose the worker after durable success, before reply delivery.
        print("SAVED_RECEIPT " + json.dumps(result), flush=True)
    finally:
        active_context.reset(token)


@pytest.mark.parametrize("tool,table", [("create_note", "crm_notes"), ("create_task", "crm_tasks")])
async def test_concurrent_readback_and_replay_share_one_verified_receipt(monkeypatch, tool, table):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from robothor.crm import dal
    from robothor.engine.runtime import note_recovery, task_recovery

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant = "receipt-race-" + uuid4().hex
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    adapter = task_recovery if tool == "create_task" else note_recovery
    real_verify = adapter.verify
    monkeypatch.setattr(adapter, "verify", lambda record: effects.Verification("unknown"))
    ctx = ExecutionContext(tenant, "service:main", str(uuid4()))

    async def call():
        token = active_context.set(ctx)
        try:
            return await dispatch._execute_tool(
                tool,
                {"title": "Concurrent recovery", "body": "Synthetic"},
                agent_id="main",
                run_id=str(uuid4()),
                tenant_id=tenant,
                user_id=ctx.principal_id,
                user_role="service",
            )
        finally:
            active_context.reset(token)

    first = await call()
    assert first["outcome_unknown"] and first["retryable"] is False
    before = effects.read(ctx, first["effect_id"])
    assert before["state"] == "uncertain"
    readers = 20
    barrier = Barrier(readers, timeout=10)

    def simultaneous_verdict(record):
        verdict = real_verify(record)
        assert verdict.outcome == "applied"
        # Every verifier reads the same unresolved version before any can commit.
        # No DB connection is held while waiting at this synchronization barrier.
        barrier.wait()
        return verdict

    monkeypatch.setattr(adapter, "verify", simultaneous_verdict)

    def recover_together():
        with ThreadPoolExecutor(max_workers=readers) as pool:
            return list(
                pool.map(lambda _: adapter.recover(ctx, first["effect_id"]), range(readers))
            )

    receipts = await asyncio.to_thread(recover_together)
    assert all(receipt and receipt["recovered"] for receipt in receipts)
    assert len({receipt["id"] for receipt in receipts}) == 1
    after = effects.read(ctx, first["effect_id"])
    assert after["state"] == "confirmed" and after["version"] == before["version"] + 1

    async def forbidden(*args, **kwargs):
        raise AssertionError("A recovered request dispatched another business write")

    monkeypatch.setattr(dispatch, "_dispatch_admitted", forbidden)
    repeated = await asyncio.gather(*(call() for _ in range(readers)))
    assert all(receipt["id"] == receipts[0]["id"] and receipt["recovered"] for receipt in repeated)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table} WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (1,)
        cur.execute("SELECT count(*) FROM agent_runtime_effects WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (1,)
