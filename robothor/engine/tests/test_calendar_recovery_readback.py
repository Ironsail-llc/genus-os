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
