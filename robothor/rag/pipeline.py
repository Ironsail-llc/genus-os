"""
Unified RAG Pipeline — full orchestration from query to answer.

Ties together all RAG components: classification, parallel retrieval
(memory + web), merging, reranking, context injection, and generation.

Usage:
    from robothor.rag.pipeline import run_pipeline

    result = await run_pipeline("What is quantum computing?")
    print(result["answer"])

    # With profile override
    result = await run_pipeline("quick weather check", profile="fast")
"""

from __future__ import annotations

import asyncio
import time

from robothor.rag.context import SYSTEM_PROMPT, format_merged_context
from robothor.rag.profiles import RAG_PROFILES, classify_query
from robothor.rag.reranker import rerank_with_fallback
from robothor.rag.web_search import search_web, web_results_to_memory_format


async def run_pipeline(
    query: str,
    profile: str | None = None,
    messages: list[dict] | None = None,
) -> dict:
    """Run the full RAG pipeline.

    Steps:
      1. Classify query → select profile
      2. Parallel retrieval (memory + web)
      3. Merge results
      4. Rerank
      5. Inject context into prompt
      6. Generate via LLM

    Args:
        query: The user's question.
        profile: Optional profile override ('fast', 'general', 'research', 'code', etc.).
        messages: Optional chat history for multi-turn.

    Returns:
        Dict with 'answer', 'profile', 'sources', 'timing'.
    """
    from robothor.llm.ollama import chat, generate
    from robothor.memory.tiers import search_all_memory

    t0 = time.time()

    # Step 1: Classify
    selected_profile = profile or classify_query(query)
    p = RAG_PROFILES.get(selected_profile, RAG_PROFILES["general"])

    # Step 2: Parallel retrieval
    retrieval_tasks: list = []

    retrieval_tasks.append(
        asyncio.to_thread(
            search_all_memory,
            query,
            limit=p["memory_limit"],
        )
    )

    if p["use_web"]:
        retrieval_tasks.append(search_web(query, limit=p["web_limit"]))
    else:

        async def _no_web() -> list:
            return []

        retrieval_tasks.append(_no_web())

    raw_results = await asyncio.gather(*retrieval_tasks, return_exceptions=True)

    memory_results: list[dict] = raw_results[0] if isinstance(raw_results[0], list) else []
    raw_web_results: list[dict] = (
        raw_results[1] if len(raw_results) > 1 and isinstance(raw_results[1], list) else []
    )

    t_retrieval = time.time() - t0

    # Step 3: Merge — convert web results to memory format for reranking
    web_as_memory = web_results_to_memory_format(raw_web_results) if raw_web_results else []
    all_results = memory_results + web_as_memory

    # Step 4: Rerank
    t_rerank_start = time.time()
    if p["use_reranker"] and len(all_results) > p["rerank_top_k"]:
        reranked = await rerank_with_fallback(query, all_results, top_k=p["rerank_top_k"])
    else:
        reranked = all_results[: p["rerank_top_k"]]
    t_rerank = time.time() - t_rerank_start

    reranked_memory = [r for r in reranked if r.get("tier") != "web"]
    reranked_web = [r for r in reranked if r.get("tier") == "web"]

    # Step 5: Format context
    context = format_merged_context(
        reranked_memory,
        reranked_web if reranked_web else raw_web_results,
    )

    # Step 6: Generate
    t_gen_start = time.time()

    if messages:
        system_msg = f"{SYSTEM_PROMPT}\n\n## Retrieved Context\n{context}"
        chat_messages = [{"role": "system", "content": system_msg}] + messages
        answer = await chat(
            messages=chat_messages,
            temperature=p["temperature"],
            max_tokens=p["max_tokens"],
        )
    else:
        augmented_prompt = (
            f"## Retrieved Context\n{context}\n\n"
            f"## Question\n{query}\n\n"
            f"Answer using the context above. Cite sources when applicable."
        )
        answer = await generate(
            prompt=augmented_prompt,
            system=SYSTEM_PROMPT,
            temperature=p["temperature"],
            max_tokens=p["max_tokens"],
        )

    t_gen = time.time() - t_gen_start
    t_total = time.time() - t0

    return {
        "answer": answer,
        "profile": selected_profile,
        "query": query,
        "memories_found": len(memory_results),
        "web_results_found": len(raw_web_results),
        "reranked_count": len(reranked),
        "timing": {
            "retrieval_ms": round(t_retrieval * 1000),
            "rerank_ms": round(t_rerank * 1000),
            "generation_ms": round(t_gen * 1000),
            "total_ms": round(t_total * 1000),
        },
        "sources": {
            "memory": [
                {
                    "tier": r.get("tier"),
                    "type": r.get("content_type"),
                    "similarity": round(r.get("similarity", 0), 4),
                    "rerank_relevant": r.get("rerank_relevant"),
                    "preview": r.get("content", "")[:100],
                }
                for r in reranked_memory[:5]
            ],
            "web": [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", r.get("metadata", {}).get("url", "")),
                }
                for r in (reranked_web if reranked_web else raw_web_results)[:5]
            ],
        },
    }
