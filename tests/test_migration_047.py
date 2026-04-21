"""
Schema tests for migration 047_messaging_kernel.sql.

The Twenty-style channel-agnostic messaging fabric:
  - connected_account, message_thread, message,
    message_participant, message_attachment.

Behavior tests round-trip a tiny conversation: insert a thread, two messages
(inbound + outbound), participants, and assert the join "all messages for
person X" returns both rows.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def _table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=%s",
        (table,),
    )
    return cur.fetchone() is not None


def _column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        (table, column),
    )
    return cur.fetchone() is not None


def _has_unique(cur, table: str, *cols: str) -> bool:
    """True if a UNIQUE constraint covers exactly the given columns (any order)."""
    cur.execute(
        """
        SELECT array_agg(a.attname ORDER BY a.attname) AS cols
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
         WHERE t.relname = %s AND c.contype = 'u'
         GROUP BY c.conname
        """,
        (table,),
    )
    target = sorted(cols)
    for row in cur.fetchall():
        if sorted(row["cols"]) == target:
            return True
    return False


# ── Schema-shape ─────────────────────────────────────────────────────────────


class TestSchema047:
    @pytest.mark.parametrize(
        "table",
        [
            "connected_account",
            "message_thread",
            "message",
            "message_participant",
            "message_attachment",
        ],
    )
    def test_table_exists(self, db_cursor, table):
        assert _table_exists(db_cursor, table), f"missing table: {table}"

    def test_message_thread_unique(self, db_cursor):
        assert _has_unique(
            db_cursor, "message_thread", "tenant_id", "channel", "external_thread_id"
        )

    def test_message_unique(self, db_cursor):
        assert _has_unique(db_cursor, "message", "tenant_id", "channel", "external_message_id")

    def test_connected_account_unique(self, db_cursor):
        assert _has_unique(db_cursor, "connected_account", "tenant_id", "provider", "identifier")

    @pytest.mark.parametrize(
        "table,column",
        [
            ("message", "thread_id"),
            ("message", "connected_account_id"),
            ("message_participant", "message_id"),
            ("message_participant", "person_id"),
            ("message_attachment", "message_id"),
        ],
    )
    def test_fk_columns_exist(self, db_cursor, table, column):
        assert _column_exists(db_cursor, table, column)


# ── Round-trip: write a thread + 2 messages + participants and query back ────


class TestMessagingRoundtrip:
    def test_all_messages_for_person(self, db_cursor, db_conn):
        # Seed a person and a connected_account (the bot/inbox).
        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name, last_name) VALUES (%s, 'Round', 'Tripper')",
            (person_id,),
        )
        db_cursor.execute(
            """
            INSERT INTO connected_account (provider, identifier, display_name)
            VALUES ('telegram_bot', '@test_bot', 'Test Bot')
            RETURNING id
            """
        )
        account_id = db_cursor.fetchone()["id"]

        # Thread.
        db_cursor.execute(
            """
            INSERT INTO message_thread (channel, external_thread_id, subject, last_message_at, message_count)
            VALUES ('telegram', 'chat-555', NULL, NOW(), 0)
            RETURNING id
            """
        )
        thread_id = db_cursor.fetchone()["id"]

        # Inbound + outbound.
        for direction, ext_id, body in [
            ("inbound", "ext-1", "hello"),
            ("outbound", "ext-2", "world"),
        ]:
            db_cursor.execute(
                """
                INSERT INTO message (thread_id, connected_account_id, channel, direction,
                                     external_message_id, body_text, snippet, occurred_at)
                VALUES (%s, %s, 'telegram', %s, %s, %s, %s, NOW())
                RETURNING id
                """,
                (thread_id, account_id, direction, ext_id, body, body[:200]),
            )
            msg_id = db_cursor.fetchone()["id"]
            role = "from" if direction == "inbound" else "to"
            db_cursor.execute(
                "INSERT INTO message_participant (message_id, role, person_id, handle) VALUES (%s, %s, %s, '555')",
                (msg_id, role, person_id),
            )

        # The whole point: "all messages for X" via single FK join.
        db_cursor.execute(
            """
            SELECT m.direction, m.body_text
              FROM message m
              JOIN message_participant p ON p.message_id = m.id
             WHERE p.person_id = %s
             ORDER BY m.occurred_at ASC, m.created_at ASC
            """,
            (person_id,),
        )
        rows = db_cursor.fetchall()
        assert [(r["direction"], r["body_text"]) for r in rows] == [
            ("inbound", "hello"),
            ("outbound", "world"),
        ]

    def test_participant_can_be_unresolved(self, db_cursor, db_conn):
        """Inbound from unknown sender → participant with handle but person_id NULL."""
        db_cursor.execute(
            """
            INSERT INTO connected_account (provider, identifier, display_name)
            VALUES ('gmail', 'unresolved@example.com', 'Test Mailbox')
            RETURNING id
            """
        )
        account_id = db_cursor.fetchone()["id"]
        db_cursor.execute(
            "INSERT INTO message_thread (channel, external_thread_id) VALUES ('email', 'thread-unknown') RETURNING id"
        )
        thread_id = db_cursor.fetchone()["id"]
        db_cursor.execute(
            """
            INSERT INTO message (thread_id, connected_account_id, channel, direction,
                                 external_message_id, occurred_at)
            VALUES (%s, %s, 'email', 'inbound', 'ext-x', NOW())
            RETURNING id
            """,
            (thread_id, account_id),
        )
        msg_id = db_cursor.fetchone()["id"]
        db_cursor.execute(
            "INSERT INTO message_participant (message_id, role, handle) VALUES (%s, 'from', 'mystery@example.com') RETURNING person_id",
            (msg_id,),
        )
        assert db_cursor.fetchone()["person_id"] is None

    def test_message_uniqueness_blocks_dupes(self, db_cursor, db_conn):
        """A second insert with the same (tenant, channel, external_message_id) must fail."""
        db_cursor.execute(
            "INSERT INTO message_thread (channel, external_thread_id) VALUES ('email', 'dup-thread') RETURNING id"
        )
        thread_id = db_cursor.fetchone()["id"]
        db_cursor.execute(
            """
            INSERT INTO message (thread_id, channel, direction, external_message_id, occurred_at)
            VALUES (%s, 'email', 'inbound', 'gmail-msg-id-42', NOW())
            """,
            (thread_id,),
        )
        with pytest.raises(Exception):
            db_cursor.execute(
                """
                INSERT INTO message (thread_id, channel, direction, external_message_id, occurred_at)
                VALUES (%s, 'email', 'inbound', 'gmail-msg-id-42', NOW())
                """,
                (thread_id,),
            )
