"""Tests for optional DataScope filtering in search_facts (Task 5, Unified
Identity Context).

``scope=None`` (the default — every pre-existing caller) must issue the
exact same vector/BM25 SQL as before Task 5. A restricted scope adds the
"own data + shared" predicate (own person_id, or person_id IS NULL) to both
candidate-generating queries.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from robothor.identity.scope import DataScope

RESTRICTED = DataScope(tenant_id="tenant-a", person_id="person-1", restricted=True)


def _run(coro):
    return asyncio.run(coro)


def _mock_cursor():
    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = []
    return mock_cur


def _mock_conn(mock_cur):
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn


def _candidate_queries(mock_cur):
    """The two primary SELECTs against memory_facts (vector + BM25) — skips
    the SET LOCAL hnsw.* session-tuning statement issued first."""
    return [
        c
        for c in mock_cur.execute.call_args_list
        if "FROM memory_facts" in c[0][0] and "ORDER BY" in c[0][0]
    ]


@patch("robothor.memory.facts.get_connection")
@patch("robothor.memory.facts.llm_client")
def test_scope_none_unaffected(mock_llm, mock_get_conn):
    from robothor.memory.facts import search_facts

    async def _fake_embed(*a, **kw):
        return [0.1] * 384

    mock_llm.get_embedding_async = _fake_embed
    mock_cur = _mock_cursor()
    mock_get_conn.return_value = _mock_conn(mock_cur)

    _run(search_facts("Alice", tenant_id="tenant-a", use_reranker=False))

    for call in _candidate_queries(mock_cur):
        sql = call[0][0]
        assert "person_id = %s OR person_id IS NULL" not in sql


@patch("robothor.memory.facts.get_connection")
@patch("robothor.memory.facts.llm_client")
def test_restricted_scope_adds_predicate_to_both_candidate_queries(mock_llm, mock_get_conn):
    from robothor.memory.facts import search_facts

    async def _fake_embed(*a, **kw):
        return [0.1] * 384

    mock_llm.get_embedding_async = _fake_embed
    mock_cur = _mock_cursor()
    mock_get_conn.return_value = _mock_conn(mock_cur)

    _run(search_facts("Alice", tenant_id="tenant-a", scope=RESTRICTED, use_reranker=False))

    matched = _candidate_queries(mock_cur)
    assert len(matched) == 2  # vector search + BM25 search
    for call in matched:
        sql, params = call[0][0], call[0][1]
        assert "person_id = %s OR person_id IS NULL" in sql
        assert "person-1" in params


@patch("robothor.memory.facts.get_connection")
@patch("robothor.memory.facts.llm_client")
def test_unrestricted_scope_unaffected(mock_llm, mock_get_conn):
    from robothor.memory.facts import search_facts

    async def _fake_embed(*a, **kw):
        return [0.1] * 384

    mock_llm.get_embedding_async = _fake_embed
    mock_cur = _mock_cursor()
    mock_get_conn.return_value = _mock_conn(mock_cur)

    unrestricted = DataScope(tenant_id="tenant-a", person_id="person-1", restricted=False)
    _run(search_facts("Alice", tenant_id="tenant-a", scope=unrestricted, use_reranker=False))

    for call in _candidate_queries(mock_cur):
        sql = call[0][0]
        assert "person_id = %s OR person_id IS NULL" not in sql
