"""A database checkout that costs one round trip, not four or five.

Today every ``get_connection()`` starts an OS thread just to call
``pool.getconn()``, then runs ``set_config('app.tenant_id', ...)`` (when RLS
is on) and ``SET hnsw.iterative_scan`` on a connection that very often already
carries both, and finally commits. A single-statement read therefore pays
BEGIN + set_config + SET + the query + COMMIT.

Under ``ROBOTHOR_DB_FAST_CHECKOUT`` the session settings are remembered per
connection and re-applied only when they change, a ``read_only`` checkout runs
without a transaction, and no thread is started. RLS must not regress: a
connection reused for a different tenant re-runs ``set_config``.

The fake connection below models what psycopg2 sends to the server: an
implicit BEGIN before the first statement of a transaction, COMMIT/ROLLBACK
only when a transaction is open, session settings that a rollback reverts when
they were made inside the transaction, and the refusal to toggle autocommit
mid-transaction. ``log`` is therefore the round-trip trace.
"""

from __future__ import annotations

import pathlib
import re
import threading

import pytest

from robothor.db import connection

# ---------------------------------------------------------------------------
# A psycopg2-shaped fake: statements, transactions, session settings.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self._row: tuple | None = None

    def execute(self, sql: str, params: tuple | None = None) -> None:
        conn = self.conn
        if conn.closed:
            raise RuntimeError("connection already closed")
        if not conn.autocommit and not conn.in_transaction:
            conn.log.append("BEGIN")
            conn.in_transaction = True
            conn.pending = dict(conn.session)
        conn.log.append(sql)
        lowered = sql.lower()
        if "set_config('app.tenant_id'" in lowered:
            value = params[0] if params else ""
            if lowered.rstrip().endswith("true)"):
                conn.local["app.tenant_id"] = value
            else:
                conn.session["app.tenant_id"] = value
        elif lowered.startswith("set hnsw.iterative_scan"):
            conn.session["hnsw.iterative_scan"] = sql.split("=")[-1].strip()
        elif "current_setting('app.tenant_id'" in lowered:
            self._row = (conn.local.get("app.tenant_id", conn.session.get("app.tenant_id", "")),)
        if conn.autocommit:
            conn.local.clear()

    def fetchone(self) -> tuple | None:
        return self._row

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _Conn:
    def __init__(self, log: list[str]) -> None:
        self.log = log
        self._autocommit = False
        self.in_transaction = False
        self.closed = 0
        self.session: dict[str, str] = {}
        self.pending: dict[str, str] = {}
        self.local: dict[str, str] = {}

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        if self.in_transaction:
            raise RuntimeError("set_session cannot be used inside a transaction")
        self._autocommit = value

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def commit(self) -> None:
        if self.in_transaction:
            self.log.append("COMMIT")
        self.in_transaction = False
        self.local.clear()

    def rollback(self) -> None:
        if self.in_transaction:
            self.log.append("ROLLBACK")
            self.session = dict(self.pending)
        self.in_transaction = False
        self.local.clear()

    def close(self) -> None:
        self.closed = 1


class _Pool:
    """ThreadedConnectionPool's contract: idle list, discard closed ones."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self.idle: list[_Conn] = []
        self.created: list[_Conn] = []

    def getconn(self) -> _Conn:
        if self.idle:
            return self.idle.pop()
        conn = _Conn(self.log)
        self.created.append(conn)
        return conn

    def putconn(self, conn: _Conn) -> None:
        if conn.closed:
            return
        if conn.in_transaction:
            conn.rollback()
        self.idle.append(conn)


@pytest.fixture
def pool(monkeypatch: pytest.MonkeyPatch) -> _Pool:
    fake = _Pool()
    monkeypatch.setattr(connection, "get_pool", lambda: fake)
    # The once-per-process superuser probe is not what these tests measure.
    monkeypatch.setattr(connection, "_warned_superuser", True)
    monkeypatch.setenv("ROBOTHOR_RLS_ENABLED", "true")
    monkeypatch.setenv("ROBOTHOR_TENANT_ID", "tenant-a")
    return fake


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch, pool: _Pool) -> _Pool:
    monkeypatch.setenv("ROBOTHOR_DB_FAST_CHECKOUT", "true")
    return pool


def _select(conn: _Conn, sql: str = "SELECT 1") -> None:
    with conn.cursor() as cur:
        cur.execute(sql)


def _bound_tenant(conn: _Conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.tenant_id', true)")
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _set_configs(log: list[str]) -> list[str]:
    return [s for s in log if "set_config('app.tenant_id'" in s.lower()]


# ---------------------------------------------------------------------------
# Baseline: the flag off keeps today's trace exactly.
# ---------------------------------------------------------------------------


def test_flag_off_a_repeat_read_still_costs_five_round_trips(pool: _Pool) -> None:
    for _ in range(2):
        pool.log.clear()
        with connection.get_connection() as conn:
            _select(conn)
    assert len(pool.created) == 1, "the fake pool must hand back the same connection"
    assert pool.log == [
        "BEGIN",
        "SELECT set_config('app.tenant_id', %s, false)",
        f"SET hnsw.iterative_scan = {connection.ANN_ITERATIVE_SCAN_MODE}",
        "SELECT 1",
        "COMMIT",
    ]


# ---------------------------------------------------------------------------
# Flag on.
# ---------------------------------------------------------------------------


def test_a_second_read_only_checkout_with_the_same_tenant_is_one_statement(fast: _Pool) -> None:
    with connection.get_connection(read_only=True) as conn:
        _select(conn)
    fast.log.clear()

    with connection.get_connection(read_only=True) as conn:
        _select(conn)

    assert len(fast.created) == 1
    assert fast.log == ["SELECT 1"], (
        "a repeat read on a connection already bound to this tenant must send "
        f"only the caller's own statement, got {fast.log}"
    )


def test_a_second_write_checkout_with_the_same_tenant_skips_the_setup(fast: _Pool) -> None:
    with connection.get_connection() as conn:
        _select(conn, "INSERT INTO t VALUES (1)")
    fast.log.clear()

    with connection.get_connection() as conn:
        _select(conn, "INSERT INTO t VALUES (2)")

    assert fast.log == ["BEGIN", "INSERT INTO t VALUES (2)", "COMMIT"]


def test_the_first_checkout_still_applies_both_settings(fast: _Pool) -> None:
    with connection.get_connection(read_only=True) as conn:
        assert _bound_tenant(conn) == "tenant-a"
    conn_obj = fast.created[0]
    assert conn_obj.session["hnsw.iterative_scan"] == connection.ANN_ITERATIVE_SCAN_MODE
    assert _set_configs(fast.log), "the first checkout must bind the tenant"


def test_a_tenant_change_reapplies_set_config_every_time(fast: _Pool) -> None:
    seen: list[str] = []
    for tenant in ("tenant-a", "tenant-b", "tenant-a", "tenant-b"):
        fast.log.clear()
        with connection.tenant_scope(tenant), connection.get_connection(read_only=True) as conn:
            seen.append(_bound_tenant(conn))
        assert len(_set_configs(fast.log)) == 1, (
            f"switching the pooled connection to {tenant} must re-run set_config, got {fast.log}"
        )
    assert len(fast.created) == 1
    assert seen == ["tenant-a", "tenant-b", "tenant-a", "tenant-b"]


def test_a_rollback_does_not_leave_the_connection_on_a_stale_tenant(fast: _Pool) -> None:
    """Settings a rolled-back transaction carried must not be trusted afterwards."""
    with pytest.raises(RuntimeError, match="boom"):
        with connection.tenant_scope("tenant-b"), connection.get_connection() as conn:
            _select(conn, "INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")

    with connection.tenant_scope("tenant-b"), connection.get_connection(read_only=True) as conn:
        assert _bound_tenant(conn) == "tenant-b"
    with connection.get_connection(read_only=True) as conn:
        assert _bound_tenant(conn) == "tenant-a"


def test_a_discarded_connection_is_not_remembered(fast: _Pool) -> None:
    with connection.get_connection(read_only=True) as conn:
        _select(conn)
        conn.close()  # the server went away; the pool will discard it
    fast.log.clear()

    with connection.get_connection(read_only=True) as conn:
        _select(conn)

    assert len(fast.created) == 2
    assert _set_configs(fast.log), "a fresh connection must be bound to the tenant"
    assert any(s.startswith("SET hnsw.iterative_scan") for s in fast.log)


def test_no_thread_is_started_per_checkout(fast: _Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[object] = []

    class _NoThread(threading.Thread):
        def __init__(self, *args: object, **kwargs: object) -> None:
            started.append(kwargs.get("target"))
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(threading, "Thread", _NoThread)
    for _ in range(50):
        with connection.get_connection(read_only=True) as conn:
            _select(conn)
    assert started == [], f"{len(started)} threads started for 50 checkouts"


def test_an_acquisition_error_keeps_its_cause(fast: _Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    cause = RuntimeError("connection pool exhausted")

    def _boom() -> _Conn:
        raise cause

    monkeypatch.setattr(fast, "getconn", _boom)
    with pytest.raises(ConnectionError, match="Failed to acquire") as raised:
        with connection.get_connection():
            pytest.fail("failed acquisition must not yield")
    assert raised.value.__cause__ is cause


def test_a_read_only_checkout_leaves_the_connection_transactional_for_the_next(
    fast: _Pool,
) -> None:
    with connection.get_connection(read_only=True) as conn:
        _select(conn)
    with connection.get_connection() as conn:
        assert conn.autocommit is False


# ---------------------------------------------------------------------------
# Nothing outside the factory may move a pooled connection's session tenant.
# ---------------------------------------------------------------------------

_SESSION_TENANT = re.compile(
    r"set_config\(\s*'app\.tenant_id'\s*,[^)]*,\s*false\s*\)", re.IGNORECASE
)


def test_no_module_outside_the_factory_sets_a_session_level_tenant() -> None:
    """The per-connection memo is only sound if connection.py is the one writer.

    A session-level ``set_config('app.tenant_id', ..., false)`` elsewhere would
    leave a pooled connection on a tenant the memo does not know about, and
    the next borrower would skip the re-bind. Transaction-local (``true``)
    settings revert at commit/rollback and are fine.
    """
    root = pathlib.Path(connection.__file__).resolve().parents[1]
    offenders = [
        str(path.relative_to(root.parent))
        for path in root.rglob("*.py")
        if "tests" not in path.parts
        and path.name != "connection.py"
        and _SESSION_TENANT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"session-level app.tenant_id set outside connection.py: {offenders}"


# ---------------------------------------------------------------------------
# Fakes of the factory must accept its keywords.
# ---------------------------------------------------------------------------

_PATCHES_FACTORY = re.compile(
    r"""setattr\(\s*["']robothor\.db\.connection\.get_connection["']\s*,\s*(\w+)"""
)

_PATCHES_FACTORY_WITH_BARE_LAMBDA = re.compile(
    r"""setattr\(\s*["']robothor\.db\.connection\.get_connection["']\s*,\s*lambda\s*:"""
)


def test_every_fake_of_the_factory_accepts_its_keywords() -> None:
    """A fake ``get_connection()`` with no parameters turns ``read_only=True`` into a TypeError.

    ``check_tool_permission`` catches every exception and denies, so a
    permission test built on such a fake goes vacuous: every deny assertion
    passes without the migration under test being exercised, and every allow
    assertion fails.
    """
    repo = pathlib.Path(connection.__file__).resolve().parents[2]
    offenders: list[str] = []
    for path in [
        *repo.joinpath("robothor").rglob("test_*.py"),
        *repo.joinpath("robothor").rglob("conftest.py"),
        *repo.joinpath("tests").rglob("*.py"),
    ]:
        text = path.read_text(encoding="utf-8")
        where = path.relative_to(repo)
        offenders.extend(
            f"{where}: def {name}()"
            for name in set(_PATCHES_FACTORY.findall(text))
            if re.search(rf"def {name}\(\s*\)", text)
        )
        if _PATCHES_FACTORY_WITH_BARE_LAMBDA.search(text):
            offenders.append(f"{where}: lambda: ...")
    assert offenders == [], f"get_connection fakes that reject read_only: {offenders}"
