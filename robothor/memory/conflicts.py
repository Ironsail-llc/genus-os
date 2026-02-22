"""
Conflict Resolution & Deduplication for Robothor Memory System.

Detects duplicate, contradictory, or updated facts before storing them.
Uses semantic similarity search to find related facts, then LLM-based
classification to determine the relationship.

Architecture:
    New fact -> find_similar_facts -> classify_relationship -> act (store/skip/supersede)
"""

from __future__ import annotations

import json
import logging

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client
from robothor.memory.facts import store_fact

logger = logging.getLogger(__name__)

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
) -> list[dict]:
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
        cur.execute("SET ivfflat.probes = 10")
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
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, embedding, limit),
        )
        results = [dict(r) for r in cur.fetchall()]

    return [r for r in results if r["similarity"] >= threshold]


def build_classification_prompt(new_fact: str, existing_fact: str) -> str:
    """Build LLM prompt for classifying the relationship between two facts."""
    return f"""Compare these two facts and classify their relationship.

Existing fact: "{existing_fact}"
New fact: "{new_fact}"

Classify as one of:
- "new": The facts are about different things, no relationship
- "duplicate": The facts say the same thing (possibly worded differently)
- "update": The new fact adds more detail or refines the existing fact
- "contradiction": The facts directly contradict each other

Return JSON: {{"classification": "<type>", "reasoning": "<brief explanation>"}}
Return ONLY the JSON object, no other text."""


async def classify_relationship(new_fact: str, existing_fact: str) -> dict:
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


def _supersede_fact(old_id: int, new_id: int) -> None:
    """Mark an old fact as superseded by a new one."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE memory_facts
            SET is_active = FALSE, superseded_by = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (new_id, old_id),
        )


async def resolve_and_store(
    fact: dict,
    source_content: str,
    source_type: str,
    similarity_threshold: float = 0.7,
) -> dict:
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
    )

    if not similar:
        fact_id = await store_fact(fact, source_content, source_type)
        return {"action": "stored", "new_id": fact_id}

    best_match = similar[0]
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
        new_id = await store_fact(fact, source_content, source_type)
        _supersede_fact(best_match["id"], new_id)
        return {
            "action": "superseded",
            "new_id": new_id,
            "old_id": best_match["id"],
            "classification": classification["classification"],
            "reasoning": classification["reasoning"],
        }

    # classification == "new"
    fact_id = await store_fact(fact, source_content, source_type)
    return {"action": "stored", "new_id": fact_id}
