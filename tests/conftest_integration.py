"""Integration test fixtures — real PostgreSQL and Redis connections.

These fixtures are only used by tests marked ``@pytest.mark.integration``.
They are skipped in pre-commit (``pytest -m "not integration"``).

Configure via environment variables:
    ROBOTHOR_TEST_DB_DSN     default: dbname=robothor_test user=robothor host=/var/run/postgresql
    ROBOTHOR_TEST_REDIS_URL  default: redis://localhost:6379/15
"""

from __future__ import annotations

import os

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor


@pytest.fixture(scope="session")
def db_dsn() -> str:
    """Database DSN for integration tests."""
    return os.environ.get(
        "ROBOTHOR_TEST_DB_DSN",
        "dbname=robothor_test user=robothor host=/var/run/postgresql",
    )


@pytest.fixture(scope="session")
def redis_url() -> str:
    """Redis URL for integration tests (uses db=15 to avoid collision)."""
    return os.environ.get("ROBOTHOR_TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
def db_conn(db_dsn: str):
    """Provide a PostgreSQL connection that rolls back after each test."""
    conn = psycopg2.connect(db_dsn)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture
def db_cursor(db_conn):
    """Provide a RealDictCursor for easy row access."""
    cur = db_conn.cursor(cursor_factory=RealDictCursor)
    yield cur
    cur.close()


@pytest.fixture
def redis_client(redis_url: str):
    """Provide a Redis client that flushes db=15 after each test."""
    import redis

    r = redis.from_url(redis_url)
    yield r
    r.flushdb()
    r.close()


@pytest.fixture
def mock_get_connection(db_conn, monkeypatch):
    """Monkeypatch get_connection to use the test DB connection.

    This allows testing code that uses ``get_connection()`` internally
    without modifying the production connection pool.
    """
    from contextlib import contextmanager

    @contextmanager
    def _fake_get_connection(autocommit=False):
        if autocommit:
            db_conn.autocommit = True
        try:
            yield db_conn
        finally:
            if autocommit:
                db_conn.autocommit = False

    monkeypatch.setattr("robothor.db.connection.get_connection", _fake_get_connection)
