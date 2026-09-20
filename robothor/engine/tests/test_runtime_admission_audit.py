"""Pre-execution expiry produces a scoped recoverable audit, never a replay."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from robothor.engine.chat_recovery import read_outcome
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.runtime.chat_control import request_key
from robothor.engine.runtime.deadlines import RuntimeDeadlineError
from robothor.engine.tests.test_chat_per_user_sessions import chat_app, mock_runner  # noqa: F401
from robothor.engine.tests.test_chat_recovery import identity, records  # noqa: F401
from robothor.goals.tests.test_store import private_database  # noqa: F401


@pytest.fixture
def audit_store(records, monkeypatch):  # noqa: F811
    # Keep the real tracking DAL. The shared recovery fixture has a minimal
    # run table; add the other columns required by create_run/update_run.
    with records() as conn, conn.cursor() as cur:
        cur.execute("""ALTER TABLE agent_runs
            ADD COLUMN IF NOT EXISTS user_role TEXT DEFAULT '',
            ADD COLUMN IF NOT EXISTS trigger_type TEXT,
            ADD COLUMN IF NOT EXISTS trigger_detail TEXT,
            ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS model_used TEXT,
            ADD COLUMN IF NOT EXISTS system_prompt_chars INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS user_prompt_chars INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS task_text TEXT,
            ADD COLUMN IF NOT EXISTS tools_provided TEXT[],
            ADD COLUMN IF NOT EXISTS delivery_mode TEXT,
            ADD COLUMN IF NOT EXISTS nesting_depth INTEGER DEFAULT 0,
            ADD COLUMN IF NOT EXISTS task_id UUID,
            ADD COLUMN IF NOT EXISTS person_id UUID""")
    monkeypatch.setattr("robothor.engine.tracking.get_connection", records)


@pytest.mark.parametrize("seconds", [-1, 0.02])
async def test_expired_admission_is_recovered_without_execution(records, audit_store, seconds):  # noqa: F811
    auth, client = identity(), str(uuid4())
    request = RunRequest(
        ExecutionContext(
            auth.tenant_id,
            auth.user_id,
            request_key(auth, "web:main", client),
            deadline=datetime.now(UTC) + timedelta(seconds=seconds),
        ),
        "main",
        "Go",
    )
    execute = AsyncMock()

    async def stalled(event):
        await asyncio.Event().wait()

    with pytest.raises(RuntimeDeadlineError):
        await CurrentRuntime(execute, audit_admission=True).run(request, stalled)
    execute.assert_not_awaited()
    outcome = read_outcome(auth, "web:main", client)
    assert outcome["terminal"] and outcome["state"] == "timeout"
    assert "expired before execution began" in outcome["text"]
    assert not outcome["verified"] and outcome["effects"] == []
    with records() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT completed_at,runtime_context FROM agent_runs WHERE id=%s",
            (outcome["run_id"],),
        )
        completed_at, context = cur.fetchone()
    assert completed_at is not None
    assert context["tenant_id"] == auth.tenant_id
    assert context["principal_id"] == auth.user_id
    assert context["request_id"] == request.context.request_id
    assert context["deadline"] == request.context.deadline.isoformat()
    assert read_outcome(identity(), "web:main", client)["state"] == "not_found"
    assert read_outcome(auth, "other-session", client)["state"] == "not_found"


async def test_execution_expiry_does_not_create_duplicate_admission_run(monkeypatch):
    audit = AsyncMock()
    monkeypatch.setattr("robothor.engine.runtime.admission_audit.record_timeout", audit)

    async def executing(**kwargs):
        await asyncio.Event().wait()

    request = RunRequest(
        ExecutionContext(
            "tenant", "owner", "request", deadline=datetime.now(UTC) + timedelta(seconds=0.02)
        ),
        "main",
        "Go",
    )
    with pytest.raises(RuntimeDeadlineError):
        await CurrentRuntime(executing, audit_admission=True).run(request)
    audit.assert_not_awaited()


async def test_failed_admission_audit_preserves_original_timeout(monkeypatch):
    monkeypatch.setattr(
        "robothor.engine.tracking.create_run",
        lambda run: (_ for _ in ()).throw(ConnectionError("unavailable")),
    )
    execute = AsyncMock()
    request = RunRequest(
        ExecutionContext(
            "tenant", "owner", "request", deadline=datetime.now(UTC) - timedelta(seconds=1)
        ),
        "main",
        "Go",
    )
    with pytest.raises(RuntimeDeadlineError, match="new dispatch denied"):
        await CurrentRuntime(execute, audit_admission=True).run(request)
    execute.assert_not_awaited()


async def test_native_entrypoint_enables_admission_audit(records, audit_store):  # noqa: F811
    from types import SimpleNamespace

    from robothor.engine.runtime.current import active_context, runtime_entrypoint

    auth, client = identity(), str(uuid4())

    class Runner:
        config = SimpleNamespace(tenant_id=auth.tenant_id)

        @runtime_entrypoint
        async def execute(self, agent_id, message, user_id="", tenant_id=None, correlation_id=None):
            pytest.fail("expired request entered native execution")

    token = active_context.set(
        ExecutionContext(
            auth.tenant_id,
            auth.user_id,
            request_key(auth, "web:main", client),
            deadline=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    try:
        with pytest.raises(RuntimeDeadlineError):
            await Runner().execute("main", "Go", user_id=auth.user_id)
    finally:
        active_context.reset(token)
    outcome = read_outcome(auth, "web:main", client)
    assert outcome["terminal"] and "before execution began" in outcome["text"]


async def test_chat_recovers_admission_expiry_without_repeating_request(
    records,  # noqa: F811
    audit_store,
    chat_app,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
):
    from dataclasses import replace

    from httpx import ASGITransport, AsyncClient

    from robothor.engine.runtime.current import active_context

    auth, client_id = identity(), str(uuid4())
    monkeypatch.setattr("robothor.engine.chat._auth_context", lambda request: auth)
    monkeypatch.setattr("robothor.engine.chat._resolve_webchat_identity", lambda auth: None)
    execute = AsyncMock()

    async def expire(**kwargs):
        context = replace(active_context.get(), deadline=datetime.now(UTC) - timedelta(seconds=1))
        return await CurrentRuntime(execute, audit_admission=True).run(
            RunRequest(context, kwargs["agent_id"], kwargs["message"])
        )

    mock_runner.execute = AsyncMock(side_effect=expire)
    async with AsyncClient(transport=ASGITransport(app=chat_app), base_url="http://test") as client:
        response = await client.post(
            "/chat/send", json={"message": "Go", "session_key": "web:main", "request_id": client_id}
        )
        assert response.status_code == 200
        assert "event: error" in response.text
        outcome = await client.get(
            "/chat/outcome", params={"session_key": "web:main", "request_id": client_id}
        )
    assert outcome.status_code == 200
    assert outcome.json()["terminal"]
    assert "expired before execution began" in outcome.json()["text"]
    mock_runner.execute.assert_awaited_once()
    execute.assert_not_awaited()
