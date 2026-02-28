"""
One-time migration: SQLite (~/garmin-sync/garmin.db) → PostgreSQL.

Reads all 13 tables from the SQLite DB and batch-inserts into health_* tables.
Idempotent via ON CONFLICT DO NOTHING (preserves any newer PG data).

Usage:
    python -m robothor.health.migrate_sqlite
    python -m robothor.health.migrate_sqlite --db /path/to/garmin.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import psycopg2.extras

from robothor.db.connection import get_connection

DEFAULT_SQLITE_PATH = Path.home() / "garmin-sync" / "garmin.db"

# Table mappings: (sqlite_table, pg_table, columns, key_type)
# key_type: "ts" = BIGINT PK, "date" = DATE PK, "id" = BIGINT PK, "serial" = auto
TABLES = [
    ("heart_rate", "health_heart_rate", ["timestamp", "heart_rate", "source"], "ts"),
    ("stress", "health_stress", ["timestamp", "stress_level"], "ts"),
    ("body_battery", "health_body_battery", ["timestamp", "level", "charged", "drained"], "ts"),
    ("spo2", "health_spo2", ["timestamp", "spo2_value", "reading_type"], "ts"),
    ("respiration", "health_respiration", ["timestamp", "respiration_rate"], "ts"),
    ("hrv", "health_hrv", ["timestamp", "hrv_value", "reading_type", "status"], "ts"),
    (
        "sleep",
        "health_sleep",
        [
            "date",
            "start_timestamp",
            "end_timestamp",
            "total_sleep_seconds",
            "deep_sleep_seconds",
            "light_sleep_seconds",
            "rem_sleep_seconds",
            "awake_seconds",
            "score",
            "quality",
            "raw_data",
        ],
        "date",
    ),
    (
        "steps",
        "health_steps",
        ["date", "total_steps", "goal", "distance_meters", "calories", "timestamp"],
        "date",
    ),
    (
        "resting_heart_rate",
        "health_resting_heart_rate",
        ["date", "resting_hr", "timestamp"],
        "date",
    ),
    (
        "daily_summary",
        "health_daily_summary",
        [
            "date",
            "calories_total",
            "calories_active",
            "calories_bmr",
            "floors_climbed",
            "intensity_minutes",
            "raw_data",
        ],
        "date",
    ),
    (
        "training_status",
        "health_training_status",
        [
            "date",
            "training_status",
            "training_status_phrase",
            "vo2max_running",
            "vo2max_cycling",
            "training_load_7_day",
            "training_load_28_day",
            "recovery_time_hours",
            "raw_data",
        ],
        "date",
    ),
    (
        "activities",
        "health_activities",
        [
            "activity_id",
            "name",
            "activity_type",
            "start_timestamp",
            "duration_seconds",
            "distance_meters",
            "calories",
            "avg_heart_rate",
            "max_heart_rate",
            "avg_pace",
            "elevation_gain",
            "vo2max",
            "training_effect_aerobic",
            "training_effect_anaerobic",
            "training_load",
            "raw_data",
        ],
        "id",
    ),
    (
        "sync_log",
        "health_sync_log",
        ["sync_timestamp", "metric_type", "records_synced", "status", "error_message"],
        "serial",
    ),
]

# Columns that should be JSONB in PG (were TEXT in SQLite)
JSONB_COLUMNS = {"raw_data"}


def migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    sqlite_table: str,
    pg_table: str,
    columns: list[str],
    key_type: str,
) -> tuple[int, int]:
    """Migrate one table. Returns (sqlite_count, pg_inserted)."""
    sqlite_cur = sqlite_conn.cursor()

    # Check if SQLite table exists
    sqlite_cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (sqlite_table,),
    )
    if not sqlite_cur.fetchone():
        return 0, 0

    # Read all rows from SQLite
    sqlite_cur.execute(f"SELECT {', '.join(columns)} FROM {sqlite_table}")
    rows = sqlite_cur.fetchall()
    if not rows:
        return 0, 0

    sqlite_count = len(rows)

    # Convert raw_data TEXT → valid JSON for JSONB columns
    jsonb_indices = [i for i, c in enumerate(columns) if c in JSONB_COLUMNS]
    if jsonb_indices:
        converted = []
        for row in rows:
            row = list(row)
            for idx in jsonb_indices:
                val = row[idx]
                if val is not None:
                    # Validate it's parseable JSON; wrap as string if not
                    try:
                        json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        row[idx] = json.dumps(str(val))
            converted.append(tuple(row))
        rows = converted

    # Build the INSERT ... ON CONFLICT DO NOTHING statement
    col_list = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))

    if key_type == "serial":
        # For sync_log, skip the id column — let PG auto-generate
        sql = f"INSERT INTO {pg_table} ({col_list}) VALUES %s ON CONFLICT DO NOTHING"
    else:
        sql = f"INSERT INTO {pg_table} ({col_list}) VALUES %s ON CONFLICT DO NOTHING"

    with pg_conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, sql, rows, page_size=500)

    # Count what's in PG now
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {pg_table}")
        pg_count = cur.fetchone()[0]

    return sqlite_count, pg_count


def main():
    parser = argparse.ArgumentParser(description="Migrate Garmin data from SQLite to PostgreSQL")
    parser.add_argument(
        "--db",
        type=str,
        default=str(DEFAULT_SQLITE_PATH),
        help=f"SQLite database path (default: {DEFAULT_SQLITE_PATH})",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: SQLite DB not found at {db_path}")
        return

    print(f"Migrating from {db_path} → PostgreSQL (robothor_memory)")
    print()

    sqlite_conn = sqlite3.connect(str(db_path))

    with get_connection() as pg_conn:
        print(f"{'Table':<30} {'SQLite':>8} {'PG':>8}")
        print("-" * 50)

        total_sqlite = 0
        total_pg = 0

        for sqlite_table, pg_table, columns, key_type in TABLES:
            sqlite_count, pg_count = migrate_table(
                sqlite_conn,
                pg_conn,
                sqlite_table,
                pg_table,
                columns,
                key_type,
            )
            total_sqlite += sqlite_count
            total_pg += pg_count
            print(f"{pg_table:<30} {sqlite_count:>8} {pg_count:>8}")

        print("-" * 50)
        print(f"{'TOTAL':<30} {total_sqlite:>8} {total_pg:>8}")

    sqlite_conn.close()
    print("\nMigration complete.")


if __name__ == "__main__":
    main()
