"""Legacy calendar operation records are settled from evidence, never replayed.

The draft-and-confirm flow that created these rows is retired (attendee changes
are direct writes now, see ``test_gws_calendar_update.py``). What remains is
reading back a record an interrupted write left in ``executing``. These tests
use a private schema in the test database and fake Google.
"""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import Json, RealDictCursor

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
    """Replace Google. Nothing else."""
    api = calendar_api
    monkeypatch.setattr("robothor.engine.calendar_transport.CalendarTransport", lambda: api)
    return api


def draft(ctx, *, status: str = "draft", result: dict[str, Any] | None = None) -> dict[str, Any]:
    """A record as the retired flow left it: arguments stored, nothing executed."""
    with operations.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO calendar_operations
               (tenant_id,user_id,agent_id,calendar_id,event_id,arguments,status,result)
               VALUES (%s,%s,%s,'owner@example.com','meeting',%s,%s,%s) RETURNING id""",
            (
                ctx.tenant_id,
                ctx.user_id,
                ctx.agent_id,
                Json(
                    {
                        "calendar_id": "owner@example.com",
                        "event_id": "meeting",
                        "attendees": ["sam@example.com"],
                    }
                ),
                status,
                Json(result) if result is not None else None,
            ),
        )
        operation_id = str(cur.fetchone()[0])
    return {"operation_id": operation_id}


def load_operation(operation_id: str, tenant: str, user: str, agent: str) -> dict[str, Any] | None:
    with operations.get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM calendar_operations WHERE id=%s AND tenant_id=%s AND user_id=%s AND agent_id=%s",
            (operation_id, tenant, user, agent),
        )
        row = cur.fetchone()
        return dict(row) if row else None


STORED = {"event_id": "meeting", "attendees": ["sam@example.com"]}


def test_an_unchanged_event_proves_the_write_never_landed():
    """Same version, none of the attendees present: nothing to reconcile, so
    no barrier — arming one froze the meeting for good."""
    event = {"id": "meeting", "etag": '"v1"', "attendees": [{"email": "bob@example.com"}]}
    result = operations._reconcile(event, STORED, '"v1"', "owner@example.com", "operator")
    assert result["no_write_confirmed"] is True
    assert result["invitations_requested"] is False
    assert result["verification"] == "verified"
    assert "gws_calendar_update" in result["error"]


def test_a_landed_write_reports_what_is_present_and_never_claims_delivery():
    event = {"id": "meeting", "etag": '"v2"', "attendees": [{"email": "Sam@Example.com"}]}
    result = operations._reconcile(event, STORED, '"v1"', "owner@example.com", "operator")
    assert result["attendees_present"] == ["sam@example.com"]
    assert result["invitations_requested"] is None
    assert result["verification"] == "unverified"


@pytest.mark.parametrize("bad", [{"error": "offline"}, {}, {"id": "other"}, None])
def test_an_unreadable_event_keeps_the_record_pending(bad):
    result = operations._reconcile(bad, STORED, '"v1"', "owner@example.com", "operator")
    assert result["reconciliation_pending"] is True
    assert result["calendar"] == {"kind": "operator", "id": "owner@example.com"}


def test_nothing_here_can_write_to_a_calendar():
    """The executor and the bare-"yes" binding are gone; only readback is left."""
    for gone in ("perform", "confirmation_id", "OperationCancellation", "OPERATION_MARKER"):
        assert not hasattr(operations, gone), gone


@pytest.mark.integration
def test_an_interrupted_record_that_changed_nothing_is_settled_by_one_read(store, google, ctx):
    from robothor.engine.calendar_reconciliation import reconcile_record

    operation_id = draft(ctx, status="executing")["operation_id"]
    with store() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE calendar_operations SET pre_write_etag=%s, updated_at=now()-interval '1 minute' WHERE id=%s",
            (google.event["etag"], operation_id),
        )
    reconcile_record(operation_id, ctx, ctx.agent_id)
    row = load_operation(operation_id, ctx.tenant_id, ctx.user_id, ctx.agent_id)
    assert row["status"] == "blocked"
    assert row["result"]["no_write_confirmed"] is True
    assert [c[0] for c in google.calls] == ["GET"]
