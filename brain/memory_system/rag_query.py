#!/usr/bin/env python3
"""
RAG Query Engine — search memory, inject context, generate via Qwen3-80B.

Extends the existing rag.py with generation capabilities.
Does NOT modify rag.py — imports from it.
"""

import asyncio
import sys
import time

from robothor.llm.ollama import chat, generate, get_embedding_async

# Import from canonical package
from robothor.memory.facts import search_facts_compat as search_all_memory


def get_embedding(text):
    """Sync wrapper for embedding."""
    return asyncio.run(get_embedding_async(text))


SYSTEM_PROMPT = """You are Robothor, an AI assistant with access to a personal memory system.
You have been given relevant context from your memory database below.
Use this context to answer the user's question accurately.
If the context doesn't contain relevant information, say so and answer from general knowledge.
Always cite which memories informed your answer when applicable."""


def format_context(results: list[dict], max_chars: int = 40000) -> str:
    """Format search results into a context string for the LLM.

    Args:
        results: List of memory search results.
        max_chars: Maximum total characters for context.

    Returns:
        Formatted context string.
    """
    if not results:
        return "No relevant memories found."

    parts = []
    total = 0
    for i, r in enumerate(results, 1):
        content = r.get("content", "")
        tier = r.get("tier", "unknown")
        sim = r.get("similarity", 0)
        ctype = r.get("content_type", "unknown")
        created = str(r.get("created_at", r.get("original_date", "")))[:19]

        entry = f"[Memory {i}] ({tier}, {ctype}, sim={sim:.3f}, {created})\n{content}"

        if total + len(entry) > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                parts.append(entry[:remaining] + "...")
            break

        parts.append(entry)
        total += len(entry)

    return "\n\n---\n\n".join(parts)


async def rag_query(
    question: str,
    memory_limit: int = 20,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    include_short: bool = True,
    include_long: bool = True,
    system_prompt: str | None = None,
    use_reranker: bool = True,
    rerank_top_k: int = 10,
) -> dict:
    """Full RAG pipeline: search memory → rerank → inject context → generate.

    Args:
        question: The user's question.
        memory_limit: How many memory results to retrieve (over-retrieves for reranking).
        temperature: Generation temperature.
        max_tokens: Max tokens to generate.
        include_short: Search short-term memory.
        include_long: Search long-term memory.
        system_prompt: Override the default system prompt.
        use_reranker: Whether to rerank results with cross-encoder.
        rerank_top_k: Number of results to keep after reranking.

    Returns:
        Dict with 'answer', 'context_used', 'memories_found', 'timing'.
    """
    t0 = time.time()

    # Step 1: Search memory (over-retrieve for reranking)
    results = search_all_memory(
        question,
        limit=memory_limit,
        include_short=include_short,
        include_long=include_long,
    )
    t_search = time.time() - t0

    # Step 1.5: Rerank with cross-encoder
    t_rerank = 0.0
    if use_reranker and len(results) > rerank_top_k:
        from reranker import rerank_with_fallback

        t_r0 = time.time()
        results = await rerank_with_fallback(question, results, top_k=rerank_top_k)
        t_rerank = time.time() - t_r0

    # Step 2: Format context
    context = format_context(results)

    # Step 3: Build prompt with injected context
    augmented_prompt = f"""## Retrieved Context from Memory
{context}

## User Question
{question}

## Instructions
Answer the question using the context above. If the context is relevant, reference it.
If not relevant, answer from your general knowledge and note that no relevant memories were found."""

    # Step 4: Generate via Qwen3-80B
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
            "rerank_ms": round(t_rerank * 1000),
            "generation_ms": round(t_gen * 1000),
            "total_ms": round((time.time() - t0) * 1000),
        },
        "sources": [
            {
                "tier": r.get("tier"),
                "type": r.get("content_type"),
                "similarity": round(r.get("similarity", 0), 4),
                "rerank_relevant": r.get("rerank_relevant"),
                "preview": r.get("content", "")[:100],
            }
            for r in results
        ],
    }


async def rag_chat(
    messages: list[dict[str, str]],
    memory_limit: int = 20,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    use_reranker: bool = True,
    rerank_top_k: int = 10,
) -> dict:
    """Multi-turn RAG chat — searches memory based on the latest user message.

    Args:
        messages: Chat history [{"role": "user", "content": "..."}].
        memory_limit: How many memory results to retrieve.
        temperature: Generation temperature.
        max_tokens: Max tokens to generate.
        use_reranker: Whether to rerank results with cross-encoder.
        rerank_top_k: Number of results to keep after reranking.

    Returns:
        Dict with 'answer', 'memories_found', 'timing'.
    """
    # Extract the latest user message for search
    last_user_msg = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user_msg = m["content"]
            break

    if not last_user_msg:
        return {"answer": "No user message found.", "memories_found": 0, "timing": {}}

    t0 = time.time()

    # Search memory
    results = search_all_memory(last_user_msg, limit=memory_limit)
    t_search = time.time() - t0

    if use_reranker and len(results) > rerank_top_k:
        from reranker import rerank_with_fallback

        results = await rerank_with_fallback(last_user_msg, results, top_k=rerank_top_k)

    # Build system message with context
    context = format_context(results)
    system_msg = f"""{SYSTEM_PROMPT}

## Retrieved Context from Memory
{context}"""

    # Prepend system message and send full chat
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


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: rag_query.py <question>")
        print("  Options: --limit N  --temp F")
        sys.exit(1)

    question = sys.argv[1]
    limit = 10
    temp = 0.7

    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
        if arg == "--temp" and i + 1 < len(sys.argv):
            temp = float(sys.argv[i + 1])

    result = query_sync(question, memory_limit=limit, temperature=temp)

    print(f"\n{'=' * 60}")
    print(f"Question: {result['question']}")
    print(f"Memories found: {result['memories_found']}")
    print(
        f"Timing: search={result['timing']['search_ms']}ms, "
        f"gen={result['timing']['generation_ms']}ms, "
        f"total={result['timing']['total_ms']}ms"
    )
    print(f"{'=' * 60}")
    print(f"\n{result['answer']}")

    if result.get("sources"):
        print(f"\n{'=' * 60}")
        print("Sources:")
        for s in result["sources"]:
            print(f"  [{s['tier']}] ({s['type']}, sim={s['similarity']}) {s['preview'][:80]}...")
