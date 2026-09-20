"""A failed provider read cannot establish reconciliation of an interrupted write."""

from copy import deepcopy

import pytest

from robothor.engine import calendar_operations as operations
from robothor.engine.tests.test_calendar_operations import (  # noqa: F401
    calendar_api,
    ctx,
    draft,
    google,
    store,
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
