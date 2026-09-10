"""Unit tests for the canonical PostgreSQL migration runner."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from robothor.db import migrate


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
            target = str(params[0]) if params else ""
            if target == "public.schema_migrations":
                self._one = ("schema_migrations",) if self.connection.legacy else (None,)
            elif target == f"public.{migrate.BASELINE_EVIDENCE_TABLE}":
                self._one = (
                    (migrate.BASELINE_EVIDENCE_TABLE,)
                    if self.connection.schema_present
                    else (None,)
                )
            else:
                self._one = (None,)
        elif normalized.startswith("SELECT version, filename, applied_at, checksum"):
            self._rows = list(self.connection.legacy)
        elif normalized.startswith("SELECT migration_id, version, filename"):
            self._rows = list(self.connection.history.values())
        elif normalized.startswith("INSERT INTO schema_migrations_v2"):
            assert params is not None
            (
                migration_id,
                version,
                filename,
                source,
                applied_at,
                checksum,
                reconciled,
                adopted_from,
            ) = params
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
                    adopted_from,
                ),
            )
        elif normalized.startswith(
            ("CREATE TABLE IF NOT EXISTS schema_migrations_v2", "ALTER TABLE schema_migrations_v2")
        ):
            return  # ledger DDL, not migration SQL
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
        schema_present: bool = False,
    ) -> None:
        self.history = history or {}
        self.legacy = legacy or []
        self.schema_present = schema_present
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


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Keep the legacy YAML side-ledger lookup off the operator's real workspace.

    `_legacy_yaml_filenames` reads ``$ROBOTHOR_WORKSPACE/.robothor/
    migrations_applied.yaml``. Without this, a test's adoption set would depend
    on whatever the box happens to have there.
    """
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path_factory.mktemp("workspace")))


def _ledger_row(
    migration_id: str,
    version: str,
    filename: str,
    source: str,
    checksum: str,
    *,
    applied_at: str = "then",
    reconciled_from_legacy: bool = False,
    adopted_from: str | None = None,
) -> tuple[Any, ...]:
    """One `schema_migrations_v2` row in the column order `_applied` selects."""
    return (
        migration_id,
        version,
        filename,
        source,
        applied_at,
        checksum,
        reconciled_from_legacy,
        adopted_from,
    )


def _write_yaml_ledger(workspace: Path, filenames: list[str]) -> Path:
    """Write the retired `.robothor/migrations_applied.yaml` side-ledger."""
    path = workspace / ".robothor" / "migrations_applied.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(
            {
                "migrations": [
                    {"file": name, "applied_at": "2026-01-01T00:00:00Z"} for name in filenames
                ],
                "template_hashes": {"SOUL.md": "abc"},
            }
        )
    )
    return path


def _write_migration(directory: Path, filename: str, body: str) -> Path:
    path = directory / filename
    path.write_text(body, encoding="utf-8")
    return path


def test_discovers_complete_manifest_with_unique_immutable_ids() -> None:
    migrations = migrate._discover()
    ids = [migration.migration_id for migration in migrations]

    # Checked against the manifest itself, not a hardcoded count. The magic
    # number only caught "someone added a migration" — not a defect — and it
    # went stale on 093, 094, 095 and 096 in a row.
    #
    # A missing file raises MigrationDiscoveryError inside `_discover`, so this
    # is not what guards against that (verified by adding a bogus entry: it
    # raises, it does not skip). What it does pin is that discovery neither
    # drops nor invents entries relative to the registry — a dedup or filter bug
    # in `_manifest_paths` would show up here and nowhere else.
    manifest_ids = [
        Path(line.strip()).stem
        for line in migrate._MIGRATION_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Compared as sets, not sequences: `_discover` sorts deterministically while
    # the manifest lists two same-version files (034_*) in the opposite order,
    # and that ordering is arbitrary for equal versions.
    assert set(ids) == set(manifest_ids), "discovery does not match the canonical manifest"
    assert len(ids) == len(manifest_ids)
    assert len(ids) == len(set(ids))
    assert ids[0] == "001_init"
    assert "001_crm_tables" in ids
    assert "071_memory_vault" in ids
    assert "071_user_accounts" in ids
    assert "092_memory_access_rollup" in ids
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
        "001_first": _ledger_row("001_first", "001", path.name, tmp_path.name, "0" * 64),
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
    assert connection.history["071_alpha"][6] is True  # reconciled_from_legacy


def test_legacy_duplicate_version_without_identity_is_rejected(tmp_path: Path) -> None:
    _write_migration(tmp_path, "071_alpha.sql", "SELECT 'alpha';")
    _write_migration(tmp_path, "071_beta.sql", "SELECT 'beta';")
    connection = _FakeConnection(legacy=[("071", "unknown.sql", "then", None)])

    with pytest.raises(migrate.MigrationHistoryError, match="ambiguous"):
        migrate.status(migrations_dir=tmp_path, connection=connection)


def test_legacy_instance_local_migration_is_left_unmanaged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A legacy row for a migration deliberately excluded from the manifest
    (e.g. delphi persona SQL, per manifest.txt's own contract) must not wedge
    reconciliation. Its version does not collide with any manifest version at
    all, so it is skipped rather than raising.
    """
    _write_migration(tmp_path, "001_first.sql", "SELECT 1;")
    connection = _FakeConnection(legacy=[("058", "058_delphi_proposals.sql", "then", "f" * 64)])

    with caplog.at_level(logging.WARNING, logger="robothor.db.migrate"):
        rows = migrate.status(migrations_dir=tmp_path, connection=connection)

    # The instance-local row was not adopted into the canonical ledger.
    assert connection.history == {}
    # It is surfaced in status output as unmanaged, not silently dropped.
    unmanaged_rows = [row for row in rows if row["status"] == "unmanaged"]
    assert len(unmanaged_rows) == 1
    assert unmanaged_rows[0]["filename"] == "058_delphi_proposals.sql"
    assert unmanaged_rows[0]["version"] == "058"
    # The canonical migration is still reported normally.
    assert any(row["migration_id"] == "001_first" for row in rows)
    assert any(
        "058" in record.message and "instance-local" in record.message for record in caplog.records
    )


def test_legacy_version_collision_with_manifest_still_raises(tmp_path: Path) -> None:
    """A legacy version that DOES collide with the manifest (multiple files
    share that numeric prefix) but can't be disambiguated by filename or
    checksum remains a hard failure — this is genuine ambiguity/rename risk,
    not an excluded instance-local migration.
    """
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


def test_do_not_contact_migration_is_manifested_after_federation_principals() -> None:
    """113 must be discoverable, and must apply after 112.

    A migration file that exists on disk but not in the manifest is invisible
    to `_discover` — the shape PR #457 found: an untracked 113 that no
    deployment would ever run. Ordering is derived from the prefix, so the
    manifest line has to sit after 112 for the ledger to read in apply order.
    """
    lines = [
        line.strip()
        for line in migrate._MIGRATION_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "crm/113_add_do_not_contact.sql" in lines
    assert lines.index("crm/113_add_do_not_contact.sql") == (
        lines.index("crm/112_federation_principals.sql") + 1
    )

    sql = (migrate._REPO_ROOT / "crm/migrations/113_add_do_not_contact.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS do_not_contact BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "idx_crm_people_dnc" in sql

    assert "113_add_do_not_contact" in [m.migration_id for m in migrate._discover()]


def _write_baseline_pair(directory: Path) -> None:
    """The two-file chain used by the baseline-adoption tests.

    ``001_init.sql`` stands in for the real baseline that
    ``docker-entrypoint-initdb.d`` used to run outside the ledger.
    """
    _write_migration(directory, "001_init.sql", "CREATE TABLE memory_facts (id int);\n")
    _write_migration(directory, "002_second.sql", "CREATE TABLE second_table (id int);\n")


def test_manifest_count_matches_the_canonical_manifest() -> None:
    entries = [
        line.strip()
        for line in migrate._MIGRATION_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert migrate.manifest_count() == len(entries)
    assert migrate.manifest_count() == len(migrate._discover())


def test_empty_ledger_on_a_virgin_database_applies_the_whole_chain(tmp_path: Path) -> None:
    _write_baseline_pair(tmp_path)
    connection = _FakeConnection(schema_present=False)

    applied = migrate.apply(migrations_dir=tmp_path, connection=connection)

    assert applied == ["001_init", "002_second"]
    assert len(connection.executed_sql) == 2


def test_apply_refuses_to_rerun_the_baseline_when_the_schema_already_exists(
    tmp_path: Path,
) -> None:
    """The initdb path created the schema without writing a ledger row.

    Re-running ``001_init.sql`` against a populated production database is the
    exact accident this guard exists to prevent, so nothing may execute.
    """
    _write_baseline_pair(tmp_path)
    connection = _FakeConnection(schema_present=True)

    with pytest.raises(migrate.MigrationHistoryError, match="--adopt-baseline"):
        migrate.apply(migrations_dir=tmp_path, connection=connection)

    assert connection.executed_sql == []
    assert connection.history == {}


def test_adopt_baseline_records_the_first_migration_without_executing_it(
    tmp_path: Path,
) -> None:
    _write_baseline_pair(tmp_path)
    connection = _FakeConnection(schema_present=True)

    applied = migrate.apply(migrations_dir=tmp_path, connection=connection, adopt_baseline=True)

    # The baseline is adopted into the ledger; only the rest actually runs.
    assert applied == ["002_second"]
    assert set(connection.history) == {"001_init", "002_second"}
    assert connection.executed_sql == ["CREATE TABLE second_table (id int);\n"]
    assert connection.history["001_init"][5] == migrate._sha256(tmp_path / "001_init.sql")


def test_adopt_baseline_is_inert_once_the_ledger_has_rows(tmp_path: Path) -> None:
    """The flag must never skip a migration on a database already under the ledger."""
    _write_baseline_pair(tmp_path)
    path = tmp_path / "001_init.sql"
    history = {
        "001_init": _ledger_row("001_init", "001", path.name, tmp_path.name, migrate._sha256(path)),
    }
    connection = _FakeConnection(history=history, schema_present=True)

    applied = migrate.apply(migrations_dir=tmp_path, connection=connection, adopt_baseline=True)

    assert applied == ["002_second"]
    assert connection.executed_sql == ["CREATE TABLE second_table (id int);\n"]


def test_status_names_the_adopt_baseline_remedy_when_the_ledger_is_empty(
    tmp_path: Path,
) -> None:
    _write_baseline_pair(tmp_path)
    connection = _FakeConnection(schema_present=True)

    rows = migrate.status(migrations_dir=tmp_path, connection=connection)

    unadopted = [row for row in rows if row["status"] == "baseline-unadopted"]
    assert len(unadopted) == 1
    message = unadopted[0]["message"]
    assert "ledger empty but schema present" in message
    assert "robothor migrate --adopt-baseline" in message


def test_status_is_quiet_about_the_baseline_on_a_virgin_database(tmp_path: Path) -> None:
    _write_baseline_pair(tmp_path)
    connection = _FakeConnection(schema_present=False)

    rows = migrate.status(migrations_dir=tmp_path, connection=connection)

    assert not [row for row in rows if row["status"] == "baseline-unadopted"]


def test_status_replaces_the_baseline_row_instead_of_adding_a_row(tmp_path: Path) -> None:
    """One row per manifest entry. An extra row makes the count lie."""
    _write_baseline_pair(tmp_path)
    connection = _FakeConnection(schema_present=True)

    rows = migrate.status(migrations_dir=tmp_path, connection=connection)

    assert len(rows) == 2
    by_id = {row["migration_id"]: row["status"] for row in rows}
    assert by_id == {"001_init": "baseline-unadopted", "002_second": "pending"}


def test_apply_refuses_when_the_whole_ledger_came_from_the_legacy_table(
    tmp_path: Path,
) -> None:
    """The dangerous state is not an *empty* ledger — it is an unverified one.

    An instance migrated by the retired `robothor upgrade` glob carries the
    legacy `schema_migrations` table that 018 backfills, so reconciliation
    fills `schema_migrations_v2` and the ledger is no longer empty. Every row
    is still hearsay: nothing proves the files after the backfill ran or did
    not. Replaying them is destructive (019 DELETEs live chat_sessions), so
    the refusal must key on provenance, not on emptiness.
    """
    first = _write_migration(tmp_path, "001_init.sql", "CREATE TABLE memory_facts (id int);\n")
    _write_migration(tmp_path, "002_second.sql", "DELETE FROM chat_sessions;\n")
    connection = _FakeConnection(
        legacy=[("001", first.name, "then", migrate._sha256(first))],
        schema_present=True,
    )

    with pytest.raises(migrate.MigrationHistoryError, match="--adopt-baseline"):
        migrate.apply(migrations_dir=tmp_path, connection=connection)

    assert connection.executed_sql == []


def test_apply_proceeds_when_this_runner_wrote_a_ledger_row(tmp_path: Path) -> None:
    """A normal upgrade must never be refused.

    A partially-applied ledger is the ordinary state of every instance between
    releases; only the absence of any row this runner wrote is evidence.
    """
    first = _write_migration(tmp_path, "001_init.sql", "CREATE TABLE memory_facts (id int);\n")
    _write_migration(tmp_path, "002_second.sql", "CREATE TABLE second_table (id int);\n")
    history = {
        "001_init": _ledger_row(
            "001_init", "001", first.name, tmp_path.name, migrate._sha256(first)
        ),
    }
    connection = _FakeConnection(history=history, schema_present=True)

    applied = migrate.apply(migrations_dir=tmp_path, connection=connection)

    assert applied == ["002_second"]


def test_apply_does_not_refuse_when_there_is_nothing_left_to_apply(tmp_path: Path) -> None:
    """No pending work means no replay risk, so an unverified ledger is fine."""
    first = _write_migration(tmp_path, "001_init.sql", "CREATE TABLE memory_facts (id int);\n")
    connection = _FakeConnection(
        legacy=[("001", first.name, "then", migrate._sha256(first))],
        schema_present=True,
    )

    assert migrate.apply(migrations_dir=tmp_path, connection=connection) == []


def test_adopt_baseline_adopts_every_entry_the_yaml_side_ledger_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The YAML side-ledger is the only record of what the glob path applied.

    Adopting the baseline alone would replay 002..N over live data, so every
    manifest entry the side-ledger names is adopted too — recorded, never
    executed — and only what it does not name actually runs.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_migration(migrations_dir, "001_init.sql", "CREATE TABLE memory_facts (id int);\n")
    _write_migration(migrations_dir, "002_second.sql", "DELETE FROM chat_sessions;\n")
    _write_migration(migrations_dir, "003_third.sql", "CREATE TABLE third_table (id int);\n")
    _write_yaml_ledger(workspace, ["001_init.sql", "002_second.sql"])

    connection = _FakeConnection(schema_present=True)

    applied = migrate.apply(
        migrations_dir=migrations_dir, connection=connection, adopt_baseline=True
    )

    assert applied == ["003_third"]
    assert connection.executed_sql == ["CREATE TABLE third_table (id int);\n"]
    assert set(connection.history) == {"001_init", "002_second", "003_third"}
    # Provenance stays visible: adopted rows say where the claim came from.
    assert connection.history["001_init"][7] == "baseline"
    assert connection.history["002_second"][7] == "yaml_ledger"
    assert connection.history["003_third"][7] is None


def test_adopt_baseline_falls_back_to_the_first_entry_without_a_side_ledger(
    tmp_path: Path,
) -> None:
    _write_baseline_pair(tmp_path)
    connection = _FakeConnection(schema_present=True)

    applied = migrate.apply(migrations_dir=tmp_path, connection=connection, adopt_baseline=True)

    assert applied == ["002_second"]
    assert connection.history["001_init"][7] == "baseline"


def _write_chain(directory: Path, count: int) -> list[str]:
    """A baseline plus ``count - 1`` follow-ons, each creating its own table."""
    _write_migration(directory, "001_init.sql", "CREATE TABLE memory_facts (id int);\n")
    names = ["001_init.sql"]
    for index in range(2, count + 1):
        name = f"{index:03d}_step.sql"
        _write_migration(directory, name, f"CREATE TABLE step_{index} (id int);\n")
        names.append(name)
    return names


def test_adopt_baseline_refuses_when_legacy_rows_outrun_a_missing_side_ledger(
    tmp_path: Path,
) -> None:
    """Adopting only the baseline here would execute the rest over live data.

    The legacy `schema_migrations` table vouches for migrations past the
    baseline, so the schema is further along than 001 — but with no side-ledger
    there is nothing saying *how* much further. Adopting 001 alone and then
    running 003 onward turns the remedy into the hazard, so this must refuse
    and tell the operator to say where the schema really is.
    """
    _write_chain(tmp_path, 3)
    first = tmp_path / "001_init.sql"
    second = tmp_path / "002_step.sql"
    connection = _FakeConnection(
        legacy=[
            ("001", first.name, "then", migrate._sha256(first)),
            ("002", second.name, "then", migrate._sha256(second)),
        ],
        schema_present=True,
    )

    with pytest.raises(migrate.MigrationHistoryError, match="--adopt-through"):
        migrate.apply(migrations_dir=tmp_path, connection=connection, adopt_baseline=True)

    assert connection.executed_sql == []
    # Nothing was adopted on the way out: a half-done adoption is its own trap.
    assert all(row[7] is None for row in connection.history.values())


def test_adoption_names_the_side_ledger_path_it_looked_for(tmp_path: Path) -> None:
    _write_chain(tmp_path, 3)
    first = tmp_path / "001_init.sql"
    second = tmp_path / "002_step.sql"
    connection = _FakeConnection(
        legacy=[
            ("001", first.name, "then", migrate._sha256(first)),
            ("002", second.name, "then", migrate._sha256(second)),
        ],
        schema_present=True,
    )

    with pytest.raises(migrate.MigrationHistoryError) as excinfo:
        migrate.apply(migrations_dir=tmp_path, connection=connection, adopt_baseline=True)

    assert str(migrate._legacy_yaml_ledger_path()) in str(excinfo.value)


def test_an_unreadable_side_ledger_refuses_instead_of_reading_as_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evidence that exists but cannot be parsed is not the same as no evidence.

    Degrading a malformed file to an empty set silently narrows the adoption
    to the baseline — the same destructive shape, with no warning at all.
    """
    workspace = tmp_path / "workspace"
    (workspace / ".robothor").mkdir(parents=True)
    (workspace / ".robothor" / "migrations_applied.yaml").write_text("migrations: {[\n")
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(workspace))
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    _write_chain(migrations_dir, 2)
    connection = _FakeConnection(schema_present=True)

    with pytest.raises(migrate.MigrationHistoryError, match="could not be read"):
        migrate.apply(migrations_dir=migrations_dir, connection=connection, adopt_baseline=True)

    assert connection.executed_sql == []


def test_adopt_through_adopts_the_named_range_and_executes_only_the_rest(
    tmp_path: Path,
) -> None:
    _write_chain(tmp_path, 4)
    connection = _FakeConnection(schema_present=True)

    applied = migrate.apply(
        migrations_dir=tmp_path, connection=connection, adopt_through="003_step"
    )

    assert applied == ["004_step"]
    assert connection.executed_sql == ["CREATE TABLE step_4 (id int);\n"]
    assert connection.history["001_init"][7] == "baseline"
    assert connection.history["002_step"][7] == "operator"
    assert connection.history["003_step"][7] == "operator"
    assert connection.history["004_step"][7] is None


def test_adopt_through_alone_satisfies_the_refusal(tmp_path: Path) -> None:
    """It implies the baseline adoption — the operator need not pass both."""
    _write_chain(tmp_path, 3)
    first = tmp_path / "001_init.sql"
    second = tmp_path / "002_step.sql"
    connection = _FakeConnection(
        legacy=[
            ("001", first.name, "then", migrate._sha256(first)),
            ("002", second.name, "then", migrate._sha256(second)),
        ],
        schema_present=True,
    )

    applied = migrate.apply(
        migrations_dir=tmp_path, connection=connection, adopt_through="002_step"
    )

    assert applied == ["003_step"]
    assert connection.executed_sql == ["CREATE TABLE step_3 (id int);\n"]


def test_adopt_through_rejects_a_migration_id_that_is_not_in_the_manifest(
    tmp_path: Path,
) -> None:
    _write_chain(tmp_path, 3)
    connection = _FakeConnection(schema_present=True)

    with pytest.raises(migrate.MigrationSelectionError, match="099_nope"):
        migrate.apply(migrations_dir=tmp_path, connection=connection, adopt_through="099_nope")

    assert connection.executed_sql == []


def test_an_adopted_ledger_is_not_refused_on_the_next_run(tmp_path: Path) -> None:
    """Adoption is the operator vouching for the ledger; it must stick."""
    first = _write_migration(tmp_path, "001_init.sql", "CREATE TABLE memory_facts (id int);\n")
    _write_migration(tmp_path, "002_second.sql", "CREATE TABLE second_table (id int);\n")
    history = {
        "001_init": _ledger_row(
            "001_init",
            "001",
            first.name,
            tmp_path.name,
            migrate._sha256(first),
            reconciled_from_legacy=False,
            adopted_from="baseline",
        ),
    }
    connection = _FakeConnection(history=history, schema_present=True)

    assert migrate.apply(migrations_dir=tmp_path, connection=connection) == ["002_second"]
