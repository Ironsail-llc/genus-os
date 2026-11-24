"""
Cross-encoder reranker using Qwen3-Reranker via Ollama.

The model answers "yes" or "no". The score is the probability it gives "yes"
against "no" at that position, read from the logprobs Ollama returns: a graded
relevance in 0..1. Candidates are ordered by that score alone.

Usage:
    from robothor.rag.reranker import rerank_with_fallback

    results = await rerank_with_fallback(query, search_results, top_k=10)
"""

from __future__ import annotations

import asyncio
import math
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


# The reranker's task line. Qwen3-Reranker takes a free-text instruction; the
# model card's default is about web search, which is not what memory recall
# asks. This one says what a "relevant" memory is for an agent's question.
MEMORY_RECALL_INSTRUCTION = (
    "Given a question or topic, judge whether this stored memory (a fact, note or "
    "conversation excerpt) helps answer it or is directly about it"
)

# Candidates scored per search. The pool arrives in fused (RRF) order, so pairs
# past the cap were already ranked out by every leg; each pair is one
# ~37 ms local generate call, serialised to the device's slot count. Scoring
# all 30-60 fused candidates was 97% of search latency (measured 2026-08-24).
# Env MEMORY_RERANK_MAX_CANDIDATES overrides; 0 disables the cap.
RERANK_MAX_CANDIDATES = 16

# Alternatives requested at the single generated position: Ollama's maximum.
# Case variants ("Yes", "YES") crowd the list, so on the local 0.6B model both
# "yes" and "no" appear in only 20 of 40 pairs at 5 and 25 of 40 at 20; the
# rest are bounded in _graded_score. Asking for 20 cost no measurable latency.
_TOP_LOGPROBS = 20


def _max_candidates() -> int:
    try:
        return int(os.environ.get("MEMORY_RERANK_MAX_CANDIDATES", str(RERANK_MAX_CANDIDATES)))
    except ValueError:
        return RERANK_MAX_CANDIDATES


def build_reranker_prompt(
    query: str,
    document: str,
    instruction: str = MEMORY_RECALL_INSTRUCTION,
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
) -> float:
    """Score one query-document pair: graded relevance in 0..1.

    Takes one inference slot for the duration of the call. This is the leaf request,
    never the batch: holding a slot around the surrounding ``gather`` would deadlock
    the moment the batch needed the last slot.

    Any failure — a gate refusal, a transport error, a bad response — raises.
    Scoring a document 0 on error would silently corrupt the ranking it was asked
    to improve; raising reaches ``rerank_with_fallback``, which returns the fused
    order instead.
    """
    from robothor.llm.local_gate import Lane, gate

    prompt = build_reranker_prompt(query, document)
    async with gate().slot(lane=Lane.BACKGROUND):
        resp = await client.post(
            f"{_ollama_url()}/api/generate",
            json={
                "model": RERANKER_MODEL,
                "prompt": prompt,
                "raw": True,
                "stream": False,
                "keep_alive": _keep_alive_reranker(),
                "logprobs": True,
                "top_logprobs": _TOP_LOGPROBS,
                # One token is the answer; its alternatives carry the grade.
                "options": {"temperature": 0.0, "num_predict": 1},
            },
        )
        resp.raise_for_status()
        return _graded_score(resp.json())


def _graded_score(body: dict[str, Any]) -> float:
    """P(yes) / (P(yes) + P(no)) at the answer position, in 0..1.

    This is how the Qwen3-Reranker model card scores a pair (softmax over the
    "yes" and "no" logits). Ollama returns ``logprobs`` when asked (0.12.11 and
    later); an older server answers with text only, which degrades to 1.0 / 0.0
    — still an order, with ties kept in fused order.
    """
    positions = body.get("logprobs") or []
    if positions:
        best: dict[str, float] = {}
        listed: list[float] = []
        for alt in positions[0].get("top_logprobs") or []:
            lp = float(alt.get("logprob", float("-inf")))
            listed.append(lp)
            token = str(alt.get("token", "")).strip().lower()
            if token in ("yes", "no"):
                best[token] = max(lp, best.get(token, float("-inf")))
        if best:
            # A confident answer often pushes the other token out of the top
            # list (15 of 40 measured pairs, even at 20). Its logprob is then
            # at most the lowest one listed: use that bound, which keeps the
            # score graded instead of a flat 1.0 / 0.0.
            floor = min(listed)
            p_yes = math.exp(best.get("yes", floor))
            p_no = math.exp(best.get("no", floor))
            if p_yes + p_no > 0:
                return p_yes / (p_yes + p_no)
    text = str(body.get("response", "")).strip().lower()
    return 1.0 if text.startswith("yes") else 0.0


async def rerank(
    query: str,
    results: list[dict[str, Any]],
    top_k: int = 10,
    batch_size: int | None = None,
) -> list[dict[str, Any]]:
    """Rerank candidates by the cross-encoder's graded score.

    Scores the first ``RERANK_MAX_CANDIDATES`` (fused order) and returns the
    best ``top_k`` of them ordered by ``rerank_score`` alone — never re-sorted
    by cosine, so a candidate without a ``similarity`` (keyword-only, graph,
    episode) is ranked like any other. Equal scores keep the input order.

    Args:
        query: The search query.
        results: Candidates in fused order, each with a 'content' key.
        top_k: Number of results to return.
        batch_size: Concurrent reranking requests per batch.

    Returns:
        Up to top_k scored rows, each with ``rerank_score`` (0..1) and, for
        older readers, ``rerank_relevant`` ("yes" when the score is >= 0.5).
        Raises if any pair fails; ``rerank_with_fallback`` turns that into the
        fused order.
    """
    if not results:
        return []

    available = await check_reranker_available()
    if not available:
        for r in results[:top_k]:
            r["rerank_relevant"] = "skipped"
        return results[:top_k]

    max_candidates = _max_candidates()
    if max_candidates > 0:
        results = results[:max_candidates]

    # Queueing ten deep behind a two-slot server just moves the wait; size the
    # batch to what the device actually serves.
    if batch_size is None:
        from robothor.llm.local_gate import gate as _gate

        batch_size = max(1, _gate().slots)

    t0 = time.time()
    scored: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=120.0) as client:
        for i in range(0, len(results), batch_size):
            batch = results[i : i + batch_size]
            tasks = [rerank_pair(client, query, r.get("content", "")) for r in batch]
            scores = await asyncio.gather(*tasks)
            for r, score in zip(batch, scores, strict=True):
                r_copy = dict(r)
                r_copy["rerank_score"] = float(score)
                r_copy["rerank_relevant"] = "yes" if score >= 0.5 else "no"
                scored.append(r_copy)

    # sorted() is stable: equal scores keep the fused order.
    final = sorted(scored, key=lambda x: x["rerank_score"], reverse=True)[:top_k]

    elapsed_ms = round((time.time() - t0) * 1000)
    for r in final:
        r["rerank_time_ms"] = elapsed_ms

    return final


async def rerank_with_fallback(
    query: str,
    results: list[dict[str, Any]],
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Rerank; on any reranker error return the input (fused) order, cut to top_k."""
    try:
        return await rerank(query, results, top_k=top_k)
    except Exception:
        return results[:top_k]


def rerank_sync(query: str, results: list[dict[str, Any]], top_k: int = 10) -> list[dict[str, Any]]:
    """Synchronous wrapper for rerank()."""
    return asyncio.run(rerank_with_fallback(query, results, top_k=top_k))
