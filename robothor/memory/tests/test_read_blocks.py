"""``read_blocks`` reads many memory blocks in one statement, without writing.

``read_block`` is an ``UPDATE ... RETURNING`` on its own connection checkout,
so a warmup that loads four blocks pays four write transactions just to read
memory. ``read_blocks`` is the batched read; ``touch_blocks`` is the read
accounting (``read_count`` / ``last_read_at``) as one separate batched write,
so the read path can defer it.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from robothor.memory.blocks import read_blocks, touch_blocks

WRITTEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


class _CountingCursor:
    def __init__(self, log: list[tuple[str, Any]], rows: list[tuple[Any, ...]]) -> None:
        self._log = log
        self._rows = rows
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> _CountingCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._log.append((" ".join(sql.split()), params))
        if sql.lstrip().upper().startswith("SELECT"):
            names = next(p for p in params if isinstance(p, list | tuple))
            self._result = [r for r in self._rows if r[0] in names]
        else:
            self._result = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None


class _FakeConn:
    def __init__(self, log: list[tuple[str, Any]], rows: list[tuple[Any, ...]]) -> None:
        self._log = log
        self._rows = rows

    def cursor(self, *a: Any, **kw: Any) -> _CountingCursor:
        return _CountingCursor(self._log, self._rows)

    def commit(self) -> None:
        return None


def _fake_db(rows: list[tuple[Any, ...]]):
    log: list[tuple[str, Any]] = []

    @contextmanager
    def _get_connection(*a: Any, **kw: Any):
        yield _FakeConn(log, rows)

    return log, patch("robothor.memory.blocks.get_connection", _get_connection)


ROWS = [
    ("persona", "I am the agent.", WRITTEN),
    ("user_profile", "The operator likes tea.", None),
    ("user_model", "", WRITTEN),
    ("working_context", "Shipping the release.", WRITTEN),
]
NAMES = ["persona", "user_profile", "user_model", "working_context"]


def test_four_blocks_are_one_select_statement() -> None:
    log, patcher = _fake_db(ROWS)
    with patcher:
        read_blocks(NAMES, tenant_id="test-tenant")
    assert len(log) == 1
    sql, params = log[0]
    assert sql.upper().startswith("SELECT")
    assert "UPDATE" not in sql.upper()
    assert "pruned_at IS NULL" in sql
    assert "test-tenant" in params


def test_returns_read_block_shape_per_found_name() -> None:
    log, patcher = _fake_db(ROWS[:2])
    with patcher:
        got = read_blocks(["persona", "user_profile", "missing"], tenant_id="test-tenant")
    assert got == {
        "persona": {
            "block_name": "persona",
            "content": "I am the agent.",
            "last_written_at": WRITTEN.isoformat(),
        },
        "user_profile": {
            "block_name": "user_profile",
            "content": "The operator likes tea.",
            "last_written_at": None,
        },
    }


def test_no_names_runs_no_statement() -> None:
    log, patcher = _fake_db(ROWS)
    with patcher:
        assert read_blocks([], tenant_id="test-tenant") == {}
        touch_blocks([], tenant_id="test-tenant")
    assert log == []


def test_touch_is_one_batched_update() -> None:
    log, patcher = _fake_db(ROWS)
    with patcher:
        touch_blocks(NAMES, tenant_id="test-tenant")
    assert len(log) == 1
    sql, params = log[0]
    assert sql.upper().startswith("UPDATE AGENT_MEMORY_BLOCKS")
    assert "read_count = read_count + 1" in sql
    assert "last_read_at = NOW()" in sql
    assert "pruned_at IS NULL" in sql
    assert list(next(p for p in params if isinstance(p, list | tuple))) == NAMES
