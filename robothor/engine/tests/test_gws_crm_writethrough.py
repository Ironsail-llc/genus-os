"""
Phase 2c + 2d — Email and calendar write-through tests for gws handlers.

After a successful gws_gmail_send / gws_gmail_reply, the handler must write
  * message_thread (upsert by Gmail thread_id)
  * message (insert, idempotent on external_message_id)
  * message_participant (one per recipient, resolved via resolve_contact)
  * timeline_activity (one per resolved person, activity_type='email')

After a successful gws_calendar_create, the handler must write
  * calendar_event (insert, idempotent on google_event_id)
  * calendar_event_participant (one per attendee)
  * timeline_activity per resolved attendee (activity_type='calendar_event')

_run_gws is mocked — tests don't invoke the real CLI.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_recipient(db_cursor):
    person_id = str(uuid.uuid4())
    db_cursor.execute(
        "INSERT INTO crm_people (id, first_name, last_name, email) VALUES (%s, 'Eve', 'Email', 'eve@example.com')",
        (person_id,),
    )
    db_cursor.execute(
        "INSERT INTO contact_identifiers (channel, identifier, person_id) VALUES ('email', 'eve@example.com', %s)",
        (person_id,),
    )
    return person_id


# ── Email ────────────────────────────────────────────────────────────────────


class TestGmailSendWriteThrough:
    def test_send_writes_message_and_participant(
        self, db_cursor, db_conn, seeded_recipient, mock_get_connection
    ):
        from robothor.engine.tools.handlers import gws

        fake_gmail_response = {
            "id": "gmail-msg-abc123",
            "threadId": "gmail-thread-xyz",
            "labelIds": ["SENT"],
        }

        with patch.object(gws, "_run_gws", return_value=fake_gmail_response):
            result = gws._handle_gws_tool(
                "gws_gmail_send",
                {
                    "to": "eve@example.com",
                    "subject": "Hello",
                    "body": "Hi Eve, welcome aboard.",
                },
            )

        assert "error" not in result

        # message row written
        db_cursor.execute(
            "SELECT id, direction, subject, body_text, channel FROM message WHERE external_message_id = 'gmail-msg-abc123'"
        )
        row = db_cursor.fetchone()
        assert row is not None
        assert row["direction"] == "outbound"
        assert row["channel"] == "email"
        assert row["subject"] == "Hello"

        # participant resolved to person
        db_cursor.execute(
            """
            SELECT mp.role, mp.person_id, mp.handle
              FROM message_participant mp
              JOIN message m ON m.id = mp.message_id
             WHERE m.external_message_id = 'gmail-msg-abc123'
               AND mp.role = 'to'
            """
        )
        p = db_cursor.fetchone()
        assert p is not None
        assert str(p["person_id"]) == seeded_recipient
        assert p["handle"] == "eve@example.com"

        # timeline_activity emitted
        db_cursor.execute(
            """
            SELECT activity_type, channel, direction
              FROM timeline_activity
             WHERE source_table = 'message' AND person_id = %s
             ORDER BY id DESC LIMIT 1
            """,
            (seeded_recipient,),
        )
        t = db_cursor.fetchone()
        assert t is not None
        assert t["activity_type"] == "email"
        assert t["channel"] == "email"
        assert t["direction"] == "outbound"

    def test_send_idempotent(self, db_cursor, db_conn, seeded_recipient, mock_get_connection):
        from robothor.engine.tools.handlers import gws

        fake_response = {"id": "gmail-dup-1", "threadId": "thr-dup"}
        with patch.object(gws, "_run_gws", return_value=fake_response):
            gws._handle_gws_tool(
                "gws_gmail_send",
                {"to": "eve@example.com", "subject": "Dup", "body": "x"},
            )
            gws._handle_gws_tool(
                "gws_gmail_send",
                {"to": "eve@example.com", "subject": "Dup", "body": "x"},
            )
        db_cursor.execute(
            "SELECT COUNT(*) AS c FROM message WHERE external_message_id = 'gmail-dup-1'"
        )
        assert db_cursor.fetchone()["c"] == 1


# ── Calendar ─────────────────────────────────────────────────────────────────


class TestCalendarCreateWriteThrough:
    def test_create_writes_event_and_participants(
        self, db_cursor, db_conn, seeded_recipient, mock_get_connection
    ):
        from robothor.engine.tools.handlers import gws

        fake_event = {
            "id": "gcal-event-1",
            "summary": "Welcome sync",
            "status": "confirmed",
            "start": {"dateTime": "2026-05-01T15:00:00Z"},
            "end": {"dateTime": "2026-05-01T15:30:00Z"},
            "attendees": [
                {"email": "operator@example.com", "responseStatus": "accepted"},
                {"email": "eve@example.com", "responseStatus": "needsAction"},
            ],
            "htmlLink": "https://calendar.example/eid",
        }

        # The calendar-create handler calls _run_gws twice on some paths
        # (duplicate-event check + insert). Patch both to return the fake event.
        with (
            patch.object(gws, "_run_gws", return_value=fake_event),
            patch.object(gws, "_find_duplicate_event", return_value=None),
        ):
            result = gws._handle_gws_tool(
                "gws_calendar_create",
                {
                    "summary": "Welcome sync",
                    "start": "2026-05-01T15:00:00Z",
                    "end": "2026-05-01T15:30:00Z",
                    "attendees": ["operator@example.com", "eve@example.com"],
                },
            )
        assert "error" not in result

        db_cursor.execute(
            "SELECT id, title FROM calendar_event WHERE google_event_id = 'gcal-event-1'"
        )
        ev = db_cursor.fetchone()
        assert ev is not None
        assert ev["title"] == "Welcome sync"

        db_cursor.execute(
            """
            SELECT email, person_id
              FROM calendar_event_participant
             WHERE event_id = %s
             ORDER BY email
            """,
            (ev["id"],),
        )
        parts = db_cursor.fetchall()
        emails = [p["email"] for p in parts]
        assert "eve@example.com" in emails

        # Eve is resolved (seeded); the operator is not in our test DB so stays unresolved.
        eve_part = next(p for p in parts if p["email"] == "eve@example.com")
        assert str(eve_part["person_id"]) == seeded_recipient

        # timeline_activity per resolved attendee.
        db_cursor.execute(
            """
            SELECT activity_type
              FROM timeline_activity
             WHERE source_table = 'calendar_event'
               AND person_id = %s
            """,
            (seeded_recipient,),
        )
        t = db_cursor.fetchone()
        assert t is not None
        assert t["activity_type"] == "calendar_event"
