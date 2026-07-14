"""Destructive-upgrade regression fixtures against a real PostgreSQL server."""

from __future__ import annotations

import os
import uuid
from contextlib import redirect_stdout
from io import StringIO

import psycopg2
import pytest
from psycopg2 import sql

from robothor.db import migrate

pytestmark = pytest.mark.integration


def _apply_quietly(connection, migration_id: str) -> None:
    with redirect_stdout(StringIO()):
        migrate.apply(version=migration_id, connection=connection)


def test_legacy_data_is_preserved_and_buddy_cutover_is_enforced() -> None:
    admin_dsn = os.environ["ROBOTHOR_TEST_DB_DSN"]
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
