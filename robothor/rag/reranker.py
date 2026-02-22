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

import httpx

RERANKER_MODEL = os.environ.get(
    "ROBOTHOR_RERANKER_MODEL", "dengcao/Qwen3-Reranker-0.6B:F16"
)


def _ollama_url() -> str:
    """Get Ollama URL from config or env."""
    url = os.environ.get("ROBOTHOR_OLLAMA_URL") or os.environ.get("OLLAMA_URL")
    if url:
        return url
    try:
        from robothor.config import get_config
        cfg_url: str = get_config().ollama.url  # type: ignore[attr-defined]
        return cfg_url
    except Exception:
        return "http://localhost:11434"


async def check_reranker_available() -> bool:
    """Check if a reranker model is available in Ollama."""
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
        'Judge whether the Document meets the requirements based on the Query and the Instruct provided. '
        'Note that the answer can only be "yes" or "no".'
    )
    user = (
        f"<Instruct>: {instruction}\n"
        f"<Query>: {query}\n"
        f"<Document>: {document[:3000]}"
    )
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
    """Score a single query-document pair. Returns 'yes' or 'no'."""
    prompt = build_reranker_prompt(query, document)
    try:
        resp = await client.post(
            f"{_ollama_url()}/api/generate",
            json={
                "model": RERANKER_MODEL,
                "prompt": prompt,
                "raw": True,
                "stream": False,
                "options": {"temperature": 0.0, "num_predict": 2},
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
    results: list[dict],
    top_k: int = 10,
    batch_size: int = 10,
) -> list[dict]:
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

    t0 = time.time()
    yes_results: list[dict] = []
    no_results: list[dict] = []

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
    results: list[dict],
    top_k: int = 10,
) -> list[dict]:
    """Rerank with automatic fallback to cosine similarity ordering."""
    try:
        return await rerank(query, results, top_k=top_k)
    except Exception:
        return results[:top_k]


def rerank_sync(query: str, results: list[dict], top_k: int = 10) -> list[dict]:
    """Synchronous wrapper for rerank()."""
    return asyncio.run(rerank_with_fallback(query, results, top_k=top_k))
