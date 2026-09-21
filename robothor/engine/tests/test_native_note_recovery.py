"""Real CRM writes/readback on disposable canonical storage; no model or live provider."""

import os
from uuid import uuid4

import psycopg2
import pytest

from robothor.engine.runtime import ExecutionContext, effects, note_recovery
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.effect_recovery import sweep_terminal
from robothor.engine.tools import dispatch

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("deferred", [False, True])
async def test_committed_note_response_loss_recovers_without_another_write(monkeypatch, deferred):
    from robothor.crm import dal

    dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN", "")
    if "host=/tmp/runtime-migrated-" not in dsn:
        pytest.skip("requires disposable canonical migration harness")
    tenant, run_id = "note-recovery-" + uuid4().hex, str(uuid4())
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO crm_tenants(id,display_name) VALUES (%s,%s)", (tenant, tenant))
        cur.execute(
            "INSERT INTO agent_runs(id,tenant_id,agent_id,trigger_type,status) VALUES (%s,%s,'main','manual','failed')",
            (run_id, tenant),
        )
    monkeypatch.setattr(dal, "get_connection", effects.get_connection)
    monkeypatch.setattr(dal, "_safe_audit", lambda *a, **k: None)
    real_create, real_verify = dal.create_note, note_recovery.verify
    calls = []

    def lose_response(*args, **kwargs):
        identifier = real_create(*args, **kwargs)
        assert identifier
        calls.append(identifier)
        raise TimeoutError("synthetic acknowledgement lost after the real CRM commit")

    monkeypatch.setattr(dal, "create_note", lose_response)
    if deferred:
        monkeypatch.setattr(note_recovery, "verify", lambda row: effects.Verification("unknown"))
    ctx = ExecutionContext(tenant, "service:main", str(uuid4()))

    async def call():
        token = active_context.set(ctx)
        try:
            return await dispatch._execute_tool(
                "create_note",
                {"title": "Synthetic recovery", "body": "Once"},
                agent_id="main",
                run_id=run_id,
                tenant_id=tenant,
                user_id="service:main",
                user_role="service",
            )
        finally:
            active_context.reset(token)

    result = await call()
    if deferred:
        assert result["outcome_unknown"]
        assert effects.read(ctx, result["effect_id"])["state"] == "uncertain"
        monkeypatch.setattr(note_recovery, "verify", real_verify)
        sweep_terminal(tenant)
        result = await call()
    assert result["recovered"] and result["id"] == calls[0]
    assert result["title"] == "Synthetic recovery"
    assert effects.read(ctx, result["effect_id"])["state"] == "confirmed"
    repeated = await call()
    assert repeated["id"] == result["id"] and len(calls) == 1
    assert effects.active_effect.get() is None
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM crm_notes WHERE tenant_id=%s", (tenant,))
        assert cur.fetchone() == (1,)

    # Reconnection reads the actual receipt even though the original run failed.
    from types import SimpleNamespace

    from robothor.engine.chat_recovery import read_outcome
    from robothor.engine.runtime.chat_control import request_key

    auth = SimpleNamespace(tenant_id=tenant, user_id="service:main")
    client = str(uuid4())
    key = request_key(auth, "web:main", client)
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_runs SET user_id=%s,correlation_id=%s WHERE id=%s",
            (auth.user_id, key, run_id),
        )
    outcome = read_outcome(auth, "web:main", client)
    assert outcome["state"] == "failed" and outcome["terminal"]
    assert not outcome["reconciliation_pending"] and not outcome["verified"]
    assert outcome["effects"][0]["verified"]
    assert "The CRM note was created" in outcome["text"]
    assert len(calls) == 1

    # Deliver the same saved facts through the actual authenticated reconnect route.
    import json
    from pathlib import Path
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from robothor.auth.deps import AuthContext
    from robothor.engine import chat

    auth = AuthContext(tenant_id=tenant, user_id="service:main", role="owner", typ="user")
    monkeypatch.setattr(chat, "_sessions", {})
    forbidden_model = AsyncMock(side_effect=AssertionError("Reconnect must not rerun the model"))
    monkeypatch.setattr("litellm.acompletion", forbidden_model)
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request, call_next):
        request.state.auth = auth
        return await call_next(request)

    app.include_router(chat.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        for _ in range(2):
            response = await http.get(
                "/chat/outcome", params={"request_id": client, "session_key": "web:main"}
            )
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == outcome
    assert len(calls) == 1
    forbidden_model.assert_not_awaited()
    artifact_dir = os.environ.get("ROBOTHOR_RUNTIME_NOTE_CHAT_ARTIFACT_DIR")
    if artifact_dir:
        directory = Path(artifact_dir)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ("deferred.json" if deferred else "immediate.json")).open("x") as stream:
            json.dump(
                {
                    "scope": "Synthetic response loss after real isolated CRM commit; native dispatcher and authenticated HTTP reconnect, no model calls",
                    "outcome": outcome,
                    "write_calls": len(calls),
                    "reconnect_model_calls": forbidden_model.await_count,
                    "manual_acceptance": False,
                },
                stream,
                indent=2,
            )
