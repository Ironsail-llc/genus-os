"""148: calendar_event identity is (tenant, provider, external id), not Google's id.

Rows written before 148 carry only ``google_event_id``; 148 must backfill them
as ``provider='google'`` with ``external_event_id = google_event_id`` so the
write-through's new upsert key finds them instead of inserting a duplicate.
The old column and its unique constraint stay (dual-write).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "148_calendar_event_provider_identity"
UPSERT = """
    INSERT INTO calendar_event (tenant_id, provider, external_event_id, google_event_id, title)
    VALUES ('default', %s, %s, %s, %s)
    ON CONFLICT (tenant_id, provider, external_event_id)
    DO UPDATE SET title = EXCLUDED.title
    RETURNING id
"""


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


@pytest.fixture
def legacy_row(scratch_db):
    db, _dsn = scratch_db(through="147_coding_job_limits")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO calendar_event (tenant_id, google_event_id, title) "
            "VALUES ('default', 'g-legacy', 'Old') RETURNING id"
        )
        row_id = cur.fetchone()[0]
    db, _dsn = scratch_db(through=MIGRATION)
    return db, row_id


def test_legacy_rows_are_backfilled_as_google(legacy_row):
    db, row_id = legacy_row
    with db.cursor() as cur:
        cur.execute(
            "SELECT provider, external_event_id, google_event_id FROM calendar_event WHERE id = %s",
            (row_id,),
        )
        assert cur.fetchone() == ("google", "g-legacy", "g-legacy")


def test_upsert_on_new_key_updates_the_backfilled_row(legacy_row):
    db, row_id = legacy_row
    with db.cursor() as cur:
        cur.execute(UPSERT, ("google", "g-legacy", "g-legacy", "New"))
        assert cur.fetchone()[0] == row_id
        cur.execute("SELECT COUNT(*), MAX(title) FROM calendar_event")
        assert cur.fetchone() == (1, "New")


def test_same_external_id_under_another_provider_is_a_separate_event(legacy_row):
    db, row_id = legacy_row
    with db.cursor() as cur:
        cur.execute(UPSERT, ("microsoft", "g-legacy", None, "MS"))
        assert cur.fetchone()[0] != row_id
        cur.execute("SELECT provider FROM calendar_event ORDER BY provider")
        assert [r[0] for r in cur.fetchall()] == ["google", "microsoft"]


def test_provider_defaults_to_google_and_old_constraint_survives(legacy_row):
    db, _row_id = legacy_row
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO calendar_event (tenant_id, google_event_id, external_event_id) "
            "VALUES ('default', 'g-2', 'g-2') RETURNING provider"
        )
        assert cur.fetchone()[0] == "google"
        cur.execute(
            "SELECT 1 FROM pg_constraint WHERE conrelid = 'calendar_event'::regclass "
            "AND contype = 'u' AND conname = 'calendar_event_tenant_id_google_event_id_key'"
        )
        assert cur.fetchone() is not None


def test_a_pre_148_writer_still_lands_on_the_new_key(legacy_row):
    """An old engine (rolling deploy) writes only google_event_id."""
    db, _row_id = legacy_row
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO calendar_event (tenant_id, google_event_id, title) "
            "VALUES ('default', 'g-old-writer', 'X') RETURNING id"
        )
        old_id = cur.fetchone()[0]
        cur.execute(UPSERT, ("google", "g-old-writer", "g-old-writer", "Y"))
        assert cur.fetchone()[0] == old_id


def test_migration_is_idempotent(legacy_row):
    db, _row_id = legacy_row
    sql = (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()
    from robothor.db.migrate import _strip_outer_transaction

    with db.cursor() as cur:
        cur.execute(_strip_outer_transaction(sql))
        cur.execute("SELECT COUNT(*) FROM calendar_event")
        assert cur.fetchone()[0] == 1
