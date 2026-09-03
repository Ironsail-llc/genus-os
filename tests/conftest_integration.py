"""Integration test fixtures — real PostgreSQL and Redis connections.

These fixtures are only used by tests marked ``@pytest.mark.integration``.

The marker does NOT de-select those tests by itself: run_tests.sh's default
expression is ``not slow and not llm and not e2e``, and neither it nor the
pre-commit hook excludes ``integration``. Pass ``-m "not integration"``
explicitly if that is what you want. A test that does something destructive to
a live server therefore needs its own opt-in gate as well — see
``ROBOTHOR_TEST_ADMIN_DSN`` in tests/test_restore_drill.py, added after every
default run on the operator's box did ``createdb``/``dropdb`` on the
operator's own cluster.

Configure via environment variables:
    ROBOTHOR_TEST_DB_DSN     default: dbname=robothor_test user=robothor host=/var/run/postgresql
    ROBOTHOR_TEST_REDIS_URL  default: redis://localhost:6379/15
"""

from __future__ import annotations

import contextlib
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


# Session-scoped holder: the "current test" wrapper. Installed once at
# test session start, replaced per test. All module-level import sites
# get this SAME proxy function so closure-vs-rebinding issues vanish.
#
# When no test has set the holder (non-integration tests that don't use the
# ``mock_get_connection`` fixture), the proxy delegates to the real
# ``get_connection`` so other unit-test mocking patterns keep working.
_current_holder: dict[str, object] = {"wrapper": None, "conn": None, "real": None}


def _session_fake_get_connection(autocommit: bool = False):
    """Shared proxy installed at session scope; dispatches to per-test holder,
    or falls through to the real function when no fixture is active."""
    from contextlib import contextmanager

    if _current_holder["conn"] is None or _current_holder["wrapper"] is None:
        real = _current_holder["real"]
        if real is None:
            raise RuntimeError("session proxy has no real get_connection to delegate to")
        return real(autocommit=autocommit)

    @contextmanager
    def _cm():
        conn = _current_holder["conn"]
        wrapper = _current_holder["wrapper"]
        if autocommit:
            conn.autocommit = True
        try:
            yield wrapper
        finally:
            if autocommit:
                conn.autocommit = False

    return _cm()


@pytest.fixture(scope="session", autouse=True)
def _install_session_patch():
    """Install the proxy once per session on robothor.db.connection + every
    module that imported get_connection at module load. After this, the
    per-test mock_get_connection fixture only needs to swap the wrapper/conn
    inside ``_current_holder`` — no re-patching across test boundaries."""
    import sys

    # Pin DEFAULT_TENANT to 'default' for the test session. Leaving it to
    # whatever ROBOTHOR_DEFAULT_TENANT env var the dev has set in their shell
    # causes test data (tenant_id='default') to be invisible to DAL queries
    # that default to the env value.
    from robothor import constants as _constants
    from robothor.db import connection as _conn_mod

    _saved_tenant = _constants.DEFAULT_TENANT
    _constants.DEFAULT_TENANT = "default"

    real = _conn_mod.get_connection
    _current_holder["real"] = real
    _conn_mod.get_connection = _session_fake_get_connection
    patched = [("robothor.db.connection", real)]

    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("robothor.") or name == "robothor.db.connection":
            continue
        local = getattr(module, "get_connection", None)
        if local is real:
            module.get_connection = _session_fake_get_connection
            patched.append((name, real))

    yield

    # Session teardown — restore every patched site.
    for name, orig in patched:
        # A module that vanished from sys.modules mid-session has nothing left
        # to restore; suppressed rather than passed so the intent is explicit.
        with contextlib.suppress(Exception):
            module = sys.modules.get(name)
            if module is not None:
                module.get_connection = orig
    _constants.DEFAULT_TENANT = _saved_tenant


@pytest.fixture
def mock_get_connection(db_conn):
    """Point the session-installed proxy at this test's db_conn. No-op
    ``commit()`` so the per-test rollback keeps isolation. Late-import
    sites also get rebound defensively so a module imported mid-session
    still sees the proxy."""
    import sys

    class _NoCommitWrapper:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def commit(self):
            return None

    wrapper = _NoCommitWrapper(db_conn)
    _current_holder["conn"] = db_conn
    _current_holder["wrapper"] = wrapper

    # Defensive: any module imported mid-session after session-install won't
    # have been re-pointed. Sweep for real-function stragglers and rebind.
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith("robothor."):
            continue
        local = getattr(module, "get_connection", None)
        if local is None:
            continue
        # If a module still has a function from robothor.db.connection (could
        # be the original real one if it was imported AFTER the session fixture
        # ran), swap it for the proxy.
        mod = getattr(local, "__module__", "")
        if mod == "robothor.db.connection":
            module.get_connection = _session_fake_get_connection

    yield

    _current_holder["conn"] = None
    _current_holder["wrapper"] = None


@pytest.fixture
def scratch_db():
    """Factory for a throwaway database migrated to a chosen point in the chain.

    Migration tests need the schema at a *specific point* — 088 asserts on the
    wide-open member row that 071 seeds and 088 then tightens. Run against the
    shared test database, which CI now builds fully migrated, that precondition
    no longer exists, so those tests were skipped and stopped protecting
    anything.

    Applies the canonical manifest in order, stopping after the named
    migration, so prerequisites come along automatically instead of each test
    hand-listing them.

        db, dsn = scratch_db(through="087_role_permission_guardrails")
        scratch_db(through="088_member_role_read_only")   # advance the same DB
    """
    import getpass
    import uuid

    from robothor.db.migrate import _discover, _strip_outer_transaction

    admin = os.environ.get("ROBOTHOR_TEST_ADMIN_DSN", f"dbname=postgres user={getpass.getuser()}")
    name = f"scratch_{uuid.uuid4().hex[:8]}"

    root = psycopg2.connect(admin)
    root.autocommit = True
    with root.cursor() as cur:
        cur.execute(f"CREATE DATABASE {name}")
    root.close()

    # Derive the scratch DSN from the admin DSN (swap dbname) so TCP
    # credentials from CI carry over; the old f-string assumed peer auth.
    parts = dict(psycopg2.extensions.parse_dsn(admin))
    parts["dbname"] = name
    dsn = " ".join(f"{k}={v}" for k, v in parts.items())
    db = psycopg2.connect(dsn)
    db.autocommit = True
    with db.cursor() as cur:
        for ext in ("vector", "pgcrypto", '"uuid-ossp"'):
            # not every scratch test needs every extension
            with contextlib.suppress(Exception):
                cur.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")

    applied: set[str] = set()

    def _apply(through: str):
        with db.cursor() as cur:
            for m in _discover():
                if m.migration_id in applied:
                    continue
                cur.execute(_strip_outer_transaction(m.path.read_text()))
                applied.add(m.migration_id)
                if m.migration_id == through:
                    break
        return db, dsn

    yield _apply

    db.close()
    root = psycopg2.connect(admin)
    root.autocommit = True
    with root.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    root.close()
