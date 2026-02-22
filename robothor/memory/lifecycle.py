"""
Lifecycle Management for Robothor Memory System.

Handles memory decay, importance scoring, consolidation of similar facts,
and periodic maintenance. Memories that are accessed frequently, reinforced,
or marked as important resist decay.

Architecture:
    Decay formula considers: recency, access frequency, reinforcement, importance
    Maintenance: score importance -> compute decay -> consolidate similar -> prune
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client

logger = logging.getLogger(__name__)

# JSON schema for importance scoring structured output.
IMPORTANCE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
    },
    "required": ["score"],
}


def compute_decay_score(
    last_accessed: datetime,
    access_count: int,
    reinforcement_count: int,
    importance_score: float,
) -> float:
    """Compute a decay score for a memory fact.

    The score represents how "alive" a memory is. Higher = more relevant.
    Recent, frequently accessed, reinforced, and important memories score higher.

    Formula:
        recency = exp(-hours_since_access / half_life)
        access_boost = log(1 + access_count) / 5, capped at 0.3
        reinforcement_boost = log(1 + reinforcement_count) / 5, capped at 0.2
        importance_floor = importance_score * 0.4
        score = max(importance_floor, recency) + access_boost + reinforcement_boost
        clamped to [0.0, 1.0]

    Args:
        last_accessed: When the memory was last accessed.
        access_count: Number of times the memory has been accessed.
        reinforcement_count: Number of times the memory was reinforced.
        importance_score: LLM-judged importance (0.0-1.0).

    Returns:
        Decay score between 0.0 and 1.0.
    """
    now = datetime.now(UTC)

    if last_accessed.tzinfo is None:
        last_accessed = last_accessed.replace(tzinfo=UTC)

    hours_since = max((now - last_accessed).total_seconds() / 3600, 0)

    # Exponential decay with half-life of 72 hours (3 days)
    half_life = 72.0
    recency = math.exp(-hours_since * math.log(2) / half_life)

    access_boost = min(math.log(1 + access_count) / 5.0, 0.3)
    reinforcement_boost = min(math.log(1 + reinforcement_count) / 5.0, 0.2)
    importance_floor = importance_score * 0.4

    base = max(recency, importance_floor)
    score = base + access_boost + reinforcement_boost

    return max(0.0, min(1.0, score))


async def judge_importance(content: str) -> float:
    """Use the LLM to judge the importance of a fact.

    Args:
        content: The fact text to evaluate.

    Returns:
        Importance score between 0.0 and 1.0.
    """
    try:
        prompt = f"""Rate the long-term importance of this fact on a scale of 0.0 to 1.0.

- 0.0-0.2: Trivial (weather, casual chat)
- 0.3-0.5: Mildly useful (routine info)
- 0.6-0.8: Important (decisions, preferences, project info)
- 0.9-1.0: Critical (security, identity, relationships)

Fact: "{content}" """

        raw = await llm_client.generate(
            prompt=prompt,
            system="Rate the importance of this fact.",
            max_tokens=64,
            format=IMPORTANCE_SCHEMA,
        )

        parsed = json.loads(raw.strip())
        score = float(parsed.get("score", 0.5))
        return max(0.0, min(1.0, score))

    except Exception:
        return 0.5


async def find_consolidation_candidates(
    min_group_size: int = 3,
    similarity_threshold: float = 0.8,
) -> list[list[dict]]:
    """Find groups of similar facts that could be consolidated.

    Args:
        min_group_size: Minimum facts in a group to consider consolidation.
        similarity_threshold: Minimum cosine similarity to group facts.

    Returns:
        List of groups, where each group is a list of fact dicts.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, fact_text, category, entities, embedding
            FROM memory_facts
            WHERE is_active = TRUE AND embedding IS NOT NULL
            ORDER BY created_at DESC
            LIMIT 500
            """
        )
        facts = [dict(r) for r in cur.fetchall()]

    if len(facts) < min_group_size:
        return []

    used: set[int] = set()
    groups: list[list[dict]] = []

    for fact in facts:
        if fact["id"] in used:
            continue

        group = [fact]
        used.add(fact["id"])

        with get_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SET ivfflat.probes = 10")
            cur.execute(
                """
                SELECT id, fact_text, category, entities,
                       1 - (embedding <=> %s::vector) as similarity
                FROM memory_facts
                WHERE is_active = TRUE
                  AND embedding IS NOT NULL
                  AND id != %s
                ORDER BY embedding <=> %s::vector
                LIMIT 10
                """,
                (fact["embedding"], fact["id"], fact["embedding"]),
            )

            for similar in cur.fetchall():
                similar = dict(similar)
                if similar["id"] not in used and similar["similarity"] >= similarity_threshold:
                    group.append(similar)
                    used.add(similar["id"])

        if len(group) >= min_group_size:
            groups.append(group)

    return groups


async def consolidate_facts(fact_group: list[dict]) -> dict:
    """Consolidate a group of similar facts into one summary fact.

    Args:
        fact_group: List of similar fact dicts.

    Returns:
        Dict with 'consolidated_text' and 'source_ids'.
    """
    facts_text = "\n".join(f"- {f['fact_text']}" for f in fact_group)

    prompt = f"""These facts are about the same topic. Combine them into a single, comprehensive statement.

Facts:
{facts_text}

Return ONLY the consolidated statement, nothing else."""

    try:
        consolidated = await llm_client.generate(
            prompt=prompt,
            system="Combine these facts into a single statement.",
            max_tokens=256,
        )
        return {
            "consolidated_text": consolidated.strip(),
            "source_ids": [f["id"] for f in fact_group],
        }
    except Exception:
        return {
            "consolidated_text": fact_group[0]["fact_text"],
            "source_ids": [f["id"] for f in fact_group],
        }


async def run_lifecycle_maintenance() -> dict:
    """Run full lifecycle maintenance on the fact store.

    Steps:
        1. Score importance for unscored facts
        2. Compute and update decay scores for all active facts

    Returns:
        Dict with maintenance statistics.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Step 1: Score importance for facts with default importance (0.5)
        cur.execute(
            """
            SELECT id, fact_text FROM memory_facts
            WHERE is_active = TRUE AND importance_score = 0.5
            ORDER BY created_at DESC
            LIMIT 50
            """
        )
        unscored = cur.fetchall()
        facts_scored = 0

        for fact in unscored:
            score = await judge_importance(fact["fact_text"])
            cur.execute("UPDATE memory_facts SET importance_score = %s WHERE id = %s", (score, fact["id"]))
            facts_scored += 1

        # Step 2: Update decay scores
        cur.execute(
            """
            SELECT id, last_accessed, access_count, reinforcement_count, importance_score
            FROM memory_facts
            WHERE is_active = TRUE
            """
        )
        all_facts = cur.fetchall()
        decay_updated = 0

        for fact in all_facts:
            score = compute_decay_score(
                last_accessed=fact["last_accessed"],
                access_count=fact["access_count"],
                reinforcement_count=fact["reinforcement_count"],
                importance_score=fact["importance_score"],
            )
            cur.execute("UPDATE memory_facts SET decay_score = %s WHERE id = %s", (score, fact["id"]))
            decay_updated += 1

    return {
        "facts_scored": facts_scored,
        "decay_updated": decay_updated,
    }
