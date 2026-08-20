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


def assert_test_database(name: str) -> None:
    """Refuse to touch a non-test database from inside pytest.

    A plain local ``pytest`` run used to resolve the production database
    (``robothor_memory``) and every fire-and-forget persistence path — chat
    exchange saves, tool-call audit logs, model-breaker escalations — landed in
    prod. The root conftest now pins ``ROBOTHOR_DB_NAME=robothor_test``; this
    guard is the backstop that turns any remaining misconfiguration into a
    crisp hard failure instead of silent production writes.

    Outside pytest this is a no-op. A non-``*_test`` name can be explicitly
    allowed via ``ROBOTHOR_TEST_DB_ALLOW`` (the release gate legitimately runs
    integration tests against ``robothor_release_gate``).
    """
    if "PYTEST_CURRENT_TEST" not in os.environ:
        return
    if name.endswith("_test") or name == os.environ.get("ROBOTHOR_TEST_DB_ALLOW", ""):
        return
    raise RuntimeError(
        f"Refusing to open a database connection to {name!r} from inside pytest — "
        "tests must never touch a production database. Set ROBOTHOR_DB_NAME to a "
        "*_test database, or set ROBOTHOR_TEST_DB_ALLOW to this exact name to "
        "explicitly allow it."
    )


def get_pool(minconn: int = 2, maxconn: int = 30) -> psycopg2.pool.ThreadedConnectionPool:
    """Get or create the connection pool."""
    global _pool
    if _pool is not None and not _pool.closed:
        return _pool

    with _pool_lock:
        if _pool is not None and not _pool.closed:
            return _pool

        cfg = get_config().db
        assert_test_database(cfg.name)
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
                connect_timeout=int(os.environ.get("ROBOTHOR_DB_CONNECT_TIMEOUT", "5")),
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


# One-shot latch: warn once per process, not on every pooled connection.
_warned_superuser = False


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

            # RLS enabled + a SUPERUSER connection is not "RLS on". It is RLS OFF
            # with a flag that says on — Postgres ignores the policy entirely for
            # superusers — which is strictly worse than off, because the operator
            # believes they have isolation.
            #
            # The default is the trap: config.py resolves the DB user from
            # ROBOTHOR_DB_USER, falling back to $USER — whoever runs the process,
            # which on a single-box instance is usually an admin. That is exactly
            # how robothor-orchestrator and robothor-vision bypassed RLS for their
            # entire existence while the instance reported it "enabled".
            #
            # Once per process, and never fatal: this must not be able to take the
            # instance down, only to stop it lying about its isolation.
            global _warned_superuser
            if not _warned_superuser:
                _warned_superuser = True
                cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                row = cur.fetchone()
                if row and row[0]:
                    logger.error(
                        "RLS IS INERT: ROBOTHOR_RLS_ENABLED is set but this connection "
                        "is a SUPERUSER, which bypasses row-level security "
                        "unconditionally. There is NO tenant isolation on this "
                        "connection. Set ROBOTHOR_DB_USER=robothor_app (migration 082) "
                        "— see docs/runbooks/TENANT_RLS.md."
                    )
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
    acquisition_error: Exception | None = None
    acquired = threading.Event()
    state_lock = threading.Lock()
    cancelled = False

    def _get() -> None:
        nonlocal conn, acquisition_error
        try:
            candidate = pool.getconn()
        except Exception as exc:
            with state_lock:
                acquisition_error = exc
            acquired.set()
            return

        # A timed-out caller cannot consume the eventual result. Coordinate
        # cancellation under a lock so a connection obtained at the timeout
        # boundary is always returned to the pool instead of being orphaned.
        return_late_connection = False
        with state_lock:
            if cancelled:
                return_late_connection = True
            else:
                conn = candidate
            acquired.set()
        if return_late_connection:
            pool.putconn(candidate)

    t = threading.Thread(target=_get, daemon=True)
    t.start()
    if not acquired.wait(timeout=_POOL_GETCONN_TIMEOUT):
        late_connection = None
        with state_lock:
            cancelled = True
            # Cover the boundary race where the worker published a connection
            # immediately after Event.wait returned false.
            if conn is not None:
                late_connection = conn
                conn = None
        if late_connection is not None:
            pool.putconn(late_connection)
        raise ConnectionError(
            f"Could not acquire DB connection within {_POOL_GETCONN_TIMEOUT}s — pool exhausted"
        )
    if conn is None:
        if acquisition_error is not None:
            raise ConnectionError(
                "Failed to acquire DB connection from pool"
            ) from acquisition_error
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
