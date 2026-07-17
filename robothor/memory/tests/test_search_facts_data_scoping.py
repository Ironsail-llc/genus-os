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


def _expansion_queries(mock_cur):
    """The entity-graph expansion SELECT against memory_facts (``%s = ANY
    (entities)``) — a third, best-effort query that runs only when
    ``expand_entities=True`` and the primary candidates carry entities."""
    return [c for c in mock_cur.execute.call_args_list if "ANY(entities)" in c[0][0]]


@patch("robothor.memory.entities.get_entity")
@patch("robothor.memory.facts.get_connection")
@patch("robothor.memory.facts.llm_client")
def test_restricted_scope_adds_predicate_to_entity_expansion_query(
    mock_llm, mock_get_conn, mock_get_entity
):
    """Finding 3 (Task 5 review, CRITICAL): the entity-graph expansion query
    ran WITHOUT the scope predicate the two primary (vector/BM25) candidate
    queries apply — a restricted caller's expand_entities=True search could
    pull in another person's facts through the expansion fan-out, bypassing
    the "own data + shared" rule entirely for that path."""
    from robothor.memory.facts import search_facts

    async def _fake_embed(*a, **kw):
        return [0.1] * 384

    mock_llm.get_embedding_async = _fake_embed

    async def _fake_get_entity(*a, **kw):
        return {"relations": [{"target": "Bob"}]}

    mock_get_entity.side_effect = _fake_get_entity

    mock_cur = _mock_cursor()
    mock_cur.fetchall.return_value = [
        {
            "id": 1,
            "fact_text": "Alice works at Acme",
            "category": "work",
            "entities": ["Alice"],
            "confidence": 0.9,
            "source_type": "note",
            "metadata": {},
            "created_at": None,
            "importance_score": 0.9,
            "access_count": 0,
            "superseded_by": None,
            "person_id": "person-2",
            "age_seconds": 0,
            "similarity": 0.9,
            "bm25_score": 0.1,
        }
    ]
    mock_get_conn.return_value = _mock_conn(mock_cur)

    _run(
        search_facts(
            "Alice",
            tenant_id="tenant-a",
            scope=RESTRICTED,
            expand_entities=True,
            use_reranker=False,
        )
    )

    expansion_calls = _expansion_queries(mock_cur)
    assert len(expansion_calls) >= 1, "entity expansion query never ran — test setup is wrong"
    for call in expansion_calls:
        sql, params = call[0][0], call[0][1]
        assert "person_id = %s OR person_id IS NULL" in sql
        assert "person-1" in params


@patch("robothor.memory.entities.get_entity")
@patch("robothor.memory.facts.get_connection")
@patch("robothor.memory.facts.llm_client")
def test_scope_none_expansion_query_unaffected(mock_llm, mock_get_conn, mock_get_entity):
    from robothor.memory.facts import search_facts

    async def _fake_embed(*a, **kw):
        return [0.1] * 384

    mock_llm.get_embedding_async = _fake_embed

    async def _fake_get_entity(*a, **kw):
        return {"relations": [{"target": "Bob"}]}

    mock_get_entity.side_effect = _fake_get_entity

    mock_cur = _mock_cursor()
    mock_cur.fetchall.return_value = [
        {
            "id": 1,
            "fact_text": "Alice works at Acme",
            "category": "work",
            "entities": ["Alice"],
            "confidence": 0.9,
            "source_type": "note",
            "metadata": {},
            "created_at": None,
            "importance_score": 0.9,
            "access_count": 0,
            "superseded_by": None,
            "person_id": "person-2",
            "age_seconds": 0,
            "similarity": 0.9,
            "bm25_score": 0.1,
        }
    ]
    mock_get_conn.return_value = _mock_conn(mock_cur)

    _run(search_facts("Alice", tenant_id="tenant-a", expand_entities=True, use_reranker=False))

    expansion_calls = _expansion_queries(mock_cur)
    assert len(expansion_calls) >= 1, "entity expansion query never ran — test setup is wrong"
    for call in expansion_calls:
        sql = call[0][0]
        assert "person_id = %s OR person_id IS NULL" not in sql


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
