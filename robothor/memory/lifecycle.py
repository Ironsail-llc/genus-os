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
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection, tenant_scope
from robothor.llm import ollama as llm_client
from robothor.memory import generation
from robothor.memory.confirmed import NOT_OPERATOR_CONFIRMED_SQL
from robothor.memory.vector_tuning import apply_hnsw_session

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

# WS-2: consolidation churn guard. Default OFF (observe). When on:
#   - find_consolidation_candidates excludes source_type='consolidation' rows
#     so a consolidated fact is never re-consolidated (the 150-deep chains and
#     ~80%-inactive table came from consolidation re-eating its own output);
#   - the consolidated fact inherits MAX(source importance) instead of the 0.5
#     default (so it doesn't bury its high-importance sources);
#   - a chain-depth cap stops any single supersession chain from running deep.
_MAX_CHAIN_DEPTH = 3


def _consolidation_guard_enabled() -> bool:
    raw = os.environ.get("MEMORY_CONSOLIDATION_GUARD", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _intent_populate_enabled() -> bool:
    """WS-6: run intent inference in nightly maintenance. Default OFF.

    RIP 14 (intents) is enabled live but the store is empty — nothing calls
    infer_intents_from_facts. When on, the nightly pass proposes standing
    intents from recent facts so the recall leg has data to return.
    """
    raw = os.environ.get("MEMORY_INTENT_POPULATE", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _chain_depth(cur: Any, fact_id: int) -> int:
    """Depth of the supersession chain that has collapsed INTO ``fact_id``.

    Walks ``superseded_by`` backwards: how many facts were transitively
    superseded into this one. Used to refuse extending an already-deep chain.
    """
    cur.execute(
        """
        WITH RECURSIVE chain(id, depth) AS (
            SELECT id, 0 FROM memory_facts WHERE id = %s
            UNION ALL
            SELECT m.id, c.depth + 1
            FROM memory_facts m JOIN chain c ON m.superseded_by = c.id
            WHERE c.depth < 200
        )
        SELECT COALESCE(MAX(depth), 0) FROM chain
        """,
        (fact_id,),
    )
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _apply_consolidation_supersession(new_id: int, source_ids: list[int]) -> None:
    """Supersede ``source_ids`` into the consolidated ``new_id``.

    With the guard off this is the original loop (set is_active=false,
    superseded_by=new_id). With the guard on it also (a) propagates the max
    source importance onto the consolidated fact and (b) skips any source whose
    chain is already at ``_MAX_CHAIN_DEPTH`` so chains stay bounded.
    """
    guard = _consolidation_guard_enabled()
    with get_connection() as conn:
        cur = conn.cursor()
        if guard:
            cur.execute(
                "SELECT COALESCE(MAX(importance_score), 0.5) FROM memory_facts WHERE id = ANY(%s)",
                (list(source_ids),),
            )
            row = cur.fetchone()
            max_imp = float(row[0]) if row and row[0] is not None else 0.5
            cur.execute(
                "UPDATE memory_facts SET importance_score = GREATEST(importance_score, %s) "
                "WHERE id = %s",
                (max_imp, new_id),
            )
        for source_id in source_ids:
            if guard and _chain_depth(cur, source_id) >= _MAX_CHAIN_DEPTH:
                logger.debug("consolidation: skip supersede of deep-chain fact %s", source_id)
                continue
            # Re-checked here: selection and this write are separated by the
            # LLM call, and a fact confirmed in that window must survive.
            cur.execute(
                f"""
                UPDATE memory_facts
                SET is_active = FALSE, superseded_by = %s, updated_at = NOW()
                WHERE id = %s AND is_active = TRUE AND {NOT_OPERATOR_CONFIRMED_SQL}
                """,
                (new_id, source_id),
            )


#: Half-life of the recency term, from the last use.
RECENCY_HALF_LIFE_HOURS = 72.0
#: Half-life of the access boost, from the last use. Use fades: one recall long
#: ago leaves a fact where it would be had it never been recalled.
ACCESS_BOOST_HALF_LIFE_DAYS = 30.0
#: A fact last used longer ago than this counts as unused for pruning (three
#: access-boost half-lives: at most 1/8 of the boost is left).
PRUNE_RECENT_USE_DAYS = 90


def access_boost(access_count: int, *, days_since_use: float) -> float:
    """``min(ln(1 + count) / 5, 0.3)``, halved every ACCESS_BOOST_HALF_LIFE_DAYS."""
    if access_count <= 0:
        return 0.0
    full = min(math.log(1 + access_count) / 5.0, 0.3)
    return full * math.pow(0.5, max(days_since_use, 0.0) / ACCESS_BOOST_HALF_LIFE_DAYS)


def compute_decay_score(
    last_accessed: datetime,
    access_count: int,
    reinforcement_count: int,
    importance_score: float,
    outcome_failures: int = 0,
    utility: float | None = None,
) -> float:
    """Compute a decay score for a memory fact.

    The score represents how "alive" a memory is. Higher = more relevant::

        max(recency, 0.4 * importance) + access_boost + reinforcement_boost
            + utility_decay_term(utility)

    clamped to 0..1. Recency halves every 72 hours since the last use and the
    access boost every 30 days, so use fades. Utility is one signed, bounded
    term (``robothor.memory.utility``): positive for facts that helped runs,
    negative — capped at 0.4, as the failure penalty it replaces was — for
    facts behind failed runs.

    Args:
        last_accessed: When the memory was last used.
        access_count: Number of times recall returned the memory.
        reinforcement_count: Number of times the memory was reinforced.
        importance_score: LLM-judged importance (0.0-1.0).
        outcome_failures: Failed runs that recalled this fact. Read only when
            ``utility`` is None (failures alone, no successes).
        utility: The fact's outcome utility in [-1, 1].

    Returns:
        Decay score between 0.0 and 1.0.
    """
    from robothor.memory.utility import compute_utility, utility_decay_term

    now = datetime.now(UTC)

    if last_accessed.tzinfo is None:
        last_accessed = last_accessed.replace(tzinfo=UTC)

    hours_since = max((now - last_accessed).total_seconds() / 3600, 0)
    recency = math.pow(0.5, hours_since / RECENCY_HALF_LIFE_HOURS)

    use_boost = access_boost(access_count, days_since_use=hours_since / 24.0)
    reinforcement_boost = min(math.log(1 + reinforcement_count) / 5.0, 0.2)
    importance_floor = importance_score * 0.4

    if utility is None:
        utility = compute_utility(citations=0, failures=outcome_failures)

    base = max(recency, importance_floor)
    score = base + use_boost + reinforcement_boost + utility_decay_term(utility)

    return max(0.0, min(1.0, score))


# High-stakes cues the LLM judge (or the 0.5 default on timeout) under-scores:
# security incidents and resolutions must not sit at neutral importance, or the
# blend's importance term can't lift them above routine noise (WS-7).
_HIGH_SIGNAL_CUES = (
    "unauthorized",
    "suspicious",
    "breach",
    "compromise",
    "unrecognized",
    "security alert",
    "confirmed legitimate",
    "marked as closed",
    "false alarm",
    "resolved",
)
_HIGH_SIGNAL_FLOOR = 0.7


def importance_floor(content: str) -> float:
    """Deterministic minimum importance for clearly high-stakes facts (pure)."""
    low = (content or "").lower()
    return _HIGH_SIGNAL_FLOOR if any(cue in low for cue in _HIGH_SIGNAL_CUES) else 0.0


async def judge_importance(content: str, timeout_s: float = 30.0) -> float:
    """Use the LLM to judge the importance of a fact.

    Args:
        content: The fact text to evaluate.
        timeout_s: Maximum seconds to wait for LLM response.

    Returns:
        Importance score between 0.0 and 1.0. A deterministic floor is applied
        for high-stakes facts so a security/resolution fact never scores neutral.
    """
    floor = importance_floor(content)
    try:
        prompt = f"""Rate the long-term importance of this fact on a scale of 0.0 to 1.0.

- 0.0-0.2: Trivial (weather, casual chat)
- 0.3-0.5: Mildly useful (routine info)
- 0.6-0.8: Important (decisions, preferences, project info)
- 0.9-1.0: Critical (security, identity, relationships)

Fact: "{content}" """

        raw = await asyncio.wait_for(
            generation.generate(
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
        return max(floor, min(1.0, score))

    except TimeoutError:
        logger.warning("judge_importance timed out after %.0fs", timeout_s)
        return max(floor, 0.5)
    except Exception:
        return max(floor, 0.5)


async def find_consolidation_candidates(
    min_group_size: int = 3,
    similarity_threshold: float = 0.8,
    unconsolidated_only: bool = False,
    *,
    tenant_id: str = "",
) -> list[list[dict[str, Any]]]:
    """Find groups of one tenant's similar facts that could be consolidated.

    Args:
        min_group_size: Minimum facts in a group to consider consolidation.
        similarity_threshold: Minimum cosine similarity to group facts.
        unconsolidated_only: When True, only consider facts where
            consolidated_at IS NULL. Uses smaller LIMIT (100 vs 500).
        tenant_id: The tenant whose facts are grouped. A group never spans
            two tenants, because a consolidated fact is written to one.

    Returns:
        List of groups, where each group is a list of fact dicts. An
        operator-confirmed fact is never in one: consolidation would retire it.
        Every fact of a group has the same tenant and the same scope: a
        consolidated row is never derived across tenants or across readers.
    """
    unconsolidated_filter = "AND consolidated_at IS NULL" if unconsolidated_only else ""
    # WS-2: never feed a consolidation output back into consolidation — that is
    # the loop that built the 150-deep chains and the ~80%-inactive table.
    consolidation_filter = (
        "AND source_type <> 'consolidation'" if _consolidation_guard_enabled() else ""
    )
    fetch_limit = 100 if unconsolidated_only else 500
    _tenant = tenant_id or DEFAULT_TENANT

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"""
            SELECT id, fact_text, category, entities, embedding, tenant_id, origin,
                   scope_kind, scope_id
            FROM memory_facts
            WHERE is_active = TRUE AND embedding IS NOT NULL
              AND tenant_id = %s
              AND {NOT_OPERATOR_CONFIRMED_SQL}
              {unconsolidated_filter}
              {consolidation_filter}
            ORDER BY created_at DESC
            LIMIT {fetch_limit}
            """,
            (_tenant,),
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
            apply_hnsw_session(cur)
            cur.execute(
                f"""
                SELECT id, fact_text, category, entities, tenant_id, origin,
                       scope_kind, scope_id,
                       1 - (embedding <=> %s::vector) as similarity
                FROM memory_facts
                WHERE is_active = TRUE
                  AND embedding IS NOT NULL
                  AND id != %s
                  AND tenant_id = %s
                  AND scope_kind = %s
                  AND scope_id IS NOT DISTINCT FROM %s
                  AND {NOT_OPERATOR_CONFIRMED_SQL}
                  {consolidation_filter}
                ORDER BY embedding <=> %s::vector
                LIMIT 10
                """,
                (
                    fact["embedding"],
                    fact["id"],
                    _tenant,
                    fact.get("scope_kind") or "shared",
                    fact.get("scope_id"),
                    fact["embedding"],
                ),
            )

            for similar in cur.fetchall():
                similar = dict(similar)
                if similar["id"] not in used and similar["similarity"] >= similarity_threshold:
                    group.append(similar)
                    used.add(similar["id"])

        if len(group) >= min_group_size:
            groups.append(group)

    return groups


async def store_consolidated(group: list[dict[str, Any]], text: str) -> int:
    """Store the consolidation of ``group`` through the write path and retire the group.

    The new fact takes the group's tenant and scope (every member shares them,
    see :func:`find_consolidation_candidates`; a mixed group is refused) and the
    LEAST trusted origin among the members, so consolidating an email with what
    a person said does not launder the email. It inherits the members' mentions
    and becomes evidence of every edge they supported; once the members are
    superseded, those edges' origins are recomputed from what still supports
    them. Returns the new fact's id, or 0 when nothing was stored.
    """
    from robothor.memory.write_path import recompute_derived_edges, write_derived_fact

    consolidated_fact = {
        "fact_text": text,
        "category": group[0].get("category", "personal"),
        "entities": list({e for f in group for e in (f.get("entities") or [])}),
        "confidence": 0.9,
    }
    new_id, edges = await write_derived_fact(
        consolidated_fact,
        group,
        source_content="[consolidated from similar facts]",
        source_type="consolidation",
    )
    if not new_id:
        return 0
    # Supersede originals (guarded: importance propagation + chain cap)
    _apply_consolidation_supersession(new_id, [int(f["id"]) for f in group])
    await recompute_derived_edges(group[0].get("tenant_id") or "", edges)
    return new_id


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
        consolidated = await generation.generate(
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


async def prune_low_quality_facts(*, tenant_id: str = "") -> dict[str, Any]:
    """Deactivate one tenant's facts that are low-quality garbage.

    Targets:
        - decay_score < 0.1 AND importance_score < 0.3 AND not used recently
        - fact_text < 15 characters (garbage that got in before quality gate)

    "Not used recently" means never recalled, or last recalled more than
    PRUNE_RECENT_USE_DAYS ago. Use reads the live ``access_count`` /
    ``last_accessed`` (raised whenever the search tool or warm-up returns the
    fact) and migration 094's shadow columns (history recovered from
    fact_access_log), so a fact agents use is not retired on age alone — and a
    fact recalled once long ago is not protected forever. Positive utility
    protects in proportion: it lifts ``decay_score`` by up to 0.3, so a fact
    that has helped runs stays above the 0.1 line.

    Never prunes: decisions, preferences, resolutions (category-protected), or
    an operator-confirmed fact.

    Returns:
        Dict with pruning statistics.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Prune short garbage
        cur.execute(
            f"""
            UPDATE memory_facts SET is_active = false, updated_at = NOW()
            WHERE is_active = true
              AND tenant_id = %s
              AND length(fact_text) < 15
              AND category NOT IN ('decision', 'preference', 'resolution')
              AND {NOT_OPERATOR_CONFIRMED_SQL}
            RETURNING id, fact_text
            """,
            (_tenant,),
        )
        short_pruned = cur.fetchall()

        # Prune decayed, unimportant facts not used recently enough to matter
        cur.execute(
            f"""
            UPDATE memory_facts SET is_active = false, updated_at = NOW()
            WHERE is_active = true
              AND tenant_id = %s
              AND decay_score < 0.1
              AND importance_score < 0.3
              AND ({_USE_COUNT_SQL} = 0
                   OR {_LAST_USED_SQL} < NOW() - make_interval(days => %s))
              AND category NOT IN ('decision', 'preference', 'resolution')
              AND {NOT_OPERATOR_CONFIRMED_SQL}
            RETURNING id, fact_text
            """,
            (_tenant, PRUNE_RECENT_USE_DAYS),
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


def get_unconsolidated_count(tenant_id: str = "") -> int:
    """Count one tenant's active facts that haven't been consolidated yet."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM memory_facts "
            "WHERE is_active = TRUE AND consolidated_at IS NULL AND tenant_id = %s",
            (tenant_id or DEFAULT_TENANT,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _mark_facts_consolidated(fact_ids: list[int] | None = None, tenant_id: str = "") -> int:
    """Mark one tenant's facts as consolidated by setting consolidated_at = NOW().

    Args:
        fact_ids: Specific fact IDs to mark. If None, marks all unconsolidated.
        tenant_id: The tenant whose facts are marked.

    Returns:
        Number of facts marked.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        if fact_ids:
            cur.execute(
                """
                UPDATE memory_facts SET consolidated_at = NOW()
                WHERE id = ANY(%s) AND consolidated_at IS NULL AND tenant_id = %s
                """,
                (fact_ids, _tenant),
            )
        else:
            cur.execute(
                """
                UPDATE memory_facts SET consolidated_at = NOW()
                WHERE is_active = TRUE AND consolidated_at IS NULL AND tenant_id = %s
                """,
                (_tenant,),
            )
        return int(cur.rowcount)


async def run_intraday_consolidation(threshold: int = 5, *, tenant_id: str = "") -> dict[str, Any]:
    """Run lightweight consolidation if enough unconsolidated facts exist.

    Called after each continuous_ingest run. Only merges similar facts —
    no importance scoring, no decay, no pruning.

    Args:
        threshold: Minimum unconsolidated facts required to trigger.
        tenant_id: The tenant consolidated; the merged fact is written to it.

    Returns:
        Dict with consolidation stats, including 'skipped' flag.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    count = get_unconsolidated_count(_tenant)
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
            tenant_id=_tenant,
        )
        for group in groups:
            result = await consolidate_facts(group)
            if (
                result
                and result.get("consolidated_text")
                and await store_consolidated(group, result["consolidated_text"])
            ):
                consolidation_groups += 1
    except Exception as e:
        logger.warning("Intra-day consolidation failed: %s", e)

    # Mark all remaining unconsolidated facts as consolidated
    marked = _mark_facts_consolidated(tenant_id=_tenant)

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


# ── Cross-Entity Relationship Inference ───────────────────────────────────────


async def infer_entity_relationships(
    min_mentions: int = 2,
    max_relations: int = 1,
    max_pairs: int = 20,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Orchestrate cross-fact entity relationship inference inside one tenant."""
    from robothor.memory.entities import (
        find_cooccurring_entity_pairs,
        find_underconnected_entities,
        infer_relations,
    )

    _tenant = tenant_id or DEFAULT_TENANT
    underconnected = await find_underconnected_entities(
        min_mentions, max_relations, tenant_id=_tenant
    )
    if not underconnected:
        logger.info("No underconnected entities found for relationship inference")
        return []

    entity_ids = [e["id"] for e in underconnected]
    pairs = await find_cooccurring_entity_pairs(entity_ids, tenant_id=_tenant)
    if not pairs:
        logger.info("No co-occurring entity pairs found")
        return []

    # Enrich pairs with shared fact text for LLM context
    for pair in pairs[:max_pairs]:
        fact_ids = pair.get("shared_fact_ids", [])
        if fact_ids:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT fact_text FROM memory_facts WHERE id = ANY(%s) AND tenant_id = %s",
                    (fact_ids[:5], _tenant),
                )
                pair["shared_facts_text"] = [row[0] for row in cur.fetchall()]
        else:
            pair["shared_facts_text"] = []

    logger.info(
        "Inferring relations for %d entity pairs (from %d underconnected entities)",
        min(len(pairs), max_pairs),
        len(underconnected),
    )

    return await infer_relations(pairs[:max_pairs], tenant_id=_tenant)


# ── Cross-Domain Insight Discovery (P1) ──────────────────────────────────────


async def discover_cross_domain_insights(
    hours_back: int = 24,
    max_facts: int = 50,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Find non-obvious connections between one tenant's facts from different categories.

    Selects recent facts ensuring category diversity (>= 2 categories,
    >= 3 facts), then asks the LLM to find cross-domain connections.

    Args:
        hours_back: How far back to look for recent facts.
        max_facts: Maximum facts to include in the LLM prompt.
        tenant_id: The tenant whose facts are read.

    Returns:
        List of validated insight dicts with 'insight_text' and 'source_fact_ids'.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    cutoff = datetime.now(UTC) - timedelta(hours=hours_back)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, fact_text, category, entities
            FROM memory_facts
            WHERE is_active = TRUE AND created_at >= %s AND tenant_id = %s
              AND scope_kind = 'shared'  -- derived for every reader: shared facts only (memory/visibility.py)
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (cutoff, _tenant, max_facts),
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
        raw = await generation.generate(
            prompt=prompt,
            system="Find cross-domain connections between these facts.",
            # 512 truncated this the same way 1024 truncated extraction: the
            # parse errors are all at the ceiling ("Unterminated string ...
            # char 1482/1587", ~512 tokens of JSON), which is truncation
            # mid-object, not a model that answered badly.
            max_tokens=generation.INSIGHT_MAX_TOKENS,
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


async def _find_similar_insight(text: str, threshold: float = 0.85, *, tenant_id: str = "") -> bool:
    """Check if a similar insight already exists in this tenant (cosine dedup).

    Args:
        text: Insight text to check.
        threshold: Minimum cosine similarity to consider a duplicate.
        tenant_id: The tenant searched. Another tenant's insight never
            suppresses this one's.

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
              AND tenant_id = %s
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> %s::vector) >= %s
            """,
            (tenant_id or DEFAULT_TENANT, embedding, threshold),
        )
        row = cur.fetchone()
        return bool(row and row[0] > 0)


async def store_insight(insight: dict[str, Any], *, tenant_id: str = "") -> int | None:
    """Store a cross-domain insight with its embedding, under its facts' tenant.

    Args:
        insight: Dict with 'insight_text' and 'source_fact_ids'.
        tenant_id: The tenant the source facts were read from. The insight is
            written to it, never left to the column default.

    Returns:
        The database ID of the stored insight, or None if deduped.
    """
    text = insight["insight_text"]
    source_ids = insight["source_fact_ids"]
    _tenant = tenant_id or DEFAULT_TENANT

    # Dedup check
    if await _find_similar_insight(text, tenant_id=_tenant):
        logger.debug("Insight deduped (similar exists): %s", text[:60])
        return None

    embedding = await llm_client.get_embedding_async(text)

    # Gather categories and entities from source facts
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT category, entities FROM memory_facts
            WHERE id = ANY(%s) AND is_active = TRUE AND tenant_id = %s
            """,
            (source_ids, _tenant),
        )
        source_facts = cur.fetchall()

    categories = list({r["category"] for r in source_facts})
    entities = list({e for r in source_facts for e in (r["entities"] or [])})

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_insights
            (insight_text, source_fact_ids, categories, entities, embedding, tenant_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (text, source_ids, categories, entities, embedding, _tenant),
        )
        insight_id: int = cur.fetchone()[0]

    logger.info("Stored insight %d: %s", insight_id, text[:80])
    return insight_id


async def run_insight_discovery(hours_back: int = 24, *, tenant_id: str = "") -> dict[str, Any]:
    """Orchestrate cross-domain insight discovery in one tenant: discover, dedup, store.

    Args:
        hours_back: How far back to look for recent facts.
        tenant_id: The tenant whose facts are read and whose insights are written.

    Returns:
        Dict with discovery stats.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    insights = await discover_cross_domain_insights(hours_back=hours_back, tenant_id=_tenant)
    if not insights:
        return {"discovered": 0, "stored": 0, "deduped": 0}

    stored = 0
    deduped = 0
    for insight in insights:
        insight_id = await store_insight(insight, tenant_id=_tenant)
        if insight_id is not None:
            stored += 1
        else:
            deduped += 1

    logger.info(
        "Insight discovery: %d discovered, %d stored, %d deduped", len(insights), stored, deduped
    )
    return {"discovered": len(insights), "stored": stored, "deduped": deduped}


# ── Full Lifecycle Maintenance (Nightly) ──────────────────────────────────────


_LIFECYCLE_LOCK_KEY = "robothor:lifecycle:maintenance_lock"
_LIFECYCLE_LOCK_TTL = 1800  # 30 minutes — prevents concurrent maintenance runs

# autoDream generation-model unload gate.
#
# The deep maintenance pass used to unconditionally evict the generation
# model twice via Ollama's keep_alive=0 ("free GPU pressure" for the
# embedding-heavy steps that follow). That rationale predates the current
# unified-memory hardware (GB10, tens of GiB free at idle) and on 2026-08-18
# the eviction raced concurrent chat traffic reloading ~30GiB, fast-failing
# the embedding endpoint (503) 307 times in 23 minutes. The unload now only
# fires when system memory is measurably under pressure.
_MEMINFO_PATH = "/proc/meminfo"
_DEFAULT_UNLOAD_BELOW_GB = 24.0


def _available_memory_gb() -> float | None:
    """Read available system memory (GiB) from /proc/meminfo.

    Returns None when it can't be determined (missing file, unexpected
    format, non-Linux host) — callers must treat that as "unknown" and
    skip the unload rather than guess.
    """
    try:
        with Path(_MEMINFO_PATH).open() as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb / (1024 * 1024)
    except Exception as e:
        logger.debug("Could not read %s: %s", _MEMINFO_PATH, e)
    return None


def _should_unload_generation_model() -> tuple[bool, float | None, float]:
    """Decide whether autoDream should unload the generation model.

    Returns (should_unload, available_gb, threshold_gb). Unloads only when
    available memory is measurably below threshold_gb (env-tunable via
    ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB, default 24GiB). When availability
    can't be determined, defaults to NOT unloading.
    """
    raw_threshold = os.environ.get(
        "ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB", str(_DEFAULT_UNLOAD_BELOW_GB)
    )
    try:
        threshold_gb = float(raw_threshold)
    except ValueError:
        logger.warning(
            "Invalid ROBOTHOR_AUTODREAM_UNLOAD_BELOW_GB=%r, using default %.1f",
            raw_threshold,
            _DEFAULT_UNLOAD_BELOW_GB,
        )
        threshold_gb = _DEFAULT_UNLOAD_BELOW_GB
    available_gb = _available_memory_gb()
    if available_gb is None:
        return False, None, threshold_gb
    return available_gb < threshold_gb, available_gb, threshold_gb


async def _perform_generation_model_unload(pause_after_s: float = 0.0) -> None:
    """Unload the generation model via Ollama keep_alive=0, gated on memory pressure.

    Never targets the embedding model — a config collision there would evict
    the model a sibling fix pins resident, so that case is refused with a
    loud log and a skip (never an exception: a crash here would abort the
    whole nightly deep pass, and ``assert`` would vanish under ``python -O``).
    """
    should_unload, available_gb, threshold_gb = _should_unload_generation_model()
    if not should_unload:
        if available_gb is not None:
            logger.info("autoDream: skipping model unload, %.1fGiB available", available_gb)
        else:
            logger.info("autoDream: skipping model unload, memory availability unknown")
        return

    target_model = llm_client.GENERATION_MODEL
    embedding_model = llm_client._embedding_model()
    if target_model == embedding_model:
        logger.error(
            "autoDream refused to unload %r: it matches the embedding model "
            "(check ROBOTHOR_GENERATION_MODEL / embedding config)",
            target_model,
        )
        return

    # The fleet's offline tier is the model `config._with_last_resort` appends
    # to EVERY agent's chain. Evicting it during memory pressure would take the
    # whole fleet's fallback away at precisely the moment it is carrying the
    # fleet — and memory pressure is exactly what a resident 27B creates.
    #
    # Latent rather than theoretical: GENERATION_MODEL defaults to qwen3:8b,
    # but detect_generation_model()'s final fallback matches "any qwen3 that is
    # not an embedder or reranker", which matches qwen3.8:27b. One env var or
    # one caller away. Compared canonically so the ollama_chat/ routing prefix
    # cannot defeat the check. Logged loudly and returned, never raised: an
    # exception here would abort the whole nightly pass.
    last_resort = os.environ.get("ROBOTHOR_LAST_RESORT_MODEL", "").strip()
    if last_resort:
        try:
            from robothor.engine.model_registry import canonical_model_id

            same = canonical_model_id(target_model) == canonical_model_id(last_resort)
        except Exception:  # noqa: BLE001 - fall back to a plain comparison
            same = target_model.split("/")[-1] == last_resort.split("/")[-1]
        if same:
            logger.error(
                "autoDream refused to unload %r: it is the fleet's last-resort tier "
                "(ROBOTHOR_LAST_RESORT_MODEL) and every agent's chain ends there",
                target_model,
            )
            return

    logger.warning(
        "autoDream: unloading generation model (available=%.1fGiB < %.1fGiB)",
        available_gb,
        threshold_gb,
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{llm_client._ollama_url()}/api/generate",
                json={"model": target_model, "keep_alive": 0},
            )
        logger.info("Unloaded generation model")
        if pause_after_s:
            await asyncio.sleep(pause_after_s)  # Brief pause for Ollama to release GPU
    except Exception as e:
        logger.warning("Failed to unload generation model: %s", e)


def _release_lifecycle_lock() -> None:
    """Release the Redis distributed lock. Best-effort — TTL is the backstop."""
    try:
        import redis as _redis

        from robothor.config import get_config

        cfg = get_config()
        r = _redis.Redis(
            host=cfg.redis.host,
            port=cfg.redis.port,
            db=cfg.redis.db,
            password=cfg.redis.password or None,
        )
        r.delete(_LIFECYCLE_LOCK_KEY)
        r.close()
    except Exception as e:  # noqa: BLE001 - the lock's TTL is the backstop
        logger.warning("Lifecycle lock release failed, it will expire via TTL: %s", e)


# Step 2 (decay) processes facts in chunks of this size, each inside its own
# get_connection() transaction, so a run against the full ~29k-row corpus
# never holds every row's lock for the whole pass and a mid-pass failure only
# loses the in-flight chunk instead of the entire run.
_DECAY_CHUNK_SIZE = 500

# Use signals for decay and prune. The live columns are written by recall
# (outcomes.record_fact_access); migration 094's shadow columns hold the
# history scripts/memory_decay_backfill.py recovered from fact_access_log
# before anything wrote the live ones. GREATEST, not a sum: the backfill
# counts log rows, and every search_memory hit both logs a row and bumps the
# live count, so adding them would count one use twice. GREATEST ignores the
# NULL an unbackfilled shadow timestamp holds.
_USE_COUNT_SQL = "GREATEST(COALESCE(access_count, 0), COALESCE(access_count_shadow, 0))"
_LAST_USED_SQL = "GREATEST(last_accessed, last_accessed_shadow)"

# Importance candidates. Selecting ``importance_score = 0.5`` missed every
# reinforced fact: conflicts._reinforce_fact adds 0.05 per reinforcement
# (``LEAST(score + 0.05, 1.0)``), so the judge never saw a re-observed fact.
# Selecting on "reinforced" instead would overwrite scores that were judged or
# set on purpose (a resolution lifted to 0.85, a consolidated fact's inherited
# importance), because rows scored before the marker existed carry none.
#
# So a fact is a candidate when it carries no marker AND its score is still
# exactly what reinforcement alone gives from the 0.5 default:
# ``LEAST(0.5 + 0.05 * reinforcement_count, 1.0)``, within float tolerance
# (the reinforcement adds are accumulated in float8). Any other score was set
# by someone and is left alone. The fragments carry no tenant predicate, so a
# per-tenant caller ANDs its own.
_IMPORTANCE_MARKER = "importance_scored_at"
_REINFORCE_STEP = 0.05
_IMPORTANCE_DEFAULT = 0.5
_REINFORCEMENT_EARNED_SQL = f"({_REINFORCE_STEP} * COALESCE(reinforcement_count, 0))"
_UNSCORED_IMPORTANCE_SQL = (
    f"NOT (COALESCE(metadata, '{{}}'::jsonb) ? '{_IMPORTANCE_MARKER}') "
    f"AND ABS(importance_score - LEAST({_IMPORTANCE_DEFAULT} + {_REINFORCEMENT_EARNED_SQL}, 1.0))"
    " < 1e-6"
)
_MARK_IMPORTANCE_SQL = (
    f"COALESCE(metadata, '{{}}'::jsonb) || jsonb_build_object('{_IMPORTANCE_MARKER}', NOW())"
)


def _auto_score_old_events(cur: Any, tenant_id: str) -> int:
    """Score one tenant's unjudged events older than 30 days 0.3 (plus any
    reinforcement earned) without the judge, and mark them scored."""
    cur.execute(
        f"""
        UPDATE memory_facts
        SET importance_score = LEAST(0.3 + {_REINFORCEMENT_EARNED_SQL}, 1.0),
            metadata = {_MARK_IMPORTANCE_SQL}
        WHERE is_active = TRUE AND {_UNSCORED_IMPORTANCE_SQL}
          AND category = 'event'
          AND created_at < NOW() - INTERVAL '30 days'
          AND tenant_id = %s
        """,
        (tenant_id,),
    )
    return int(cur.rowcount or 0)


def _select_unscored_facts(cur: Any, tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """One tenant's newest active facts that were never judged or set on purpose."""
    cur.execute(
        f"""
        SELECT id, fact_text FROM memory_facts
        WHERE is_active = TRUE AND {_UNSCORED_IMPORTANCE_SQL} AND tenant_id = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (tenant_id, limit),
    )
    return list(cur.fetchall())


def _record_importance(cur: Any, fact_id: int, score: float) -> None:
    """Store the judge's score plus the reinforcement the fact earned so far.

    Marks the fact scored, except a neutral 0.5 on a never-reinforced fact:
    judge_importance also returns 0.5 on a timeout, so that fact stays a
    candidate, and rewriting it is idempotent (0.5 + nothing). A reinforced
    fact is marked whatever the verdict, so a neutral verdict cannot be
    re-selected nightly and reset the reinforcement earned since.
    """
    cur.execute(
        f"""
        UPDATE memory_facts
        SET importance_score = LEAST(%s + {_REINFORCEMENT_EARNED_SQL}, 1.0),
            metadata = CASE
                WHEN %s <> {_IMPORTANCE_DEFAULT} OR COALESCE(reinforcement_count, 0) > 0
                THEN {_MARK_IMPORTANCE_SQL}
                ELSE metadata
            END
        WHERE id = %s
        """,
        (score, score, fact_id),
    )


# Wall-clock budget for Step 2 (decay). asyncio.to_thread already keeps this
# work off the event loop (see _run_decay_step), but the budget still bounds
# total row-lock time and lets a slow pass yield to Steps 3+ instead of
# running unbounded — same style as Step 1's scoring_budget_s.
_DECAY_BUDGET_S = 120.0


def _decay_chunk(rows: list[dict[str, Any]]) -> tuple[int, int]:
    """Score and UPDATE one chunk of facts inside a single transaction.

    Returns (updated, skipped_null). Rows with a NULL last_accessed are
    skipped rather than passed to compute_decay_score(), which requires a
    datetime and is NOT touched here — see scripts/memory_decay_dryrun.py
    for why the formula itself must stay a single source of truth.
    """
    from robothor.memory.utility import utility_for_row

    updated = 0
    skipped_null = 0
    with get_connection() as conn:
        cur = conn.cursor()
        for fact in rows:
            utility = utility_for_row(fact)
            if fact["last_accessed"] is None:
                skipped_null += 1
                # No decay score to write; touch the row only if utility moved,
                # or every such row would be rewritten (a dead tuple) nightly.
                cur.execute(
                    "UPDATE memory_facts SET utility = %s "
                    "WHERE id = %s AND utility IS DISTINCT FROM %s",
                    (utility, fact["id"], utility),
                )
                continue
            score = compute_decay_score(
                last_accessed=fact["last_accessed"],
                access_count=fact["access_count"],
                reinforcement_count=fact["reinforcement_count"],
                importance_score=fact["importance_score"],
                utility=utility,
            )
            cur.execute(
                "UPDATE memory_facts SET decay_score = %s, utility = %s WHERE id = %s",
                (score, utility, fact["id"]),
            )
            updated += 1
    return updated, skipped_null


def _has_origin_column(cur: Any) -> bool:
    """Whether ``memory_facts.origin`` exists (it arrives with the graph schema)."""
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'memory_facts' "
        "AND column_name = 'origin'"
    )
    return cur.fetchone() is not None


def _run_decay_pass_sync(budget_s: float = _DECAY_BUDGET_S, tenant_id: str = "") -> dict[str, int]:
    """Sync decay pass: fetch all candidates once, then UPDATE in budgeted,
    chunked transactions. Runs inside asyncio.to_thread — see
    _run_decay_step(). Fully synchronous psycopg2 by design; there is no
    async driver in this repo (robothor/db/connection.py pool is sync).

    Replaces the old single-transaction SELECT-all + UPDATE-per-row pass
    that ran 200-330s on the production corpus directly on the asyncio event
    loop, starving it long enough that systemd's watchdog (WatchdogSec=300,
    pinged every 30s from daemon.py) SIGABRTs the engine. Chunking bounds
    per-transaction lock time and survives a mid-pass failure; the budget
    stops the pass cleanly instead of running unbounded.
    """
    t_start = time.monotonic()
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        # Outcome counts feed utility; origin, when the column exists, decides
        # whether the fact may earn positive utility at all.
        origin = ", origin" if _has_origin_column(cur) else ""
        cur.execute(
            f"""
            SELECT id, {_LAST_USED_SQL} AS last_accessed,
                   {_USE_COUNT_SQL} AS access_count, reinforcement_count,
                   importance_score, outcome_failures,
                   outcome_successes, outcome_citations{origin}
            FROM memory_facts
            WHERE is_active = TRUE AND tenant_id = %s
            """,
            (tenant_id or DEFAULT_TENANT,),
        )
        all_facts = cur.fetchall()

    updated = 0
    skipped_null = 0
    processed = 0
    budget_exhausted = False

    for start in range(0, len(all_facts), _DECAY_CHUNK_SIZE):
        elapsed = time.monotonic() - t_start
        if elapsed > budget_s:
            budget_exhausted = True
            break
        chunk = all_facts[start : start + _DECAY_CHUNK_SIZE]
        chunk_updated, chunk_skipped = _decay_chunk(chunk)
        updated += chunk_updated
        skipped_null += chunk_skipped
        processed += len(chunk)

    remaining = len(all_facts) - processed
    if skipped_null:
        logger.info("Step 2 (decay): %d skipped (NULL last_accessed)", skipped_null)
    if budget_exhausted:
        logger.warning(
            "Step 2 (decay): budget exhausted (%.0fs): updated %d, %d remaining",
            time.monotonic() - t_start,
            updated,
            remaining,
        )

    return {
        "updated": updated,
        "skipped_null": skipped_null,
        "budget_exhausted": budget_exhausted,
        "remaining": remaining,
    }


async def _run_decay_step(tenant_id: str = "") -> int:
    """Step 2 entry point: dispatch the sync, chunked, budgeted decay pass to
    a worker thread so it never blocks the event loop.

    Split out from run_lifecycle_maintenance so the to_thread dispatch, the
    budget stop, and NULL handling can be tested directly without mocking
    the rest of the 13-step maintenance pass.
    """
    result = await asyncio.to_thread(_run_decay_pass_sync, _DECAY_BUDGET_S, tenant_id)
    return result["updated"]


def active_tenant_ids() -> list[str]:
    """Every active tenant, so per-tenant maintenance reaches each of them.

    A fresh deployment has one tenant; multi-tenant instances have many.
    ``crm_tenants`` carries no row-level security, so this list is complete
    whatever tenant the process is bound to. Falls back to DEFAULT_TENANT when
    the table cannot be read, so maintenance still runs for the instance.
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor(cursor_factory=RealDictCursor)
            # The column is `active`, not `is_active`. The typo raised, the
            # surrounding except swallowed it, and tenant_ids silently collapsed
            # to [DEFAULT_TENANT] — so steps 8-13 of nightly maintenance ran for
            # exactly one tenant. Visible in the data: the default tenant's
            # access log was GC'd on schedule while every other tenant's kept
            # growing.
            #
            # Only safe to fix once migration 092's roll-up was deployed:
            # before it, correcting this would have started hard-deleting the
            # other tenants' access history on the very first nightly pass.
            cur.execute("SELECT id FROM crm_tenants WHERE active = TRUE ORDER BY id")
            tenant_ids = [r["id"] for r in cur.fetchall()]
    except Exception as e:
        logger.warning("Could not enumerate tenants — falling back to DEFAULT_TENANT: %s", e)
        tenant_ids = [DEFAULT_TENANT]
    return tenant_ids or [DEFAULT_TENANT]


# Step 1's wall-clock budget for LLM importance scoring in one pass, shared by
# every tenant so adding tenants does not lengthen the pass without bound. Only
# time spent scoring is charged to it, and each tenant is offered an equal share
# of what is left (see run_lifecycle_maintenance), so no tenant goes unscored.
_IMPORTANCE_BUDGET_S = 600.0


def _add_timing(step_timings: dict[str, float], step: str, seconds: float) -> None:
    step_timings[step] = step_timings.get(step, 0.0) + seconds


async def _run_tenant_core_steps(
    tenant_id: str,
    scoring_budget_s: float,
    step_timings: dict[str, float],
) -> dict[str, Any]:
    """Steps 1-7 of nightly maintenance for ONE tenant.

    Every read and every write here is limited to ``tenant_id``, and every
    derived row (consolidated fact, insight, inferred edge) is written to it:
    a consolidated fact never merges two tenants' facts, an insight never cites
    another tenant's, and an edge never joins two tenants' entities. The caller
    runs this inside ``tenant_scope(tenant_id)`` so that with row-level
    security on the connections are bound to the same tenant.
    """
    # Step 1: Score importance
    t0 = time.monotonic()

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Fast-path: events older than 30 days auto-score 0.3 (skip LLM call)
        auto_scored = _auto_score_old_events(cur, tenant_id)
        unscored = _select_unscored_facts(cur, tenant_id, limit=200)
        facts_scored = 0
        facts_skipped_budget = 0

        for fact in unscored:
            # This tenant's share of the pass's scoring budget
            elapsed = time.monotonic() - t0
            if elapsed > scoring_budget_s:
                facts_skipped_budget = len(unscored) - facts_scored
                logger.warning(
                    "Importance scoring budget exhausted for %s (%.0fs of a %.0fs share): "
                    "scored %d, skipping %d",
                    tenant_id,
                    elapsed,
                    scoring_budget_s,
                    facts_scored,
                    facts_skipped_budget,
                )
                break
            try:
                score = await judge_importance(fact["fact_text"])
                _record_importance(cur, fact["id"], score)
                facts_scored += 1
            except Exception as e:
                logger.warning("Failed to score fact %d: %s", fact["id"], e)

    _add_timing(step_timings, "importance_scoring", time.monotonic() - t0)
    logger.info(
        "Step 1 (importance) [%s]: %d scored, %d auto, %d skipped (%.1fs)",
        tenant_id,
        facts_scored,
        auto_scored,
        facts_skipped_budget,
        time.monotonic() - t0,
    )

    # Unload generation model to free GPU for embedding model (used by later steps),
    # but only under real memory pressure — see _should_unload_generation_model().
    await _perform_generation_model_unload()

    # Step 2: Update decay scores — chunked, budgeted, off the event loop.
    # See _run_decay_pass_sync for why (was a 200-330s synchronous pass
    # holding the loop and every row's lock for the whole run).
    t1 = time.monotonic()
    decay_updated = await _run_decay_step(tenant_id)
    _add_timing(step_timings, "decay", time.monotonic() - t1)
    logger.info(
        "Step 2 (decay) [%s]: %d updated (%.1fs)", tenant_id, decay_updated, time.monotonic() - t1
    )

    # Step 3: Prune low-quality facts
    t2 = time.monotonic()
    prune_result = await prune_low_quality_facts(tenant_id=tenant_id)
    _add_timing(step_timings, "prune", time.monotonic() - t2)
    logger.info(
        "Step 3 (prune) [%s]: %d pruned (%.1fs)",
        tenant_id,
        prune_result.get("total_pruned", 0),
        time.monotonic() - t2,
    )

    # Step 4: Consolidation — two-phase to avoid model contention
    # Phase A: Find candidates (DB only) + generate consolidated text (LLM/chat)
    # Phase B: Unload generation model, then store with embeddings
    t3 = time.monotonic()
    consolidation_groups = 0
    pending_consolidations: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    try:
        groups = await find_consolidation_candidates(
            min_group_size=3, similarity_threshold=0.8, tenant_id=tenant_id
        )
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
        discovered_insights = await discover_cross_domain_insights(
            hours_back=72, tenant_id=tenant_id
        )
    except Exception as e:
        logger.warning("Insight discovery LLM phase failed: %s", e)

    # Unload generation model to free GPU for embedding-heavy storage,
    # but only under real memory pressure — see _should_unload_generation_model().
    # The 2s pause (only taken when the unload actually runs) gives Ollama
    # time to release the GPU before the embedding-heavy phase below.
    await _perform_generation_model_unload(pause_after_s=2.0)

    # Phase B: Store consolidated facts (needs embeddings)
    for result, group in pending_consolidations:
        try:
            if await store_consolidated(group, result["consolidated_text"]):
                consolidation_groups += 1
        except Exception as e:
            logger.warning("Failed to store consolidated fact: %s", e)

    _add_timing(step_timings, "consolidation", time.monotonic() - t3)
    logger.info(
        "Step 4 (consolidation) [%s]: %d groups (%.1fs)",
        tenant_id,
        consolidation_groups,
        time.monotonic() - t3,
    )

    # Step 5: Sweep remaining unconsolidated facts (safety net)
    swept = _mark_facts_consolidated(tenant_id=tenant_id)
    if swept > 0:
        logger.info("Nightly sweep: marked %d remaining facts as consolidated", swept)

    # Step 6: Store discovered insights (needs embeddings; generation model was
    # unloaded above only if memory pressure warranted it)
    insight_result: dict[str, Any] = {
        "discovered": len(discovered_insights),
        "stored": 0,
        "deduped": 0,
    }
    for insight in discovered_insights:
        try:
            insight_id = await store_insight(insight, tenant_id=tenant_id)
            if insight_id is not None:
                insight_result["stored"] += 1
            else:
                insight_result["deduped"] += 1
        except Exception as e:
            logger.warning("Failed to store insight: %s", e)
    _add_timing(step_timings, "insights", time.monotonic() - t4)
    logger.info(
        "Step 6 (insights) [%s]: %s (%.1fs)", tenant_id, insight_result, time.monotonic() - t4
    )

    # Step 7: Cross-entity relationship inference
    t5 = time.monotonic()
    inferred_relations: list[dict[str, Any]] = []
    try:
        inferred_relations = await infer_entity_relationships(tenant_id=tenant_id)
    except Exception as e:
        logger.warning("Relationship inference failed: %s", e)
    _add_timing(step_timings, "relationship_inference", time.monotonic() - t5)
    logger.info(
        "Step 7 (relationship inference) [%s]: %d relations inferred (%.1fs)",
        tenant_id,
        len(inferred_relations),
        time.monotonic() - t5,
    )

    return {
        "facts_scored": facts_scored,
        "auto_scored": auto_scored,
        "facts_skipped_budget": facts_skipped_budget,
        "decay_updated": decay_updated,
        "facts_pruned": prune_result.get("total_pruned", 0),
        "consolidation_groups": consolidation_groups,
        "unconsolidated_swept": swept,
        "insights": insight_result,
        "relations_inferred": len(inferred_relations),
    }


async def _chat_ttl_step(tenant_ids: list[str]) -> dict[str, int]:
    """Step 10: prune stale chat turns and the conversation segments copying them.

    Per tenant, inside ``tenant_scope(tenant)``, off the loop. Segments use the
    same window as the turns (``SEGMENT_RETENTION_DAYS`` is no longer than
    ``CHAT_TURN_TTL_DAYS``), so a verbatim copy never outlives what it copied.
    Each half is best-effort per tenant: a failure stops neither the other
    half nor the next tenant.
    """
    from robothor.engine import chat_store
    from robothor.memory import segments

    stats = {"chat_pruned": 0, "segments_pruned": 0}
    for tid in tenant_ids:
        try:
            with tenant_scope(tid):
                stats["chat_pruned"] += await asyncio.to_thread(
                    chat_store.cleanup_stale_chat_turns, chat_store.CHAT_TURN_TTL_DAYS, tid
                )
        except Exception as e:
            logger.warning("Chat turn TTL failed for tenant %s: %s", tid, e)
        try:
            with tenant_scope(tid):
                stats["segments_pruned"] += await asyncio.to_thread(
                    segments.cleanup_stale_segments, segments.SEGMENT_RETENTION_DAYS, tid
                )
        except Exception as e:
            logger.warning("Conversation segment TTL failed for tenant %s: %s", tid, e)
    return stats


async def sync_crm_identity(tenant_ids: list[str]) -> dict[str, int]:
    """Keep every tenant's CRM people and companies linked to their memory nodes.

    The CRM DAL keeps the link current on each write; this nightly pass catches
    what bypassed the DAL (imports, direct SQL) and any hook that failed. Each
    tenant runs inside ``tenant_scope(tenant)``; one failing does not stop the
    others.
    """
    from robothor.memory import identity

    done = failed = 0
    for tid in tenant_ids:
        try:
            with tenant_scope(tid):
                await asyncio.to_thread(identity.sync_crm_entities, tid)
            done += 1
        except Exception as e:
            failed += 1
            logger.warning("CRM identity sync failed for tenant %s: %s", tid, e)
    return {"tenants": done, "failed": failed}


async def run_lifecycle_maintenance() -> dict[str, Any]:
    """Run full lifecycle maintenance on the fact store.

    Uses a Redis distributed lock to prevent concurrent consolidation runs
    from creating duplicate facts.

    Steps:
        1. Score importance for unscored facts (200 per run, 600s budget)
           - Fast-path: events older than 30 days auto-score 0.3
           - Each judge_importance() wrapped in try/except with 30s timeout
        2. Compute and update decay scores for all active facts
        3. Prune low-quality facts (garbage collection)
        4. Find and consolidate similar fact groups
        5. Sweep any remaining unconsolidated facts
        6. Cross-domain insight discovery (72h window)
        7. Cross-entity relationship inference
        8-13. Episodes, preferences, chat TTL, breadcrumbs, access log GC,
           intent inference

    Every step runs once per active tenant, inside ``tenant_scope(tenant)``:
    with row-level security on, a connection taken outside one is bound to
    the process's own tenant and every child tenant would go unmaintained.

    Returns:
        Dict with maintenance statistics.
    """
    # Acquire distributed lock via Redis SETNX
    try:
        import redis as _redis

        from robothor.config import get_config

        cfg = get_config()
        r = _redis.Redis(
            host=cfg.redis.host,
            port=cfg.redis.port,
            db=cfg.redis.db,
            password=cfg.redis.password or None,
        )
        if not r.set(_LIFECYCLE_LOCK_KEY, "running", nx=True, ex=_LIFECYCLE_LOCK_TTL):
            logger.info("Lifecycle maintenance skipped — another instance holds the lock")
            r.close()
            return {"skipped": True, "reason": "lock_held"}
        r.close()
    except Exception as e:
        logger.warning("Redis lock acquisition failed (proceeding anyway): %s", e)

    step_timings: dict[str, float] = {}
    t0 = time.monotonic()
    tenant_ids = active_tenant_ids()

    # Steps 1-7, one tenant at a time. Each tenant may spend an equal share of
    # the scoring budget still unspent, and only its scoring time is charged:
    # a tenant that uses less leaves more for those after it, and none is ever
    # offered nothing because an earlier tenant's other steps were slow.
    scoring_left = _IMPORTANCE_BUDGET_S
    core: dict[str, int] = {
        "facts_scored": 0,
        "auto_scored": 0,
        "facts_skipped_budget": 0,
        "decay_updated": 0,
        "facts_pruned": 0,
        "consolidation_groups": 0,
        "unconsolidated_swept": 0,
        "relations_inferred": 0,
    }
    insight_result: dict[str, int] = {"discovered": 0, "stored": 0, "deduped": 0}
    for i, tid in enumerate(tenant_ids):
        share = max(scoring_left, 0.0) / (len(tenant_ids) - i)
        scored_before = step_timings.get("importance_scoring", 0.0)
        try:
            with tenant_scope(tid):
                stats = await _run_tenant_core_steps(tid, share, step_timings)
        except Exception as e:  # noqa: BLE001 - one tenant must not sink the rest
            logger.warning("Lifecycle steps 1-7 failed for tenant %s: %s", tid, e)
            continue
        finally:
            scoring_left -= step_timings.get("importance_scoring", 0.0) - scored_before
        for k in core:
            core[k] += int(stats.get(k, 0))
        for k in insight_result:
            insight_result[k] += int(stats["insights"].get(k, 0))

    # Step 8: Episodic memory — cluster recent facts into time-bucketed episodes.
    t6 = time.monotonic()
    episode_stats: dict[str, Any] = {"candidates": 0, "clusters": 0, "episodes_stored": 0}
    try:
        from robothor.memory.episodes import build_episodes_from_facts

        for tid in tenant_ids:
            with tenant_scope(tid):
                stats = await build_episodes_from_facts(hours_back=72, tenant_id=tid)
            for k, v in stats.items():
                if isinstance(v, int):
                    episode_stats[k] = episode_stats.get(k, 0) + v
    except Exception as e:
        logger.warning("Episode building failed: %s", e)
    step_timings["episodes"] = time.monotonic() - t6
    logger.info("Step 8 (episodes): %s (%.1fs)", episode_stats, step_timings["episodes"])

    # Step 9: Preference tracking — extract new preferences, detect drift.
    t7 = time.monotonic()
    preference_stats: dict[str, Any] = {
        "extract": {"candidates": 0, "new": 0, "reinforced": 0, "skipped": 0},
        "drift": {"checked": 0, "marked_stale": 0},
    }
    try:
        from robothor.memory.preferences import (
            detect_drift,
            extract_preferences_from_facts,
        )

        for tid in tenant_ids:
            with tenant_scope(tid):
                ext = await extract_preferences_from_facts(hours_back=72, tenant_id=tid)
                drift = await detect_drift(tenant_id=tid)
            for k, v in ext.items():
                if isinstance(v, int):
                    preference_stats["extract"][k] = preference_stats["extract"].get(k, 0) + v
            for k, v in drift.items():
                if isinstance(v, int):
                    preference_stats["drift"][k] = preference_stats["drift"].get(k, 0) + v
    except Exception as e:
        logger.warning("Preference tracking failed: %s", e)
    step_timings["preferences"] = time.monotonic() - t7
    logger.info(
        "Step 9 (preferences): %s (%.1fs)",
        preference_stats,
        step_timings["preferences"],
    )

    # Step 10: Chat turn TTL — delete old un-pinned, un-referenced chat turns,
    # and the conversation segments that copy them.
    t8 = time.monotonic()
    ttl_stats = await _chat_ttl_step(tenant_ids)
    chat_pruned = ttl_stats["chat_pruned"]
    step_timings["chat_ttl"] = time.monotonic() - t8
    logger.info(
        "Step 10 (chat_ttl): %d pruned, %d segments pruned (%.1fs)",
        chat_pruned,
        ttl_stats["segments_pruned"],
        step_timings["chat_ttl"],
    )

    # Step 11: Breadcrumbs — prune expired + promote hot ones to memory_facts.
    t9 = time.monotonic()
    breadcrumb_stats: dict[str, Any] = {"pruned": 0, "promoted": 0}
    try:
        from robothor.memory.breadcrumbs import (
            promote_hot_breadcrumbs,
            prune_expired_breadcrumbs,
        )

        for tid in tenant_ids:
            with tenant_scope(tid):
                breadcrumb_stats["pruned"] += await asyncio.to_thread(
                    prune_expired_breadcrumbs, tid
                )
                promo = await promote_hot_breadcrumbs(tenant_id=tid)
            breadcrumb_stats["promoted"] += promo.get("promoted", 0)
    except Exception as e:
        logger.warning("Breadcrumb maintenance failed: %s", e)
    step_timings["breadcrumbs"] = time.monotonic() - t9
    logger.info(
        "Step 11 (breadcrumbs): %s (%.1fs)",
        breadcrumb_stats,
        step_timings["breadcrumbs"],
    )

    # Step 12: Outcome access log GC — trim raw attribution rows past the
    # configured retention window (MEMORY_ACCESS_LOG_RETENTION_DAYS). Passing
    # `days=None` lets the function read the knob; hardcoding a literal here is
    # what made the window unchangeable and destroyed the decay formula's only
    # input. Lifetime counts survive in fact_access_rollup.
    t10 = time.monotonic()
    access_log_pruned = 0
    try:
        from robothor.memory.outcomes import cleanup_old_access_logs

        for tid in tenant_ids:
            with tenant_scope(tid):
                access_log_pruned += await asyncio.to_thread(cleanup_old_access_logs, None, tid)
    except Exception as e:
        logger.warning("Access log cleanup failed: %s", e)
    step_timings["access_log_cleanup"] = time.monotonic() - t10
    logger.info(
        "Step 12 (access log cleanup): %d pruned (%.1fs)",
        access_log_pruned,
        step_timings["access_log_cleanup"],
    )

    # Step 13: Intent inference — propose standing intents from recent facts so
    # the (already-enabled) intent store stops being empty. Gated + best-effort.
    intents_proposed = 0
    if _intent_populate_enabled():
        t11 = time.monotonic()
        try:
            from robothor.memory.intents import infer_intents_from_facts

            for tid in tenant_ids:
                with tenant_scope(tid):
                    created = await infer_intents_from_facts(tenant_id=tid)
                intents_proposed += len(created)
        except Exception as e:
            logger.warning("Intent inference failed: %s", e)
        step_timings["intent_inference"] = time.monotonic() - t11
        logger.info(
            "Step 13 (intent inference): %d proposed (%.1fs)",
            intents_proposed,
            step_timings["intent_inference"],
        )

    # Step 14: CRM-anchored identity — every CRM person and company keeps one
    # linked memory node, whatever wrote the CRM row.
    t12 = time.monotonic()
    identity_stats = await sync_crm_identity(tenant_ids)
    step_timings["crm_identity"] = time.monotonic() - t12
    logger.info("Step 14 (crm identity): %s (%.1fs)", identity_stats, step_timings["crm_identity"])

    total_time = time.monotonic() - t0
    logger.info("Lifecycle maintenance complete in %.1fs: %s", total_time, step_timings)

    _release_lifecycle_lock()

    return {
        "facts_scored": core["facts_scored"],
        "auto_scored": core["auto_scored"],
        "facts_skipped_budget": core["facts_skipped_budget"],
        "decay_updated": core["decay_updated"],
        "facts_pruned": core["facts_pruned"],
        "consolidation_groups": core["consolidation_groups"],
        "unconsolidated_swept": core["unconsolidated_swept"],
        "episodes": episode_stats,
        "preferences": preference_stats,
        "chat_turns_pruned": chat_pruned,
        "segments_pruned": ttl_stats["segments_pruned"],
        "breadcrumbs": breadcrumb_stats,
        "access_log_pruned": access_log_pruned,
        "insights": insight_result,
        "relations_inferred": core["relations_inferred"],
        "intents_proposed": intents_proposed,
        "crm_identity": identity_stats,
        "step_timings": step_timings,
    }
