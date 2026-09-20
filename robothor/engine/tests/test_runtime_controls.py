"""Runtime controls exercised on a private cluster, including descendants and restart state."""

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

from robothor.engine.runtime import controls
from robothor.goals.tests.test_store import private_database  # noqa: F401

_REAL_STOPPED = controls.stopped
_REAL_ISSUE = controls.issue


@pytest.fixture
def runtime_db(private_database, monkeypatch):  # noqa: F811
    @contextmanager
    def connect():
        conn = psycopg2.connect(private_database)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS agent_runs(id UUID PRIMARY KEY,tenant_id TEXT,parent_run_id UUID)"
        )
        migration = Path(__file__).parents[3] / "crm/migrations/127_runtime_contract.sql"
        cur.execute(migration.read_text())
        cur.execute(migration.read_text())
    monkeypatch.setattr(controls, "get_connection", connect)
    monkeypatch.setattr(controls, "stopped", _REAL_STOPPED)
    monkeypatch.setattr(controls, "issue", _REAL_ISSUE)
    return connect


def test_durable_stop_is_tenant_scoped_and_inherited_after_registry_loss(runtime_db):
    root, child, grandchild = [str(uuid4()) for _ in range(3)]
    with runtime_db() as conn, conn.cursor() as cur:
        for run, parent in [(root, None), (child, root), (grandchild, child)]:
            cur.execute(
                "INSERT INTO agent_runs VALUES (%s,%s,%s,DEFAULT)", (run, "tenant-a", parent)
            )
    assert not controls.stopped("tenant-a", child)
    with pytest.raises(ValueError, match="tenant"):
        controls.issue("tenant-b", root, "cancel")
    receipt = controls.issue("tenant-a", root, "cancel")
    assert receipt["status"] == "stopping"
    for run in (root, child, grandchild):
        assert controls.stopped("tenant-a", run)
        assert not controls.stopped("tenant-b", run)
    # A late pause cannot weaken an already committed cancel.
    assert controls.issue("tenant-a", root, "pause")["action"] == "cancel"
    # A subsequently admitted descendant also sees the ancestor's stop.
    later = str(uuid4())
    with runtime_db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO agent_runs VALUES (%s,%s,%s,DEFAULT)", (later, "tenant-a", root))
    assert controls.stopped("tenant-a", later)


def test_stopped_source_remains_stopped_across_new_runtime_admissions(runtime_db):
    # Rollback changes only new admission. Existing source IDs retain their durable stop.
    old_run, new_run = str(uuid4()), str(uuid4())
    with runtime_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs VALUES (%s,%s,NULL,DEFAULT)", (old_run, "rollback-test")
        )
    controls.issue("rollback-test", old_run, "cancel")
    with runtime_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs VALUES (%s,%s,NULL,DEFAULT)", (new_run, "rollback-test")
        )
    assert controls.stopped("rollback-test", old_run)
    assert not controls.stopped("rollback-test", new_run)
    with runtime_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT runtime_context FROM agent_runs WHERE id=%s", (new_run,))
        assert cur.fetchone()[0]["runtime_id"] == "current"


@pytest.mark.asyncio
async def test_chat_stop_before_admission_denies_provider_and_is_principal_scoped(runtime_db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from robothor.engine.request_budget import RequestBudgetError, bounded_completion
    from robothor.engine.runtime.chat_control import start, stop

    auth = SimpleNamespace(tenant_id="tenant-a", user_id="owner-a")
    session = SimpleNamespace(active_request_id=None, active_task=None)
    client_id = str(uuid4())
    receipt = await stop(session, auth, "shared-session", client_id)
    assert receipt == {"ok": True, "aborted": False, "durable_stopped": True}
    provider = AsyncMock(return_value={"usage": {"total_tokens": 1}})

    async def work():
        return await bounded_completion(provider, messages=[], model="fixture")

    with pytest.raises(RequestBudgetError, match="Durable stop"):
        await start(session, work, auth, "shared-session", client_id)
    provider.assert_not_awaited()
    # A guessed client UUID cannot stop another caller, even in a shared session.
    other = SimpleNamespace(tenant_id="tenant-a", user_id="owner-b")
    await start(session, work, other, "shared-session", client_id)
    provider.assert_awaited_once()


def test_request_stop_survives_restart_and_stops_descendant_runs(runtime_db):
    import json

    parent, child, request_id = [str(uuid4()) for _ in range(3)]
    with runtime_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs VALUES (%s,%s,NULL,%s::jsonb)",
            (parent, "request-test", json.dumps({"request_id": request_id})),
        )
        cur.execute(
            "INSERT INTO agent_runs VALUES (%s,%s,%s,DEFAULT)", (child, "request-test", parent)
        )
    controls.issue_request("request-test", request_id)
    assert controls.stopped("request-test", parent)
    assert controls.stopped("request-test", child)
    assert not controls.stopped("another-tenant", child)


@pytest.mark.asyncio
async def test_measured_durable_stop_cancels_active_descendants(runtime_db, tmp_path):
    import asyncio
    import json
    import os
    import time
    from types import SimpleNamespace

    from bench.interactive.statistics import summary
    from robothor.engine.models import AgentRun
    from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
    from robothor.engine.runtime.activity import register

    samples = []
    for _ in range(30):
        root, child = str(uuid4()), str(uuid4())
        tenant = str(uuid4())
        with runtime_db() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_runs VALUES (%s,%s,NULL,DEFAULT),(%s,%s,%s,DEFAULT)",
                (root, tenant, child, tenant, root),
            )
        started = asyncio.Event()

        async def execute(child=child, tenant=tenant, root=root, started=started, **kwargs):
            run = AgentRun(id=child, tenant_id=tenant, parent_run_id=root)
            register(SimpleNamespace(run=run, run_id=child, interrupt=lambda note: None))
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            CurrentRuntime(execute).run(
                RunRequest(
                    ExecutionContext(tenant, "operator", str(uuid4())), "main", "synthetic work"
                )
            )
        )
        await started.wait()
        start = time.perf_counter()
        receipt = await asyncio.to_thread(controls.issue, tenant, root, "cancel")
        ack = (time.perf_counter() - start) * 1000
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = (time.perf_counter() - start) * 1000
        assert controls.stopped(tenant, child) and receipt["status"] == "stopping"
        samples.append({"stop_ack_ms": ack, "descendant_control_ms": elapsed})
    report = {
        "scope": "private PostgreSQL durable parent stop with an active descendant; synthetic wait, no provider",
        "measurements": samples,
        "stop_ack_ms": summary([s["stop_ack_ms"] for s in samples]),
        "descendant_control_ms": summary([s["descendant_control_ms"] for s in samples]),
    }
    output = os.environ.get("ROBOTHOR_RUNTIME_STOP_OUTPUT")
    if output:
        Path(output).write_text(json.dumps(report, indent=2) + "\n")
    assert report["stop_ack_ms"]["p95"] < 2000
    assert report["descendant_control_ms"]["p95"] < 5000


def test_request_context_does_not_stop_unrelated_runs_in_same_tenant(runtime_db):
    from robothor.engine.runtime.contracts import ExecutionContext
    from robothor.engine.runtime.current import active_context

    request_id, unrelated = str(uuid4()), str(uuid4())
    with runtime_db() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs VALUES (%s,%s,NULL,DEFAULT)", (unrelated, "context-test")
        )
    token = active_context.set(ExecutionContext("context-test", "owner", request_id))
    try:
        controls.issue_request("context-test", request_id)
        assert controls.stopped("context-test", "")
        assert not controls.stopped("context-test", unrelated)
    finally:
        active_context.reset(token)


@pytest.mark.asyncio
async def test_chat_abort_route_commits_before_ack_and_denies_cancel_resistant_work(
    runtime_db, monkeypatch
):
    import asyncio
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from robothor.auth.deps import AuthContext
    from robothor.engine import chat
    from robothor.engine.request_budget import RequestBudgetError, bounded_completion
    from robothor.engine.runtime.chat_control import start

    auth = AuthContext(user_id="owner", tenant_id="route-test", role="owner", typ="user")
    session = chat.ChatSession()
    monkeypatch.setattr(chat, "_get_session", lambda key: session)
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = auth
        return await call_next(request)

    app.include_router(chat.router)
    provider = AsyncMock()
    ready = asyncio.Event()

    async def resistant_work():
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # A transport/library swallowing cancellation must not regain authority.
            await bounded_completion(provider, messages=[], model="fixture")

    client_id = str(uuid4())
    task = start(session, resistant_work, auth, "agent:main:primary", client_id)
    await ready.wait()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/chat/abort", json={"session_key": "agent:main:primary", "request_id": client_id}
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "aborted": True, "durable_stopped": True}
    with runtime_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_runtime_request_stops WHERE tenant_id='route-test'")
        assert cur.fetchone()[0] == 1
    with pytest.raises(RequestBudgetError, match="Durable stop"):
        await task
    provider.assert_not_awaited()
