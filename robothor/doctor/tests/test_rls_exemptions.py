"""A tenant_id table may be deliberately exempt from row-level security.

``audit_log`` records the tenant each event happened in (migration 159) but
carries no ``tenant_isolation`` policy, on purpose: it is the appliance-wide
trail the platform's root operator reads unrestricted, and per-tenant reads are
scoped in the bridge (``routers._operator.audit_scope``). With RLS on it, the
bridge's per-request tenant binding would hide every other tenant's rows and
every untenanted row from exactly that reader.

Every place that enforces "a tenant_id table carries tenant_isolation" must
therefore know the exemption: the static migration guard, the doctor's
``db.rls_coverage`` check, and the doctor's ``--fix`` backstop. One list,
``robothor.db.rls_exemptions``, read by all three.
"""

from __future__ import annotations

import pytest

from robothor.doctor.checks import database as db_checks
from robothor.doctor.tests.conftest import fake_db, make_ctx


def test_the_exemption_list_names_audit_log_with_a_reason() -> None:
    from robothor.db.rls_exemptions import RLS_EXEMPT_TENANT_TABLES

    assert "audit_log" in RLS_EXEMPT_TENANT_TABLES
    for table, reason in RLS_EXEMPT_TENANT_TABLES.items():
        assert table.isidentifier(), table
        assert len(reason) > 40, f"{table} needs a real reason"


def test_the_coverage_query_excludes_the_exempt_tables() -> None:
    from robothor.db.rls_exemptions import RLS_EXEMPT_TENANT_TABLES

    for table in RLS_EXEMPT_TENANT_TABLES:
        assert f"'{table}'" in db_checks._BARE_TENANT_TABLES_SQL


def test_the_backstop_fix_skips_the_exempt_tables(monkeypatch) -> None:
    from robothor.doctor.runner import run_sync

    factory = fake_db([("user_accounts",)])
    report = run_sync(make_ctx(fix=True, db_factory=factory), checks=[_check("db.rls_coverage")])
    executed = [sql for cursor in factory.connection.cursors for sql, _params in cursor.executed]
    loops = [sql for sql in executed if "CREATE POLICY tenant_isolation" in sql]
    assert len(loops) == 1
    assert "'audit_log'" in loops[0]
    assert report.results[0].status == "pass"


def test_the_backstop_sql_is_migration_120_with_only_the_exemption_added() -> None:
    original = db_checks._migration_sql(db_checks.RLS_BACKSTOP_MIGRATION)
    backstop = db_checks._backstop_sql()
    assert backstop != original
    assert "'audit_log'" in backstop
    assert backstop.replace(db_checks._exemption_clause(), "") == original


def _check(check_id: str):
    return next(c for c in db_checks.CHECKS if c.id == check_id)


# ─── Real Postgres ────────────────────────────────────────────────────


@pytest.mark.integration
def test_the_coverage_check_passes_on_a_schema_with_migration_159(db_conn) -> None:
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[3] / "crm" / "migrations" / "159_audit_log_tenant.sql"
    )
    with db_conn.cursor() as cur:
        cur.execute(migration.read_text(encoding="utf-8"))
        cur.execute(db_checks._BARE_TENANT_TABLES_SQL)
        bare = [row[0] for row in cur.fetchall()]
    assert "audit_log" not in bare
    assert bare == [], f"a migrated schema has bare tenant tables: {bare}"


@pytest.mark.integration
def test_the_backstop_leaves_audit_log_without_a_policy(db_conn) -> None:
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[3] / "crm" / "migrations" / "159_audit_log_tenant.sql"
    )
    with db_conn.cursor() as cur:
        cur.execute(migration.read_text(encoding="utf-8"))
        cur.execute(db_checks._backstop_sql())
        cur.execute(
            "SELECT count(*) FROM pg_policies WHERE tablename = 'audit_log' "
            "AND policyname = 'tenant_isolation'"
        )
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'audit_log'")
        assert cur.fetchone()[0] is False
