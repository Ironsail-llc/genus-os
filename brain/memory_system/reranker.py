#!/usr/bin/env python3
"""
Reranker module using Qwen3-Reranker-0.6B (F16) via Ollama.

The Qwen3-Reranker is a cross-encoder that outputs "yes"/"no" for relevance,
NOT a numeric score. We use the raw /api/generate endpoint with a ChatML
template and pre-filled <think> tags.

Scoring strategy (Option B from plan):
  - Binary yes/no as relevance filter
  - Sort "yes" results by original cosine similarity
  - Backfill from top "no" results if too few pass

Preserved public API:
  - check_reranker_available()
  - rerank()
  - rerank_with_fallback()
  - rerank_sync()
"""

import asyncio
import time

import httpx

OLLAMA_URL = "http://localhost:11434"
RERANKER_MODEL = "dengcao/Qwen3-Reranker-0.6B:F16"


async def check_reranker_available() -> bool:
    """Check if the Qwen3-Reranker-0.6B:F16 model is available in Ollama."""
    global RERANKER_MODEL
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            # Look for F16 variant first (Q8_0 outputs garbage)
            for m in models:
                if "reranker" in m.lower() and "f16" in m.lower():
                    RERANKER_MODEL = m
                    return True
            # Fallback: any reranker model (may not work well)
            for m in models:
                if "reranker" in m.lower():
                    RERANKER_MODEL = m
                    return True
            return False
    except Exception:
        return False


def _build_reranker_prompt(
    query: str,
    document: str,
    instruction: str = "Given a web search query, retrieve relevant passages that answer the query",
) -> str:
    """Build the raw ChatML prompt for the Qwen3-Reranker cross-encoder.

    The prompt format follows the Qwen3-Reranker specification:
      - System message: "Judge whether the Document meets the requirements..."
      - User message: <Instruct> + <Query> + <Document>
      - Pre-filled assistant with <think>\\n\\n</think>\\n\\n to skip reasoning
    """
    system = (
        "Judge whether the Document meets the requirements based on the Query and the Instruct provided. "
        'Note that the answer can only be "yes" or "no".'
    )

    user = f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document[:3000]}"

    # ChatML template with pre-filled think tags to skip reasoning
    prompt = (
        f"<|im_start|>system\n{system}<|im_end|>\n"
        f"<|im_start|>user\n{user}<|im_end|>\n"
        f"<|im_start|>assistant\n<think>\n\n</think>\n\n"
    )

    return prompt


async def rerank_pair(
    client: httpx.AsyncClient,
    query: str,
    document: str,
) -> str:
    """Score a single query-document pair using the Qwen3-Reranker.

    Returns "yes" or "no" indicating relevance.
    """
    prompt = _build_reranker_prompt(query, document)

    try:
        resp = await client.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": RERANKER_MODEL,
                "prompt": prompt,
                "raw": True,
                "stream": False,
                "options": {
                    "temperature": 0.0,
                    "num_predict": 2,
                },
            },
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip().lower()
        # Extract yes/no from response
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
    """Rerank search results using the Qwen3-Reranker cross-encoder.

    Strategy:
      1. Score each result as yes/no with the cross-encoder
      2. Sort "yes" results by original cosine similarity
      3. Backfill from top "no" results (by cosine sim) if fewer than top_k pass

    Args:
        query: The search query.
        results: List of search results from search_all_memory().
        top_k: Number of results to return after reranking.
        batch_size: How many reranking requests to run concurrently.

    Returns:
        Top-k results with 'rerank_relevant' field added.
    """
    if not results:
        return []

    available = await check_reranker_available()
    if not available:
        # Fallback: return original results sorted by cosine similarity
        for r in results[:top_k]:
            r_copy = dict(r)
            r_copy["rerank_relevant"] = "skipped"
        return results[:top_k]

    t0 = time.time()

    # Score all results in batches
    yes_results = []
    no_results = []

    async with httpx.AsyncClient(timeout=120.0) as client:
        for i in range(0, len(results), batch_size):
            batch = results[i : i + batch_size]
            tasks = [rerank_pair(client, query, r.get("content", "")) for r in batch]
            verdicts = await asyncio.gather(*tasks)
            for r, verdict in zip(batch, verdicts):
                r_copy = dict(r)
                r_copy["rerank_relevant"] = verdict
                if verdict == "yes":
                    yes_results.append(r_copy)
                else:
                    no_results.append(r_copy)

    # Sort each group by cosine similarity (descending)
    yes_results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    no_results.sort(key=lambda x: x.get("similarity", 0), reverse=True)

    # Build final list: yes first, then backfill from no
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
    """Rerank with automatic fallback to cosine similarity ordering.

    This is the primary entry point for the pipeline. It tries the reranker
    and falls back gracefully if unavailable.
    """
    try:
        return await rerank(query, results, top_k=top_k)
    except Exception:
        # Any failure → fall back to original ordering
        return results[:top_k]


# Synchronous wrapper
def rerank_sync(query: str, results: list[dict], top_k: int = 10) -> list[dict]:
    """Synchronous wrapper for rerank()."""
    return asyncio.run(rerank_with_fallback(query, results, top_k=top_k))


if __name__ == "__main__":

    async def main():
        available = await check_reranker_available()
        print(f"Reranker model available: {available}")
        print(f"Model: {RERANKER_MODEL}")

        if available:
            # Test with dummy data
            test_results = [
                {
                    "content": "Philip scheduled a meeting for Tuesday at 3pm.",
                    "similarity": 0.8,
                    "tier": "short_term",
                    "content_type": "task",
                },
                {
                    "content": "The weather in Melbourne will be sunny.",
                    "similarity": 0.75,
                    "tier": "short_term",
                    "content_type": "conversation",
                },
                {
                    "content": "Philip's email password was reset yesterday.",
                    "similarity": 0.7,
                    "tier": "long_term",
                    "content_type": "email",
                },
                {
                    "content": "NVIDIA Grace Blackwell has 128GB unified memory.",
                    "similarity": 0.65,
                    "tier": "long_term",
                    "content_type": "conversation",
                },
            ]
            reranked = await rerank("What meetings does Philip have?", test_results, top_k=3)
            print("\nReranked results:")
            for r in reranked:
                print(
                    f"  [{r.get('rerank_relevant', '?')}] (sim={r.get('similarity', 0):.3f}) {r['content'][:80]}"
                )
        else:
            print("Pull the reranker: ollama pull dengcao/Qwen3-Reranker-0.6B:F16")

    asyncio.run(main())
