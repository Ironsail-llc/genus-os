"""
Test for migration 046b_orphan_cleanup.sql.

Production data drift surfaced during the 046 apply: some
``contact_identifiers.person_id`` values point to ``crm_people`` rows that no
longer exist (deleted_at IS NOT NULL or wholly removed). This migration
NULLs those orphan pointers so the table invariant becomes
"if person_id is set, the person exists." Idempotent.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def _orphan_count(cur) -> int:
    cur.execute(
        """
        SELECT COUNT(*) AS c
          FROM contact_identifiers ci
         WHERE ci.person_id IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM crm_people p WHERE p.id = ci.person_id
           )
        """
    )
    return cur.fetchone()["c"]


class TestOrphanCleanup046b:
    def test_nulls_orphan_pointers(self, db_cursor, db_conn):
        # New orphans can no longer be inserted (FK now enforces existence).
        # We need to simulate the historical drift: seed an orphan with the
        # FK temporarily disabled, then verify the cleanup SQL fixes it.
        ghost_id = str(uuid.uuid4())
        db_cursor.execute("SET session_replication_role = 'replica'")  # disable FK checks
        db_cursor.execute(
            "INSERT INTO contact_identifiers (channel, identifier, person_id) VALUES ('email', 'ghost@example.com', %s) RETURNING id",
            (ghost_id,),
        )
        ci_id = db_cursor.fetchone()["id"]
        db_cursor.execute("SET session_replication_role = 'origin'")  # re-enable

        assert _orphan_count(db_cursor) >= 1

        # The cleanup SQL the migration runs.
        db_cursor.execute(
            """
            UPDATE contact_identifiers ci
               SET person_id = NULL
             WHERE ci.person_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM crm_people p WHERE p.id = ci.person_id
               )
            """
        )
        db_cursor.execute("SELECT person_id FROM contact_identifiers WHERE id = %s", (ci_id,))
        assert db_cursor.fetchone()["person_id"] is None
        assert _orphan_count(db_cursor) == 0

    def test_leaves_live_pointers_alone(self, db_cursor, db_conn):
        # Live person + valid identifier — must survive cleanup untouched.
        person_id = str(uuid.uuid4())
        db_cursor.execute(
            "INSERT INTO crm_people (id, first_name, last_name) VALUES (%s, 'Live', 'Person')",
            (person_id,),
        )
        db_cursor.execute(
            "INSERT INTO contact_identifiers (channel, identifier, person_id) VALUES ('email', 'live@example.com', %s) RETURNING id",
            (person_id,),
        )
        ci_id = db_cursor.fetchone()["id"]

        db_cursor.execute(
            """
            UPDATE contact_identifiers ci
               SET person_id = NULL
             WHERE ci.person_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM crm_people p WHERE p.id = ci.person_id
               )
            """
        )
        db_cursor.execute("SELECT person_id FROM contact_identifiers WHERE id = %s", (ci_id,))
        assert str(db_cursor.fetchone()["person_id"]) == person_id

    def test_idempotent(self, db_cursor, db_conn):
        # Running cleanup twice on a clean state should be a no-op.
        for _ in range(2):
            db_cursor.execute(
                """
                UPDATE contact_identifiers ci
                   SET person_id = NULL
                 WHERE ci.person_id IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM crm_people p WHERE p.id = ci.person_id
                   )
                """
            )
        assert _orphan_count(db_cursor) == 0
