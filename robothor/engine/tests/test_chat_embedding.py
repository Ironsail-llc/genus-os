"""Tests for verbatim chat embedding (chat_store additions)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from robothor.engine.chat_store import (
    _embed_turns,
    backfill_chat_embeddings,
    search_chat_turns,
)


class _RecordingCursor:
    """Fake cursor: returns canned SELECT rows, records every execute."""

    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, *a, **kw):
        return self._cursor

    def commit(self):
        pass


class TestBackfillChatEmbeddings:
    @pytest.mark.asyncio
    async def test_backfills_unembedded_rows(self, monkeypatch):
        from robothor.llm import ollama as llm_client

        rows = [
            {"id": 11, "message": {"role": "user", "content": "hello"}},
            {"id": 12, "message": {"role": "assistant", "content": "hi there"}},
        ]
        cursor = _RecordingCursor(rows)

        seen_texts = []

        async def _fake_embed(texts):
            seen_texts.extend(texts)
            return [[0.1] * 1024 for _ in texts]

        monkeypatch.setattr(llm_client, "get_embeddings_batch_async", _fake_embed)

        with patch("robothor.engine.chat_store.get_connection", return_value=_FakeConn(cursor)):
            count = await backfill_chat_embeddings(limit=100)

        assert count == 2
        assert seen_texts == ["hello", "hi there"]
        updates = [(sql, params) for sql, params in cursor.executed if "UPDATE" in sql]
        assert len(updates) == 2
        assert updates[0][1][1] == 11
        assert updates[1][1][1] == 12
        select_sql = cursor.executed[0][0]
        assert "embedded_at IS NULL" in select_sql
        assert "ORDER BY created_at DESC" in select_sql

    @pytest.mark.asyncio
    async def test_skips_gracefully_when_embedder_down(self, monkeypatch):
        from robothor.llm import ollama as llm_client

        rows = [{"id": 11, "message": {"role": "user", "content": "hello"}}]
        cursor = _RecordingCursor(rows)

        async def _boom(texts):
            raise RuntimeError("ollama down")

        monkeypatch.setattr(llm_client, "get_embeddings_batch_async", _boom)

        with patch("robothor.engine.chat_store.get_connection", return_value=_FakeConn(cursor)):
            count = await backfill_chat_embeddings()

        assert count == 0
        # No UPDATE was attempted — rows stay NULL and the next sweep retries.
        assert not [sql for sql, _ in cursor.executed if "UPDATE" in sql]

    @pytest.mark.asyncio
    async def test_noop_when_nothing_unembedded(self, monkeypatch):
        from robothor.llm import ollama as llm_client

        called = False

        async def _fake_embed(texts):
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(llm_client, "get_embeddings_batch_async", _fake_embed)
        cursor = _RecordingCursor([])

        with patch("robothor.engine.chat_store.get_connection", return_value=_FakeConn(cursor)):
            count = await backfill_chat_embeddings()

        assert count == 0
        assert called is False

    @pytest.mark.asyncio
    async def test_returns_zero_when_db_unreachable(self, monkeypatch):
        with patch(
            "robothor.engine.chat_store.get_connection",
            side_effect=RuntimeError("db down"),
        ):
            count = await backfill_chat_embeddings()
        assert count == 0


class TestEmbedTurns:
    @pytest.mark.asyncio
    async def test_skips_when_no_message_ids(self):
        # Should not raise or attempt any work
        await _embed_turns([], [])

    @pytest.mark.asyncio
    async def test_handles_embedding_failure(self, monkeypatch):
        from robothor.llm import ollama as llm_client

        async def _boom(texts):
            raise RuntimeError("ollama down")

        monkeypatch.setattr(llm_client, "get_embeddings_batch_async", _boom)
        # Should swallow and return without raising
        await _embed_turns([1, 2], ["hello", "world"])

    @pytest.mark.asyncio
    async def test_persists_successful_embeddings(self, monkeypatch):
        from robothor.llm import ollama as llm_client

        async def _fake_embed(texts):
            return [[0.1] * 1024 for _ in texts]

        monkeypatch.setattr(llm_client, "get_embeddings_batch_async", _fake_embed)

        captured = []

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params):
                captured.append((sql, params))

            def cursor(self, *a, **kw):
                return self

        class FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def cursor(self, *a, **kw):
                return FakeCursor()

            def commit(self):
                pass

        with patch("robothor.engine.chat_store.get_connection", return_value=FakeConn()):
            await _embed_turns([101, 102], ["user says", "assistant says"])

        # Expect 2 UPDATEs (one per message id)
        assert len(captured) == 2
        assert captured[0][1][1] == 101
        assert captured[1][1][1] == 102


class TestSearchChatTurnsDAL:
    """Unit test for result shaping — no DB required."""

    def test_shapes_results(self):
        fake_rows = [
            {
                "id": 1,
                "message": {"role": "user", "content": "Hello?"},
                "created_at": None,
                "session_key": "s1",
                "similarity": 0.7,
            }
        ]

        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def execute(self, *a, **k):
                pass

            def fetchall(self):
                return self._rows

        class FakeConn:
            def __init__(self, rows):
                self._rows = rows

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def cursor(self, *a, **k):
                return FakeCursor(self._rows)

        with patch(
            "robothor.engine.chat_store.get_connection",
            return_value=FakeConn(fake_rows),
        ):
            results = search_chat_turns([0.0] * 1024, limit=5, tenant_id="test")

        assert len(results) == 1
        r = results[0]
        assert r["role"] == "user"
        assert r["content"] == "Hello?"
        assert r["source"] == "chat_turn"
        assert r["session_key"] == "s1"
