"""The audit log carries a tenant, and reads are filtered by it.

``audit_log`` had no ``tenant_id``: the export was appliance-wide, so a child
tenant's admin could not be given one without handing them every tenant's
trail. Now every write records the tenant it happened in — the enclosing
``tenant_scope`` (a bridge request's acting tenant), else the process default —
and a read names the tenants it may see. Rows written before the column existed
stay NULL ("appliance-wide") and only a root operator at home sees them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from robothor.audit import logger as audit
from robothor.db.connection import tenant_scope

MIGRATION = Path(__file__).resolve().parents[3] / "crm" / "migrations" / "154_audit_log_tenant.sql"


@pytest.fixture(autouse=True)
def _factory():
    audit.reset_connection_factory()
    yield
    audit.reset_connection_factory()


def _mock_conn():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    cursor.fetchone.return_value = (1, datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC))
    cursor.fetchall.return_value = []
    return conn, cursor


def _insert_args(cursor) -> tuple[str, tuple]:
    sql, params = cursor.execute.call_args.args
    return sql, params


class TestWritesCarryTheTenant:
    def test_a_write_inside_a_scope_records_that_tenant(self):
        conn, cursor = _mock_conn()
        audit.set_connection_factory(lambda: conn)
        with tenant_scope("acme-child-a"):
            audit.log_event("crm.create", "made a thing")
        sql, params = _insert_args(cursor)
        assert "tenant_id" in sql
        assert "acme-child-a" in params

    def test_a_write_outside_any_scope_records_the_process_default(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_TENANT_ID", "acme-parent")
        conn, cursor = _mock_conn()
        audit.set_connection_factory(lambda: conn)
        audit.log_event("crm.create", "made a thing")
        _sql, params = _insert_args(cursor)
        assert "acme-parent" in params

    def test_an_explicit_tenant_wins(self):
        conn, cursor = _mock_conn()
        audit.set_connection_factory(lambda: conn)
        with tenant_scope("acme-child-a"):
            audit.log_event("crm.create", "made a thing", tenant_id="acme-child-b")
        _sql, params = _insert_args(cursor)
        assert "acme-child-b" in params
        assert "acme-child-a" not in params


class TestReadsAreFiltered:
    def test_unfiltered_by_default(self):
        conn, cursor = _mock_conn()
        audit.set_connection_factory(lambda: conn)
        audit.query_log()
        sql, _params = cursor.execute.call_args.args
        assert "tenant_id" not in sql

    def test_tenants_filter_without_the_untenanted(self):
        conn, cursor = _mock_conn()
        audit.set_connection_factory(lambda: conn)
        audit.query_log(tenants=["acme-child-a"])
        sql, params = cursor.execute.call_args.args
        assert "tenant_id = ANY(%s)" in sql
        assert "IS NULL" not in sql
        assert ["acme-child-a"] in params

    def test_the_untenanted_rows_are_opt_in(self):
        conn, cursor = _mock_conn()
        audit.set_connection_factory(lambda: conn)
        audit.query_log(tenants=["acme-parent"], include_untenanted=True)
        sql, _params = cursor.execute.call_args.args
        assert "tenant_id IS NULL" in sql

    def test_an_empty_tenant_list_reads_nothing_rather_than_everything(self):
        conn, cursor = _mock_conn()
        audit.set_connection_factory(lambda: conn)
        audit.query_log(tenants=[])
        sql, params = cursor.execute.call_args.args
        assert "tenant_id = ANY(%s)" in sql
        assert [] in params

    def test_stats_are_filtered_the_same_way(self):
        conn, cursor = _mock_conn()
        cursor.fetchone.return_value = (0, 0, None, None)
        audit.set_connection_factory(lambda: conn)
        audit.stats(tenants=["acme-child-a"])
        for call in cursor.execute.call_args_list:
            sql = call.args[0]
            assert "tenant_id = ANY(%s)" in sql
            assert "IS NULL" not in sql


# ─── Real Postgres ────────────────────────────────────────────────────


class _NoCommit:
    """``db_conn`` with commit made a no-op, so the test's rollback isolates."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def commit(self):
        return None


@pytest.fixture
def migrated(db_conn, monkeypatch):
    with db_conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    monkeypatch.setattr(audit, "_release_connection", lambda conn: None)
    audit.set_connection_factory(lambda: _NoCommit(db_conn))
    return db_conn


@pytest.mark.integration
class TestOnPostgres:
    def test_the_migration_adds_a_nullable_column_and_an_index(self, migrated):
        with migrated.cursor() as cur:
            cur.execute(
                "SELECT is_nullable, data_type FROM information_schema.columns "
                "WHERE table_name = 'audit_log' AND column_name = 'tenant_id'"
            )
            assert cur.fetchone() == ("YES", "text")
            cur.execute(
                "SELECT indexdef FROM pg_indexes WHERE tablename = 'audit_log' "
                "AND indexdef LIKE '%%tenant_id%%'"
            )
            assert cur.fetchone() is not None

    def test_the_migration_is_idempotent(self, migrated):
        with migrated.cursor() as cur:
            cur.execute(MIGRATION.read_text(encoding="utf-8"))

    def test_reads_see_their_tenants_and_only_root_sees_the_untenanted(self, migrated):
        marker = "audit-tenant-test"
        with tenant_scope("acme-child-a"):
            audit.log_event("test.event", "a", actor=marker)
        with tenant_scope("acme-child-b"):
            audit.log_event("test.event", "b", actor=marker)
        with migrated.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (event_type, actor, action) VALUES ('test.event', %s, 'n')",
                (marker,),
            )

        child = audit.query_log(actor=marker, tenants=["acme-child-a"])
        assert [e["action"] for e in child] == ["a"]

        root = audit.query_log(
            actor=marker, tenants=["acme-parent", "acme-child-a"], include_untenanted=True
        )
        assert sorted(e["action"] for e in root) == ["a", "n"]
