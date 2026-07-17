"""088 tightens the __default__ member role to read-only.

Migration 071 seeded ('__default__', 'member', '*', 'allow'); migration 087
tried to add a member deny-all guardrail row but it collided with 071's row
on the UNIQUE(tenant_id, role, tool_pattern) constraint and was silently
skipped by ON CONFLICT DO NOTHING (documented in 087 itself). 088 is the
deliberate follow-up: it UPDATEs the row 071 created (same "mutate a row an
earlier migration created" precedent as migration 083, which replaced a
policy migration 081 created) rather than leaving member's '*' at 'allow'
forever. Scope is surgical: only tenant_id='__default__', role='member',
tool_pattern='*' is touched — no other tenant, role, or tool_pattern.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "crm" / "migrations"
MIGRATION_037 = MIGRATIONS_DIR / "037_access_control.sql"
MIGRATION_071 = MIGRATIONS_DIR / "071_user_accounts.sql"
MIGRATION_087 = MIGRATIONS_DIR / "087_role_permission_guardrails.sql"
MIGRATION_088 = MIGRATIONS_DIR / "088_member_role_read_only.sql"


def test_migration_file_exists():
    assert MIGRATION_088.exists(), "088_member_role_read_only.sql must exist"


def _member_rows(db_cursor):
    db_cursor.execute(
        "SELECT tool_pattern, access FROM role_permissions "
        "WHERE tenant_id = '__default__' AND role = 'member' ORDER BY tool_pattern"
    )
    return {r["tool_pattern"]: r["access"] for r in db_cursor.fetchall()}


def test_088_after_071_and_087_tightens_member_to_read_only(db_cursor, db_conn):
    """The realistic production ordering: 037 -> 071 -> 087 -> 088.

    Before 088: member is wide open ('*' = allow, inherited from 071, since
    087's deny row silently lost the UNIQUE-constraint conflict). After 088:
    member's '*' row is 'deny' and the read-only allow rows are present —
    matching 'viewer's shape from 037.
    """
    db_cursor.execute(MIGRATION_037.read_text())
    db_cursor.execute(MIGRATION_071.read_text())
    db_cursor.execute(MIGRATION_087.read_text())

    before = _member_rows(db_cursor)
    assert before.get("*") == "allow", "precondition: 071's member wide-open row must exist"

    db_cursor.execute(MIGRATION_088.read_text())

    after = _member_rows(db_cursor)
    assert after["*"] == "deny"
    assert after["search_*"] == "allow"
    assert after["get_*"] == "allow"
    assert after["list_*"] == "allow"


def test_088_does_not_touch_user_admin_owner_rows(db_cursor, db_conn):
    db_cursor.execute(MIGRATION_037.read_text())
    db_cursor.execute(MIGRATION_071.read_text())
    db_cursor.execute(MIGRATION_087.read_text())
    db_cursor.execute(MIGRATION_088.read_text())

    for role in ("user", "admin", "owner"):
        db_cursor.execute(
            "SELECT access FROM role_permissions "
            "WHERE tenant_id = '__default__' AND role = %s AND tool_pattern = '*'",
            (role,),
        )
        row = db_cursor.fetchone()
        assert row is not None, f"{role} '*' row must still exist"
        assert row["access"] == "allow", f"{role} must remain untouched (still allow)"


def test_088_does_not_touch_other_tenant_rows(db_cursor, db_conn):
    db_cursor.execute(MIGRATION_037.read_text())
    db_cursor.execute(MIGRATION_071.read_text())
    db_cursor.execute(MIGRATION_087.read_text())
    db_cursor.execute(
        "INSERT INTO role_permissions (tenant_id, role, tool_pattern, access) "
        "VALUES ('tenant-a', 'member', '*', 'allow')"
    )

    db_cursor.execute(MIGRATION_088.read_text())

    db_cursor.execute(
        "SELECT access FROM role_permissions "
        "WHERE tenant_id = 'tenant-a' AND role = 'member' AND tool_pattern = '*'"
    )
    assert db_cursor.fetchone()["access"] == "allow"


def test_088_is_idempotent(db_cursor, db_conn):
    db_cursor.execute(MIGRATION_037.read_text())
    db_cursor.execute(MIGRATION_071.read_text())
    db_cursor.execute(MIGRATION_087.read_text())
    db_cursor.execute(MIGRATION_088.read_text())
    db_cursor.execute(MIGRATION_088.read_text())  # second apply must not raise

    after = _member_rows(db_cursor)
    assert after["*"] == "deny"


def test_088_without_071_or_087_is_a_safe_noop_on_update_but_seeds_readonly_rows(
    db_cursor, db_conn
):
    """An environment where 071/087 never ran (no member rows at all): the
    UPDATE matches zero rows (nothing to tighten), and the defensive INSERT
    still seeds the read-only allow rows so 088 is self-sufficient.

    Simulated by deleting any pre-existing __default__/member rows within
    this test's rolled-back transaction rather than assuming a from-scratch
    database — some environments' role_permissions table already carries
    071's row from real, previously-applied history, and this test must
    hold regardless of what state the underlying database happens to be in.
    """
    db_cursor.execute(MIGRATION_037.read_text())
    db_cursor.execute(
        "DELETE FROM role_permissions WHERE tenant_id = '__default__' AND role = 'member'"
    )

    db_cursor.execute(MIGRATION_088.read_text())

    after = _member_rows(db_cursor)
    assert "*" not in after  # nothing to tighten — 071 never ran
    assert after["search_*"] == "allow"
    assert after["get_*"] == "allow"
    assert after["list_*"] == "allow"
