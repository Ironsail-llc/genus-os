"""Authenticated chat recovery checks committed CRM effects without a daemon sweep."""

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from robothor.engine import chat
from robothor.engine.runtime import ExecutionContext, effects
from robothor.engine.tests.test_chat_recovery import (  # noqa: F401
    chat_app,
    identity,
    insert,
    mock_runner,
    private_database,
    records,
)


def record(connect, auth, run, tool, state, *, stored=True):
    ctx = ExecutionContext(auth.tenant_id, auth.user_id, str(uuid4()))
    row = effects.begin(ctx, run, "main", tool, {"title": str(uuid4())})
    if state != "prepared":
        effects.mark_dispatched(ctx, row["id"], run)
    if state == "uncertain":
        effects.finish(ctx, row["id"], run, uncertain=True)
    if stored:
        table = "crm_tasks" if tool == "create_task" else "crm_notes"
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {table}(id,tenant_id,title) VALUES (%s,%s,'Stored result')",
                (row["id"], auth.tenant_id),
            )
    return ctx, row


@pytest.fixture
def crm_records(records, monkeypatch):  # noqa: F811
    monkeypatch.setattr(effects, "get_connection", records)
    with records() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS crm_notes(
            id UUID PRIMARY KEY,tenant_id TEXT,title TEXT,deleted_at TIMESTAMPTZ)""")
    return records


@pytest.mark.parametrize("tool", ["create_task", "create_note"])
@pytest.mark.parametrize("state", ["uncertain", "dispatching"])
async def test_chat_poll_reads_back_original_effect_without_reexecution(
    crm_records,
    chat_app,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
    tool,
    state,
):
    auth, client = identity(), str(uuid4())
    run = insert(crm_records, auth, client, status="failed", verified_status=None)
    ctx, row = record(crm_records, auth, run, tool, state)
    # Even another request by this same user must not be reconciled by this poll.
    unrelated_run = insert(crm_records, auth, str(uuid4()), status="failed")
    unrelated_ctx, unrelated = record(crm_records, auth, unrelated_run, tool, state)
    monkeypatch.setattr(chat, "_auth_context", lambda _: auth)
    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "off")
    forbidden = AsyncMock(side_effect=AssertionError("Recovery cannot run a model or tool"))
    monkeypatch.setattr("litellm.acompletion", forbidden)
    monkeypatch.setattr("robothor.engine.tools.dispatch._execute_tool", forbidden)
    async with AsyncClient(transport=ASGITransport(app=chat_app), base_url="http://test") as http:
        first = await http.get(
            "/chat/outcome", params={"request_id": client, "session_key": "web:main"}
        )
        assert first.status_code == 200 and first.json()["reconciliation_pending"]
        second = await http.get(
            "/chat/outcome", params={"request_id": client, "session_key": "web:main"}
        )
    result = second.json()
    assert result["state"] == "failed" and result["terminal"]
    assert not result["reconciliation_pending"] and not result["verified"]
    assert result["effects"][0]["verified"]
    assert "without creating another" in result["text"]
    assert effects.read(ctx, row["id"])["state"] == "confirmed"
    assert effects.read(unrelated_ctx, unrelated["id"])["state"] == state
    mock_runner.execute.assert_not_called()
    forbidden.assert_not_awaited()


@pytest.mark.parametrize("case", ["live-child", "foreign-tenant", "foreign-user", "missing-record"])
def test_readback_preserves_unresolved_or_foreign_effects(crm_records, case):
    from robothor.engine.runtime.chat_effect_recovery import reconcile_record

    auth, client = identity(), str(uuid4())
    run = insert(crm_records, auth, client, status="running" if case == "live-child" else "failed")
    ctx, row = record(
        crm_records, auth, run, "create_task", "dispatching", stored=case != "missing-record"
    )
    caller = (
        replace(auth, tenant_id=str(uuid4()))
        if case == "foreign-tenant"
        else (replace(auth, user_id="foreign") if case == "foreign-user" else auth)
    )
    reconcile_record(str(row["id"]), caller)
    expected = "uncertain" if case == "missing-record" else "dispatching"
    assert effects.read(ctx, row["id"])["state"] == expected


def test_undispatched_terminal_effect_cannot_later_dispatch(crm_records):
    from robothor.engine.runtime.chat_effect_recovery import reconcile_record

    auth = identity()
    run = insert(crm_records, auth, str(uuid4()), status="cancelled")
    ctx, row = record(crm_records, auth, run, "create_task", "prepared", stored=False)
    reconcile_record(str(row["id"]), auth)
    assert effects.read(ctx, row["id"])["state"] == "not_applied"
    assert not effects.mark_dispatched(ctx, row["id"], run)


def test_nonapplication_remains_visible_and_overrides_a_success_claim(crm_records):
    from robothor.engine import chat_recovery
    from robothor.engine.runtime.chat_effect_recovery import reconcile_record

    auth, client = identity(), str(uuid4())
    run = insert(crm_records, auth, client, output_text="Everything was completed.")
    ctx, row = record(crm_records, auth, run, "create_task", "prepared", stored=False)
    reconcile_record(str(row["id"]), auth)
    result = chat_recovery.read_outcome(auth, "web:main", client)
    assert effects.read(ctx, row["id"])["state"] == "not_applied"
    assert not result["reconciliation_pending"] and not result["verified"]
    assert result["effects"][0]["status"] == "not_applied"
    assert "Everything was completed" not in result["text"]
    assert "action was not applied" in result["text"]


def test_unsupported_effect_remains_unresolved(crm_records):
    from robothor.engine.runtime.chat_effect_recovery import reconcile_record

    auth = identity()
    run = insert(crm_records, auth, str(uuid4()), status="failed")
    ctx, row = record(crm_records, auth, run, "send_email", "uncertain", stored=False)
    reconcile_record(str(row["id"]), auth)
    assert effects.read(ctx, row["id"])["state"] == "uncertain"


def test_calendar_outage_does_not_prevent_independent_crm_readback(crm_records, monkeypatch):
    from robothor.engine import calendar_reconciliation, chat_recovery

    auth, client = identity(), str(uuid4())
    run = insert(crm_records, auth, client, status="failed")
    ctx, row = record(crm_records, auth, run, "create_task", "uncertain")
    outcome = chat_recovery.read_outcome(auth, "web:main", client)
    outcome["effects"].insert(
        0, {"status": "executing", "operation_id": str(uuid4()), "agent_id": "main"}
    )
    monkeypatch.setattr(chat_recovery, "read_outcome", lambda *a: outcome)

    def calendar_unavailable(*args):
        raise OSError("Synthetic provider unavailable")

    monkeypatch.setattr(calendar_reconciliation, "reconcile_record", calendar_unavailable)
    calendar_reconciliation.reconcile_outcome(auth, "web:main", client)
    assert effects.read(ctx, row["id"])["state"] == "confirmed"
