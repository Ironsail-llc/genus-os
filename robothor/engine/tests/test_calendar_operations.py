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
def store(monkeypatch, db_dsn):
    from psycopg2.extensions import parse_dsn

    from robothor.db.connection import assert_test_database

    assert_test_database(parse_dsn(db_dsn).get("dbname", ""))
    schema = "calendar_" + uuid4().hex
    try:
        admin = psycopg2.connect(db_dsn, connect_timeout=3)
    except psycopg2.OperationalError as exc:
        pytest.fail(f"Calendar integration database unavailable: {type(exc).__name__}")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        migrations = Path(__file__).parents[3] / "crm/migrations"
        for name in ("126_calendar_operations.sql", "127_calendar_operation_etag.sql"):
            cur.execute((migrations / name).read_text())

    @contextmanager
    def connection():
        conn = psycopg2.connect(db_dsn, options=f"-c search_path={schema}")
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
    """Replace Google and contact screening. NOTHING else.

    This fixture used to monkeypatch ``gws._handle_gws_tool`` itself into a
    direct ``add_attendees`` call, which quietly dropped the tool's own
    validation, the cancellation check and the ``calendar`` identity block —
    i.e. it replaced the code these tests exist to exercise.
    """
    api = calendar_api
    from robothor.engine.tools.handlers import gws

    monkeypatch.setattr("robothor.engine.calendar_transport.CalendarTransport", lambda: api)
    monkeypatch.setattr(gws, "_dnc_refusal", lambda *args, **kwargs: None)
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


@pytest.mark.integration
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


@pytest.mark.integration
def test_scope_and_argument_change_refused(store, google, ctx):
    prepared = draft(ctx)
    args = {"operation_id": prepared["operation_id"]}
    other = SimpleNamespace(**{**vars(ctx), "user_id": "someone-else"})
    assert "error" in operations.perform(args, other)
    assert "error" in operations.perform({**args, "attendees": ["wrong@example.com"]}, ctx)
    assert len(google.calls) == 1


@pytest.mark.integration
def test_changed_meeting_requires_new_draft(store, google, ctx):
    prepared = draft(ctx)
    google.event["start"] = {"dateTime": "2026-09-23T16:00:00-04:00"}
    result = operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    assert "changed since the draft" in result["error"]
    assert result["repair_task_id"] == "repair-fixture"
    assert all(c[0] == "GET" for c in google.calls)
    assert draft(ctx)["status"] == "draft"


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
def test_operation_id_with_draft_flag_cannot_execute(store, google, ctx):
    prepared = draft(ctx)
    result = operations.perform({"operation_id": prepared["operation_id"], "draft": True}, ctx)
    assert result["invitations_requested"] is False
    assert len(google.calls) == 1


@pytest.mark.integration
def test_older_draft_cannot_bypass_uncertain_operation(store, google, ctx):
    first = draft(ctx)
    second = draft(ctx)
    google.failure = {"error": "timeout", "outcome_unknown": True}
    uncertain = operations.perform({"operation_id": first["operation_id"]}, ctx)
    assert uncertain["invitations_requested"] is None
    writes = sum(call[0] == "PATCH" for call in google.calls)
    google.failure = None
    result = operations.perform({"operation_id": second["operation_id"]}, ctx)
    assert "reconciliation" in result["error"]
    assert sum(call[0] == "PATCH" for call in google.calls) == writes


async def test_the_feature_flag_is_a_real_kill_switch(monkeypatch):
    """A neutered flag admitted the whole operation and nothing failed."""
    from robothor.engine.tools.dispatch import ToolContext
    from robothor.engine.tools.handlers import gws as gws_handlers
    from robothor.settings import reset_settings

    monkeypatch.delenv("ROBOTHOR_CALENDAR_OPERATIONS_ENABLED", raising=False)
    reset_settings()
    reached = []
    monkeypatch.setattr(
        "robothor.engine.calendar_operations.perform",
        lambda *args, **kwargs: reached.append(args) or {},
    )
    try:
        result = await gws_handlers.HANDLERS["gws_calendar_add_attendees"](
            {"event_id": "meeting", "attendees": ["sam@example.com"]},
            ToolContext(agent_id="main", run_id="run", tenant_id="fixture", user_id="owner"),
        )
    finally:
        reset_settings()
    assert "not enabled" in result["error"]
    assert reached == []


@pytest.mark.integration
def test_an_unverified_requester_reaches_neither_the_store_nor_google(store, google):
    """Without a tenant AND a user there is nobody to scope the record to."""
    for anonymous in (
        SimpleNamespace(tenant_id="", user_id="owner", agent_id="main", run_id="run"),
        SimpleNamespace(tenant_id="fixture", user_id="", agent_id="main", run_id="run"),
    ):
        result = operations.perform(
            {"calendar_id": "owner@example.com", "event_id": "meeting", "attendees": ["sam@example.com"]},
            anonymous,
        )
        assert result == {"error": "A verified requester and tenant are required"}
    assert google.calls == []


@pytest.mark.integration
def test_a_completion_between_the_first_read_and_the_lock_is_seen(store, google, ctx, monkeypatch):
    """The module docstring exists for this re-read: another confirmation can
    complete the operation while this one waits for the resource lock."""
    prepared = draft(ctx)
    args = {"operation_id": prepared["operation_id"]}
    stale = operations.load_operation(
        prepared["operation_id"], ctx.tenant_id, ctx.user_id, ctx.agent_id
    )
    assert stale["status"] == "draft"
    operations.perform(args, ctx)
    writes = sum(c[0] == "PATCH" for c in google.calls)

    monkeypatch.setattr(operations, "load_operation", lambda *a, **kw: dict(stale))
    result = operations.perform(args, ctx)
    assert result["replayed"] is True
    assert sum(c[0] == "PATCH" for c in google.calls) == writes


@pytest.mark.integration
def test_a_malformed_operation_id_never_reaches_the_store(store, google, ctx):
    result = operations.perform({"operation_id": "not-a-uuid'; DROP TABLE --"}, ctx)
    assert result == {"error": "Invalid calendar operation id"}
    assert google.calls == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "args",
    [
        {"attendees": ["sam@example.com"]},
        {"event_id": "", "attendees": ["sam@example.com"]},
        {"event_id": "meeting"},
        {"event_id": "meeting", "attendees": []},
        {"event_id": 7, "attendees": ["sam@example.com"]},
    ],
)
def test_an_incomplete_request_is_refused_before_anything_happens(store, google, ctx, args):
    result = operations.perform({"calendar_id": "owner@example.com", **args}, ctx)
    assert result["error"] == "event_id and attendees are required"
    assert google.calls == []


@pytest.mark.integration
@pytest.mark.parametrize(
    "bad", ["not an email", "sam@example", "sam<@>example.com", "", "sam@example.com,bob@example.com", 5, None]
)
def test_an_invalid_attendee_address_is_refused_before_anything_happens(store, google, ctx, bad):
    result = operations.perform(
        {"calendar_id": "owner@example.com", "event_id": "meeting", "attendees": [bad]}, ctx
    )
    assert result["error"] == "Invalid attendee email"
    assert google.calls == []


@pytest.mark.integration
def test_a_draft_that_never_captured_the_meeting_cannot_be_confirmed(store, google, ctx):
    """A draft with no snapshot cannot detect drift, so it cannot be executed."""
    prepared = draft(ctx)
    with store() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE calendar_operations SET draft_event=NULL WHERE id=%s",
            (prepared["operation_id"],),
        )
    result = operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    assert "prepare a new draft" in result["error"]
    assert sum(c[0] == "PATCH" for c in google.calls) == 0


def occurrence(ctx, draft_only=True):
    """Google names an occurrence ``<master id>_<instance timestamp>``."""
    return operations.perform(
        {
            "calendar_id": "owner@example.com",
            "event_id": "meeting_20260922T160000Z",
            "attendees": ["sam@example.com"],
            **({"draft": True} if draft_only else {}),
        },
        ctx,
    )


@pytest.mark.integration
def test_a_series_is_one_lock_across_its_master_and_its_occurrences(store, google, ctx):
    """The lock keyed on ``event_id``, so a master and an occurrence of the
    same series took DIFFERENT locks and two engines could race one series."""
    import hashlib

    prepared = occurrence(ctx)
    digest = hashlib.sha256(b"fixture\0owner@example.com\0meeting").digest()
    key = int.from_bytes(digest[:8], "big", signed=True)
    with store() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (key,))
        result = operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    assert "Another operation" in result["error"]


@pytest.mark.integration
def test_an_uncertain_master_write_also_holds_back_its_occurrences(store, google, ctx):
    first = draft(ctx)
    google.failure = {"error": "timeout", "outcome_unknown": True}
    assert (
        operations.perform({"operation_id": first["operation_id"]}, ctx)["invitations_requested"]
        is None
    )
    google.failure = None
    assert "reconciliation" in occurrence(ctx)["error"]


def direct(ctx, attendees=("sam@example.com",)):
    """A write with no draft — the other way an operator reaches this event."""
    return operations.perform(
        {
            "calendar_id": "owner@example.com",
            "event_id": "meeting",
            "attendees": list(attendees),
        },
        ctx,
    )


def _die(*args, **kwargs):
    raise RuntimeError("process ended before the write")


@pytest.mark.integration
def test_an_interrupted_write_that_changed_nothing_clears_its_own_barrier(
    store, google, ctx, monkeypatch
):
    """Killed between the executing marker and the PATCH.

    Google is untouched and the operation record can prove it: the readback
    etag is the one recorded before the write and none of the requested
    attendees are present. Arming the uncertainty barrier on that evidence
    blocked every later draft AND every direct write for the meeting forever.
    """
    prepared = draft(ctx)
    from robothor.engine.tools.handlers import gws

    original = gws._handle_gws_tool
    monkeypatch.setattr(gws, "_handle_gws_tool", _die)
    with pytest.raises(RuntimeError):
        operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    monkeypatch.setattr(gws, "_handle_gws_tool", original)
    assert sum(c[0] == "PATCH" for c in google.calls) == 0

    reconciled = operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    assert reconciled["invitations_requested"] is False
    assert reconciled["attendees_present"] == []
    assert sum(c[0] == "PATCH" for c in google.calls) == 0

    assert draft(ctx)["status"] == "draft"
    assert direct(ctx)["verification"] == "verified"


@pytest.mark.integration
def test_an_unfiled_repair_task_never_arms_an_unclearable_barrier(store, google, ctx, monkeypatch):
    """The barrier is cleared by reconciling the repair task. With no task
    filed there is nothing to reconcile, so arming it freezes the meeting."""
    prepared = draft(ctx)
    google.failure = {"error": "timeout", "outcome_unknown": True}
    monkeypatch.setattr("robothor.crm.dal.create_task", lambda **kw: None)
    uncertain = operations.perform({"operation_id": prepared["operation_id"]}, ctx)
    assert uncertain["repair_task_error"]
    assert uncertain["invitations_requested"] is None

    google.failure = None
    assert draft(ctx)["status"] == "draft"
