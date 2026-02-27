"""Tests for SQLite → PostgreSQL migration."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from robothor.db.connection import get_connection
from robothor.health.migrate_sqlite import migrate_table, TABLES


pytestmark = pytest.mark.integration


@pytest.fixture
def sqlite_db(tmp_path):
    """Create a temp SQLite DB with the garmin schema and test data."""
    db_path = tmp_path / "garmin.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE heart_rate (
            timestamp INTEGER PRIMARY KEY,
            heart_rate INTEGER NOT NULL,
            source TEXT DEFAULT 'monitoring'
        );
        CREATE TABLE stress (
            timestamp INTEGER PRIMARY KEY,
            stress_level INTEGER NOT NULL
        );
        CREATE TABLE body_battery (
            timestamp INTEGER PRIMARY KEY,
            level INTEGER NOT NULL,
            charged INTEGER,
            drained INTEGER
        );
        CREATE TABLE sleep (
            date TEXT PRIMARY KEY,
            start_timestamp INTEGER,
            end_timestamp INTEGER,
            total_sleep_seconds INTEGER,
            deep_sleep_seconds INTEGER,
            light_sleep_seconds INTEGER,
            rem_sleep_seconds INTEGER,
            awake_seconds INTEGER,
            score INTEGER,
            quality TEXT,
            raw_data TEXT
        );
        CREATE TABLE steps (
            date TEXT PRIMARY KEY,
            total_steps INTEGER NOT NULL,
            goal INTEGER,
            distance_meters REAL,
            calories INTEGER,
            timestamp INTEGER
        );
        CREATE TABLE resting_heart_rate (
            date TEXT PRIMARY KEY,
            resting_hr INTEGER NOT NULL,
            timestamp INTEGER
        );
        CREATE TABLE hrv (
            timestamp INTEGER PRIMARY KEY,
            hrv_value REAL,
            reading_type TEXT,
            status TEXT
        );
        CREATE TABLE spo2 (
            timestamp INTEGER PRIMARY KEY,
            spo2_value INTEGER NOT NULL,
            reading_type TEXT
        );
        CREATE TABLE respiration (
            timestamp INTEGER PRIMARY KEY,
            respiration_rate REAL NOT NULL
        );
        CREATE TABLE daily_summary (
            date TEXT PRIMARY KEY,
            calories_total INTEGER,
            calories_active INTEGER,
            calories_bmr INTEGER,
            floors_climbed INTEGER,
            intensity_minutes INTEGER,
            raw_data TEXT
        );
        CREATE TABLE training_status (
            date TEXT PRIMARY KEY,
            training_status TEXT,
            training_status_phrase TEXT,
            vo2max_running REAL,
            vo2max_cycling REAL,
            training_load_7_day INTEGER,
            training_load_28_day INTEGER,
            recovery_time_hours INTEGER,
            raw_data TEXT
        );
        CREATE TABLE activities (
            activity_id INTEGER PRIMARY KEY,
            name TEXT,
            activity_type TEXT,
            start_timestamp INTEGER NOT NULL,
            duration_seconds INTEGER,
            distance_meters REAL,
            calories INTEGER,
            avg_heart_rate INTEGER,
            max_heart_rate INTEGER,
            avg_pace REAL,
            elevation_gain REAL,
            vo2max REAL,
            training_effect_aerobic REAL,
            training_effect_anaerobic REAL,
            training_load INTEGER,
            raw_data TEXT
        );
        CREATE TABLE sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sync_timestamp INTEGER NOT NULL,
            metric_type TEXT NOT NULL,
            records_synced INTEGER,
            status TEXT,
            error_message TEXT
        );
    """)

    # Insert test data
    cur.execute("INSERT INTO heart_rate VALUES (1000001, 72, 'monitoring')")
    cur.execute("INSERT INTO heart_rate VALUES (1000002, 75, 'monitoring')")
    cur.execute("INSERT INTO stress VALUES (2000001, 35)")
    cur.execute("INSERT INTO sleep VALUES ('2026-02-27', 1000, 2000, 29460, 3960, 19080, 6420, 960, 83, 'GOOD', '{}')")
    cur.execute("INSERT INTO steps VALUES ('2026-02-27', 5806, 7300, 4989.0, 2804, 1000)")
    cur.execute("INSERT INTO sync_log VALUES (NULL, 1709000000, 'full_sync', 10, 'success', NULL)")

    conn.commit()
    conn.close()
    return db_path


class TestMigrateTable:
    def test_heart_rate_migration(self, sqlite_db):
        sqlite_conn = sqlite3.connect(str(sqlite_db))
        with get_connection() as pg_conn:
            sqlite_count, pg_count = migrate_table(
                sqlite_conn, pg_conn,
                "heart_rate", "health_heart_rate",
                ["timestamp", "heart_rate", "source"], "ts",
            )
        sqlite_conn.close()
        assert sqlite_count == 2
        assert pg_count == 2

    def test_sleep_migration_with_jsonb(self, sqlite_db):
        sqlite_conn = sqlite3.connect(str(sqlite_db))
        with get_connection() as pg_conn:
            sqlite_count, pg_count = migrate_table(
                sqlite_conn, pg_conn,
                "sleep", "health_sleep",
                ["date", "start_timestamp", "end_timestamp",
                 "total_sleep_seconds", "deep_sleep_seconds",
                 "light_sleep_seconds", "rem_sleep_seconds",
                 "awake_seconds", "score", "quality", "raw_data"],
                "date",
            )
        sqlite_conn.close()
        assert sqlite_count == 1
        assert pg_count == 1

    def test_idempotent(self, sqlite_db):
        """Running migration twice should not duplicate data."""
        sqlite_conn = sqlite3.connect(str(sqlite_db))
        with get_connection() as pg_conn:
            migrate_table(
                sqlite_conn, pg_conn,
                "stress", "health_stress",
                ["timestamp", "stress_level"], "ts",
            )
            _, pg_count_1 = migrate_table(
                sqlite_conn, pg_conn,
                "stress", "health_stress",
                ["timestamp", "stress_level"], "ts",
            )
        sqlite_conn.close()
        assert pg_count_1 == 1  # Still just 1 row

    def test_missing_table(self, sqlite_db):
        """Non-existent SQLite table returns (0, 0)."""
        sqlite_conn = sqlite3.connect(str(sqlite_db))
        with get_connection() as pg_conn:
            sqlite_count, pg_count = migrate_table(
                sqlite_conn, pg_conn,
                "nonexistent_table", "health_heart_rate",
                ["timestamp", "heart_rate", "source"], "ts",
            )
        sqlite_conn.close()
        assert sqlite_count == 0

    def test_sync_log_migration(self, sqlite_db):
        """Serial PK table migrates correctly."""
        sqlite_conn = sqlite3.connect(str(sqlite_db))
        with get_connection() as pg_conn:
            sqlite_count, pg_count = migrate_table(
                sqlite_conn, pg_conn,
                "sync_log", "health_sync_log",
                ["sync_timestamp", "metric_type", "records_synced",
                 "status", "error_message"],
                "serial",
            )
        sqlite_conn.close()
        assert sqlite_count == 1
        assert pg_count >= 1
