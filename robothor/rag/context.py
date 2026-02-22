"""
Context formatting for RAG pipeline.

Formats memory search results and web search results into context
strings suitable for LLM prompt injection.

Usage:
    from robothor.rag.context import format_context, format_merged_context

    ctx = format_context(memory_results)
    merged = format_merged_context(memory_results, web_results)
"""

from __future__ import annotations

SYSTEM_PROMPT = """You are an AI assistant with access to a personal memory system and web search.
You have been given relevant context from memory and/or web search below.
Use this context to answer the user's question accurately.
If the context doesn't contain relevant information, say so and answer from general knowledge.
Cite your sources when applicable — reference memory entries or web URLs."""


def format_context(results: list[dict], max_chars: int = 40000) -> str:
    """Format memory search results into a context string for the LLM.

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


def format_merged_context(
    memory_results: list[dict],
    web_results: list[dict],
    max_chars: int = 40000,
) -> str:
    """Format merged memory + web results into a context block.

    Args:
        memory_results: Reranked memory search results.
        web_results: Web search results (raw or in memory format).
        max_chars: Maximum total characters for context.

    Returns:
        Formatted context string.
    """
    parts = []
    total = 0

    # Memory results first
    for i, r in enumerate(memory_results, 1):
        content = r.get("content", "")
        tier = r.get("tier", "unknown")
        sim = r.get("similarity", 0)
        ctype = r.get("content_type", "unknown")

        score_str = f"sim={sim:.3f}"
        rerank_rel = r.get("rerank_relevant")
        if rerank_rel is not None:
            score_str += f", relevant={rerank_rel}"

        entry = f"[Memory {i}] ({tier}, {ctype}, {score_str})\n{content}"
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)

    # Web results
    for i, r in enumerate(web_results, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")

        entry = f"[Web {i}] {title}\nURL: {url}\n{content}"
        if total + len(entry) > max_chars:
            break
        parts.append(entry)
        total += len(entry)

    if not parts:
        return "No relevant context found."

    return "\n\n---\n\n".join(parts)
