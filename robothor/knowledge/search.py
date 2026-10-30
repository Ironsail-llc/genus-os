"""Hybrid search over one knowledge-base collection.

The same pipeline as memory's ``search_facts``, without the memory-only
extras (reranker, entity expansion, recency blend):

1. vector leg: nearest chunks by cosine similarity;
2. keyword leg: chunks matching the query by BM25 (``ts_rank``);
3. reciprocal rank fusion, ``1/(60 + rank)`` summed over the legs
   (:func:`robothor.memory.fusion.rrf_fuse`).

When the embedding service is unreachable the keyword leg carries the search
and every hit is marked ``degraded``, as memory search does.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from robothor.knowledge import store as kb_store
from robothor.memory.fusion import rrf_fuse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["MAX_LIMIT", "search"]

logger = logging.getLogger(__name__)

MAX_LIMIT = 50
DEGRADED = "keyword-only (embedding service unavailable)"


async def search(
    tenant_id: str,
    collection: str,
    query: str,
    limit: int = 5,
    *,
    embed: Callable[[str], Awaitable[list[float]]] | None = None,
    backend: kb_store.KnowledgeBackend | None = None,
) -> list[dict[str, Any]]:
    """``[{"doc_id", "title", "chunk_ix", "text", "score", "source_uri"}]``, best first."""
    kb_store.validate_collection(collection)
    if not query.strip():
        return []
    tenant = kb_store.resolve_tenant(tenant_id)
    limit = max(1, min(int(limit), MAX_LIMIT))
    fetch_limit = max(30, limit * 3)
    store = backend or kb_store.get_backend()
    embedder = embed or kb_store.default_embed

    degraded: str | None = None
    try:
        embedding: list[float] | None = await embedder(query)
    except httpx.HTTPError as exc:
        logger.warning(
            "kb search: embedding fetch failed (%s) — degrading to keyword-only search",
            type(exc).__name__,
        )
        embedding = None
        degraded = DEGRADED

    vector_hits: list[dict[str, Any]] = []
    if embedding is not None:
        vector_hits = await asyncio.to_thread(
            store.vector_search, tenant, collection, embedding, fetch_limit
        )
    keyword_hits = await asyncio.to_thread(
        store.keyword_search, tenant, collection, query, fetch_limit
    )

    fused = rrf_fuse([vector_hits, keyword_hits], key=lambda hit: (hit["doc_id"], hit["chunk_ix"]))
    results: list[dict[str, Any]] = []
    for hit in fused[:limit]:
        row = {
            "doc_id": hit["doc_id"],
            "title": hit["title"],
            "chunk_ix": hit["chunk_ix"],
            "text": hit["text"],
            "score": hit["rrf_score"],
            "source_uri": hit["source_uri"],
        }
        if degraded is not None:
            row["degraded"] = degraded
        results.append(row)
    return results
