"""Shared candidate lifecycle persisted through the real run/control schema."""

from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

pytest.importorskip("pydantic_ai")
pytest.importorskip("deepagents")

from bench.runtime import recovery, store_host
from bench.runtime.adapter import CandidateRuntime
from bench.runtime.candidates import FixtureGateway
from bench.runtime.test_candidate_boundaries import candidate
from robothor.engine.runtime import controls
from robothor.engine.runtime.contracts import ExecutionContext, RunRequest
from robothor.goals.tests import test_store

private_database = test_store.private_database


@pytest.fixture
def database(private_database, monkeypatch):
    @contextmanager
    def connect():
        conn = psycopg2.connect(private_database)
        conn.set_client_encoding("UTF8")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    root = Path(__file__).resolve().parents[2] / "crm/migrations"
    with connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS crm_tenants(id TEXT PRIMARY KEY)")
        for name in (
            "011_agent_engine.sql",
            "016_sub_agents.sql",
            "100_run_verification.sql",
            "127_runtime_contract.sql",
        ):
            cur.execute((root / name).read_text())
        # Apply the agent_runs additions from the wider access-control migration.
        source = (root / "037_access_control.sql").read_text()
        cur.execute(source[source.index("ALTER TABLE agent_runs") :])
        tenant = str(uuid4())
        cur.execute("INSERT INTO crm_tenants(id) VALUES (%s)", (tenant,))
    monkeypatch.setattr(store_host, "get_connection", connect)
    monkeypatch.setattr(recovery, "get_connection", connect)
    monkeypatch.setattr(controls, "get_connection", connect)
    return tenant, connect


def setup(tenant, name, on_request=lambda: None):
    gateway, calls = FixtureGateway(tenant), []

    async def authorized(req):
        if req.context.principal_id != "operator" or req.options:
            raise ValueError("host authorization denied")
        return gateway

    host = store_host.StoreHost(authorized)
    adapter = candidate(
        name, [("record", {"key": "report", "value": "delivered"})], calls, on_request
    )
    runtime = CandidateRuntime(adapter, host)
    req = RunRequest(
        ExecutionContext(tenant, "operator", str(uuid4())), "synthetic", "record report=delivered"
    )
    return runtime, req, gateway, calls


def rows(connect, tenant):
    with connect() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM agent_runs WHERE tenant_id=%s", (tenant,))
        return cur.fetchall()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_real_schema_records_identity_verification_and_unknown_cost(database, name):
    tenant, connect = database
    runtime, req, gateway, calls = setup(tenant, name)
    result = await runtime.run(req)
    (saved,) = rows(connect, tenant)
    assert result.verified and gateway.writes == len(calls) == 1
    assert saved["status"] == "completed" and saved["verified_status"] == "verified"
    assert saved["user_id"] == req.context.principal_id
    context = saved["runtime_context"]
    assert (
        context["runtime_id"] == name
        and context["runtime_version"] == runtime.identity.runtime_version
    )
    assert context["request_id"] == req.context.request_id
    assert context["usage"]["model_calls"] == 1
    assert context["usage"]["cost_usd"] is None and saved["total_cost_usd"] is None
    assert saved["input_tokens"] == 10 and saved["output_tokens"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_remote_durable_stop_during_model_survives_new_host(database, name):
    tenant, connect = database

    def stop():
        (saved,) = rows(connect, tenant)
        controls.issue(tenant, str(saved["id"]), "cancel")

    runtime, req, gateway, calls = setup(tenant, name, stop)
    result = await runtime.run(req)
    (saved,) = rows(connect, tenant)
    assert result.unresolved and not result.verified
    assert saved["status"] == "cancelled"
    assert gateway.writes == gateway.dispatches == 0 and len(calls) == 1
    assert controls.stopped(tenant, str(saved["id"]))
    replacement, _, _, _ = setup(tenant, name)
    receipt = await replacement.control(tenant, str(saved["id"]), "pause")
    assert receipt["action"] == "cancel"


@pytest.mark.asyncio
async def test_unverified_terminal_result_is_valid_under_real_schema(database):
    tenant, connect = database
    runtime, req, gateway, _ = setup(tenant, "pydantic-ai")

    async def unverified(*args, **kwargs):
        return {"verified": False}

    runtime.candidate.run = unverified
    result = await runtime.run(req)
    (saved,) = rows(connect, tenant)
    assert result.unresolved and saved["status"] == "failed"
    assert saved["verified_status"] == "failed_verification"
    assert saved["input_tokens"] is None
    assert gateway.writes == 0


@pytest.mark.asyncio
async def test_request_stopped_before_admission_has_no_model_call(database):
    tenant, connect = database
    runtime, req, gateway, calls = setup(tenant, "pydantic-ai")
    controls.issue_request(tenant, req.context.request_id)
    with pytest.raises(ValueError, match="already stopped"):
        await runtime.run(req)
    assert not calls and gateway.writes == 0 and rows(connect, tenant) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["stopped-parent", "other-tenant-parent"])
async def test_descendant_admission_respects_persisted_parent_authority(database, condition):
    from dataclasses import replace

    tenant, connect = database
    parent_runtime, parent_request, _, _ = setup(tenant, "pydantic-ai")
    parent = await parent_runtime.run(parent_request)
    child_tenant = tenant
    if condition == "stopped-parent":
        await parent_runtime.control(tenant, parent.run.id, "cancel")
    else:
        child_tenant = str(uuid4())
        with connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO crm_tenants(id) VALUES (%s)", (child_tenant,))
    runtime, req, gateway, calls = setup(child_tenant, "pydantic-ai")
    req = replace(req, context=replace(req.context, parent_id=parent.run.id))
    with pytest.raises(ValueError, match="parent"):
        await runtime.run(req)
    assert not calls and gateway.writes == 0
    assert len(rows(connect, tenant)) == 1


@pytest.mark.asyncio
async def test_late_finalization_cannot_overwrite_terminal_record(database):
    from dataclasses import replace

    from robothor.engine.models import RunStatus

    tenant, connect = database
    runtime, req, _, _ = setup(tenant, "pydantic-ai")
    result = await runtime.run(req)
    altered = replace(
        result, run=replace(result.run, status=RunStatus.FAILED), verified=False, unresolved=True
    )
    with pytest.raises(ValueError, match="finalization"):
        await runtime.host.finish(altered)
    (saved,) = rows(connect, tenant)
    assert saved["status"] == "completed" and saved["runtime_context"]["verified"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "deadline"])
@pytest.mark.parametrize("lost_ack", [False, True])
async def test_cancelled_admission_reconciles_late_database_insert(
    database, monkeypatch, stop, lost_ack
):
    import asyncio
    import threading

    tenant, connect = database
    runtime, req, gateway, calls = setup(tenant, "pydantic-ai")
    if stop == "deadline":
        from dataclasses import replace
        from datetime import UTC, datetime, timedelta

        req = replace(
            req, context=replace(req.context, deadline=datetime.now(UTC) + timedelta(seconds=0.2))
        )
    entered, release = threading.Event(), threading.Event()
    insert = runtime.host._insert

    def delayed_insert(run, metadata):
        entered.set()
        assert release.wait(5), "test did not release the database worker"
        insert(run, metadata)
        if lost_ack:
            raise psycopg2.OperationalError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(runtime.host, "_insert", delayed_insert)
    task = asyncio.create_task(runtime.run(req))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if stop == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if stop == "cancel" else TimeoutError):
            await task
    finally:
        release.set()
    await runtime.host.drain_admissions()
    (saved,) = rows(connect, tenant)
    assert saved["status"] == "cancelled"
    assert saved["runtime_context"]["usage"]["model_calls"] == 0
    assert not calls and gateway.writes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
async def test_crashed_worker_is_recovered_without_repeating_committed_effect(
    database, private_database, name
):
    import asyncio
    import sys

    tenant, connect = database
    with connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS synthetic_crash_receipts(tenant_id TEXT)")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "bench.runtime.crash_fixture",
        private_database,
        tenant,
        name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 23, (stdout, stderr)
    (saved,) = rows(connect, tenant)
    assert saved["status"] == "running"
    replacement, _, gateway, calls = setup(tenant, name)
    assert await replacement.host.recover_expired(tenant) == []  # Active deadline is respected.
    with connect() as conn, conn.cursor() as cur:
        # Expire only the private fixture's deadline to exercise recovery.
        cur.execute(
            """UPDATE agent_runs SET runtime_context=jsonb_set(runtime_context,'{deadline}',
                       to_jsonb((now()-interval '1 second')::text)) WHERE tenant_id=%s""",
            (tenant,),
        )
    other, _, _, _ = setup(tenant, name)
    results = await asyncio.gather(
        replacement.host.recover_expired(tenant), other.host.recover_expired(tenant)
    )
    assert sum(len(result) for result in results) == 1
    (recovered,) = rows(connect, tenant)
    assert recovered["status"] == "timeout"
    assert recovered["runtime_context"]["unresolved"]
    assert recovered["runtime_context"]["usage"]["model_calls"] is None
    assert controls.stopped(tenant, str(saved["id"]))
    assert await other.host.recover_expired(tenant) == []
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM synthetic_crash_receipts WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone()[0] == 1
    assert not calls and gateway.writes == 0
    replay = RunRequest(
        ExecutionContext(tenant, "operator", saved["runtime_context"]["request_id"]),
        "synthetic",
        "repeat original action",
    )
    with pytest.raises(ValueError, match="already stopped"):
        await replacement.run(replay)
    assert not calls and len(rows(connect, tenant)) == 1


@pytest.mark.asyncio
async def test_recovery_preserves_known_usage_and_excludes_other_tenants_and_native_runs(database):
    from psycopg2.extras import Json

    tenant, connect = database
    outsider = str(uuid4())
    ids = [str(uuid4()) for _ in range(3)]
    usage = {"model_calls": 1, "input_tokens": 10, "output_tokens": 3, "cost_usd": None}
    with connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id) VALUES (%s)", (outsider,))
        for run_id, owner, engine in zip(
            ids, [tenant, tenant, outsider], ["pydantic-ai", "current", "deepagents"], strict=True
        ):
            cur.execute(
                """INSERT INTO agent_runs(id,tenant_id,agent_id,trigger_type,status,started_at,
                   input_tokens,output_tokens,total_cost_usd,runtime_context)
                   VALUES (%s,%s,'synthetic','manual','running',now()-interval '2 seconds',10,3,NULL,%s)""",
                (
                    run_id,
                    owner,
                    Json(
                        {
                            "runtime_id": engine,
                            "deadline": "2000-01-01T00:00:00+00:00",
                            "request_id": str(uuid4()),
                            "usage": usage,
                        }
                    ),
                ),
            )
    runtime, _, _, calls = setup(tenant, "pydantic-ai")
    assert await runtime.host.recover_expired(tenant) == [ids[0]]
    saved = {str(row["id"]): row for row in rows(connect, tenant)}
    assert saved[ids[0]]["runtime_context"]["usage"] == usage
    assert saved[ids[0]]["input_tokens"] == 10
    assert saved[ids[1]]["status"] == "running"
    assert rows(connect, outsider)[0]["status"] == "running"
    assert not controls.stopped(tenant, ids[1])
    assert not controls.stopped(outsider, ids[2])
    assert not calls
