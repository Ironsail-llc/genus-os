"""
Three-Tier Memory System for Robothor.

Tier 1: Working memory (context window, not stored)
Tier 2: Short-term memory (PostgreSQL, 48h TTL, auto-decays)
Tier 3: Long-term memory (PostgreSQL + pgvector, permanent, importance-scored)

Provides vector search across tiers, maintenance (archival from short-term
to long-term), and memory statistics.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import requests
from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)


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


def _embedding_model() -> str:
    """Get embedding model name."""
    model = os.environ.get("ROBOTHOR_EMBEDDING_MODEL")
    if model:
        return model
    try:
        from robothor.config import get_config
        return get_config().ollama.embedding_model
    except Exception:
        return "qwen3-embedding:0.6b"


def _llm_model() -> str:
    """Get LLM model name for summarization."""
    return os.environ.get("ROBOTHOR_GENERATION_MODEL", "llama3.2-vision:11b")


def get_embedding(text: str) -> list[float]:
    """Generate embedding using Ollama (sync)."""
    response = requests.post(
        f"{_ollama_url()}/api/embed",
        json={"model": _embedding_model(), "input": text},
    )
    embeddings: list[float] = response.json()["embeddings"][0]
    return embeddings


# ============== TIER 2: SHORT-TERM MEMORY ==============


def store_short_term(
    content: str,
    content_type: str,
    metadata: dict | None = None,
    ttl_hours: int = 48,
) -> int:
    """Store content in short-term memory with embedding."""
    embedding = get_embedding(content)

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO short_term_memory
            (content, content_type, embedding, metadata, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                content,
                content_type,
                embedding,
                json.dumps(metadata or {}),
                datetime.now() + timedelta(hours=ttl_hours),
            ),
        )
        memory_id: int = cur.fetchone()[0]

    return memory_id


def search_short_term(query: str, limit: int = 5) -> list[dict]:
    """Search short-term memory semantically."""
    query_embedding = get_embedding(query)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                id,
                content,
                content_type,
                metadata,
                created_at,
                1 - (embedding <=> %s::vector) as similarity
            FROM short_term_memory
            WHERE embedding IS NOT NULL
              AND expires_at > NOW()
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (query_embedding, query_embedding, limit),
        )
        results = cur.fetchall()

    # Update access counts
    if results:
        with get_connection() as conn:
            cur = conn.cursor()
            for r in results:
                cur.execute(
                    """
                    UPDATE short_term_memory
                    SET access_count = access_count + 1, accessed_at = NOW()
                    WHERE id = %s
                    """,
                    (r["id"],),
                )

    return [dict(r) for r in results]


# ============== TIER 3: LONG-TERM MEMORY ==============


def archive_to_long_term(
    content: str,
    summary: str,
    content_type: str,
    original_date: datetime,
    metadata: dict | None = None,
    source_ids: list[int] | None = None,
) -> int:
    """Archive content to long-term memory."""
    embedding = get_embedding(summary or content)

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO long_term_memory
            (content, summary, content_type, embedding, metadata, original_date, source_tier2_ids)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                content,
                summary,
                content_type,
                embedding,
                json.dumps(metadata or {}),
                original_date,
                source_ids,
            ),
        )
        memory_id: int = cur.fetchone()[0]

    return memory_id


def search_long_term(query: str, limit: int = 5) -> list[dict]:
    """Search long-term memory semantically."""
    query_embedding = get_embedding(query)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT
                id,
                COALESCE(summary, content) as content,
                content_type,
                metadata,
                original_date,
                1 - (embedding <=> %s::vector) as similarity
            FROM long_term_memory
            WHERE embedding IS NOT NULL
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (query_embedding, query_embedding, limit),
        )
        results = cur.fetchall()

    return [dict(r) for r in results]


# ============== UNIFIED SEARCH ==============


def search_all_memory(
    query: str,
    limit: int = 10,
    include_short: bool = True,
    include_long: bool = True,
) -> list[dict]:
    """Search across all memory tiers."""
    results: list[dict] = []

    if include_short:
        short_results = search_short_term(query, limit)
        for r in short_results:
            r["tier"] = "short_term"
        results.extend(short_results)

    if include_long:
        long_results = search_long_term(query, limit)
        for r in long_results:
            r["tier"] = "long_term"
        results.extend(long_results)

    results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
    return results[:limit]


# ============== MAINTENANCE ==============


def summarize_with_local_llm(content: str) -> str:
    """Use local LLM to summarize content for archival."""
    response = requests.post(
        f"{_ollama_url()}/api/generate",
        json={
            "model": _llm_model(),
            "prompt": f"Summarize the following in 2-3 sentences, preserving key facts and decisions:\n\n{content}",
            "stream": False,
        },
    )
    result: str = response.json()["response"]
    return result


def run_maintenance() -> dict:
    """Maintenance job: archive expiring short-term, clean up, run lifecycle."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT * FROM short_term_memory
            WHERE expires_at < NOW() + INTERVAL '6 hours'
              AND access_count >= 2
            ORDER BY access_count DESC
            LIMIT 10
            """
        )
        to_archive = cur.fetchall()

    archived_count = 0
    for entry in to_archive:
        summary = summarize_with_local_llm(entry["content"])
        archive_to_long_term(
            content=entry["content"],
            summary=summary,
            content_type=entry["content_type"],
            original_date=entry["created_at"],
            metadata=entry["metadata"],
            source_ids=[entry["id"]],
        )
        archived_count += 1

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM short_term_memory WHERE expires_at < NOW()")
        deleted_count: int = cur.rowcount

    # Run lifecycle maintenance on facts
    lifecycle_result: dict = {}
    try:
        import asyncio

        from robothor.memory.lifecycle import run_lifecycle_maintenance

        lifecycle_result = asyncio.run(run_lifecycle_maintenance())
    except Exception:
        pass

    return {"archived": archived_count, "deleted": deleted_count, "lifecycle": lifecycle_result}


# ============== STATS ==============


def get_memory_stats() -> dict:
    """Get memory system statistics."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT COUNT(*) as count FROM short_term_memory")
        short_count: int = cur.fetchone()["count"]

        cur.execute("SELECT COUNT(*) as count FROM long_term_memory")
        long_count: int = cur.fetchone()["count"]

        cur.execute(
            """
            SELECT content_type, COUNT(*) as count
            FROM short_term_memory
            GROUP BY content_type
            """
        )
        short_by_type = {r["content_type"]: r["count"] for r in cur.fetchall()}

    return {
        "short_term_count": short_count,
        "long_term_count": long_count,
        "short_by_type": short_by_type,
    }
