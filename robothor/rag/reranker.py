"""
Cross-encoder reranker using Qwen3-Reranker via Ollama.

The reranker outputs binary "yes"/"no" relevance judgments, not numeric
scores. Results marked "yes" are kept (sorted by original cosine similarity),
then backfilled from "no" results if fewer than top_k pass.

Usage:
    from robothor.rag.reranker import rerank_with_fallback

    results = await rerank_with_fallback(query, search_results, top_k=10)
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

RERANKER_MODEL = os.environ.get("ROBOTHOR_RERANKER_MODEL", "dengcao/Qwen3-Reranker-0.6B:F16")


def _keep_alive_reranker() -> str:
    """Get the keep_alive duration for the reranker model."""
    try:
        from robothor.config import get_config

        return get_config().ollama.keep_alive_reranker
    except Exception:
        return "15m"


def _ollama_url() -> str:
    """Get Ollama URL from config or env."""
    url = os.environ.get("ROBOTHOR_OLLAMA_URL") or os.environ.get("OLLAMA_URL")
    if url:
        return url
    try:
        from robothor.config import get_config

        return get_config().ollama.base_url
    except Exception:
        return "http://localhost:11434"


# Availability cache. An /api/tags probe ran on EVERY search — pure tax, since
# the reranker's availability does not change between searches milliseconds
# apart. A short TTL keeps the degrade-on-outage behaviour: an Ollama restart
# is noticed within a minute, and a failed probe is cached only briefly so
# recovery is just as fast.
_AVAILABILITY_TTL_SECONDS = 60.0
_availability_cache: dict[str, object] = {}


def _availability_cache_clear() -> None:
    _availability_cache.clear()


def _availability_cache_age_for_tests(seconds: float) -> None:
    if "at" in _availability_cache:
        _availability_cache["at"] = float(_availability_cache["at"]) - seconds  # type: ignore[arg-type]


async def check_reranker_available() -> bool:
    """Check if a reranker model is available in Ollama (cached, 60s TTL)."""
    cached_at = _availability_cache.get("at")
    if cached_at is not None and (time.time() - float(cached_at)) < _AVAILABILITY_TTL_SECONDS:  # type: ignore[arg-type]
        return bool(_availability_cache.get("value"))
    result = await _check_reranker_available_uncached()
    _availability_cache["at"] = time.time()
    _availability_cache["value"] = result
    return result


async def _check_reranker_available_uncached() -> bool:
    global RERANKER_MODEL  # noqa: PLW0603
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_ollama_url()}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            # Look for F16 variant first (Q8_0 outputs garbage)
            for m in models:
                if "reranker" in m.lower() and "f16" in m.lower():
                    RERANKER_MODEL = m
                    return True
            # Fallback: any reranker model
            for m in models:
                if "reranker" in m.lower():
                    RERANKER_MODEL = m
                    return True
            return False
    except Exception:
        return False


def build_reranker_prompt(
    query: str,
    document: str,
    instruction: str = "Given a web search query, retrieve relevant passages that answer the query",
) -> str:
    """Build the ChatML prompt for the Qwen3-Reranker cross-encoder.

    Uses pre-filled <think> tags to skip reasoning and get a direct yes/no.
    """
    system = (
        "Judge whether the Document meets the requirements based on the Query and the Instruct provided. "
        'Note that the answer can only be "yes" or "no".'
    )
    user = f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document[:3000]}"
    return (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\n{user}<|im_end|>\n"
        f"<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )


async def rerank_pair(
    client: httpx.AsyncClient,
    query: str,
    document: str,
) -> str:
    """Score a single query-document pair. Returns 'yes' or 'no'.

    Takes one inference slot for the duration of the call. This is the leaf request,
    never the batch: holding a slot around the surrounding ``gather`` would deadlock
    the moment the batch needed the last slot.

    The slot is acquired OUTSIDE the try below on purpose. Every other failure here
    degrades to "no", but a gate refusal must not — marking every document
    irrelevant silently destroys the ranking it was asked to improve. Letting it
    propagate reaches ``rerank_with_fallback``, which falls back to cosine order.
    """
    from robothor.llm.local_gate import Lane, gate

    prompt = build_reranker_prompt(query, document)
    async with gate().slot(lane=Lane.BACKGROUND):
        try:
            resp = await client.post(
                f"{_ollama_url()}/api/generate",
                json={
                    "model": RERANKER_MODEL,
                    "prompt": prompt,
                    "raw": True,
                    "stream": False,
                    "keep_alive": _keep_alive_reranker(),
                    # One token is enough for yes/no — the parser only checks the prefix.
                    "options": {"temperature": 0.0, "num_predict": 1},
                },
            )
            resp.raise_for_status()
            text = resp.json().get("response", "").strip().lower()
            if "yes" in text:
                return "yes"
            return "no"
        except Exception:
            return "no"


async def rerank(
    query: str,
    results: list[dict[str, Any]],
    top_k: int = 10,
    batch_size: int | None = None,
) -> list[dict[str, Any]]:
    """Rerank search results using the cross-encoder.

    Strategy:
      1. Score each result as yes/no
      2. Sort "yes" results by original cosine similarity
      3. Backfill from top "no" results if fewer than top_k pass

    Args:
        query: The search query.
        results: Search results (must have 'content' and 'similarity' keys).
        top_k: Number of results to return.
        batch_size: Concurrent reranking requests per batch.

    Returns:
        Top-k results with 'rerank_relevant' field added.
    """
    if not results:
        return []

    available = await check_reranker_available()
    if not available:
        for r in results[:top_k]:
            r["rerank_relevant"] = "skipped"
        return results[:top_k]

    # Cap the scored pool. The candidates arrive RRF-ordered from the fused
    # retrieval legs, so pairs past the cap were already ranked out by BOTH
    # legs — scoring them buys ~nothing and each pair costs a serialized
    # ~37ms Ollama generate call. Scoring all 30-60 fused candidates was 97%
    # of search_facts's 1.3s p50 (measured 2026-08-24). 0 disables the cap.
    try:
        max_candidates = int(os.environ.get("MEMORY_RERANK_MAX_CANDIDATES", "16"))
    except ValueError:
        max_candidates = 16
    if max_candidates > 0:
        results = results[:max_candidates]

    # Queueing ten deep behind a two-slot server just moves the wait; size the
    # batch to what the device actually serves.
    if batch_size is None:
        from robothor.llm.local_gate import gate as _gate

        batch_size = max(1, _gate().slots)

    t0 = time.time()
    yes_results: list[dict[str, Any]] = []
    no_results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=120.0) as client:
        for i in range(0, len(results), batch_size):
            batch = results[i : i + batch_size]
            tasks = [rerank_pair(client, query, r.get("content", "")) for r in batch]
            verdicts = await asyncio.gather(*tasks)
            for r, verdict in zip(batch, verdicts, strict=True):
                r_copy = dict(r)
                r_copy["rerank_relevant"] = verdict
                if verdict == "yes":
                    yes_results.append(r_copy)
                else:
                    no_results.append(r_copy)

    yes_results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    no_results.sort(key=lambda x: x.get("similarity", 0), reverse=True)

    final = yes_results[:top_k]
    if len(final) < top_k:
        remaining = top_k - len(final)
        final.extend(no_results[:remaining])

    elapsed_ms = round((time.time() - t0) * 1000)
    for r in final:
        r["rerank_time_ms"] = elapsed_ms

    return final


async def rerank_with_fallback(
    query: str,
    results: list[dict[str, Any]],
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Rerank with automatic fallback to cosine similarity ordering."""
    try:
        return await rerank(query, results, top_k=top_k)
    except Exception:
        return results[:top_k]


def rerank_sync(query: str, results: list[dict[str, Any]], top_k: int = 10) -> list[dict[str, Any]]:
    """Synchronous wrapper for rerank()."""
    return asyncio.run(rerank_with_fallback(query, results, top_k=top_k))
