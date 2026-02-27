"""
Test fixtures for health package.

Uses a dedicated test database (robothor_test) to avoid touching production data.
Tables are created once per session and truncated between tests.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Point all DB connections to the test database BEFORE importing anything
os.environ["ROBOTHOR_DB_NAME"] = "robothor_test"
os.environ.setdefault("ROBOTHOR_DB_USER", os.environ.get("USER", "philip"))

from robothor.config import reset_config
reset_config()  # Force config reload with test DB name

from robothor.db.connection import get_connection, close_pool


MIGRATION_SQL = Path(__file__).parent.parent.parent.parent / "crm" / "migrations" / "010_health_tables.sql"

HEALTH_TABLES = [
    "health_heart_rate", "health_stress", "health_body_battery",
    "health_spo2", "health_respiration", "health_hrv",
    "health_sleep", "health_steps", "health_resting_heart_rate",
    "health_daily_summary", "health_training_status",
    "health_activities", "health_sync_log",
]


@pytest.fixture(scope="session", autouse=True)
def create_health_tables():
    """Create health tables once per test session."""
    sql = MIGRATION_SQL.read_text()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    yield
    close_pool()


@pytest.fixture(autouse=True)
def clean_health_tables():
    """Truncate all health tables after each test."""
    yield
    with get_connection() as conn:
        with conn.cursor() as cur:
            for table in HEALTH_TABLES:
                cur.execute(f"TRUNCATE {table} CASCADE")
