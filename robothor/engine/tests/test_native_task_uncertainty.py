"""A lost task commit acknowledgement must not permit another task creation."""

import os
from contextlib import contextmanager
from uuid import uuid4

import psycopg2
import pytest

from robothor.engine.runtime import ExecutionContext, effects
from robothor.engine.runtime.current import active_context
from robothor.engine.tools import dispatch

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("deferred", [False, True])
async def test_task_commit_acknowledgement_loss_cannot_duplicate_task(monkeypatch, deferred):
    from robothor.crm import dal
    from robothor.engine.runtime import task_recovery

    verify = task_recovery.verify
    if deferred:
        monkeypatch.setattr(task_recovery, "verify", lambda record: effects.Verification("unknown"))

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, run = "task-ack-" + uuid4().hex, str(uuid4())
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
    commits = []

    class LostAcknowledgement:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            self.conn.commit()
            commits.append(True)
            raise OSError("synthetic response loss after the database committed")

    @contextmanager
    def connect():
        with effects.get_connection() as conn:
            yield LostAcknowledgement(conn)

    monkeypatch.setattr(dal, "get_connection", connect)
    ctx = ExecutionContext(tenant, "service:main", str(uuid4()))
    token = active_context.set(ctx)
    try:
        results = [
            await dispatch._execute_tool(
                "create_task",
                {"title": "Synthetic task", "body": "Create once"},
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
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM crm_tasks WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (1,), "lost acknowledgement produced duplicate tasks"
    assert len(commits) == 1
    assert results[0]["effect_id"] == results[1]["effect_id"]
    if deferred:
        assert all(result.get("outcome_unknown") is True for result in results)
        assert all(result.get("retryable") is False for result in results)
        monkeypatch.setattr(task_recovery, "verify", verify)
        task_recovery.sweep(tenant)
    else:
        assert all(result.get("recovered") is True for result in results)
    recovered = task_recovery.recover(ctx, results[0]["effect_id"])
    assert recovered["recovered"] and recovered["title"] == "Synthetic task"
    assert len(commits) == 1
    assert effects.read(ctx, recovered["effect_id"])["state"] == "confirmed"
