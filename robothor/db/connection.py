"""
Single connection factory with pooling for PostgreSQL.

Replaces 8+ duplicate DB_CONFIG dicts scattered across the codebase.
Uses psycopg2 connection pooling for thread safety.

Usage:
    from robothor.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Generator
from contextlib import contextmanager

import psycopg2
import psycopg2.pool

from robothor.config import get_config

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool(
    minconn: int = 2, maxconn: int = 20
) -> psycopg2.pool.ThreadedConnectionPool:
    """Get or create the connection pool."""
    global _pool
    if _pool is not None and not _pool.closed:
        return _pool

    with _pool_lock:
        if _pool is not None and not _pool.closed:
            return _pool

        cfg = get_config().db
        logger.info(
            "Creating connection pool: %s@%s:%s/%s (min=%d, max=%d)",
            cfg.user, cfg.host, cfg.port, cfg.name, minconn, maxconn,
        )
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=minconn,
            maxconn=maxconn,
            **cfg.dict,
        )
        return _pool


@contextmanager
def get_connection(
    autocommit: bool = False,
) -> Generator[psycopg2.extensions.connection, None, None]:
    """Get a connection from the pool.

    Usage:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        # Connection is returned to pool automatically.
        # On exception, transaction is rolled back.
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        if autocommit:
            conn.autocommit = True
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        if autocommit:
            conn.autocommit = False
        pool.putconn(conn)


def release_connection(conn: psycopg2.extensions.connection) -> None:
    """Return a connection to the pool (for manual management)."""
    pool = get_pool()
    pool.putconn(conn)


def close_pool() -> None:
    """Close all connections in the pool."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
