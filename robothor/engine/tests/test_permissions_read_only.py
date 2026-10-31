"""check_tool_permission only reads, and declares it, so a fast checkout skips the transaction."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from robothor.engine.permissions import check_tool_permission


def test_check_tool_permission_declares_a_read_only_checkout(monkeypatch) -> None:
    calls: list[dict] = []

    @contextmanager
    def fake_get_connection(**kwargs):
        calls.append(kwargs)
        conn = MagicMock()
        conn.cursor.return_value.fetchall.return_value = [("*", "allow", "__default__")]
        yield conn

    monkeypatch.setattr("robothor.db.connection.get_connection", fake_get_connection)
    assert check_tool_permission("user", "test-tenant", "search_memory") is None
    assert calls == [{"read_only": True}]
