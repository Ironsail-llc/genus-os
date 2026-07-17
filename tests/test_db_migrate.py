"""Unit tests for the canonical PostgreSQL migration runner."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from robothor.db import migrate

if TYPE_CHECKING:
    from pathlib import Path


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self._one: tuple[Any, ...] | None = None
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        normalized = " ".join(sql.split())
        self.connection.statements.append((normalized, params))
        self._one = None
        self._rows = []

        if "pg_advisory_unlock" in normalized:
            self._one = (True,)
        elif "pg_advisory_lock" in normalized:
            self._one = (None,)
        elif "SELECT to_regclass" in normalized:
            self._one = ("schema_migrations",) if self.connection.legacy else (None,)
        elif normalized.startswith("SELECT version, filename, applied_at, checksum"):
            self._rows = list(self.connection.legacy)
        elif normalized.startswith("SELECT migration_id, version, filename"):
            self._rows = list(self.connection.history.values())
        elif normalized.startswith("INSERT INTO schema_migrations_v2"):
            assert params is not None
            migration_id, version, filename, source, applied_at, checksum, reconciled = params
            self.connection.history.setdefault(
                str(migration_id),
                (
                    migration_id,
                    version,
                    filename,
                    source,
                    applied_at or "now",
                    checksum,
                    reconciled,
                ),
            )
        elif normalized.startswith("CREATE TABLE IF NOT EXISTS schema_migrations_v2"):
            return
        else:
            if "FAIL_ME" in sql:
                raise RuntimeError("synthetic migration failure")
            self.connection.executed_sql.append(sql)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._one

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _FakeConnection:
    def __init__(
        self,
        *,
        history: dict[str, tuple[Any, ...]] | None = None,
        legacy: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.history = history or {}
        self.legacy = legacy or []
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self.executed_sql: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _write_migration(directory: Path, filename: str, body: str) -> Path:
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_discovers_complete_manifest_with_unique_immutable_ids() -> None:
    migrations = migrate._discover()
    ids = [migration.migration_id for migration in migrations]

    assert len(migrations) == 92
    assert len(ids) == len(set(ids))
    assert ids[0] == "001_init"
    assert "001_crm_tables" in ids
    assert "071_memory_vault" in ids
    assert "071_user_accounts" in ids
    # This ignored, installation-specific SQL exists in some workspaces but is
    # intentionally absent from the product manifest.
    assert "063_delphi_pmf_experiments" not in ids


def test_numeric_selector_must_be_unambiguous() -> None:
    migrations = migrate._discover()

    with pytest.raises(migrate.MigrationSelectionError, match="ambiguous"):
        migrate._select_migrations(migrations, "071")

    selected = migrate._select_migrations(migrations, "071_user_accounts")
    assert [migration.migration_id for migration in selected] == ["071_user_accounts"]


def test_outer_transaction_is_removed_but_plpgsql_block_is_preserved() -> None:
    sql = """-- heading
BEGIN;
DO $$
BEGIN
    RAISE NOTICE 'inside';
END $$;
COMMIT;
-- trailing note
"""

    stripped = migrate._strip_outer_transaction(sql)

    assert "\nBEGIN;\n" not in stripped
    assert "DO $$\nBEGIN\n" in stripped
    assert "COMMIT;" not in stripped
    assert "-- trailing note" in stripped


def test_apply_locks_and_commits_each_file_with_ledger_record(tmp_path: Path) -> None:
    _write_migration(
        tmp_path,
        "001_first.sql",
        "BEGIN;\nCREATE TABLE first_table (id int);\nCOMMIT;\n",
    )
    _write_migration(tmp_path, "002_second.sql", "CREATE TABLE second_table (id int);\n")
    connection = _FakeConnection()

    applied = migrate.apply(migrations_dir=tmp_path, connection=connection)

    assert applied == ["001_first", "002_second"]
    assert set(connection.history) == {"001_first", "002_second"}
    assert len(connection.executed_sql) == 2
    assert "BEGIN;" not in connection.executed_sql[0]
    assert "COMMIT;" not in connection.executed_sql[0]
    assert any("pg_advisory_lock" in sql for sql, _ in connection.statements)
    assert any("pg_advisory_unlock" in sql for sql, _ in connection.statements)
    # lock acquisition + history preparation + two per-file commits + unlock
    assert connection.commits == 5


def test_apply_rolls_back_failed_file_without_recording_it(tmp_path: Path) -> None:
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;")
    _write_migration(tmp_path, "002_second.sql", "FAIL_ME;")
    connection = _FakeConnection()

    with pytest.raises(RuntimeError, match="synthetic migration failure"):
        migrate.apply(migrations_dir=tmp_path, connection=connection)

    assert set(connection.history) == {"001_first"}
    assert connection.rollbacks >= 1
    assert any("pg_advisory_unlock" in sql for sql, _ in connection.statements)


def test_apply_refuses_checksum_drift_before_executing_sql(tmp_path: Path) -> None:
    path = _write_migration(tmp_path, "001_first.sql", "SELECT 1;")
    history = {
        "001_first": (
            "001_first",
            "001",
            path.name,
            tmp_path.name,
            "then",
            "0" * 64,
            False,
        )
    }
    connection = _FakeConnection(history=history)

    with pytest.raises(migrate.MigrationDriftError, match="checksum"):
        migrate.apply(migrations_dir=tmp_path, connection=connection)

    assert connection.executed_sql == []


def test_legacy_duplicate_version_adopts_exact_file_only(tmp_path: Path) -> None:
    first = _write_migration(tmp_path, "071_alpha.sql", "SELECT 'alpha';")
    _write_migration(tmp_path, "071_beta.sql", "SELECT 'beta';")
    connection = _FakeConnection(legacy=[("071", first.name, "then", migrate._sha256(first))])

    rows = migrate.status(migrations_dir=tmp_path, connection=connection)

    statuses = {row["migration_id"]: row["status"] for row in rows}
    assert statuses == {"071_alpha": "applied", "071_beta": "pending"}
    assert connection.history["071_alpha"][-1] is True


def test_legacy_duplicate_version_without_identity_is_rejected(tmp_path: Path) -> None:
    _write_migration(tmp_path, "071_alpha.sql", "SELECT 'alpha';")
    _write_migration(tmp_path, "071_beta.sql", "SELECT 'beta';")
    connection = _FakeConnection(legacy=[("071", "unknown.sql", "then", None)])

    with pytest.raises(migrate.MigrationHistoryError, match="ambiguous"):
        migrate.status(migrations_dir=tmp_path, connection=connection)


def test_memory_v4_migration_archives_legacy_tables_instead_of_dropping() -> None:
    sql = (migrate._REPO_ROOT / "crm/migrations/023_memory_v4_schema.sql").read_text()

    assert "DROP TABLE" not in sql.upper()
    assert "migration_archive_023_short_term_memory" in sql
    assert "migration_archive_023_long_term_memory" in sql
    assert "refusing ambiguous migration" in sql


def test_buddy_cutover_enforces_soak_and_archives_before_drop() -> None:
    sql = (migrate._REPO_ROOT / "infra/migrations/035_drop_legacy_buddy_columns.sql").read_text()
    preflight = sql.index("achievement_days < 30")
    archive = sql.index("CREATE TABLE IF NOT EXISTS migration_archive_035_buddy_rpg")
    destructive_change = sql.index("DROP COLUMN IF EXISTS debugging_score")

    assert preflight < archive < destructive_change
    assert "to_jsonb(s)" in sql
    assert "RAISE EXCEPTION" in sql
