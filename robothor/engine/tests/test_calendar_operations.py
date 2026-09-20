"""Durability tests use a private schema in the test database and fake Google."""

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

from robothor.engine import calendar_operations as operations
from robothor.engine.tests.test_calendar_attendees import api as calendar_api  # noqa: F401


@pytest.fixture
def store(monkeypatch):
    schema = "calendar_" + uuid4().hex
    try:
        admin = psycopg2.connect(dbname="robothor_test", connect_timeout=3)
    except psycopg2.OperationalError:
        pytest.skip("Local robothor_test database unavailable")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        cur.execute(
            (Path(__file__).parents[3] / "crm/migrations/126_calendar_operations.sql").read_text()
        )

    @contextmanager
    def connection():
        conn = psycopg2.connect(dbname="robothor_test", options=f"-c search_path={schema}")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    monkeypatch.setattr(operations, "get_connection", connection)
    monkeypatch.setattr("robothor.crm.dal.create_task", lambda **kw: "repair-fixture")
    try:
        yield connection
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture
def ctx():
    return SimpleNamespace(tenant_id="fixture", user_id="owner", agent_id="main", run_id="run")


@pytest.fixture
def google(calendar_api, monkeypatch):  # noqa: F811 -- imported pytest fixture
    api = calendar_api
    from robothor.engine.tools.handlers import gws

    monkeypatch.setattr("robothor.engine.calendar_transport.CalendarTransport", lambda: api)
    # Exercise the real merge and readback while replacing only contact screening.
    from robothor.engine.calendar_attendees import add_attendees

    monkeypatch.setattr(
        gws,
        "_handle_gws_tool",
        lambda name, args, **kw: add_attendees(
            args["calendar_id"],
            args["event_id"],
            args["attendees"],
            screen=lambda *a: None,
            expected_event=args.get("_expected_event"),
        ),
    )
    return api


def draft(ctx):
    return operations.perform(
        {
            "calendar_id": "owner@example.com",
            "event_id": "meeting",
            "attendees": ["sam@example.com"],
            "draft": True,
        },
        ctx,
    )


def test_draft_confirm_and_duplicate(store, google, ctx):
    prepared = draft(ctx)
    assert prepared["status"] == "draft"
    assert [c[0] for c in google.calls] == ["GET"]
    args = {"operation_id": prepared["operation_id"]}
    assert operations.perform(args, ctx)["verification"] == "verified"
    before = deepcopy(google.calls)
    assert operations.perform(args, ctx)["replayed"] is True
    assert google.calls == before
    assert sum(c[0] == "PATCH" for c in google.calls) == 1


def test_scope_and_argument_change_refused(store, google, ctx):
    prepared = draft(ctx)
    args = {"operation_id": prepared["operation_id"]}
    other = SimpleNamespace(**{**vars(ctx), "user_id": "someone-else"})
    assert "error" in operations.perform(args, other)
    assert "error" in operations.perform({**args, "attendees": ["wrong@example.com"]}, ctx)
    assert len(google.calls) == 1


def test_changed_meeting_requires_new_draft(store, google, ctx):
    prepared = draft(ctx)
    google.event["start"] = {"dateTime": "2026-09-23T16:00:00-04:00"}
    result = operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    assert "changed since the draft" in result["error"]
    assert result["repair_task_id"] == "repair-fixture"
    assert all(c[0] == "GET" for c in google.calls)
    assert draft(ctx)["status"] == "draft"


def test_crash_after_write_reconciles_without_second_write(store, google, ctx, monkeypatch):
    prepared = draft(ctx)
    from robothor.engine.tools.handlers import gws

    original = gws._handle_gws_tool

    def crash(*a, **kw):
        original(*a, **kw)
        raise RuntimeError("process ended before recording result")

    monkeypatch.setattr(gws, "_handle_gws_tool", crash)
    args = {"operation_id": prepared["operation_id"]}
    with pytest.raises(RuntimeError):
        operations.perform(args, ctx)
    row = operations.load_operation(
        prepared["operation_id"], ctx.tenant_id, ctx.user_id, ctx.agent_id
    )
    assert row["status"] == "executing"
    result = operations.perform(args, ctx)
    assert result["invitations_requested"] is None
    assert result["attendees_present"] == ["sam@example.com"]
    assert sum(c[0] == "PATCH" for c in google.calls) == 1


def test_resource_lock_prevents_overlapping_writes(store, google, ctx):
    import hashlib

    prepared = draft(ctx)
    digest = hashlib.sha256(b"fixture\0owner@example.com\0meeting").digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    with store() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (key,))
        result = operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    assert "Another operation" in result["error"]
    assert len(google.calls) == 1


def test_confirmation_must_be_unambiguous_and_immediately_preceding():
    op = str(uuid4())
    history = [{"role": "assistant", "content": "Calendar operation: " + op}]
    assert operations.confirmation_id("Go!", history) == op
    assert operations.confirmation_id("Go but add Bob", history) is None
    assert operations.confirmation_id("Go", history + [{"role": "user", "content": "No"}]) is None
    history[0]["content"] += "\nCalendar operation: " + str(uuid4())
    assert operations.confirmation_id("Go", history) is None


def test_expired_draft_refuses_without_google_request(store, google, ctx):
    prepared = draft(ctx)
    with store() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE calendar_operations SET created_at=now()-interval '25 hours' WHERE id=%s",
            (prepared["operation_id"],),
        )
    result = operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    assert "expired" in result["error"]
    assert len(google.calls) == 1


def test_operator_interrupt_is_visible_to_worker_without_consuming_it():
    from robothor.engine import session_registry
    from robothor.engine.session import AgentSession

    session = AgentSession(agent_id="fixture")
    session_registry.register(session)
    try:
        signal = operations.OperationCancellation(session.run_id)
        assert not signal.is_set()
        session.interrupt("Stop")
        assert signal.is_set()
        assert session.consume_interrupt() == "Stop"
    finally:
        session_registry.unregister(session)


def test_operation_id_with_draft_flag_cannot_execute(store, google, ctx):
    prepared = draft(ctx)
    result = operations.perform({"operation_id": prepared["operation_id"], "draft": True}, ctx)
    assert result["invitations_requested"] is False
    assert len(google.calls) == 1
