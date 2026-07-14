"""084 creates the flag store and seeds it without changing any value.

The whole point of DB-backed flags is that flipping the source of truth from env
to DB must be a NO-OP on day one: the seeded value must equal what the flag
resolved to from env, or the cutover silently changes a guardrail.
"""

from __future__ import annotations

from pathlib import Path

MIGRATION = Path(__file__).resolve().parents[2] / "crm" / "migrations" / "084_feature_flags.sql"

GOVERNED = {
    "ROBOTHOR_RBAC_MODE",
    "ROBOTHOR_INJECTION_SCAN_MODE",
    "ROBOTHOR_EXEC_ALLOWLIST_STRICT_MODE",
    "ROBOTHOR_APPROVAL_MODE",
    "ROBOTHOR_SANDBOX_DEFAULT_MODE",
    "ROBOTHOR_COMPLETION_CONTRACTS_MODE",
    "ROBOTHOR_RIP_7_MODE",
    "ROBOTHOR_RIP_13_MODE",
    "ROBOTHOR_RIP_1_ENABLED",
    "ROBOTHOR_RIP_4_ENABLED",
    "ROBOTHOR_RIP_5_ENABLED",
    "ROBOTHOR_JUDGE_ENABLED",
}


def test_migration_file_exists():
    assert MIGRATION.exists(), "084_feature_flags.sql must exist"


def test_migration_creates_both_tables_and_seeds_twelve(pg_scratch):
    """pg_scratch: a fixture yielding a psycopg2 conn to an empty scratch DB.

    No commit here: the fixture's isolation is the uncommitted transaction, and
    statements within it see each other's writes without needing one. Committing
    would leak these tables into the real database (see fixture docstring).
    """
    with pg_scratch.cursor() as cur:
        cur.execute(MIGRATION.read_text())

        cur.execute("SELECT to_regclass('public.feature_flags')")
        assert cur.fetchone()[0] is not None
        cur.execute("SELECT to_regclass('public.feature_flag_audit')")
        assert cur.fetchone()[0] is not None

        cur.execute("SELECT name FROM feature_flags")
        seeded = {r[0] for r in cur.fetchall()}
        assert seeded == GOVERNED, f"seed drift: {seeded ^ GOVERNED}"


def test_migration_is_idempotent(pg_scratch):
    with pg_scratch.cursor() as cur:
        cur.execute(MIGRATION.read_text())
        cur.execute(MIGRATION.read_text())  # second apply must not raise
        cur.execute("SELECT count(*) FROM feature_flags")
        assert cur.fetchone()[0] == 12
