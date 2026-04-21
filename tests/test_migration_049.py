"""
Schema + behavior tests for migration 049_timeline_activity.sql.

The denormalized append-only feed that powers Contact 360.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def _table_exists(cur, table: str) -> bool:
    cur.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=%s", (table,))
    return cur.fetchone() is not None


def _index_exists(cur, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_indexes WHERE indexname=%s", (name,))
    return cur.fetchone() is not None


class TestSchema049:
    def test_timeline_activity_exists(self, db_cursor):
        assert _table_exists(db_cursor, "timeline_activity")

    @pytest.mark.parametrize(
        "name", ["idx_timeline_person", "idx_timeline_recent", "idx_timeline_type"]
    )
    def test_indexes_exist(self, db_cursor, name):
        assert _index_exists(db_cursor, name), f"missing index {name}"

    def test_unique_constraint_blocks_dupes(self, db_cursor, db_conn):
        person_id = str(uuid.uuid4())
        db_cursor.execute("INSERT INTO crm_people (id, first_name) VALUES (%s, 'U')", (person_id,))
        db_cursor.execute(
            """
            INSERT INTO timeline_activity (person_id, occurred_at, activity_type, source_table, source_id, title)
            VALUES (%s, NOW(), 'note', 'crm_notes', 'note-uniq-1', 'first')
            """,
            (person_id,),
        )
        with pytest.raises(Exception):
            db_cursor.execute(
                """
                INSERT INTO timeline_activity (person_id, occurred_at, activity_type, source_table, source_id, title)
                VALUES (%s, NOW(), 'note', 'crm_notes', 'note-uniq-1', 'second')
                """,
                (person_id,),
            )

    def test_activity_type_check(self, db_cursor, db_conn):
        with pytest.raises(Exception):
            db_cursor.execute(
                """
                INSERT INTO timeline_activity (occurred_at, activity_type, source_table, source_id)
                VALUES (NOW(), 'banana_event', 'x', 'y')
                """
            )


class TestTimelineRead:
    def test_chronological_order(self, db_cursor, db_conn):
        person_id = str(uuid.uuid4())
        db_cursor.execute("INSERT INTO crm_people (id, first_name) VALUES (%s, 'C')", (person_id,))
        for i, ago in enumerate(["3 hours", "1 hour", "10 minutes"]):
            db_cursor.execute(
                f"""
                INSERT INTO timeline_activity (person_id, occurred_at, activity_type, source_table, source_id, title)
                VALUES (%s, NOW() - INTERVAL '{ago}', 'note', 'crm_notes', %s, %s)
                """,
                (person_id, f"order-{i}", f"item {i}"),
            )
        db_cursor.execute(
            """
            SELECT title FROM timeline_activity
             WHERE person_id = %s
             ORDER BY occurred_at DESC
            """,
            (person_id,),
        )
        titles = [r["title"] for r in db_cursor.fetchall()]
        # Most recent first.
        assert titles == ["item 2", "item 1", "item 0"]

    def test_filter_by_activity_type(self, db_cursor, db_conn):
        person_id = str(uuid.uuid4())
        db_cursor.execute("INSERT INTO crm_people (id, first_name) VALUES (%s, 'F')", (person_id,))
        for atype, src in [("email", "msg-1"), ("note", "note-1"), ("email", "msg-2")]:
            db_cursor.execute(
                """
                INSERT INTO timeline_activity (person_id, occurred_at, activity_type, source_table, source_id)
                VALUES (%s, NOW(), %s, 'message', %s)
                """,
                (person_id, atype, src),
            )
        db_cursor.execute(
            """
            SELECT COUNT(*) AS c FROM timeline_activity
             WHERE person_id = %s AND activity_type = 'email'
            """,
            (person_id,),
        )
        assert db_cursor.fetchone()["c"] == 2
