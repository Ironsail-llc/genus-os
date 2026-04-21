"""
Schema + behavior tests for migration 048_voice_calendar.sql.

Tables: call_log, calendar_event, calendar_event_participant.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=%s", (table,))
    return cur.fetchone() is not None


def _col_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, column),
    )
    return cur.fetchone() is not None


class TestSchema048:
    @pytest.mark.parametrize("table", ["call_log", "calendar_event", "calendar_event_participant"])
    def test_table_exists(self, db_cursor, table):
        assert _table_exists(db_cursor, table)

    @pytest.mark.parametrize(
        "table,column",
        [
            ("call_log", "person_id"),
            ("call_log", "twilio_call_sid"),
            ("call_log", "duration_ms"),
            ("call_log", "recording_url"),
            ("calendar_event", "google_event_id"),
            ("calendar_event", "start_at"),
            ("calendar_event_participant", "event_id"),
            ("calendar_event_participant", "person_id"),
            ("calendar_event_participant", "email"),
            ("calendar_event_participant", "response_status"),
        ],
    )
    def test_columns_exist(self, db_cursor, table, column):
        assert _col_exists(db_cursor, table, column), f"{table}.{column} missing"

    def test_call_status_check(self, db_cursor, db_conn):
        """Status enum is enforced — invalid value rejected."""
        with pytest.raises(Exception):
            db_cursor.execute(
                """
                INSERT INTO call_log (direction, status, started_at)
                VALUES ('outbound', 'banana', NOW())
                """
            )


class TestVoiceCalendarRoundtrip:
    def test_call_log_round_trip(self, db_cursor, db_conn):
        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name, last_name) VALUES (%s, 'Cal', 'ee')",
            (person_id,),
        )
        db_cursor.execute(
            """
            INSERT INTO call_log (person_id, direction, from_number, to_number, status, twilio_call_sid)
            VALUES (%s, 'outbound', '+15551111111', '+15552222222', 'completed', 'CA-test-1')
            RETURNING id
            """,
            (person_id,),
        )
        call_id = db_cursor.fetchone()["id"]
        db_cursor.execute("SELECT person_id FROM call_log WHERE id = %s", (call_id,))
        assert str(db_cursor.fetchone()["person_id"]) == person_id

    def test_call_sid_unique(self, db_cursor, db_conn):
        db_cursor.execute(
            "INSERT INTO call_log (direction, status, twilio_call_sid) VALUES ('inbound', 'completed', 'CA-dup')"
        )
        with pytest.raises(Exception):
            db_cursor.execute(
                "INSERT INTO call_log (direction, status, twilio_call_sid) VALUES ('inbound', 'completed', 'CA-dup')"
            )

    def test_calendar_event_with_attendees(self, db_cursor, db_conn):
        p1 = str(uuid.uuid4())
        p2 = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name) VALUES (%s, 'A'), (%s, 'B')",
            (p1, p2),
        )
        db_cursor.execute(
            """
            INSERT INTO calendar_event (google_event_id, calendar_id, title, start_at, end_at)
            VALUES ('gcal-evt-1', 'philip@example.com', 'Sync', NOW(), NOW() + INTERVAL '30 minutes')
            RETURNING id
            """
        )
        event_id = db_cursor.fetchone()["id"]
        db_cursor.execute(
            """
            INSERT INTO calendar_event_participant (event_id, person_id, role, email, response_status)
            VALUES (%s, %s, 'organizer', 'a@example.com', 'accepted'),
                   (%s, %s, 'attendee',  'b@example.com', 'tentative')
            """,
            (event_id, p1, event_id, p2),
        )
        # Query "events for person X"
        db_cursor.execute(
            """
            SELECT ce.title, cep.role
              FROM calendar_event ce
              JOIN calendar_event_participant cep ON cep.event_id = ce.id
             WHERE cep.person_id = %s
            """,
            (p1,),
        )
        row = db_cursor.fetchone()
        assert row["title"] == "Sync"
        assert row["role"] == "organizer"

    def test_event_cascade_drops_participants(self, db_cursor, db_conn):
        db_cursor.execute(
            "INSERT INTO calendar_event (google_event_id, title, start_at) VALUES ('cascade-test', 'X', NOW()) RETURNING id"
        )
        event_id = db_cursor.fetchone()["id"]
        db_cursor.execute(
            "INSERT INTO calendar_event_participant (event_id, role, email) VALUES (%s, 'attendee', 'x@test')",
            (event_id,),
        )
        db_cursor.execute("DELETE FROM calendar_event WHERE id = %s", (event_id,))
        db_cursor.execute(
            "SELECT COUNT(*) AS c FROM calendar_event_participant WHERE event_id = %s",
            (event_id,),
        )
        assert db_cursor.fetchone()["c"] == 0
