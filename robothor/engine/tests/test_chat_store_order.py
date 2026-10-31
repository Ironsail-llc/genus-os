"""Restored chat history keeps each exchange in the order it was written.

A user turn and the assistant's reply are written in one transaction, so both
rows carry the same ``created_at`` (``NOW()`` is the transaction's start). A
load that sorts on ``created_at`` alone leaves the pair in whatever order
Postgres returns ties, and after a restart the restored history showed the
reply before the question. ``id`` is a BIGSERIAL, so it is the write order.

The fake cursor below honours exactly the ORDER BY keys the SQL names and
returns ties in write order, as a heap scan usually does. The load fetches
newest first (for LIMIT) and reverses, so a tie that is not broken comes out
reply first.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from robothor.engine import chat_store

SAME_INSTANT = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

MESSAGES = [
    {"id": 1, "created_at": SAME_INSTANT, "message": {"role": "user", "content": "hi"}},
    {"id": 2, "created_at": SAME_INSTANT, "message": {"role": "assistant", "content": "hello"}},
]


class _OrderHonouringCursor:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        if "FROM chat_sessions" in sql:
            self._rows = [
                {
                    "id": 10,
                    "session_key": "agent:main:alice",
                    "model_override": None,
                    "plan_state": None,
                }
            ]
            return
        match = re.search(r"ORDER BY\s+(.+?)\s+LIMIT", sql, re.DOTALL)
        assert match, sql
        keys = [k.split() for k in match.group(1).split(",")]
        # Ties come back in write order (heap order) unless a key breaks them.
        rows = sorted(MESSAGES, key=lambda r: r["id"])
        for column, *direction in reversed(keys):
            rows = sorted(
                rows,
                key=lambda r, c=column: r[c],
                reverse=bool(direction) and direction[0].upper() == "DESC",
            )
        self._rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _Conn:
    def cursor(self, **_kwargs: Any) -> _OrderHonouringCursor:
        return _OrderHonouringCursor()


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def fake_connection(*_args: Any, **_kwargs: Any) -> Any:
        yield _Conn()

    monkeypatch.setattr(chat_store, "get_connection", fake_connection)


@pytest.mark.usefixtures("fake_db")
class TestSameTimestampKeepsWriteOrder:
    def test_load_all_sessions_restores_user_before_assistant(self):
        sessions = chat_store.load_all_sessions(tenant_id="test-tenant")

        roles = [m["role"] for m in sessions["agent:main:alice"]["history"]]
        assert roles == ["user", "assistant"]

    def test_load_session_restores_user_before_assistant(self):
        session = chat_store.load_session("agent:main:alice", tenant_id="test-tenant")

        roles = [m["role"] for m in session["history"]]
        assert roles == ["user", "assistant"]
