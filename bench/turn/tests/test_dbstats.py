"""pg_stat_statements per turn, and a run that survives without it."""

from __future__ import annotations

from typing import Any

from bench.turn.dbstats import StatementCounter


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self._row: tuple[Any, ...] | None = None

    def execute(self, sql: str, *args: Any) -> None:
        self.conn.executed.append(sql)
        if "CREATE EXTENSION" in sql and self.conn.create_error:
            raise RuntimeError(self.conn.create_error)
        if "pg_stat_statements" in sql and "SELECT" in sql:
            if self.conn.select_error:
                raise RuntimeError(self.conn.select_error)
            self._row = (self.conn.calls.pop(0),)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row

    def close(self) -> None:
        pass


class _Conn:
    def __init__(
        self,
        calls: list[int] | None = None,
        create_error: str = "",
        select_error: str = "",
    ) -> None:
        self.calls = list(calls or [])
        self.create_error = create_error
        self.select_error = select_error
        self.executed: list[str] = []
        self.autocommit = False

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        pass


def test_delta_between_two_snapshots() -> None:
    conn = _Conn(calls=[100, 142])
    counter = StatementCounter(connect=lambda: conn)
    assert counter.open()
    before = counter.snapshot()
    after = counter.snapshot()
    assert counter.delta(before, after) == 42
    assert any("CREATE EXTENSION IF NOT EXISTS pg_stat_statements" in s for s in conn.executed)


def test_the_counter_excludes_its_own_queries() -> None:
    conn = _Conn(calls=[1])
    counter = StatementCounter(connect=lambda: conn)
    counter.open()
    counter.snapshot()
    select = next(s for s in conn.executed if "SELECT" in s)
    assert "NOT ILIKE" in select
    assert "current_database()" in select


def test_no_database_url_is_unavailable_with_a_reason() -> None:
    counter = StatementCounter(dsn="")
    assert not counter.open()
    assert counter.reason
    assert counter.snapshot() is None


def test_a_connection_failure_fails_soft() -> None:
    def refuse() -> Any:
        raise OSError("connection refused")

    counter = StatementCounter(connect=refuse)
    assert not counter.open()
    assert "connection refused" in counter.reason
    assert counter.delta(counter.snapshot(), counter.snapshot()) is None


def test_extension_not_preloaded_fails_soft() -> None:
    conn = _Conn(select_error="pg_stat_statements must be loaded via shared_preload_libraries")
    counter = StatementCounter(connect=lambda: conn)
    assert not counter.open()
    assert "shared_preload_libraries" in counter.reason


def test_create_extension_not_permitted_still_reads_an_existing_view() -> None:
    conn = _Conn(calls=[5, 6, 9], create_error="permission denied to create extension")
    counter = StatementCounter(connect=lambda: conn)
    assert counter.open()
    assert counter.delta(counter.snapshot(), counter.snapshot()) == 3
