"""
Conflict Resolution & Deduplication for Genus OS Memory System.

Detects duplicate, contradictory, or updated facts before storing them.
Uses semantic similarity search to find related facts, then LLM-based
classification to determine the relationship.

Architecture:
    New fact -> find_similar_facts -> classify_relationship -> act (store/skip/supersede)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client
from robothor.memory.facts import _write_dedup_enabled, store_fact
from robothor.memory.vector_tuning import apply_hnsw_session

logger = logging.getLogger(__name__)

# WS-3: above this similarity, a same-category match is the SAME fact re-reported
# (e.g. the agent's own briefing re-ingested), not a new one — reinforce it
# rather than fork a near-duplicate row.
_REINFORCE_THRESHOLD = 0.92

# JSON schema for conflict classification structured output.
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["new", "duplicate", "update", "contradiction"],
        },
        "reasoning": {"type": "string"},
    },
    "required": ["classification", "reasoning"],
}


async def find_similar_facts(
    query: str,
    limit: int = 5,
    threshold: float = 0.5,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Find existing facts semantically similar to a query.

    Args:
        query: Text to search for similar facts.
        limit: Maximum number of results.
        threshold: Minimum cosine similarity score (0.0-1.0).

    Returns:
        List of similar fact dictionaries with similarity scores.
    """
    embedding = await llm_client.get_embedding_async(query)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        apply_hnsw_session(cur)
        cur.execute(
            """
            SELECT
                id,
                fact_text,
                category,
                entities,
                confidence,
                source_type,
                is_active,
                1 - (embedding <=> %s::vector) as similarity
            FROM memory_facts
            WHERE embedding IS NOT NULL
              AND is_active = TRUE
              AND tenant_id = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, tenant_id or DEFAULT_TENANT, embedding, limit),
        )
        results = [dict(r) for r in cur.fetchall()]

    return [r for r in results if r["similarity"] >= threshold]


def build_classification_prompt(new_fact: str, existing_fact: str) -> str:
    """Build LLM prompt for classifying the relationship between two facts."""
    return f"""Compare these two facts and classify their relationship.

Existing fact: "{existing_fact}"
New fact: "{new_fact}"

Classify as one of:
- "new": The facts describe DIFFERENT things, even if they mention the same entity. Default to this when unsure.
- "duplicate": The facts say the EXACT same thing (possibly worded differently). Both would be redundant.
- "update": The new fact REPLACES the old — the old fact would be FALSE if the new one is true. Only use this when the old fact is now outdated/wrong.
- "contradiction": The facts make directly opposing claims about the same specific thing.

Examples:
- "Alex works at Acme Corp" vs "Alex attended a meeting at Acme Corp" → "new" (different claims about same entity)
- "Meeting is at 3pm" vs "Meeting moved to 4pm" → "update" (old time is now wrong)
- "Alex prefers dark mode" vs "Alex prefers dark mode for coding" → "duplicate" (same preference, minor rewording)

IMPORTANT: When two facts mention the same entity but describe different events, actions, or attributes, classify as "new". Only classify as "update" when the old fact would be INCORRECT given the new information.

Return JSON: {{"classification": "<type>", "reasoning": "<brief explanation>"}}
Return ONLY the JSON object, no other text."""


async def classify_relationship(new_fact: str, existing_fact: str) -> dict[str, Any]:
    """Classify the relationship between a new fact and an existing one.

    Args:
        new_fact: The newly extracted fact text.
        existing_fact: The existing fact text from the database.

    Returns:
        Dict with 'classification' (new/duplicate/update/contradiction) and 'reasoning'.
    """
    try:
        prompt = build_classification_prompt(new_fact, existing_fact)
        raw = await llm_client.generate(
            prompt=prompt,
            system="Classify the relationship between these two facts.",
            max_tokens=256,
            format=CLASSIFICATION_SCHEMA,
        )

        parsed = json.loads(raw.strip())
        classification = parsed.get("classification", "new").lower().strip()
        if classification not in ("new", "duplicate", "update", "contradiction"):
            classification = "new"

        return {
            "classification": classification,
            "reasoning": parsed.get("reasoning", ""),
        }
    except (json.JSONDecodeError, Exception):
        return {"classification": "new", "reasoning": "Failed to classify, treating as new"}


def _reinforce_fact(fact_id: int, *, tenant_id: str = "") -> None:
    """Strengthen an existing fact instead of forking a near-duplicate.

    Repeated mentions of the same event (the dominant churn source) should raise
    the fact's salience, not inflate the table with reworded copies. Nudges
    importance up (capped at 1.0), counts a *reinforcement*, and refreshes
    updated_at.

    This used to increment access_count, which was wrong twice over. The event
    is re-observation, not retrieval, so it belongs on reinforcement_count —
    which had no writer at all, leaving one of compute_decay_score's five inputs
    permanently zero across every row. And because access_count is weighted in
    the retrieval blend (facts._blend_rank), counting it here also inflated a
    fact's search ranking every time something merely mentioned it again.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_facts
            SET importance_score = LEAST(COALESCE(importance_score, 0.5) + 0.05, 1.0),
                reinforcement_count = COALESCE(reinforcement_count, 0) + 1,
                updated_at = NOW()
            WHERE id = %s AND tenant_id = %s
            """,
            (fact_id, tenant_id or DEFAULT_TENANT),
        )


def _supersede_fact(old_id: int, new_id: int, *, tenant_id: str = "") -> None:
    """Mark an old fact as superseded by a new one."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_facts
            SET is_active = FALSE, superseded_by = %s, updated_at = NOW()
            WHERE id = %s
              AND tenant_id = %s
            """,
            (new_id, old_id, tenant_id or DEFAULT_TENANT),
        )


async def resolve_and_store(
    fact: dict[str, Any],
    source_content: str,
    source_type: str,
    similarity_threshold: float = 0.7,
    *,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Full conflict resolution pipeline: find similar -> classify -> act.

    Args:
        fact: Fact dictionary with fact_text, category, entities, confidence.
        source_content: Original content the fact was extracted from.
        source_type: Type of source (conversation, email, etc.).
        similarity_threshold: Minimum similarity to trigger classification.

    Returns:
        Dict with 'action' (stored/skipped/superseded) and optionally 'new_id'.
    """
    similar = await find_similar_facts(
        fact["fact_text"],
        limit=3,
        threshold=similarity_threshold,
        tenant_id=tenant_id,
    )

    if not similar:
        fact_id = await store_fact(fact, source_content, source_type, tenant_id=tenant_id)
        return {"action": "stored", "new_id": fact_id}

    best_match = similar[0]

    # WS-3 reinforce-not-fork: a near-identical, same-category match is the same
    # event re-reported, not a new one. Reinforce it and skip the LLM classify
    # (which is biased to "new") so re-ingested briefings stop minting copies.
    if (
        _write_dedup_enabled()
        and float(best_match.get("similarity") or 0) >= _REINFORCE_THRESHOLD
        and (best_match.get("category") or "") == (fact.get("category") or "")
    ):
        _reinforce_fact(best_match["id"], tenant_id=tenant_id)
        return {
            "action": "reinforced",
            "existing_id": best_match["id"],
            "similarity": round(float(best_match.get("similarity") or 0), 4),
        }

    classification = await classify_relationship(
        fact["fact_text"],
        best_match["fact_text"],
    )

    if classification["classification"] == "duplicate":
        return {
            "action": "skipped",
            "existing_id": best_match["id"],
            "reasoning": classification["reasoning"],
        }

    if classification["classification"] in ("contradiction", "update"):
        new_id = await store_fact(fact, source_content, source_type, tenant_id=tenant_id)
        _supersede_fact(best_match["id"], new_id, tenant_id=tenant_id)
        return {
            "action": "superseded",
            "new_id": new_id,
            "old_id": best_match["id"],
            "classification": classification["classification"],
            "reasoning": classification["reasoning"],
        }

    # Classified as new — store directly
    fact_id = await store_fact(fact, source_content, source_type, tenant_id=tenant_id)
    return {"action": "stored", "new_id": fact_id}
