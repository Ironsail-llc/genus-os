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
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import psycopg2
import psycopg2.pool

from robothor.config import get_config

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


_POOL_GETCONN_TIMEOUT = 10  # seconds to wait for a connection before raising


class DatabaseGuardError(RuntimeError):
    """Raised when pytest is about to touch a non-test database.

    A distinct type, deliberately: both benchmark writers wrap their INSERT in
    ``except Exception: logger.warning(...)`` so that a reporting hiccup cannot
    fail a passing run. That best-effort handler would swallow a plain
    ``RuntimeError`` and downgrade "this row is landing in production" to a log
    line nobody reads. Those call sites re-raise *this* type specifically.

    Subclasses ``RuntimeError`` so existing ``pytest.raises(RuntimeError)``
    call sites keep working.
    """


def in_pytest() -> bool:
    """Whether this process is running under pytest.

    Broader than the ``PYTEST_CURRENT_TEST`` check in
    :func:`assert_test_database`, which is set only during a test's
    setup/call/teardown — it is absent during collection, during
    session-scoped fixture setup, and in threads that outlive a test. Those
    are exactly the windows a stray module-level write slips through.

    ``PYTEST_VERSION`` (pytest >= 8.1) covers the whole session; the
    ``sys.modules`` probe covers older pytest and any exotic invocation. No
    production entry point imports pytest, so the probe cannot fire live.
    """
    return (
        "PYTEST_CURRENT_TEST" in os.environ
        or "PYTEST_VERSION" in os.environ
        or "pytest" in sys.modules
    )


def connection_database_name(conn: Any) -> str:
    """The database a live connection would actually write to.

    Authoritative in a way the resolved config is not: ``get_pool()`` checks
    the configured name only when it *creates* the pool, so a pool warmed
    before the guard was in force is reused forever pointing wherever it was
    built. Asking the connection itself closes that hole. Falls back to the
    configured name when the object cannot report a DSN — an unknown
    destination must never read as safe.
    """
    try:
        name = conn.get_dsn_parameters().get("dbname")
    except Exception:
        name = None
    if name:
        return str(name)
    return get_config().db.name


def assert_test_database_write(name: str, table: str) -> None:
    """Refuse to write ``table`` into a non-test database from inside pytest.

    Belt-and-braces with :func:`assert_test_database`, applied at the moment of
    the write rather than at pool creation, and using the broader
    :func:`in_pytest` detection.

    This exists because 709 synthetic ``benchmark_results`` rows accumulated in
    the production table between 2026-05-11 and 2026-08-19 — written by the
    benchmark unit tests, whose isolation fixture patched the wrong module. The
    goal metric and the Telegram ``/goals`` command read the *latest* row for an
    agent with no suite filter, so a test row set an agent's displayed score to
    100% against a real 64% for about 15 hours.

    Outside pytest this is a no-op. A non-``*_test`` name can be explicitly
    allowed via ``ROBOTHOR_TEST_DB_ALLOW`` (the release gate legitimately runs
    integration tests against ``robothor_release_gate``).
    """
    if not in_pytest():
        return
    if name.endswith("_test") or name == os.environ.get("ROBOTHOR_TEST_DB_ALLOW", ""):
        return
    raise DatabaseGuardError(
        f"Refusing to INSERT into {table} on database {name!r} from inside pytest — "
        f"{name!r} is not a *_test database, and synthetic rows written there are "
        "indistinguishable from real ones to every reader downstream. Point "
        "ROBOTHOR_DB_NAME at a *_test database, patch "
        "robothor.db.connection.get_connection in your test (patching a re-export "
        "such as robothor.crm.dal.get_connection does NOT intercept it), or set "
        f"ROBOTHOR_TEST_DB_ALLOW={name} to explicitly allow this exact database."
    )


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
    raise DatabaseGuardError(
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


# Per-context tenant override. The process-level ``ROBOTHOR_TENANT_ID`` is the
# right shape for a whole service (the memory eval unit sets it), but the
# benchmark harness needs to run ONE sub-agent against the sandbox tenant
# inside the engine process, while everything else keeps the instance's tenant.
# A ContextVar is per-asyncio-task and survives ``asyncio.to_thread`` (which
# copies the calling context), so a scoped sub-run cannot bleed into a
# concurrent production run.
_tenant_override: ContextVar[str | None] = ContextVar("db_tenant_override", default=None)


def effective_tenant() -> str:
    """The tenant this context's connections bind to. Override, then env."""
    override = _tenant_override.get()
    if override:
        return override
    return os.environ.get("ROBOTHOR_TENANT_ID", "") or os.environ.get("ROBOTHOR_DEFAULT_TENANT", "")


@contextmanager
def tenant_scope(tenant_id: str) -> Generator[None, None, None]:
    """Bind connections taken inside this block to ``tenant_id`` for RLS.

    Only affects connections checked out *inside* the block: ``get_connection``
    applies the scope at checkout. A connection already held stays on its
    original binding.
    """
    token = _tenant_override.set(tenant_id or None)
    try:
        yield
    finally:
        _tenant_override.reset(token)


def current_tenant_scope() -> str | None:
    """The tenant bound by an enclosing :func:`tenant_scope`, or None.

    Callers that create a record inside a scope need this: writing a row under a
    different tenant than the connection is bound to is refused by the RLS
    ``WITH CHECK``, and the refusal surfaces as an opaque InsufficientPrivilege
    at INSERT time rather than at the point the wrong tenant was chosen.
    """
    return _tenant_override.get()


#: pgvector walks the hnsw graph BEFORE applying WHERE predicates, so a
#: filtered vector search spends its LIMIT on candidates that are then thrown
#: away. On 2026-08-27 a tenant holding 220 of 29,551 active facts got 12 rows
#: for a LIMIT of 20 while the largest tenant got all 20 -- recall degraded in
#: proportion to tenant size, and invisibly, because a short result set is
#: indistinguishable from a sparse corpus.
#:
#: `relaxed_order` rather than `strict_order`: this engine re-ranks ANN
#: candidates through RRF and a cross-encoder, so exact index ordering is
#: discarded downstream and paying to preserve it buys nothing.
ANN_ITERATIVE_SCAN_MODE = "relaxed_order"


def _apply_ann_scan_mode(conn: object) -> None:
    """Ask pgvector to keep scanning until a filtered LIMIT is satisfied.

    Best-effort: pgvector < 0.8 has no such GUC, and a vector-search tuning
    knob must never be able to fail a database checkout.
    """
    try:
        with conn.cursor() as cur:  # type: ignore[attr-defined]
            cur.execute(f"SET hnsw.iterative_scan = {ANN_ITERATIVE_SCAN_MODE}")
    except Exception as exc:  # noqa: BLE001 -- tuning must not break the pool
        logger.debug("could not set hnsw.iterative_scan: %s", exc)


def _apply_tenant_scope(conn: psycopg2.extensions.connection) -> None:
    """Bind this connection to the current tenant for RLS.

    Postgres then refuses to serve another tenant's rows *at the database*, so a
    DAL that forgets its WHERE clause — the bridge's crm_dal.py has zero tenant
    predicates — cannot leak across tenants.

    The scope is applied on EVERY checkout, including when there is no tenant to
    bind: ``set_config`` is session-level, so a pooled connection that was last
    used inside a ``tenant_scope`` block would otherwise carry that binding into
    the next borrower and silently empty its result set. Writing an empty string
    restores the policy's permissive branch, which is exactly the pre-scope
    behaviour.

    Inert while the app connects as a SUPERUSER: superusers bypass RLS
    unconditionally. Migration 082 creates the non-superuser ``robothor_app``
    role the engine should connect as. See docs/runbooks/TENANT_RLS.md.
    """
    if not _rls_enabled():
        return
    tenant = effective_tenant()
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
        _apply_ann_scan_mode(conn)
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
