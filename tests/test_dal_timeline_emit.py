"""
Phase 2g — create_task and create_note emit a timeline_activity row
(when person_id is set).
"""

from __future__ import annotations

import uuid

import pytest

from robothor.constants import DEFAULT_TENANT

pytestmark = pytest.mark.integration


class TestNoteTaskTimelineEmission:
    def test_create_note_emits_timeline(self, db_cursor, db_conn, mock_get_connection):
        from robothor.crm.dal import create_note

        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name) VALUES (%s, 'NoteEmit')",
            (person_id,),
        )
        note_id = create_note(
            title="Met at sales kickoff",
            body="She mentioned a pilot in Q3.",
            person_id=person_id,
            tenant_id=DEFAULT_TENANT,
        )
        assert note_id
        db_cursor.execute(
            """
            SELECT activity_type, source_id, title
              FROM timeline_activity
             WHERE source_table = 'crm_notes' AND person_id = %s
            """,
            (person_id,),
        )
        row = db_cursor.fetchone()
        assert row is not None
        assert row["activity_type"] == "note"
        assert row["source_id"] == str(note_id)
        assert row["title"] == "Met at sales kickoff"

    def test_create_task_emits_timeline(self, db_cursor, db_conn, mock_get_connection):
        from robothor.crm.dal import create_task

        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name) VALUES (%s, 'TaskEmit')",
            (person_id,),
        )
        task_id = create_task(
            title="Send pilot proposal",
            body="Draft + budget",
            person_id=person_id,
            tenant_id=DEFAULT_TENANT,
        )
        assert task_id
        db_cursor.execute(
            """
            SELECT activity_type, source_id, title
              FROM timeline_activity
             WHERE source_table = 'crm_tasks' AND person_id = %s
            """,
            (person_id,),
        )
        row = db_cursor.fetchone()
        assert row is not None
        assert row["activity_type"] == "task"
        assert row["source_id"] == str(task_id)
        assert row["title"] == "Send pilot proposal"

    def test_create_note_without_person_no_emission(self, db_cursor, db_conn, mock_get_connection):
        """System-level note (no person_id) must not emit a timeline row."""
        from robothor.crm.dal import create_note

        note_id = create_note(
            title="System note",
            body="Nightly summary",
            person_id=None,
            tenant_id=DEFAULT_TENANT,
        )
        assert note_id
        db_cursor.execute(
            "SELECT 1 FROM timeline_activity WHERE source_table = 'crm_notes' AND source_id = %s",
            (str(note_id),),
        )
        assert db_cursor.fetchone() is None
