"""085 creates the one-shot SSO binding grant store.

A grant is the ONLY way an existing account (e.g. the bootstrapped owner) may
be bound to an IdP identity: `jit_provision` never turns email equality into a
binding on its own. The table must support an atomic single-UPDATE consume
(used_at stamp) so two concurrent sign-ins cannot both spend one grant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2] / "crm" / "migrations" / "085_sso_binding_grants.sql"
)


def test_migration_file_exists():
    assert MIGRATION.exists(), "085_sso_binding_grants.sql must exist"


def test_migration_creates_table_and_pending_index(db_cursor, db_conn):
    """No commit here: db_conn's fixture teardown rolls back the transaction
    (see tests/conftest_integration.py:db_conn), so the migration's DDL/DML
    never touches the real database.
    """
    db_cursor.execute(MIGRATION.read_text())

    db_cursor.execute("SELECT to_regclass('public.sso_binding_grants') AS reg")
    assert db_cursor.fetchone()["reg"] is not None

    db_cursor.execute(
        "SELECT indexdef FROM pg_indexes "
        "WHERE tablename = 'sso_binding_grants' "
        "  AND indexname = 'idx_sso_binding_grants_pending'"
    )
    row = db_cursor.fetchone()
    assert row is not None, "partial index idx_sso_binding_grants_pending must exist"
    indexdef = row["indexdef"].lower()
    assert "used_at is null" in indexdef
    assert "revoked_at is null" in indexdef


def test_migration_is_idempotent(db_cursor, db_conn):
    db_cursor.execute(MIGRATION.read_text())
    db_cursor.execute(MIGRATION.read_text())  # second apply must not raise


def test_consume_predicate_only_matches_pending_unexpired(db_cursor, db_conn):
    """The DAL consume predicate (used_at/revoked_at NULL, expires_at in the
    future) must select exactly the live grant among used/revoked/expired ones.
    """
    db_cursor.execute(MIGRATION.read_text())
    db_cursor.execute(
        """INSERT INTO sso_binding_grants (tenant_id, email, expires_at, used_at, revoked_at)
           VALUES
               ('default', 'alice@example.com', NOW() + INTERVAL '15 minutes', NULL, NULL),
               ('default', 'alice@example.com', NOW() + INTERVAL '15 minutes', NOW(), NULL),
               ('default', 'alice@example.com', NOW() + INTERVAL '15 minutes', NULL, NOW()),
               ('default', 'alice@example.com', NOW() - INTERVAL '1 minute',   NULL, NULL)
        """
    )
    db_cursor.execute(
        """SELECT count(*) AS n FROM sso_binding_grants
           WHERE tenant_id = 'default' AND email = 'alice@example.com'
             AND used_at IS NULL AND revoked_at IS NULL AND expires_at > NOW()"""
    )
    assert db_cursor.fetchone()["n"] == 1
