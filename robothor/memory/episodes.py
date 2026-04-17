"""
Episodic Memory — time-bucketed event clusters.

An episode is a cluster of related facts sharing temporal proximity and
entity overlap. Episodes answer queries like "what happened the week of X"
or "what was going on with Y last month" that flat fact retrieval handles
poorly.

Episodes are built nightly by `build_episodes_from_facts()` and stored in
`memory_episodes`. Retrieval merges them into `search_facts` via RRF when
`include_episodes=True`.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client

logger = logging.getLogger(__name__)

# Two facts split into different episodes when their timestamps are this far
# apart, regardless of entity overlap.
_TEMPORAL_BREAK_HOURS = 6

# Minimum Jaccard overlap of entity sets to keep two facts in the same episode.
_MIN_ENTITY_OVERLAP = 0.20

# Episodes need at least this many facts to be meaningful.
_MIN_EPISODE_FACTS = 2

# Per-run cap on number of episodes built, to bound maintenance wall-clock.
_MAX_EPISODES_PER_RUN = 20


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _cluster_facts(facts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group chronologically-sorted facts into episodes.

    Simple single-pass clustering:
      - Start a new cluster whenever temporal gap > _TEMPORAL_BREAK_HOURS.
      - Inside a time-contiguous block, split when entity overlap drops
        below _MIN_ENTITY_OVERLAP.
    """
    if not facts:
        return []

    clusters: list[list[dict[str, Any]]] = [[facts[0]]]
    for fact in facts[1:]:
        current = clusters[-1]
        prev = current[-1]

        gap = fact["created_at"] - prev["created_at"]
        if gap > timedelta(hours=_TEMPORAL_BREAK_HOURS):
            clusters.append([fact])
            continue

        # Entity overlap with the whole cluster so far (union of entities).
        cluster_entities: set[str] = set()
        for f in current:
            cluster_entities.update(f.get("entities") or [])
        this_entities = set(fact.get("entities") or [])

        if cluster_entities and this_entities:
            overlap = _jaccard(cluster_entities, this_entities)
            if overlap < _MIN_ENTITY_OVERLAP:
                clusters.append([fact])
                continue

        current.append(fact)

    return [c for c in clusters if len(c) >= _MIN_EPISODE_FACTS]


async def _summarize_cluster(cluster: list[dict[str, Any]]) -> tuple[str, str]:
    """LLM pass: title + short summary for a cluster. Falls back to heuristics."""
    # Fallback assembly in case LLM fails.
    entities = sorted({e for f in cluster for e in (f.get("entities") or [])})[:5]
    start = cluster[0]["created_at"].strftime("%Y-%m-%d")
    fallback_title = f"{', '.join(entities[:3]) or 'Activity'} — {start}"
    fallback_summary = "; ".join(f["fact_text"][:120] for f in cluster[: min(5, len(cluster))])

    fact_lines = [f"- {f['fact_text']}" for f in cluster[:15]]
    prompt = (
        "Given the following facts that happened together during a short time "
        "window, write:\n"
        "  (1) a concise title (max 60 chars, no trailing period)\n"
        "  (2) a summary (max 300 chars) — what was going on overall\n\n"
        'Return strict JSON: {"title": "...", "summary": "..."}\n\n'
        "Facts:\n" + "\n".join(fact_lines)
    )
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": ["title", "summary"],
    }
    try:
        raw = await llm_client.generate(
            prompt=prompt,
            system="Summarize the episode. Return valid JSON.",
            max_tokens=400,
            format=schema,
            think=False,
        )
        data = json.loads(raw) if raw else {}
        title = (data.get("title") or "").strip() or fallback_title
        summary = (data.get("summary") or "").strip() or fallback_summary
        return title[:200], summary[:1000]
    except Exception as e:
        logger.warning("Episode summary LLM failed, using fallback: %s", e)
        return fallback_title[:200], fallback_summary[:1000]


async def _resolve_entity_ids(entity_names: set[str], tenant_id: str) -> list[int]:
    """Look up memory_entities IDs for a set of entity names. Silently skips misses."""
    if not entity_names:
        return []
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id FROM memory_entities
            WHERE tenant_id = %s AND name = ANY(%s)
            """,
            (tenant_id, list(entity_names)),
        )
        return [row[0] for row in cur.fetchall()]


async def _store_episode(
    cluster: list[dict[str, Any]],
    title: str,
    summary: str,
    tenant_id: str,
) -> int | None:
    """Embed summary and insert the episode row. Returns id or None on failure."""
    try:
        embedding = await llm_client.get_embedding_async(summary)
    except Exception as e:
        logger.warning("Episode embedding failed, storing without embedding: %s", e)
        embedding = None

    fact_ids = [f["id"] for f in cluster]
    entity_names = {e for f in cluster for e in (f.get("entities") or [])}
    entity_ids = await _resolve_entity_ids(entity_names, tenant_id)
    source_types = sorted({f.get("source_type", "") for f in cluster if f.get("source_type")})
    start_time = cluster[0]["created_at"]
    end_time = cluster[-1]["created_at"]

    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO memory_episodes
                    (tenant_id, start_time, end_time, title, summary,
                     summary_embedding, entity_ids, fact_ids, source_types, fact_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    start_time,
                    end_time,
                    title,
                    summary,
                    embedding,
                    entity_ids,
                    fact_ids,
                    source_types,
                    len(cluster),
                ),
            )
            episode_id: int = cur.fetchone()[0]
            return episode_id
    except Exception as e:
        logger.warning("Episode insert failed: %s", e)
        return None


async def build_episodes_from_facts(
    hours_back: int = 72,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Build episodes from recent active facts.

    Picks facts in the last `hours_back` that are not already referenced by
    any existing episode, clusters them, and stores one episode per cluster.

    Returns stats dict: {candidates, clusters, episodes_stored, skipped}.
    """
    tid = tenant_id or DEFAULT_TENANT
    cutoff = datetime.now(UTC) - timedelta(hours=hours_back)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Find facts not already claimed by an episode. Exclude facts that
        # appear in any existing episode's fact_ids array.
        cur.execute(
            """
            SELECT f.id, f.fact_text, f.category, f.entities, f.source_type,
                   f.created_at
            FROM memory_facts f
            WHERE f.is_active = TRUE
              AND f.tenant_id = %s
              AND f.created_at >= %s
              AND NOT EXISTS (
                  SELECT 1 FROM memory_episodes e
                  WHERE e.tenant_id = f.tenant_id
                    AND e.is_active = TRUE
                    AND f.id = ANY(e.fact_ids)
              )
            ORDER BY f.created_at ASC
            """,
            (tid, cutoff),
        )
        facts = [dict(row) for row in cur.fetchall()]

    stats: dict[str, Any] = {
        "candidates": len(facts),
        "clusters": 0,
        "episodes_stored": 0,
        "skipped": 0,
    }
    if not facts:
        return stats

    clusters = _cluster_facts(facts)
    stats["clusters"] = len(clusters)

    capped = clusters[:_MAX_EPISODES_PER_RUN]
    if len(clusters) > _MAX_EPISODES_PER_RUN:
        logger.info(
            "build_episodes: %d clusters found, capping at %d for this run",
            len(clusters),
            _MAX_EPISODES_PER_RUN,
        )

    for cluster in capped:
        try:
            title, summary = await _summarize_cluster(cluster)
            ep_id = await _store_episode(cluster, title, summary, tid)
            if ep_id is not None:
                stats["episodes_stored"] += 1
            else:
                stats["skipped"] += 1
        except Exception as e:
            logger.warning("build_episodes: cluster failed: %s", e)
            stats["skipped"] += 1

    return stats


async def search_episodes(
    query: str,
    limit: int = 3,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search over episode summaries.

    Returns episode dicts with `source='episode'` and a `similarity` score so
    results merge cleanly into `search_facts` output.
    """
    tid = tenant_id or DEFAULT_TENANT
    try:
        embedding = await llm_client.get_embedding_async(query)
    except Exception as e:
        logger.warning("search_episodes embed failed: %s", e)
        return []

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, title, summary, start_time, end_time,
                   entity_ids, fact_ids, source_types, fact_count,
                   1 - (summary_embedding <=> %s::vector) AS similarity
            FROM memory_episodes
            WHERE is_active = TRUE AND tenant_id = %s
              AND summary_embedding IS NOT NULL
            ORDER BY summary_embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, tid, embedding, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        r["source"] = "episode"
        # Normalize to look like a fact for downstream consumers.
        r["fact_text"] = f"{r['title']}: {r['summary']}"
        r["category"] = "episode"
        r["confidence"] = 0.8
    return rows
