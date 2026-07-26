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
import re
from functools import partial
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client
from robothor.memory.bitemporal import record_conflict_decision, supersede_with_validity
from robothor.memory.facts import _write_dedup_enabled, store_fact
from robothor.memory.vector_tuning import apply_hnsw_session

logger = logging.getLogger(__name__)

# WS-3: above this similarity, a same-category match is the SAME fact re-reported
# (e.g. the agent's own briefing re-ingested), not a new one — reinforce it
# rather than fork a near-duplicate row.
_REINFORCE_THRESHOLD = 0.92

# Numbers, including decimals and thousands separators. Currency symbols and
# units are deliberately excluded — "$100" and "100 dollars" carry the same
# quantity and the digits are what matters.
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_differ(a: str, b: str) -> bool:
    """True when two texts disagree on the numbers they contain.

    The reinforce shortcut assumes high lexical similarity means "same fact,
    said again". A number change is the exact opposite: "$100 -> $150" scores
    0.9882 cosine — far above the threshold — while completely reversing the
    fact's meaning. Treating that as a re-report discards the update AND bumps
    the importance of the stale value, so the wrong number gets retrieved more
    often. Prices, ports, versions, dates, times and quantities all live here.

    Compared as sorted multisets so word order cannot manufacture a difference,
    and normalized so "$1,200" and "$1200" are the same amount.
    """
    def _nums(text: str) -> list[str]:
        out = []
        for raw in _NUMBER.findall(text or ""):
            cleaned = raw.replace(",", "")
            # Trim a trailing ".0" so 2 and 2.0 compare equal.
            if "." in cleaned:
                cleaned = cleaned.rstrip("0").rstrip(".")
            out.append(cleaned or "0")
        return sorted(out)

    return _nums(a) != _nums(b)

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
    """Mark an old fact as superseded by a new one.

    Delegates to the bi-temporal writer so every supersession path bounds the
    old fact in world-time. There are two callers — here and
    ``resolution.py`` — and a second writer that set ``is_active = FALSE``
    without setting ``valid_to`` would leave facts hidden from the normal read
    path AND invisible to the point-in-time one, which is worse than either
    behaviour alone.
    """
    supersede_with_validity(
        old_id, new_id, tenant_id=tenant_id, classification="resolution"
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
        # A number change is not a re-report, it IS the update. "$100 -> $150"
        # scores 0.9882 cosine, sails over the threshold, and reverses the
        # fact's meaning. Without this guard the update was discarded and the
        # STALE value had its importance bumped, so the wrong number surfaced
        # more often over time. Falls through to the LLM classifier, which
        # correctly returns "update" and supersedes.
        and not numbers_differ(fact["fact_text"], best_match.get("fact_text") or "")
    ):
        _reinforce_fact(best_match["id"], tenant_id=tenant_id)
        # Recorded too: an error rate needs the cases where nothing was
        # superseded as much as the ones where something was.
        record_conflict_decision(
            tenant_id=tenant_id or DEFAULT_TENANT,
            classification="reinforced",
            action="reinforced",
            new_fact_id=None,
            existing_fact_id=best_match["id"],
            reasoning="similarity >= reinforce threshold, same category",
            similarity=float(best_match.get("similarity") or 0),
            new_fact_text=fact["fact_text"],
            existing_fact_text=best_match["fact_text"],
        )
        return {
            "action": "reinforced",
            "existing_id": best_match["id"],
            "similarity": round(float(best_match.get("similarity") or 0), 4),
        }

    classification = await classify_relationship(
        fact["fact_text"],
        best_match["fact_text"],
    )

    kind = classification["classification"]
    _record = partial(
        record_conflict_decision,
        tenant_id=tenant_id or DEFAULT_TENANT,
        classification=kind,
        existing_fact_id=best_match["id"],
        reasoning=classification["reasoning"],
        similarity=float(best_match.get("similarity") or 0) or None,
        new_fact_text=fact["fact_text"],
        existing_fact_text=best_match["fact_text"],
    )

    if kind == "duplicate":
        _record(action="skipped", new_fact_id=None)
        return {
            "action": "skipped",
            "existing_id": best_match["id"],
            "reasoning": classification["reasoning"],
        }

    if kind in ("contradiction", "update"):
        new_id = await store_fact(fact, source_content, source_type, tenant_id=tenant_id)
        # An UPDATE and a CONTRADICTION are both bounded in world-time, but they
        # are now recorded distinctly. They mean different things — for an update
        # the old fact was genuinely true until now; for a contradiction one of
        # the two was always wrong and the bound is a guess about which. Acting
        # on that difference needs an error rate, and until this table existed
        # there was none: the classification deactivated rows and vanished.
        supersede_with_validity(
            best_match["id"], new_id, tenant_id=tenant_id, classification=kind
        )
        _record(action="superseded", new_fact_id=new_id)
        return {
            "action": "superseded",
            "new_id": new_id,
            "old_id": best_match["id"],
            "classification": kind,
            "reasoning": classification["reasoning"],
        }

    # Classified as new — store directly
    fact_id = await store_fact(fact, source_content, source_type, tenant_id=tenant_id)
    return {"action": "stored", "new_id": fact_id}
