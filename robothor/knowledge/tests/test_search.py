"""search(): vector + keyword legs fused by RRF, scoped to one collection."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from robothor.knowledge import search as kb_search
from robothor.knowledge import store
from robothor.knowledge.store import MemoryKnowledgeBackend

DOCS = {
    "refunds": ("Refunds", "Refunds are issued within 14 days of purchase."),
    "shipping": ("Shipping", "Orders ship in two business days by ground courier."),
    "warranty": ("Warranty", "The warranty covers manufacturing defects for one year."),
}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def backend(embed) -> MemoryKnowledgeBackend:
    b = MemoryKnowledgeBackend()
    for doc_id, (title, text) in DOCS.items():
        _run(
            store.upsert_document(
                "test-tenant",
                "helpdesk/policies",
                doc_id,
                title,
                text,
                source_uri=f"https://example.com/{doc_id}",
                embed=embed,
                backend=b,
            )
        )
    return b


def _search(
    backend, embed, query, *, tenant="test-tenant", collection="helpdesk/policies", limit=5
):
    return _run(kb_search.search(tenant, collection, query, limit, embed=embed, backend=backend))


def test_the_matching_document_ranks_first(backend, embed):
    results = _search(backend, embed, "when are refunds issued")
    assert results[0]["doc_id"] == "refunds"


def test_result_shape(backend, embed):
    hit = _search(backend, embed, "warranty defects")[0]
    assert set(hit) >= {"doc_id", "title", "chunk_ix", "text", "score", "source_uri"}
    assert hit["title"] == "Warranty"
    assert hit["chunk_ix"] == 0
    assert hit["source_uri"] == "https://example.com/warranty"
    assert hit["score"] > 0


def test_scores_are_rrf_and_descending(backend, embed):
    results = _search(backend, embed, "refunds")
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)
    # A chunk found by both legs at rank 0 scores 2/(60+0).
    assert results[0]["score"] == pytest.approx(2 / 60, rel=1e-4)


def test_limit_is_respected(backend, embed):
    assert len(_search(backend, embed, "orders refunds warranty", limit=2)) == 2


def test_other_collections_are_not_searched(backend, embed):
    _run(
        store.upsert_document(
            "test-tenant",
            "sales/playbooks",
            "pitch",
            "Pitch",
            "Refunds are a selling point.",
            embed=embed,
            backend=backend,
        )
    )
    assert {r["doc_id"] for r in _search(backend, embed, "refunds")} <= set(DOCS)
    only = _search(backend, embed, "refunds", collection="sales/playbooks")
    assert [r["doc_id"] for r in only] == ["pitch"]


def test_other_tenants_are_not_searched(backend, embed):
    assert _search(backend, embed, "refunds", tenant="tenant-b") == []


def test_blank_query_returns_nothing(backend, embed):
    assert _search(backend, embed, "  ") == []


def test_embedding_outage_degrades_to_keyword_only(backend):
    async def down(_text):
        raise httpx.ConnectError("embedding service down")

    results = _run(
        kb_search.search("test-tenant", "helpdesk/policies", "refunds", embed=down, backend=backend)
    )
    assert [r["doc_id"] for r in results] == ["refunds"]
    assert all(r.get("degraded") for r in results)
