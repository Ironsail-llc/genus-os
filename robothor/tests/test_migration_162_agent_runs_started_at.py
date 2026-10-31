"""162: an index the Runs page can read in order.

The Runs view filters ``agent_runs`` on ``started_at`` and sorts it
``started_at DESC NULLS LAST, id DESC``. Without an index on ``started_at``
every page of the list, and the summary strip over the same window, scans the
whole table. The index matches the sort exactly, so the list reads its first
page straight off the index and the window filter is an index range.
"""

from __future__ import annotations

from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION = "162_agent_runs_started_at"


def _sql() -> str:
    return (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()


def _statements() -> str:
    """The SQL without its comments, so a note in the header cannot pass a check."""
    return "\n".join(
        line for line in _sql().splitlines() if not line.lstrip().startswith("--")
    ).lower()


def test_migration_file_exists_and_is_in_the_manifest():
    assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists()
    manifest = (Path(__file__).resolve().parents[1] / "migrations" / "manifest.txt").read_text()
    assert f"crm/{MIGRATION}.sql" in manifest.splitlines()


def test_the_index_matches_the_runs_page_sort():
    sql = " ".join(_statements().split())
    assert "create index if not exists idx_agent_runs_started_at" in sql
    assert "on agent_runs (started_at desc nulls last, id desc)" in sql


def test_the_index_is_built_inside_the_runners_transaction():
    # The runner executes each file in one transaction with its history row,
    # where CREATE INDEX CONCURRENTLY is refused. The live build goes out of band.
    assert "concurrently" not in _statements()
    assert "CONCURRENTLY" in _sql()


@pytest.mark.integration
def test_the_index_exists_after_the_migration_applies(scratch_db):
    import psycopg2

    _, dsn = scratch_db(through=MIGRATION)
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'agent_runs' AND indexname = 'idx_agent_runs_started_at'"
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None
    assert "(started_at DESC NULLS LAST, id DESC)" in row[0]
