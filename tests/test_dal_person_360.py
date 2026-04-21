"""
Phase 3a — Contact 360 DAL tests.

Exercises the read-path helpers in robothor.crm.dal:
  * get_person_timeline — merged chronological feed
  * get_person_summary  — counts per activity_type + last touch
  * get_person_messages — full bodies joined via message_participant
  * get_person_calls, get_person_events, get_person_runs, get_person_memory

These are the contract the Bridge endpoints and the agent-facing
get_contact_360 tool call through. Tests seed one of each activity type,
then verify shape, ordering, channel filter, and pagination.
"""

from __future__ import annotations

import uuid

import pytest

from robothor.constants import DEFAULT_TENANT

pytestmark = pytest.mark.integration


@pytest.fixture
def populated_contact(db_cursor, db_conn):
    """A person with one of every activity type, each with a distinct
    ``occurred_at`` so ordering is deterministic."""
    person_id = str(uuid.uuid4())
    db_cursor.execute(
        "INSERT INTO crm_people (id, first_name, last_name, email) VALUES (%s, 'Fran', '360', 'fran@example.com')",
        (person_id,),
    )
    db_cursor.execute(
        "INSERT INTO contact_identifiers (channel, identifier, person_id) VALUES ('email', 'fran@example.com', %s)",
        (person_id,),
    )

    def _add_activity(occurred_mins_ago: int, atype: str, src_table: str, src_id: str, **kw):
        db_cursor.execute(
            f"""
            INSERT INTO timeline_activity
                (tenant_id, person_id, occurred_at, activity_type,
                 source_table, source_id, channel, direction, title, snippet)
            VALUES (
                %s, %s,
                NOW() - INTERVAL '{occurred_mins_ago} minutes',
                %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                DEFAULT_TENANT,
                person_id,
                atype,
                src_table,
                src_id,
                kw.get("channel"),
                kw.get("direction"),
                kw.get("title"),
                kw.get("snippet"),
            ),
        )

    _add_activity(
        60, "email", "message", "m-email-1", channel="email", direction="outbound", title="Welcome"
    )
    _add_activity(
        45,
        "telegram_message",
        "message",
        "m-tg-1",
        channel="telegram",
        direction="inbound",
        snippet="hi there",
    )
    _add_activity(30, "calendar_event", "calendar_event", "e-1", channel="calendar", title="Sync")
    _add_activity(20, "note", "crm_notes", "n-1", title="Met at conference")
    _add_activity(10, "task", "crm_tasks", "t-1", title="Follow up")
    _add_activity(5, "agent_run", "agent_runs", "r-1", title="dev-team-ops (completed)")

    return person_id


# ── get_person_timeline ──────────────────────────────────────────────────────


class TestGetPersonTimeline:
    def test_returns_activities_most_recent_first(
        self, db_cursor, db_conn, populated_contact, mock_get_connection
    ):
        from robothor.crm.dal import get_person_timeline

        feed = get_person_timeline(populated_contact, limit=20)
        assert len(feed) == 6
        # Most recent first (5 minutes ago → agent_run)
        assert feed[0]["activity_type"] == "agent_run"
        assert feed[-1]["activity_type"] == "email"

    def test_channel_filter(self, db_cursor, db_conn, populated_contact, mock_get_connection):
        from robothor.crm.dal import get_person_timeline

        feed = get_person_timeline(populated_contact, channels=["email", "telegram"])
        types = [r["activity_type"] for r in feed]
        assert set(types) == {"email", "telegram_message"}

    def test_limit(self, db_cursor, db_conn, populated_contact, mock_get_connection):
        from robothor.crm.dal import get_person_timeline

        feed = get_person_timeline(populated_contact, limit=2)
        assert len(feed) == 2
        assert feed[0]["activity_type"] == "agent_run"
        assert feed[1]["activity_type"] == "task"


# ── get_person_summary ───────────────────────────────────────────────────────


class TestGetPersonSummary:
    def test_counts_per_type(self, db_cursor, db_conn, populated_contact, mock_get_connection):
        from robothor.crm.dal import get_person_summary

        summary = get_person_summary(populated_contact)
        counts = summary["counts"]
        assert counts["email"] == 1
        assert counts["telegram_message"] == 1
        assert counts["calendar_event"] == 1
        assert counts["note"] == 1
        assert counts["task"] == 1
        assert counts["agent_run"] == 1
        assert summary["last_touched_at"] is not None


# ── get_person_messages ──────────────────────────────────────────────────────


class TestGetPersonMessages:
    def test_round_trip(self, db_cursor, db_conn, mock_get_connection):
        """Participant junction → messages list for a person."""
        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name) VALUES (%s, 'MsgFetch')",
            (person_id,),
        )
        db_cursor.execute(
            "INSERT INTO message_thread (channel, external_thread_id, subject) VALUES ('email', 'fetch-thread', 'Subject') RETURNING id"
        )
        thread_id = db_cursor.fetchone()["id"]
        for ext, body in [("fetch-1", "first"), ("fetch-2", "second")]:
            db_cursor.execute(
                """
                INSERT INTO message (thread_id, channel, direction, external_message_id, body_text, occurred_at)
                VALUES (%s, 'email', 'outbound', %s, %s, NOW())
                RETURNING id
                """,
                (thread_id, ext, body),
            )
            msg_id = db_cursor.fetchone()["id"]
            db_cursor.execute(
                "INSERT INTO message_participant (message_id, role, person_id, handle) VALUES (%s, 'to', %s, 'x@example.com')",
                (msg_id, person_id),
            )

        from robothor.crm.dal import get_person_messages

        rows = get_person_messages(person_id)
        assert len(rows) == 2
        bodies = {r["body_text"] for r in rows}
        assert bodies == {"first", "second"}


# ── other per-channel fetchers ───────────────────────────────────────────────


class TestGetPersonTasksNotesCalls:
    def test_tasks_notes_calls(self, db_cursor, db_conn, mock_get_connection):
        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name) VALUES (%s, 'Mixed')",
            (person_id,),
        )
        db_cursor.execute(
            """
            INSERT INTO crm_tasks (id, title, status, person_id)
            VALUES (gen_random_uuid(), 'Call Mixed', 'TODO', %s)
            """,
            (person_id,),
        )
        db_cursor.execute(
            """
            INSERT INTO crm_notes (id, title, body, person_id)
            VALUES (gen_random_uuid(), 'Biography', 'Loves cats', %s)
            """,
            (person_id,),
        )
        db_cursor.execute(
            """
            INSERT INTO call_log (person_id, direction, status, twilio_call_sid)
            VALUES (%s, 'outbound', 'completed', 'CA-dal-1')
            """,
            (person_id,),
        )

        from robothor.crm.dal import get_person_calls, get_person_notes, get_person_tasks

        assert [t["title"] for t in get_person_tasks(person_id)] == ["Call Mixed"]
        assert [n["title"] for n in get_person_notes(person_id)] == ["Biography"]
        assert [c["twilio_call_sid"] for c in get_person_calls(person_id)] == ["CA-dal-1"]
