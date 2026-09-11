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


def _apply_all_quietly(
    connection, *, adopt_baseline: bool = False, adopt_through: str | None = None
) -> list[str]:
    with redirect_stdout(StringIO()):
        return migrate.apply(
            connection=connection,
            adopt_baseline=adopt_baseline,
            adopt_through=adopt_through,
        )


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


def _adopted_ids(connection) -> set[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT migration_id FROM schema_migrations_v2 WHERE adopted_from IS NOT NULL"
        )
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


_CUTOFF_ID = "040_memory_episodes"


def _stage_glob_migrated_database(connection, cutoff_id: str = _CUTOFF_ID):
    """Leave a database in the state the retired `robothor upgrade` glob would.

    Applies the chain to ``cutoff_id`` for real, then erases the v2 ledger and
    installs 018's legacy backfill, so all that survives is what the glob path
    left behind. Also inserts a `chat_sessions` row matching 019's DELETE
    predicate (plus a message): if anything replays the chain, that row and its
    message disappear by CASCADE, which is the assertion.

    Returns ``(through_cutoff, session_id)``.
    """
    migrations = migrate._discover()
    by_id = {item.migration_id: index for index, item in enumerate(migrations)}
    through_cutoff = migrations[: by_id[cutoff_id] + 1]

    for migration in through_cutoff:
        _apply_quietly(connection, migration.migration_id)
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM schema_migrations_v2")
        cursor.executemany(
            "INSERT INTO schema_migrations (version, filename) VALUES (%s, %s) "
            "ON CONFLICT (version) DO NOTHING",
            _LEGACY_BACKFILL,
        )
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
    return through_cutoff, session_id


def _assert_live_session_survived(connection, session_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM chat_sessions WHERE session_key = 'agent:main:webchat-live'"
        )
        assert cursor.fetchone() == (1,), "019 replayed and deleted a live session"
        cursor.execute("SELECT count(*) FROM chat_messages WHERE session_id = %s", (session_id,))
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

    with _scratch_database() as connection:
        through_cutoff, session_id = _stage_glob_migrated_database(connection)
        cutoff = len(through_cutoff)
        _write_yaml_side_ledger(tmp_path, [item.filename for item in through_cutoff])

        with pytest.raises(migrate.MigrationHistoryError, match="--adopt-baseline"):
            _apply_all_quietly(connection)

        applied = _apply_all_quietly(connection, adopt_baseline=True)

        # Nothing at or before the cutoff re-executed; everything after did.
        adopted_ids = {item.migration_id for item in through_cutoff}
        assert adopted_ids.isdisjoint(applied)
        assert applied == [item.migration_id for item in migrations[cutoff:]]
        assert len(_ledger_ids(connection)) == migrate.manifest_count()

        _assert_live_session_survived(connection, session_id)
        with connection.cursor() as cursor:
            # Provenance: nothing through the cutoff was executed by this
            # runner — each row is either reconciled from the legacy table or
            # adopted from the side-ledger/baseline. Everything after it was.
            cursor.execute(
                "SELECT migration_id FROM schema_migrations_v2 "
                "WHERE reconciled_from_legacy OR adopted_from IS NOT NULL"
            )
            not_executed = {row[0] for row in cursor.fetchall()}
        assert not_executed == {item.migration_id for item in through_cutoff}
        assert migrate._legacy_yaml_filenames() >= {item.filename for item in through_cutoff}


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


def test_glob_migrated_instance_without_a_side_ledger_demands_a_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--adopt-baseline` alone would become the hazard it exists to prevent.

    The legacy table vouches for migrations past the baseline, so the schema is
    further along than 001 — but with no side-ledger nothing says how far.
    Adopting only 001 and then executing 019 onward is exactly the replay this
    guard was written for, so the remedy has to refuse too, until the operator
    says where the schema actually is.
    """
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))  # no side-ledger written
    migrations = migrate._discover()

    with _scratch_database() as connection:
        through_cutoff, session_id = _stage_glob_migrated_database(connection)
        assert not (tmp_path / ".robothor" / "migrations_applied.yaml").exists()

        with pytest.raises(migrate.MigrationHistoryError, match="--adopt-through"):
            _apply_all_quietly(connection, adopt_baseline=True)
        _assert_live_session_survived(connection, session_id)
        assert not _adopted_ids(connection), "a refused adoption must adopt nothing"

        applied = _apply_all_quietly(connection, adopt_through=_CUTOFF_ID)

        assert applied == [item.migration_id for item in migrations[len(through_cutoff) :]]
        assert len(_ledger_ids(connection)) == migrate.manifest_count()
        _assert_live_session_survived(connection, session_id)


def test_a_mid_chain_failure_resumes_without_demanding_the_flag_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption is the operator vouching once, not once per attempt.

    If a migration fails halfway, the ledger still holds only adopted rows. A
    guard keyed on "did this runner write a row" would refuse the resume and
    push the operator to re-pass an adoption flag they already gave — which is
    how an operator learns to reach for the dangerous flag by reflex.
    """
    monkeypatch.setenv("ROBOTHOR_WORKSPACE", str(tmp_path))
    target_id = "041_memory_procedures"
    migrations = migrate._discover()
    target = next(item for item in migrations if item.migration_id == target_id)
    target_sql = target.path.read_text(encoding="utf-8")
    real_strip = migrate._strip_outer_transaction
    should_fail = {"now": True}

    def failing_strip(sql: str) -> str:
        body = real_strip(sql)
        if should_fail["now"] and sql == target_sql:
            return body + "\nSELECT 1 / 0;\n"
        return body

    monkeypatch.setattr(migrate, "_strip_outer_transaction", failing_strip)

    with _scratch_database() as connection:
        through_cutoff, session_id = _stage_glob_migrated_database(connection)
        _write_yaml_side_ledger(tmp_path, [item.filename for item in through_cutoff])

        with pytest.raises(psycopg2.Error):
            _apply_all_quietly(connection, adopt_baseline=True)

        # The adoption committed; the failing file did not.
        ledger = _ledger_ids(connection)
        assert {item.migration_id for item in through_cutoff} <= ledger
        assert target_id not in ledger

        should_fail["now"] = False
        resumed = _apply_all_quietly(connection)  # no adoption flag this time

        assert resumed[0] == target_id
        assert len(_ledger_ids(connection)) == migrate.manifest_count()
        _assert_live_session_survived(connection, session_id)
