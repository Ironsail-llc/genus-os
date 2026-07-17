"""089 creates face_identities — the vision-face-label -> crm_people join
(Task 7, Unified Identity Context).

The vision service stays DB-free; this table is written by the engine's
vision tool handlers (enroll_face/enroll_face_from_image/unenroll_face) and
`robothor user link-face`, and read by
robothor/identity/resolvers.py::_resolve_vision (which probes for the table
via to_regclass and degrades to None gracefully when it's absent).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = MIGRATIONS_DIR / "089_face_identities.sql"


def test_migration_file_exists():
    assert MIGRATION.exists(), "089_face_identities.sql must exist"


def test_migration_creates_table_with_expected_columns(db_cursor, db_conn):
    """No commit here: db_conn's fixture teardown rolls back the
    transaction, so this migration's DDL never touches the real database.
    """
    db_cursor.execute(MIGRATION.read_text())

    db_cursor.execute("SELECT to_regclass('public.face_identities') AS reg")
    assert db_cursor.fetchone()["reg"] is not None

    db_cursor.execute(
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'face_identities'"
    )
    cols = {r["column_name"]: r for r in db_cursor.fetchall()}
    for expected in (
        "id",
        "tenant_id",
        "face_label",
        "person_id",
        "user_account_id",
        "display_name",
        "created_at",
    ):
        assert expected in cols, f"face_identities.{expected} must exist"

    assert cols["tenant_id"]["is_nullable"] == "NO"
    assert cols["face_label"]["is_nullable"] == "NO"
    assert cols["display_name"]["is_nullable"] == "NO"
    assert cols["display_name"]["column_default"] == "''::text"


def test_migration_is_idempotent(db_cursor, db_conn):
    db_cursor.execute(MIGRATION.read_text())
    db_cursor.execute(MIGRATION.read_text())  # second apply must not raise


def test_unique_tenant_face_label_constraint(db_cursor, db_conn):
    db_cursor.execute(MIGRATION.read_text())
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES ('t-face-089', 't-face-089') "
        "ON CONFLICT (id) DO NOTHING"
    )
    db_cursor.execute(
        "INSERT INTO face_identities (tenant_id, face_label, display_name) "
        "VALUES ('t-face-089', 'front-door', 'Guest')"
    )
    with pytest.raises(Exception):  # noqa: B017 — psycopg2 UniqueViolation, driver-specific
        db_cursor.execute(
            "INSERT INTO face_identities (tenant_id, face_label, display_name) "
            "VALUES ('t-face-089', 'front-door', 'Someone Else')"
        )


def test_person_delete_sets_face_identities_person_id_null(db_cursor, db_conn):
    """ON DELETE SET NULL: deleting the linked crm_people row must not
    cascade-delete the enrollment -- the label stays registered, unlinked."""
    db_cursor.execute(MIGRATION.read_text())
    db_cursor.execute(
        "INSERT INTO crm_tenants (id, display_name) VALUES ('t-face-del', 't-face-del') "
        "ON CONFLICT (id) DO NOTHING"
    )
    db_cursor.execute(
        "INSERT INTO crm_people (tenant_id, first_name, last_name) "
        "VALUES ('t-face-del', 'Alice', 'Rivera') RETURNING id"
    )
    person_id = db_cursor.fetchone()["id"]
    db_cursor.execute(
        "INSERT INTO face_identities (tenant_id, face_label, person_id, display_name) "
        "VALUES ('t-face-del', 'front-door', %s, 'Alice Rivera')",
        (person_id,),
    )

    db_cursor.execute("DELETE FROM crm_people WHERE id = %s", (person_id,))

    db_cursor.execute(
        "SELECT person_id FROM face_identities WHERE tenant_id = 't-face-del' "
        "AND face_label = 'front-door'"
    )
    row = db_cursor.fetchone()
    assert row is not None, "the face_identities row itself must survive the person delete"
    assert row["person_id"] is None
