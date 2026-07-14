"""084 creates the flag store and seeds it without changing any value.

The whole point of DB-backed flags is that flipping the source of truth from env
to DB must be a NO-OP on day one: the seeded value must equal what the flag
resolved to from env, or the cutover silently changes a guardrail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

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


def test_migration_creates_both_tables_and_seeds_twelve(db_cursor, db_conn):
    """No commit here: db_conn's fixture teardown rolls back the transaction
    (see tests/conftest_integration.py:db_conn), so the migration's DDL/DML
    never touches the real database.
    """
    db_cursor.execute(MIGRATION.read_text())

    db_cursor.execute("SELECT to_regclass('public.feature_flags') AS reg")
    assert db_cursor.fetchone()["reg"] is not None
    db_cursor.execute("SELECT to_regclass('public.feature_flag_audit') AS reg")
    assert db_cursor.fetchone()["reg"] is not None

    db_cursor.execute("SELECT name FROM feature_flags")
    seeded = {r["name"] for r in db_cursor.fetchall()}
    assert seeded == GOVERNED, f"seed drift: {seeded ^ GOVERNED}"


def test_migration_is_idempotent(db_cursor, db_conn):
    db_cursor.execute(MIGRATION.read_text())
    db_cursor.execute(MIGRATION.read_text())  # second apply must not raise
    db_cursor.execute("SELECT count(*) AS n FROM feature_flags")
    assert db_cursor.fetchone()["n"] == 12
