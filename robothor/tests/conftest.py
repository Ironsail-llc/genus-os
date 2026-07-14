"""Shared fixtures for robothor/tests/."""

from __future__ import annotations

import os

import psycopg2
import pytest


@pytest.fixture
def pg_scratch():
    """A throwaway connection for migration tests. Rolls back on teardown.

    Uses the same connection params as the app. Isolation comes from never
    committing: the migration's DDL/DML runs inside one uncommitted transaction,
    statements on the same connection see each other's writes without a commit,
    and teardown rolls the whole transaction back so nothing touches the real
    `public` schema. Tests using this fixture must never call `.commit()` — a
    commit here would permanently write test tables into the shared database.
    """
    dsn = os.environ.get("ROBOTHOR_TEST_DSN", "dbname=robothor_memory")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
