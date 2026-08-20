"""Memory search degrades to keyword-only when the embedding service is down.

search_facts is hybrid by design (vector + BM25 + RRF), but the embedding
fetch used to be a hard prerequisite: any Ollama outage (GPU wedge, post-boot
model load) killed the whole memory read path with a raw ReadTimeout even
though the BM25 leg needs no embedding. Now an embedding failure degrades the
search to BM25-only, marks the rows as degraded, and search_insights returns
no insights instead of raising.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

BM25_ROW = {
    "id": 1,
    "fact_text": "Alice prefers tea",
    "category": "preference",
    "entities": ["Alice"],
    "confidence": 0.9,
    "source_type": "conversation",
    "metadata": {},
    "created_at": None,
    "importance_score": 0.5,
    "access_count": 0,
    "superseded_by": None,
    "person_id": None,
    "age_seconds": 100.0,
    "bm25_score": 0.4,
}


def _mock_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    return conn


def _embed_down() -> AsyncMock:
    return AsyncMock(side_effect=httpx.ReadTimeout("embed timed out"))


@pytest.mark.asyncio
async def test_search_facts_degrades_to_bm25_when_embedding_down():
    from robothor.memory import facts

    cur = MagicMock()
    cur.fetchall.return_value = [dict(BM25_ROW)]

    with (
        patch.object(facts.llm_client, "get_embedding_async", _embed_down()),
        patch.object(facts, "get_connection", return_value=_mock_conn(cur)),
        patch.object(facts, "apply_hnsw_session"),
    ):
        results = await facts.search_facts(
            "tea",
            limit=5,
            use_reranker=False,
            expand_entities=False,
            include_insights=False,
            include_episodes=False,
        )

    assert results, "BM25 rows must still be returned"
    assert results[0]["fact_text"] == "Alice prefers tea"
    assert results[0]["degraded"] == "keyword-only (embedding service unavailable)"
    # Only the BM25 query ran — no vector query without an embedding.
    executed_sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list)
    assert "<=>" not in executed_sql


@pytest.mark.asyncio
async def test_search_facts_healthy_path_has_no_degraded_marker():
    from robothor.memory import facts

    cur = MagicMock()
    cur.fetchall.return_value = [dict(BM25_ROW)]

    with (
        patch.object(facts.llm_client, "get_embedding_async", AsyncMock(return_value=[0.1] * 8)),
        patch.object(facts, "get_connection", return_value=_mock_conn(cur)),
        patch.object(facts, "apply_hnsw_session"),
    ):
        results = await facts.search_facts(
            "tea",
            limit=5,
            use_reranker=False,
            expand_entities=False,
            include_insights=False,
            include_episodes=False,
        )

    assert results
    assert "degraded" not in results[0]


@pytest.mark.asyncio
async def test_search_facts_connect_error_also_degrades():
    from robothor.memory import facts

    cur = MagicMock()
    cur.fetchall.return_value = [dict(BM25_ROW)]

    with (
        patch.object(
            facts.llm_client,
            "get_embedding_async",
            AsyncMock(side_effect=httpx.ConnectError("down")),
        ),
        patch.object(facts, "get_connection", return_value=_mock_conn(cur)),
        patch.object(facts, "apply_hnsw_session"),
    ):
        results = await facts.search_facts(
            "tea",
            limit=5,
            use_reranker=False,
            expand_entities=False,
            include_insights=False,
            include_episodes=False,
        )

    assert results[0]["degraded"] == "keyword-only (embedding service unavailable)"


@pytest.mark.asyncio
async def test_search_insights_returns_empty_when_embedding_down():
    from robothor.memory import facts

    with patch.object(facts.llm_client, "get_embedding_async", _embed_down()):
        results = await facts.search_insights("patterns", limit=3)

    assert results == []


@pytest.mark.asyncio
async def test_store_fact_still_raises_for_dispatch_to_map():
    """store_fact keeps the failure (no NULL-embedding write path yet); the
    dispatch layer maps the httpx error to a short structured tool error."""
    from robothor.memory import facts

    with (
        patch.object(facts.llm_client, "get_embedding_async", _embed_down()),
        pytest.raises(httpx.ReadTimeout),
    ):
        await facts.store_fact(
            {"fact_text": "Alice prefers tea", "category": "preference"},
            "source",
            "conversation",
        )
