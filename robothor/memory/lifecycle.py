"""
Lifecycle Management for Genus OS Memory System.

Handles memory decay, importance scoring, consolidation of similar facts,
intra-day consolidation, cross-domain insight discovery, and periodic
maintenance. Memories that are accessed frequently, reinforced,
or marked as important resist decay.

Architecture:
    Decay formula considers: recency, access frequency, reinforcement, importance
    Maintenance: score importance -> compute decay -> prune low-quality -> consolidate -> insights
    Intra-day: lightweight consolidation after each ingest run (threshold >= 5 unconsolidated)
    Insights: cross-domain connection discovery from recent diverse-category facts
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
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

# JSON schema for cross-domain insight discovery structured output.
INSIGHT_SCHEMA = {
    "type": "object",
    "properties": {
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "insight_text": {"type": "string"},
                    "source_fact_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": ["insight_text", "source_fact_ids"],
            },
        },
    },
    "required": ["insights"],
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


async def judge_importance(content: str, timeout_s: float = 30.0) -> float:
    """Use the LLM to judge the importance of a fact.

    Args:
        content: The fact text to evaluate.
        timeout_s: Maximum seconds to wait for LLM response.

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

        raw = await asyncio.wait_for(
            llm_client.generate(
                prompt=prompt,
                system="Rate the importance of this fact.",
                max_tokens=64,
                format=IMPORTANCE_SCHEMA,
                think=False,
            ),
            timeout=timeout_s,
        )

        parsed = json.loads(raw.strip())
        score = float(parsed.get("score", 0.5))
        return max(0.0, min(1.0, score))

    except TimeoutError:
        logger.warning("judge_importance timed out after %.0fs", timeout_s)
        return 0.5
    except Exception:
        return 0.5


async def find_consolidation_candidates(
    min_group_size: int = 3,
    similarity_threshold: float = 0.8,
    unconsolidated_only: bool = False,
) -> list[list[dict[str, Any]]]:
    """Find groups of similar facts that could be consolidated.

    Args:
        min_group_size: Minimum facts in a group to consider consolidation.
        similarity_threshold: Minimum cosine similarity to group facts.
        unconsolidated_only: When True, only consider facts where
            consolidated_at IS NULL. Uses smaller LIMIT (100 vs 500).

    Returns:
        List of groups, where each group is a list of fact dicts.
    """
    unconsolidated_filter = "AND consolidated_at IS NULL" if unconsolidated_only else ""
    fetch_limit = 100 if unconsolidated_only else 500

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"""
            SELECT id, fact_text, category, entities, embedding
            FROM memory_facts
            WHERE is_active = TRUE AND embedding IS NOT NULL
              {unconsolidated_filter}
            ORDER BY created_at DESC
            LIMIT {fetch_limit}
            """
        )
        facts = [dict(r) for r in cur.fetchall()]

    if len(facts) < min_group_size:
        return []

    used: set[int] = set()
    groups: list[list[dict[str, Any]]] = []

    for fact in facts:
        if fact["id"] in used:
            continue

        group = [fact]
        used.add(fact["id"])

        with get_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
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


async def consolidate_facts(fact_group: list[dict[str, Any]]) -> dict[str, Any]:
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
            think=False,
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


async def prune_low_quality_facts() -> dict[str, Any]:
    """Deactivate facts that are low-quality garbage.

    Targets:
        - decay_score < 0.1 AND importance_score < 0.3 AND access_count = 0
        - fact_text < 15 characters (garbage that got in before quality gate)

    Never prunes: decisions, preferences (category-protected).

    Returns:
        Dict with pruning statistics.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Prune short garbage
        cur.execute(
            """
            UPDATE memory_facts SET is_active = false, updated_at = NOW()
            WHERE is_active = true
              AND length(fact_text) < 15
              AND category NOT IN ('decision', 'preference')
            RETURNING id, fact_text
            """
        )
        short_pruned = cur.fetchall()

        # Prune decayed, unimportant, never-accessed facts
        cur.execute(
            """
            UPDATE memory_facts SET is_active = false, updated_at = NOW()
            WHERE is_active = true
              AND decay_score < 0.1
              AND importance_score < 0.3
              AND access_count = 0
              AND category NOT IN ('decision', 'preference')
            RETURNING id, fact_text
            """
        )
        decay_pruned = cur.fetchall()

    total = len(short_pruned) + len(decay_pruned)
    if total > 0:
        logger.info(
            "Pruned %d facts (%d short, %d decayed)", total, len(short_pruned), len(decay_pruned)
        )
        for f in (short_pruned + decay_pruned)[:5]:
            logger.info("  Pruned: %s", f["fact_text"][:80])

    return {
        "total_pruned": total,
        "short_pruned": len(short_pruned),
        "decay_pruned": len(decay_pruned),
    }


# ── Intra-Day Consolidation (P0) ─────────────────────────────────────────────


def get_unconsolidated_count() -> int:
    """Count active facts that haven't been consolidated yet."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM memory_facts WHERE is_active = TRUE AND consolidated_at IS NULL"
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _mark_facts_consolidated(fact_ids: list[int] | None = None) -> int:
    """Mark facts as consolidated by setting consolidated_at = NOW().

    Args:
        fact_ids: Specific fact IDs to mark. If None, marks all unconsolidated.

    Returns:
        Number of facts marked.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        if fact_ids:
            cur.execute(
                """
                UPDATE memory_facts SET consolidated_at = NOW()
                WHERE id = ANY(%s) AND consolidated_at IS NULL
                """,
                (fact_ids,),
            )
        else:
            cur.execute(
                """
                UPDATE memory_facts SET consolidated_at = NOW()
                WHERE is_active = TRUE AND consolidated_at IS NULL
                """
            )
        return int(cur.rowcount)


async def run_intraday_consolidation(threshold: int = 5) -> dict[str, Any]:
    """Run lightweight consolidation if enough unconsolidated facts exist.

    Called after each continuous_ingest run. Only merges similar facts —
    no importance scoring, no decay, no pruning.

    Args:
        threshold: Minimum unconsolidated facts required to trigger.

    Returns:
        Dict with consolidation stats, including 'skipped' flag.
    """
    count = get_unconsolidated_count()
    if count < threshold:
        return {"skipped": True, "unconsolidated_count": count, "threshold": threshold}

    logger.info("Intra-day consolidation triggered: %d unconsolidated facts", count)

    consolidation_groups = 0
    try:
        # Use min_group_size=2 for intra-day (smaller window than nightly's 3)
        groups = await find_consolidation_candidates(
            min_group_size=2,
            similarity_threshold=0.8,
            unconsolidated_only=True,
        )
        for group in groups:
            result = await consolidate_facts(group)
            if result and result.get("consolidated_text"):
                from robothor.memory.facts import store_fact

                consolidated_fact = {
                    "fact_text": result["consolidated_text"],
                    "category": group[0].get("category", "personal"),
                    "entities": list({e for f in group for e in (f.get("entities") or [])}),
                    "confidence": 0.9,
                }
                new_id = await store_fact(
                    consolidated_fact,
                    source_content="[intra-day consolidation]",
                    source_type="consolidation",
                )
                with get_connection() as conn:
                    cur = conn.cursor()
                    for source_id in result["source_ids"]:
                        cur.execute(
                            """
                            UPDATE memory_facts
                            SET is_active = FALSE, superseded_by = %s, updated_at = NOW()
                            WHERE id = %s AND is_active = TRUE
                            """,
                            (new_id, source_id),
                        )
                consolidation_groups += 1
    except Exception as e:
        logger.warning("Intra-day consolidation failed: %s", e)

    # Mark all remaining unconsolidated facts as consolidated
    marked = _mark_facts_consolidated()

    logger.info(
        "Intra-day consolidation complete: %d groups merged, %d facts marked",
        consolidation_groups,
        marked,
    )

    return {
        "skipped": False,
        "unconsolidated_count": count,
        "consolidation_groups": consolidation_groups,
        "facts_marked_consolidated": marked,
    }


# ── Cross-Domain Insight Discovery (P1) ──────────────────────────────────────


async def discover_cross_domain_insights(
    hours_back: int = 24,
    max_facts: int = 50,
) -> list[dict[str, Any]]:
    """Find non-obvious connections between facts from different categories.

    Selects recent facts ensuring category diversity (>= 2 categories,
    >= 3 facts), then asks the LLM to find cross-domain connections.

    Args:
        hours_back: How far back to look for recent facts.
        max_facts: Maximum facts to include in the LLM prompt.

    Returns:
        List of validated insight dicts with 'insight_text' and 'source_fact_ids'.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=hours_back)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, fact_text, category, entities
            FROM memory_facts
            WHERE is_active = TRUE AND created_at >= %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (cutoff, max_facts),
        )
        facts = [dict(r) for r in cur.fetchall()]

    if len(facts) < 3:
        logger.debug("Insight discovery: only %d facts, need >= 3", len(facts))
        return []

    categories = {f["category"] for f in facts}
    if len(categories) < 2:
        logger.debug("Insight discovery: only %d categories, need >= 2", len(categories))
        return []

    valid_ids = {f["id"] for f in facts}

    facts_block = "\n".join(f"[{f['id']}] ({f['category']}) {f['fact_text']}" for f in facts)

    prompt = f"""Below are recent facts from different domains. Find non-obvious connections
between facts from DIFFERENT categories. Look for:
- Patterns that span multiple topics
- Cause-effect relationships across domains
- Recurring themes connecting different areas

Facts:
{facts_block}

Return up to 3 insights. Each insight must:
- Reference at least 2 fact IDs from different categories
- Be a complete, specific observation (not generic)
- Be at least 20 characters long"""

    try:
        raw = await llm_client.generate(
            prompt=prompt,
            system="Find cross-domain connections between these facts.",
            max_tokens=512,
            format=INSIGHT_SCHEMA,
            think=False,
        )

        parsed = json.loads(raw.strip())
        raw_insights = parsed.get("insights", [])
    except Exception as e:
        logger.warning("Insight discovery LLM call failed: %s", e)
        return []

    # Validate insights
    validated = []
    for item in raw_insights[:3]:
        text = item.get("insight_text", "").strip()
        source_ids = item.get("source_fact_ids", [])

        if len(text) < 20:
            logger.debug("Rejected insight (too short): %s", text[:50])
            continue

        # Filter to only valid fact IDs
        valid_source_ids = [fid for fid in source_ids if fid in valid_ids]
        if len(valid_source_ids) < 2:
            logger.debug("Rejected insight (< 2 valid fact IDs): %s", text[:50])
            continue

        # Verify cross-category: source facts must span >= 2 categories
        source_categories = {f["category"] for f in facts if f["id"] in valid_source_ids}
        if len(source_categories) < 2:
            logger.debug("Rejected insight (single category): %s", text[:50])
            continue

        validated.append(
            {
                "insight_text": text,
                "source_fact_ids": valid_source_ids,
            }
        )

    return validated


async def _find_similar_insight(text: str, threshold: float = 0.85) -> bool:
    """Check if a similar insight already exists (cosine dedup).

    Args:
        text: Insight text to check.
        threshold: Minimum cosine similarity to consider a duplicate.

    Returns:
        True if a similar insight exists.
    """
    embedding = await llm_client.get_embedding_async(text)

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) FROM memory_insights
            WHERE is_active = TRUE
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> %s::vector) >= %s
            """,
            (embedding, threshold),
        )
        row = cur.fetchone()
        return bool(row and row[0] > 0)


async def store_insight(insight: dict[str, Any]) -> int | None:
    """Store a cross-domain insight with its embedding.

    Args:
        insight: Dict with 'insight_text' and 'source_fact_ids'.

    Returns:
        The database ID of the stored insight, or None if deduped.
    """
    text = insight["insight_text"]
    source_ids = insight["source_fact_ids"]

    # Dedup check
    if await _find_similar_insight(text):
        logger.debug("Insight deduped (similar exists): %s", text[:60])
        return None

    embedding = await llm_client.get_embedding_async(text)

    # Gather categories and entities from source facts
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT category, entities FROM memory_facts
            WHERE id = ANY(%s) AND is_active = TRUE
            """,
            (source_ids,),
        )
        source_facts = cur.fetchall()

    categories = list({r["category"] for r in source_facts})
    entities = list({e for r in source_facts for e in (r["entities"] or [])})

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_insights
            (insight_text, source_fact_ids, categories, entities, embedding)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (text, source_ids, categories, entities, embedding),
        )
        insight_id: int = cur.fetchone()[0]

    logger.info("Stored insight %d: %s", insight_id, text[:80])
    return insight_id


async def run_insight_discovery(hours_back: int = 24) -> dict[str, Any]:
    """Orchestrate cross-domain insight discovery: discover, dedup, store.

    Args:
        hours_back: How far back to look for recent facts.

    Returns:
        Dict with discovery stats.
    """
    insights = await discover_cross_domain_insights(hours_back=hours_back)
    if not insights:
        return {"discovered": 0, "stored": 0, "deduped": 0}

    stored = 0
    deduped = 0
    for insight in insights:
        insight_id = await store_insight(insight)
        if insight_id is not None:
            stored += 1
        else:
            deduped += 1

    logger.info(
        "Insight discovery: %d discovered, %d stored, %d deduped", len(insights), stored, deduped
    )
    return {"discovered": len(insights), "stored": stored, "deduped": deduped}


# ── Full Lifecycle Maintenance (Nightly) ──────────────────────────────────────


async def run_lifecycle_maintenance() -> dict[str, Any]:
    """Run full lifecycle maintenance on the fact store.

    Steps:
        1. Score importance for unscored facts (200 per run, 600s budget)
           - Fast-path: events older than 30 days auto-score 0.3
           - Each judge_importance() wrapped in try/except with 30s timeout
        2. Compute and update decay scores for all active facts
        3. Prune low-quality facts (garbage collection)
        4. Find and consolidate similar fact groups
        5. Sweep any remaining unconsolidated facts
        6. Cross-domain insight discovery (72h window)

    Returns:
        Dict with maintenance statistics.
    """
    step_timings: dict[str, float] = {}

    # Step 1: Score importance
    t0 = time.monotonic()
    scoring_budget_s = 600.0  # Wall-clock budget for importance scoring

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Fast-path: events older than 30 days auto-score 0.3 (skip LLM call)
        cur.execute(
            """
            UPDATE memory_facts SET importance_score = 0.3
            WHERE is_active = TRUE AND importance_score = 0.5
              AND category = 'event'
              AND created_at < NOW() - INTERVAL '30 days'
            """
        )
        auto_scored = cur.rowcount

        cur.execute(
            """
            SELECT id, fact_text FROM memory_facts
            WHERE is_active = TRUE AND importance_score = 0.5
            ORDER BY created_at DESC
            LIMIT 200
            """
        )
        unscored = cur.fetchall()
        facts_scored = 0
        facts_skipped_budget = 0

        for fact in unscored:
            # Check wall-clock budget
            elapsed = time.monotonic() - t0
            if elapsed > scoring_budget_s:
                facts_skipped_budget = len(unscored) - facts_scored
                logger.warning(
                    "Importance scoring budget exhausted (%.0fs): scored %d, skipping %d",
                    elapsed,
                    facts_scored,
                    facts_skipped_budget,
                )
                break
            try:
                score = await judge_importance(fact["fact_text"])
                cur.execute(
                    "UPDATE memory_facts SET importance_score = %s WHERE id = %s",
                    (score, fact["id"]),
                )
                facts_scored += 1
            except Exception as e:
                logger.warning("Failed to score fact %d: %s", fact["id"], e)

    step_timings["importance_scoring"] = time.monotonic() - t0
    logger.info(
        "Step 1 (importance): %d scored, %d auto, %d skipped (%.1fs)",
        facts_scored,
        auto_scored,
        facts_skipped_budget,
        step_timings["importance_scoring"],
    )

    # Unload generation model to free GPU for embedding model (used by later steps)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{llm_client._ollama_url()}/api/generate",
                json={"model": llm_client.GENERATION_MODEL, "keep_alive": 0},
            )
        logger.info("Unloaded generation model to free GPU for embeddings")
    except Exception as e:
        logger.warning("Failed to unload generation model: %s", e)

    # Step 2: Update decay scores
    t1 = time.monotonic()
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
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
            cur.execute(
                "UPDATE memory_facts SET decay_score = %s WHERE id = %s", (score, fact["id"])
            )
            decay_updated += 1

    step_timings["decay"] = time.monotonic() - t1
    logger.info("Step 2 (decay): %d updated (%.1fs)", decay_updated, step_timings["decay"])

    # Step 3: Prune low-quality facts
    t2 = time.monotonic()
    prune_result = await prune_low_quality_facts()
    step_timings["prune"] = time.monotonic() - t2
    logger.info(
        "Step 3 (prune): %d pruned (%.1fs)",
        prune_result.get("total_pruned", 0),
        step_timings["prune"],
    )

    # Step 4: Consolidation — two-phase to avoid model contention
    # Phase A: Find candidates (DB only) + generate consolidated text (LLM/chat)
    # Phase B: Unload generation model, then store with embeddings
    t3 = time.monotonic()
    consolidation_groups = 0
    pending_consolidations: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    try:
        groups = await find_consolidation_candidates(min_group_size=3, similarity_threshold=0.8)
        for group in groups:
            result = await consolidate_facts(group)
            if result and result.get("consolidated_text"):
                pending_consolidations.append((result, group))
    except Exception as e:
        logger.warning("Consolidation text generation failed: %s", e)

    # Step 4 + 6: Discover insights via LLM (also needs chat, do before model unload)
    t4 = time.monotonic()
    discovered_insights: list[dict[str, Any]] = []
    try:
        discovered_insights = await discover_cross_domain_insights(hours_back=72)
    except Exception as e:
        logger.warning("Insight discovery LLM phase failed: %s", e)

    # Unload generation model to free GPU for embedding-heavy storage
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{llm_client._ollama_url()}/api/generate",
                json={"model": llm_client.GENERATION_MODEL, "keep_alive": 0},
            )
        logger.info("Unloaded generation model for embedding phase")
        await asyncio.sleep(2)  # Brief pause for Ollama to release GPU
    except Exception as e:
        logger.warning("Failed to unload generation model: %s", e)

    # Phase B: Store consolidated facts (needs embeddings)
    for result, group in pending_consolidations:
        try:
            from robothor.memory.facts import store_fact

            consolidated_fact = {
                "fact_text": result["consolidated_text"],
                "category": group[0].get("category", "personal"),
                "entities": list({e for f in group for e in (f.get("entities") or [])}),
                "confidence": 0.9,
            }
            new_id = await store_fact(
                consolidated_fact,
                source_content="[consolidated from similar facts]",
                source_type="consolidation",
            )
            # Supersede originals
            with get_connection() as conn:
                cur = conn.cursor()
                for source_id in result["source_ids"]:
                    cur.execute(
                        """
                        UPDATE memory_facts
                        SET is_active = FALSE, superseded_by = %s, updated_at = NOW()
                        WHERE id = %s AND is_active = TRUE
                        """,
                        (new_id, source_id),
                    )
            consolidation_groups += 1
        except Exception as e:
            logger.warning("Failed to store consolidated fact: %s", e)

    step_timings["consolidation"] = time.monotonic() - t3
    logger.info(
        "Step 4 (consolidation): %d groups (%.1fs)",
        consolidation_groups,
        step_timings["consolidation"],
    )

    # Step 5: Sweep remaining unconsolidated facts (safety net)
    swept = _mark_facts_consolidated()
    if swept > 0:
        logger.info("Nightly sweep: marked %d remaining facts as consolidated", swept)

    # Step 6: Store discovered insights (needs embeddings, model already unloaded)
    insight_result: dict[str, Any] = {
        "discovered": len(discovered_insights),
        "stored": 0,
        "deduped": 0,
    }
    for insight in discovered_insights:
        try:
            insight_id = await store_insight(insight)
            if insight_id is not None:
                insight_result["stored"] += 1
            else:
                insight_result["deduped"] += 1
        except Exception as e:
            logger.warning("Failed to store insight: %s", e)
    step_timings["insights"] = time.monotonic() - t4
    logger.info("Step 6 (insights): %s (%.1fs)", insight_result, step_timings["insights"])

    total_time = time.monotonic() - t0
    logger.info("Lifecycle maintenance complete in %.1fs: %s", total_time, step_timings)

    return {
        "facts_scored": facts_scored,
        "auto_scored": auto_scored,
        "facts_skipped_budget": facts_skipped_budget,
        "decay_updated": decay_updated,
        "facts_pruned": prune_result.get("total_pruned", 0),
        "consolidation_groups": consolidation_groups,
        "unconsolidated_swept": swept,
        "insights": insight_result,
        "step_timings": step_timings,
    }
