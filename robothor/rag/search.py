"""
RAG Search — high-level query interface for the RAG pipeline.

Provides the full RAG query flow: search memory → inject context → generate.
For the underlying memory search, see robothor.memory.tiers.

Usage:
    from robothor.rag.search import rag_query, query_sync

    result = await rag_query("What meetings do I have tomorrow?")
    print(result["answer"])
"""

from __future__ import annotations

import asyncio
import time

from robothor.rag.context import SYSTEM_PROMPT, format_context


async def rag_query(
    question: str,
    memory_limit: int = 10,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    include_short: bool = True,
    include_long: bool = True,
    system_prompt: str | None = None,
) -> dict:
    """Full RAG pipeline: search memory → inject context → generate.

    Args:
        question: The user's question.
        memory_limit: How many memory results to retrieve.
        temperature: Generation temperature.
        max_tokens: Max tokens to generate.
        include_short: Search short-term memory.
        include_long: Search long-term memory.
        system_prompt: Override the default system prompt.

    Returns:
        Dict with 'answer', 'context_used', 'memories_found', 'timing'.
    """
    from robothor.llm.ollama import generate
    from robothor.memory.tiers import search_all_memory

    t0 = time.time()

    # Step 1: Search memory
    results = search_all_memory(
        question,
        limit=memory_limit,
        include_short=include_short,
        include_long=include_long,
    )
    t_search = time.time() - t0

    # Step 2: Format context
    context = format_context(results)

    # Step 3: Build prompt with injected context
    augmented_prompt = (
        f"## Retrieved Context from Memory\n{context}\n\n"
        f"## User Question\n{question}\n\n"
        f"## Instructions\n"
        f"Answer the question using the context above. If the context is relevant, reference it. "
        f"If not relevant, answer from your general knowledge and note that no relevant memories were found."
    )

    # Step 4: Generate
    t1 = time.time()
    answer = await generate(
        prompt=augmented_prompt,
        system=system_prompt or SYSTEM_PROMPT,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    t_gen = time.time() - t1

    return {
        "answer": answer,
        "question": question,
        "memories_found": len(results),
        "context_used": context,
        "timing": {
            "search_ms": round(t_search * 1000),
            "generation_ms": round(t_gen * 1000),
            "total_ms": round((time.time() - t0) * 1000),
        },
        "sources": [
            {
                "tier": r.get("tier"),
                "type": r.get("content_type"),
                "similarity": round(r.get("similarity", 0), 4),
                "preview": r.get("content", "")[:100],
            }
            for r in results
        ],
    }


async def rag_chat(
    messages: list[dict[str, str]],
    memory_limit: int = 10,
    temperature: float = 0.7,
    max_tokens: int = 4096,
) -> dict:
    """Multi-turn RAG chat — searches memory based on the latest user message.

    Args:
        messages: Chat history [{"role": "user", "content": "..."}].
        memory_limit: How many memory results to retrieve.
        temperature: Generation temperature.
        max_tokens: Max tokens to generate.

    Returns:
        Dict with 'answer', 'memories_found', 'timing'.
    """
    from robothor.llm.ollama import chat
    from robothor.memory.tiers import search_all_memory

    # Extract the latest user message for search
    last_user_msg = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user_msg = m["content"]
            break

    if not last_user_msg:
        return {"answer": "No user message found.", "memories_found": 0, "timing": {}}

    t0 = time.time()

    results = search_all_memory(last_user_msg, limit=memory_limit)
    t_search = time.time() - t0

    context = format_context(results)
    system_msg = f"{SYSTEM_PROMPT}\n\n## Retrieved Context from Memory\n{context}"

    chat_messages = [{"role": "system", "content": system_msg}] + messages

    t1 = time.time()
    answer = await chat(
        messages=chat_messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    t_gen = time.time() - t1

    return {
        "answer": answer,
        "memories_found": len(results),
        "timing": {
            "search_ms": round(t_search * 1000),
            "generation_ms": round(t_gen * 1000),
            "total_ms": round((time.time() - t0) * 1000),
        },
    }


def query_sync(question: str, **kwargs) -> dict:
    """Synchronous wrapper for CLI usage."""
    return asyncio.run(rag_query(question, **kwargs))
