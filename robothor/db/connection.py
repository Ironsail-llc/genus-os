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
import os
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg2
import psycopg2.pool

from robothor.config import get_config

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


_POOL_GETCONN_TIMEOUT = 10  # seconds to wait for a connection before raising


def get_pool(minconn: int = 2, maxconn: int = 30) -> psycopg2.pool.ThreadedConnectionPool:
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
            cfg.user,
            cfg.host,
            cfg.port,
            cfg.name,
            minconn,
            maxconn,
        )
        try:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=minconn,
                maxconn=maxconn,
                **cfg.dict,
            )
        except psycopg2.OperationalError as e:
            raise ConnectionError(
                f"Cannot connect to PostgreSQL at {cfg.host}:{cfg.port}/{cfg.name}: {e}\n"
                f"Check ROBOTHOR_DB_* environment variables and ensure PostgreSQL is running."
            ) from e
        return _pool


def _rls_enabled() -> bool:
    """Whether connections should scope themselves to a tenant via RLS.

    Off by default: migration 081 leaves the policy permissive when
    ``app.tenant_id`` is unset, so nothing changes until this is turned on.
    """
    return os.environ.get("ROBOTHOR_RLS_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _apply_tenant_scope(conn: psycopg2.extensions.connection) -> None:
    """Bind this connection to the instance's tenant for RLS.

    Postgres then refuses to serve another tenant's rows *at the database*, so a
    DAL that forgets its WHERE clause — the bridge's crm_dal.py has zero tenant
    predicates — cannot leak across tenants.

    Inert while the app connects as a SUPERUSER: superusers bypass RLS
    unconditionally. Migration 082 creates the non-superuser ``robothor_app``
    role the engine should connect as. See docs/runbooks/TENANT_RLS.md.
    """
    if not _rls_enabled():
        return
    tenant = os.environ.get("ROBOTHOR_TENANT_ID", "") or os.environ.get(
        "ROBOTHOR_DEFAULT_TENANT", ""
    )
    if not tenant:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))
    except Exception as exc:
        # Fail loudly: a connection that silently forgets its tenant scope has
        # no isolation at all.
        logger.error("could not bind connection to tenant %s for RLS: %s", tenant, exc)
        raise


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
    # Use a threading timeout to avoid blocking indefinitely when pool is exhausted
    conn = None
    acquired = threading.Event()

    def _get() -> None:
        nonlocal conn
        try:
            conn = pool.getconn()
            acquired.set()
        except Exception:
            acquired.set()

    t = threading.Thread(target=_get, daemon=True)
    t.start()
    if not acquired.wait(timeout=_POOL_GETCONN_TIMEOUT):
        raise ConnectionError(
            f"Could not acquire DB connection within {_POOL_GETCONN_TIMEOUT}s — pool exhausted"
        )
    if conn is None:
        raise ConnectionError("Failed to acquire DB connection from pool")
    try:
        if autocommit:
            conn.autocommit = True
        _apply_tenant_scope(conn)
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
