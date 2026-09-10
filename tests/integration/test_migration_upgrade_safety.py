"""Destructive-upgrade regression fixtures against a real PostgreSQL server."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from typing import TYPE_CHECKING

import psycopg2
import pytest
import yaml
from psycopg2 import sql

from robothor.db import migrate

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

# The 18 rows `crm/migrations/018_migration_tracking.sql` backfills. An
# instance migrated by the retired `robothor upgrade` glob has exactly this
# table and nothing else vouching for its history.
_LEGACY_BACKFILL = [
    ("001", "001_crm_tables.sql"),
    ("004", "004_telemetry_table.sql"),
    ("005", "005_task_coordination.sql"),
    ("006", "006_task_state_machine.sql"),
    ("007", "007_routines.sql"),
    ("008", "008_multi_tenancy.sql"),
    ("009", "009_agent_notifications.sql"),
    ("010", "010_health_tables.sql"),
    ("011", "011_agent_engine.sql"),
    ("012", "012_webchat_trigger_type.sql"),
    ("013", "013_workflow_engine.sql"),
    ("014", "014_engine_enhancements.sql"),
    ("015", "015_chat_history.sql"),
    ("015b", "015b_supervisor_to_main.sql"),
    ("016", "016_sub_agents.sql"),
    ("017", "017_timezone_new_york.sql"),
    ("018", "018_migration_tracking.sql"),
]


def _apply_quietly(connection, migration_id: str) -> None:
    with redirect_stdout(StringIO()):
        migrate.apply(version=migration_id, connection=connection)


def _apply_all_quietly(connection, *, adopt_baseline: bool = False) -> list[str]:
    with redirect_stdout(StringIO()):
        return migrate.apply(connection=connection, adopt_baseline=adopt_baseline)


@contextmanager
def _scratch_database():
    """Yield a connection to a throwaway database, dropped afterwards."""
    admin_dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN")
    if not admin_dsn:
        pytest.skip("requires ROBOTHOR_TEST_DB_DSN")
    database = f"genus_migrate_{uuid.uuid4().hex[:12]}_test"
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    params = psycopg2.extensions.parse_dsn(admin_dsn)
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        connection = psycopg2.connect(**{**params, "dbname": database})
        try:
            yield connection
        finally:
            connection.close()
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
        admin.close()


def _run_baseline_outside_the_ledger(connection) -> None:
    """Create the schema the way `docker-entrypoint-initdb.d` used to.

    The file's SQL runs; no `schema_migrations_v2` row is written. That is the
    whole hazard: schema without history.
    """
    baseline = migrate._discover()[0]
    body = migrate._strip_outer_transaction(baseline.path.read_text(encoding="utf-8"))
    with connection.cursor() as cursor:
        cursor.execute(body)
    connection.commit()


def _ledger_ids(connection) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT migration_id FROM schema_migrations_v2")
        return {row[0] for row in cursor.fetchall()}


def _write_yaml_side_ledger(workspace: Path, filenames: list[str]) -> None:
    path = workspace / ".robothor" / "migrations_applied.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(
            {
                "migrations": [
                    {"file": name, "applied_at": "2026-01-01T00:00:00Z"} for name in filenames
                ]
            }
        )
    )


def test_initdb_baseline_is_refused_then_adopted_and_the_rest_applies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database created by the retired initdb mount must not be replayed."""
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))

    with _scratch_database() as connection:
        _run_baseline_outside_the_ledger(connection)

        # (a) schema present, ledger empty -> refuse, execute nothing.
        with pytest.raises(migrate.MigrationHistoryError, match="--adopt-baseline"):
            _apply_all_quietly(connection)
        assert _ledger_ids(connection) == set()

        # (b) adopt, then the rest of the chain applies for real.
        _apply_all_quietly(connection, adopt_baseline=True)

        ledger = _ledger_ids(connection)
        assert len(ledger) == migrate.manifest_count()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT adopted_from FROM schema_migrations_v2 WHERE migration_id = '001_init'"
            )
            assert cursor.fetchone() == ("baseline",)
            # Only the baseline was taken on trust; everything else really ran.
            cursor.execute(
                "SELECT count(*) FROM schema_migrations_v2 WHERE adopted_from IS NOT NULL"
            )
            assert cursor.fetchone() == (1,)


def test_glob_migrated_instance_is_refused_and_adopts_through_the_yaml_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The legacy `schema_migrations` table is not evidence the v2 runner ran.

    An instance migrated by the retired glob has 018's 18-row backfill, so
    reconciliation leaves the ledger non-empty. Keying the guard on emptiness
    would let `apply()` replay 019 onward — and
    `crm/migrations/019_unified_session.sql` DELETEs live `chat_sessions`.
    """
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    migrations = migrate._discover()
    by_id = {item.migration_id: index for index, item in enumerate(migrations)}
    cutoff = by_id["040_memory_episodes"] + 1
    through_cutoff = migrations[:cutoff]

    with _scratch_database() as connection:
        # Get the database genuinely to 040, then erase the v2 ledger so all
        # that remains is what the glob path would have left behind.
        for migration in through_cutoff:
            _apply_quietly(connection, migration.migration_id)
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM schema_migrations_v2")
            cursor.executemany(
                "INSERT INTO schema_migrations (version, filename) VALUES (%s, %s) "
                "ON CONFLICT (version) DO NOTHING",
                _LEGACY_BACKFILL,
            )
        connection.commit()
        _write_yaml_side_ledger(tmp_path, [item.filename for item in through_cutoff])

        # A live row matching 019's DELETE predicate. If the chain is replayed
        # it disappears (CASCADE takes its messages with it), so its survival
        # is the assertion that nothing re-executed.
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO chat_sessions (tenant_id, session_key, channel) "
                "VALUES ('default', 'agent:main:webchat-live', 'webchat') RETURNING id"
            )
            session_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO chat_messages (session_id, message) VALUES (%s, %s)",
                (session_id, '{"role": "user", "content": "keep me"}'),
            )
        connection.commit()

        with pytest.raises(migrate.MigrationHistoryError, match="--adopt-baseline"):
            _apply_all_quietly(connection)

        applied = _apply_all_quietly(connection, adopt_baseline=True)

        # Nothing at or before the cutoff re-executed; everything after did.
        adopted_ids = {item.migration_id for item in through_cutoff}
        assert adopted_ids.isdisjoint(applied)
        assert applied == [item.migration_id for item in migrations[cutoff:]]
        assert len(_ledger_ids(connection)) == migrate.manifest_count()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM chat_sessions WHERE session_key = 'agent:main:webchat-live'"
            )
            assert cursor.fetchone() == (1,), "019 replayed and deleted a live session"
            cursor.execute("SELECT count(*) FROM chat_messages WHERE session_id = %s", (session_id,))
            assert cursor.fetchone() == (1,)
            # Provenance: nothing through the cutoff was executed by this
            # runner — each row is either reconciled from the legacy table or
            # adopted from the side-ledger/baseline. Everything after it was.
            cursor.execute(
                "SELECT migration_id FROM schema_migrations_v2 "
                "WHERE reconciled_from_legacy OR adopted_from IS NOT NULL"
            )
            not_executed = {row[0] for row in cursor.fetchall()}
        assert not_executed == {item.migration_id for item in through_cutoff}
        assert migrate._legacy_yaml_filenames() >= {
            item.filename for item in through_cutoff
        }


def test_legacy_data_is_preserved_and_buddy_cutover_is_enforced() -> None:
    admin_dsn = os.environ.get("ROBOTHOR_TEST_DB_DSN")
    if not admin_dsn:
        pytest.skip("requires ROBOTHOR_TEST_DB_DSN")
    database = f"genus_upgrade_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(admin_dsn)
    admin.autocommit = True
    params = psycopg2.extensions.parse_dsn(admin_dsn)

    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))

        target_params = {**params, "dbname": database}
        connection = psycopg2.connect(**target_params)
        try:
            migrations = migrate._discover()
            by_id = {item.migration_id: index for index, item in enumerate(migrations)}

            for migration in migrations[: by_id["023_memory_v4_schema"]]:
                _apply_quietly(connection, migration.migration_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE TABLE short_term_memory (id integer PRIMARY KEY, payload text)"
                )
                cursor.execute(
                    "CREATE TABLE long_term_memory (id integer PRIMARY KEY, payload text)"
                )
                cursor.execute("INSERT INTO short_term_memory VALUES (1, 'short-preserved')")
                cursor.execute("INSERT INTO long_term_memory VALUES (2, 'long-preserved')")
            connection.commit()

            _apply_quietly(connection, "023_memory_v4_schema")
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT to_regclass('short_term_memory'), to_regclass('long_term_memory')"
                )
                assert cursor.fetchone() == (None, None)
                cursor.execute("SELECT payload FROM migration_archive_023_short_term_memory")
                assert cursor.fetchone() == ("short-preserved",)
                cursor.execute("SELECT payload FROM migration_archive_023_long_term_memory")
                assert cursor.fetchone() == ("long-preserved",)

            start = by_id["023_memory_v4_schema"] + 1
            stop = by_id["035_drop_legacy_buddy_columns"]
            for migration in migrations[start:stop]:
                _apply_quietly(connection, migration.migration_id)
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_buddy_stats "
                    "(agent_id, stat_date, achievement_score) VALUES ('main', CURRENT_DATE, 80)"
                )
            connection.commit()

            with pytest.raises(psycopg2.Error, match=">=30 achievement-score days"):
                _apply_quietly(connection, "035_drop_legacy_buddy_columns")

            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_buddy_stats (agent_id, stat_date, achievement_score) "
                    "SELECT 'main', CURRENT_DATE - days, 80 "
                    "FROM generate_series(1, 29) AS days"
                )
            connection.commit()
            _apply_quietly(connection, "035_drop_legacy_buddy_columns")

            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM migration_archive_035_buddy_rpg "
                    "WHERE source_table = 'agent_buddy_stats'"
                )
                assert cursor.fetchone() == (30,)
                cursor.execute(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'agent_buddy_stats' AND column_name = 'debugging_score'"
                )
                assert cursor.fetchone() == (0,)
        finally:
            connection.close()
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database,),
            )
            cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database)))
        admin.close()
