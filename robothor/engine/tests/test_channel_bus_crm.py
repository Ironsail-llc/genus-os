"""
Phase 2a — Telegram write-through tests.

When channel_bus records an inbound or outbound Telegram message, it must:
  1. resolve the chat_id → person_id via contact_identifiers / resolve_contact
  2. stamp channel_message_map.person_id
  3. stamp chat_sessions.person_id (upsert on first sight)
  4. UPSERT message_thread (channel='telegram', external_thread_id=chat_id)
  5. INSERT message
  6. INSERT message_participant (with role from/to)
  7. INSERT timeline_activity

These are the RED tests; record_inbound/record_outbound do not yet do any
of steps 2-7. Tests should fail before the GREEN implementation.

Hits a real DB (robothor_test) so the cross-table side effects are visible.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_person(db_cursor):
    """A person + telegram contact_identifier + chat_session for chat_id 'tg-test-1'."""
    person_id = str(uuid.uuid4())
    db_cursor.execute(
        "INSERT INTO crm_people (id, first_name, last_name) VALUES (%s, 'TG', 'Tester')",
        (person_id,),
    )
    db_cursor.execute(
        "INSERT INTO contact_identifiers (channel, identifier, person_id) VALUES ('telegram', 'tg-test-1', %s)",
        (person_id,),
    )
    db_cursor.execute(
        """
        INSERT INTO chat_sessions (tenant_id, session_key, channel)
        VALUES ('default', 'telegram:tg-test-1', 'telegram')
        RETURNING id
        """
    )
    session_id = db_cursor.fetchone()["id"]
    db_cursor.execute(
        "INSERT INTO chat_messages (session_id, message) VALUES (%s, %s) RETURNING id",
        (session_id, '{"role":"user","content":"hi"}'),
    )
    chat_message_id = db_cursor.fetchone()["id"]
    return {
        "person_id": person_id,
        "session_id": session_id,
        "chat_message_id": chat_message_id,
    }


# ── Inbound ──────────────────────────────────────────────────────────────────


class TestInboundWriteThrough:
    def test_stamps_person_id_on_map(self, db_cursor, db_conn, seeded_person, mock_get_connection):
        from robothor.engine import channel_bus

        channel_bus.record_inbound(
            tenant_id="default",
            channel="telegram",
            chat_id="tg-test-1",
            platform_message_id="100",
            session_key="telegram:tg-test-1",
            chat_message_id=seeded_person["chat_message_id"],
            author_agent_id="user",
        )
        db_cursor.execute(
            "SELECT person_id FROM channel_message_map WHERE platform_message_id = '100'"
        )
        row = db_cursor.fetchone()
        assert row is not None
        assert str(row["person_id"]) == seeded_person["person_id"]

    def test_stamps_chat_session_person_id(
        self, db_cursor, db_conn, seeded_person, mock_get_connection
    ):
        from robothor.engine import channel_bus

        channel_bus.record_inbound(
            tenant_id="default",
            channel="telegram",
            chat_id="tg-test-1",
            platform_message_id="101",
            session_key="telegram:tg-test-1",
            chat_message_id=seeded_person["chat_message_id"],
            author_agent_id="user",
        )
        db_cursor.execute(
            "SELECT person_id FROM chat_sessions WHERE id = %s",
            (seeded_person["session_id"],),
        )
        assert str(db_cursor.fetchone()["person_id"]) == seeded_person["person_id"]

    def test_writes_message_thread_and_message(
        self, db_cursor, db_conn, seeded_person, mock_get_connection
    ):
        from robothor.engine import channel_bus

        channel_bus.record_inbound(
            tenant_id="default",
            channel="telegram",
            chat_id="tg-test-1",
            platform_message_id="102",
            session_key="telegram:tg-test-1",
            chat_message_id=seeded_person["chat_message_id"],
            author_agent_id="user",
        )
        db_cursor.execute(
            "SELECT id FROM message_thread WHERE channel='telegram' AND external_thread_id='tg-test-1'"
        )
        thread_row = db_cursor.fetchone()
        assert thread_row is not None, "message_thread should be upserted"
        db_cursor.execute(
            "SELECT direction FROM message WHERE thread_id = %s AND external_message_id = '102'",
            (thread_row["id"],),
        )
        msg_row = db_cursor.fetchone()
        assert msg_row is not None and msg_row["direction"] == "inbound"

    def test_writes_message_participant_with_person(
        self, db_cursor, db_conn, seeded_person, mock_get_connection
    ):
        from robothor.engine import channel_bus

        channel_bus.record_inbound(
            tenant_id="default",
            channel="telegram",
            chat_id="tg-test-1",
            platform_message_id="103",
            session_key="telegram:tg-test-1",
            chat_message_id=seeded_person["chat_message_id"],
            author_agent_id="user",
        )
        db_cursor.execute(
            """
            SELECT mp.role, mp.person_id, mp.handle
              FROM message_participant mp
              JOIN message m ON m.id = mp.message_id
             WHERE m.external_message_id = '103'
            """
        )
        row = db_cursor.fetchone()
        assert row is not None
        assert row["role"] == "from"
        assert str(row["person_id"]) == seeded_person["person_id"]
        assert row["handle"] == "tg-test-1"

    def test_writes_timeline_activity(self, db_cursor, db_conn, seeded_person, mock_get_connection):
        from robothor.engine import channel_bus

        channel_bus.record_inbound(
            tenant_id="default",
            channel="telegram",
            chat_id="tg-test-1",
            platform_message_id="104",
            session_key="telegram:tg-test-1",
            chat_message_id=seeded_person["chat_message_id"],
            author_agent_id="user",
        )
        db_cursor.execute(
            """
            SELECT activity_type, channel, direction, person_id
              FROM timeline_activity
             WHERE source_table = 'message'
               AND person_id = %s
             ORDER BY id DESC LIMIT 1
            """,
            (seeded_person["person_id"],),
        )
        row = db_cursor.fetchone()
        assert row is not None, "timeline_activity row missing"
        assert row["activity_type"] == "telegram_message"
        assert row["channel"] == "telegram"
        assert row["direction"] == "inbound"

    def test_inbound_idempotent(self, db_cursor, db_conn, seeded_person, mock_get_connection):
        """Calling record_inbound twice with the same platform_message_id must
        not produce duplicate message rows or duplicate timeline_activity rows."""
        from robothor.engine import channel_bus

        for _ in range(2):
            channel_bus.record_inbound(
                tenant_id="default",
                channel="telegram",
                chat_id="tg-test-1",
                platform_message_id="105",
                session_key="telegram:tg-test-1",
                chat_message_id=seeded_person["chat_message_id"],
                author_agent_id="user",
            )
        db_cursor.execute("SELECT COUNT(*) AS c FROM message WHERE external_message_id = '105'")
        assert db_cursor.fetchone()["c"] == 1
        db_cursor.execute(
            """
            SELECT COUNT(*) AS c FROM timeline_activity
             WHERE source_table = 'message'
               AND person_id = %s
            """,
            (seeded_person["person_id"],),
        )
        assert db_cursor.fetchone()["c"] == 1


# ── Outbound ─────────────────────────────────────────────────────────────────


class TestOutboundWriteThrough:
    def test_outbound_writes_message_with_to_role(
        self, db_cursor, db_conn, seeded_person, mock_get_connection
    ):
        from robothor.engine import channel_bus

        channel_bus.record_outbound(
            tenant_id="default",
            channel="telegram",
            chat_id="tg-test-1",
            platform_message_ids=["200"],
            session_key="telegram:tg-test-1",
            chat_message_id=seeded_person["chat_message_id"],
            author_agent_id="main",
            author_run_id=str(uuid.uuid4()),
        )
        db_cursor.execute(
            """
            SELECT mp.role, mp.person_id, m.direction
              FROM message_participant mp
              JOIN message m ON m.id = mp.message_id
             WHERE m.external_message_id = '200'
            """
        )
        row = db_cursor.fetchone()
        assert row is not None
        assert row["direction"] == "outbound"
        assert row["role"] == "to"
        assert str(row["person_id"]) == seeded_person["person_id"]

    def test_outbound_stamps_map_person_id(
        self, db_cursor, db_conn, seeded_person, mock_get_connection
    ):
        from robothor.engine import channel_bus

        channel_bus.record_outbound(
            tenant_id="default",
            channel="telegram",
            chat_id="tg-test-1",
            platform_message_ids=["201"],
            session_key="telegram:tg-test-1",
            chat_message_id=seeded_person["chat_message_id"],
            author_agent_id="main",
            author_run_id=str(uuid.uuid4()),
        )
        db_cursor.execute(
            "SELECT person_id FROM channel_message_map WHERE platform_message_id = '201'"
        )
        assert str(db_cursor.fetchone()["person_id"]) == seeded_person["person_id"]
