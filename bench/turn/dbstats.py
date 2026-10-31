"""Statements per turn from ``pg_stat_statements``, failing soft.

The extension only works when Postgres preloads it
(``shared_preload_libraries=pg_stat_statements``); ``CREATE EXTENSION`` may also
need a privilege the bench's role lacks. Either way the benchmark still runs:
``open()`` returns False, ``reason`` says why, and every reading is None.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

#: Counted per database; the bench's own reads of the view are left out.
SNAPSHOT_SQL = (
    "SELECT COALESCE(sum(calls), 0) FROM pg_stat_statements "
    "WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) "
    "AND query NOT ILIKE '%pg_stat_statements%'"
)


class StatementCounter:
    def __init__(self, dsn: str = "", connect: Callable[[], Any] | None = None) -> None:
        self.dsn = dsn
        self._connect = connect
        self._conn: Any = None
        self.reason = ""

    @property
    def available(self) -> bool:
        return self._conn is not None

    def _dial(self) -> Any:
        if self._connect is not None:
            return self._connect()
        import psycopg2

        return psycopg2.connect(self.dsn)

    def open(self) -> bool:
        if self._connect is None and not self.dsn:
            self.reason = "no --db-url given"
            return False
        try:
            conn = self._dial()
            conn.autocommit = True
        except Exception as exc:  # noqa: BLE001 - fail soft, say why
            self.reason = f"cannot connect: {exc}"
            return False
        cur = conn.cursor()
        try:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
            except Exception as exc:  # noqa: BLE001 - may already exist; the read decides
                self.reason = f"CREATE EXTENSION failed: {exc}"
            cur.execute(SNAPSHOT_SQL)
            cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            self.reason = f"pg_stat_statements unavailable: {exc}"
            _close(conn)
            return False
        finally:
            _close(cur)
        self._conn = conn
        return True

    def snapshot(self) -> int | None:
        if self._conn is None:
            return None
        cur = self._conn.cursor()
        try:
            cur.execute(SNAPSHOT_SQL)
            row = cur.fetchone()
            return int(row[0]) if row else None
        except Exception as exc:  # noqa: BLE001
            self.reason = f"pg_stat_statements read failed: {exc}"
            return None
        finally:
            _close(cur)

    @staticmethod
    def delta(before: int | None, after: int | None) -> int | None:
        if before is None or after is None:
            return None
        return after - before

    def close(self) -> None:
        if self._conn is not None:
            _close(self._conn)
            self._conn = None


def _close(thing: Any) -> None:
    with contextlib.suppress(Exception):  # closing is best-effort
        thing.close()


def open_counter(
    db_url: str, log: Callable[[str], None]
) -> tuple[StatementCounter, dict[str, str]]:
    """A counter, opened, and the result ``notes`` saying why it is off if it is."""
    counter = StatementCounter(dsn=db_url)
    if counter.open():
        return counter, {}
    log(f"db_statements: off ({counter.reason})")
    return counter, {"db_statements": counter.reason}
