"""Lost responses recover the actual existing task chosen by native deduplication."""

import os
from uuid import uuid4

import psycopg2
import pytest

from robothor.engine.runtime import ExecutionContext, effects
from robothor.engine.runtime.current import active_context
from robothor.engine.tools import dispatch
from robothor.goals import store
from robothor.goals.model import CreateGoal
from robothor.goals.runtime import Binding, binding

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode", ["thread", "goal"])
async def test_lost_deduplicated_task_response_recovers_existing_id(monkeypatch, mode):
    from robothor.crm import dal

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, run = "task-dedup-" + uuid4().hex, str(uuid4())
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    monkeypatch.setattr("robothor.events.bus.publish", lambda *a, **k: None)
    goal_token = None
    goal_id = attempt = None
    if mode == "goal":
        store.set_enabled(tenant, True, "operator")
        store.create(
            tenant, CreateGoal(objective="Finish work", success_criteria=["Checked"]), "operator"
        )
        goal, attempt = store.claim(tenant)
        goal_id = goal["id"]
        goal_token = binding.set(Binding(tenant, goal_id, attempt, run_id=run))
    ctx = ExecutionContext(
        tenant, "service:main", str(uuid4()), goal_id=goal_id, attempt_id=attempt
    )
    args = {
        "title": "Existing synthetic task",
        "body": "threadId: synthetic-thread" if mode == "thread" else "Same goal work",
    }
    calls = []
    try:
        existing = dal.create_task(**args, tenant_id=tenant)
        assert isinstance(existing, str)
        handlers = dispatch._get_handlers()
        original = handlers["create_task"]

        async def lose_return(arguments, tool_context):
            result = await original(arguments, tool_context)
            assert str(result["id"]) == existing
            calls.append(True)
            raise TimeoutError("synthetic response lost after native task deduplication")

        monkeypatch.setattr(
            dispatch, "_get_handlers", lambda: {**handlers, "create_task": lose_return}
        )
        token = active_context.set(ctx)
        try:
            results = [
                await dispatch._execute_tool(
                    "create_task",
                    args,
                    agent_id="main",
                    run_id=run,
                    tenant_id=tenant,
                    user_id=ctx.principal_id,
                    user_role="service",
                )
                for _ in range(2)
            ]
        finally:
            active_context.reset(token)
        assert len(calls) == 1
        assert all(result.get("recovered") and result.get("deduplicated") for result in results)
        assert all(result["id"] == existing for result in results)
        assert results[0]["effect_id"] != existing
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM crm_tasks WHERE tenant_id=%s", (tenant,))
            assert cur.fetchone() == (1,)
    finally:
        if goal_token is not None:
            binding.reset(goal_token)
