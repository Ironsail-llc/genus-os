"""A failed provider read cannot establish reconciliation of an interrupted write."""

from copy import deepcopy

import pytest

from robothor.engine import calendar_operations as operations
from robothor.engine.tests.test_calendar_operations import (  # noqa: F401
    calendar_api,
    ctx,  # noqa: F811
    draft,
    google,  # noqa: F811
    store,  # noqa: F811
)
from robothor.goals.tests.test_store import private_database  # noqa: F401


@pytest.fixture
def db_dsn(private_database):  # noqa: F811
    return private_database


@pytest.mark.parametrize("bad_read", [{"error": "read unavailable"}, {}, {"id": "different-event"}])
def test_failed_read_remains_pending_and_later_read_never_repeats_write(
    store,  # noqa: F811
    google,  # noqa: F811
    ctx,  # noqa: F811
    monkeypatch,
    bad_read,
):
    prepared = draft(ctx)
    operation_id = prepared["operation_id"]
    with store() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE calendar_operations SET status='executing' WHERE id=%s", (operation_id,)
        )
    # The external system contains the effect, but the first recovery read cannot see it.
    google.event["attendees"].append({"email": "sam@example.com"})
    actual = google.request

    def unreadable(method, *args, **kwargs):
        assert method == "GET"
        return deepcopy(bad_read)

    monkeypatch.setattr(google, "request", unreadable)
    result = operations.perform({"operation_id": operation_id}, ctx)
    assert result["reconciliation_pending"] is True
    assert "attendees_present" not in result
    pending = operations.load_operation(operation_id, ctx.tenant_id, ctx.user_id, ctx.agent_id)
    assert pending["status"] == "executing"
    assert pending["result"]["reconciliation_pending"] is True
    assert "repair_task_id" not in result  # a transient read does not demand human verification
    monkeypatch.setattr(google, "request", actual)
    reconciled = operations.perform({"operation_id": operation_id}, ctx)
    assert reconciled["attendees_present"] == ["sam@example.com"]
    assert reconciled["invitations_requested"] is None
    assert all(method == "GET" for method, _args in google.calls)


def test_chat_receipt_read_uses_the_real_calendar_operation_result(store, google, ctx):  # noqa: F811
    from psycopg2.extras import Json, RealDictCursor

    from robothor.engine.chat_receipts import calendar_receipts, receipt_summary

    prepared = draft(ctx)
    result = operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    assert result["verification"] == "verified"
    with store() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""CREATE TABLE agent_run_steps (
            run_id TEXT,step_number INTEGER,tool_name TEXT,tool_input JSONB,tool_output JSONB)""")
        cur.execute(
            "INSERT INTO agent_run_steps VALUES (%s,1,'gws_calendar_add_attendees',%s,%s)",
            (ctx.run_id, Json({"operation_id": prepared["operation_id"]}), Json(result)),
        )
        receipts = calendar_receipts(cur, {"id": ctx.run_id, "agent_id": ctx.agent_id}, ctx)
    assert len(receipts) == 1 and receipts[0]["verified"]
    assert "recorded as complete and verified" in receipt_summary(receipts)
    assert "delivery is not verified" in receipt_summary(receipts)
    assert sum(method == "PATCH" for method, _args in google.calls) == 1


def age_operation(connection, operation_id):
    with connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE calendar_operations SET updated_at=now()-interval '1 minute' WHERE id=%s",
            (operation_id,),
        )


def test_background_readback_retries_only_reads_and_keeps_notification_uncertainty(
    store,  # noqa: F811
    google,  # noqa: F811
    ctx,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    from robothor.engine.calendar_reconciliation import reconcile_record

    operation_id = draft(ctx)["operation_id"]
    with store() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE calendar_operations SET status='executing' WHERE id=%s", (operation_id,)
        )
    age_operation(store, operation_id)
    google.event["attendees"].append({"email": "sam@example.com"})
    original = google.request
    failed_reads = []

    def unavailable(method, *args):
        failed_reads.append(method)
        return {"error": "offline"}

    monkeypatch.setattr(google, "request", unavailable)
    reconcile_record(operation_id, ctx, ctx.agent_id)
    reconcile_record(operation_id, ctx, ctx.agent_id)
    assert failed_reads == ["GET"]  # durable cooldown, not another provider request
    pending = operations.load_operation(operation_id, ctx.tenant_id, ctx.user_id, ctx.agent_id)
    assert pending["status"] == "executing"
    assert pending["result"]["reconciliation_pending"]
    age_operation(store, operation_id)
    monkeypatch.setattr(google, "request", original)
    reconcile_record(operation_id, ctx, ctx.agent_id)
    reconciled = operations.load_operation(operation_id, ctx.tenant_id, ctx.user_id, ctx.agent_id)
    assert reconciled["result"]["attendees_present"] == ["sam@example.com"]
    assert reconciled["result"]["invitations_requested"] is None
    assert reconciled["status"] == "blocked"
    assert all(method == "GET" for method, _ in google.calls)
    assert "repair_task_id" not in reconciled["result"]


@pytest.mark.parametrize("refusal", ["tenant", "user", "agent", "draft", "completed", "locked"])
def test_background_readback_cannot_cross_scope_or_race_writer(
    store,  # noqa: F811
    google,  # noqa: F811
    ctx,  # noqa: F811
    refusal,  # noqa: F811
):
    import hashlib
    from types import SimpleNamespace

    from robothor.engine.calendar_reconciliation import reconcile_record

    operation_id = draft(ctx)["operation_id"]
    status = refusal if refusal in {"draft", "completed"} else "executing"
    with store() as conn, conn.cursor() as cur:
        cur.execute("UPDATE calendar_operations SET status=%s WHERE id=%s", (status, operation_id))
    age_operation(store, operation_id)
    auth = SimpleNamespace(**vars(ctx))
    if refusal in {"tenant", "user", "agent"}:
        setattr(auth, refusal + "_id", "other")
    calls_before = len(google.calls)
    with store() as conn, conn.cursor() as cur:
        resource = f"{ctx.tenant_id}\0owner@example.com\0meeting"
        lock = int.from_bytes(hashlib.sha256(resource.encode()).digest()[:8], "big", signed=True)
        if refusal == "locked":
            cur.execute("SELECT pg_advisory_lock(%s)", (lock,))
        try:
            reconcile_record(operation_id, auth, auth.agent_id)
        finally:
            if refusal == "locked":
                cur.execute("SELECT pg_advisory_unlock(%s)", (lock,))
    assert len(google.calls) == calls_before


@pytest.mark.parametrize("terminal,pending", [(False, True), (True, False)])
def test_background_job_rechecks_run_state_before_provider_access(monkeypatch, terminal, pending):
    from unittest.mock import Mock

    from robothor.engine import calendar_reconciliation, chat_recovery

    readback = Mock()
    monkeypatch.setattr(calendar_reconciliation, "reconcile_record", readback)
    monkeypatch.setattr(
        chat_recovery,
        "read_outcome",
        lambda *args: {
            "terminal": terminal,
            "reconciliation_pending": pending,
            "agent_id": "main",
            "effects": [{"status": "executing", "operation_id": "stale"}],
        },
    )
    calendar_reconciliation.reconcile_outcome(object(), "session", "request")
    readback.assert_not_called()
