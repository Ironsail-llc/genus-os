"""list_blocks is a pure read and declares it, so a fast checkout skips the transaction."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from robothor.memory import blocks


def test_list_blocks_declares_a_read_only_checkout(monkeypatch) -> None:
    calls: list[dict] = []

    @contextmanager
    def fake_get_connection(**kwargs):
        calls.append(kwargs)
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.fetchall.return_value = []
        yield conn

    monkeypatch.setattr(blocks, "get_connection", fake_get_connection)
    assert blocks.list_blocks("test-tenant") == {"blocks": []}
    assert calls == [{"read_only": True}]
