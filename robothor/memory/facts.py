"""
Fact Extraction Layer for Robothor Memory System.

Extracts structured facts from unstructured content using a local LLM,
then stores them with vector embeddings in PostgreSQL for semantic search.

Architecture:
    Content -> LLM extraction -> Parse JSON -> Store with embedding -> pgvector search

Dependencies:
    - robothor.llm.ollama for LLM generation and embeddings
    - PostgreSQL with pgvector for storage and search
"""

from __future__ import annotations

import json
import logging
import re

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client

logger = logging.getLogger(__name__)

VALID_CATEGORIES = [
    "personal",
    "project",
    "decision",
    "preference",
    "event",
    "contact",
    "technical",
]

# JSON schema for Ollama structured output.
FACT_EXTRACTION_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "fact_text": {"type": "string"},
            "category": {
                "type": "string",
                "enum": VALID_CATEGORIES,
            },
            "entities": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "number"},
        },
        "required": ["fact_text", "category", "entities", "confidence"],
    },
}


def build_extraction_prompt(content: str) -> str:
    """Build the LLM prompt for fact extraction."""
    return f"""Extract discrete facts from the following content. Each fact should be a single, atomic statement. Include named entities (people, organizations, technologies, places). Skip trivial filler.

Content:
{content}"""


def parse_extraction_response(raw: str) -> list[dict]:
    """Parse the LLM's extraction response into structured facts.

    Handles markdown fences, single objects, missing fields, and
    out-of-range confidence values.

    Args:
        raw: Raw LLM response text.

    Returns:
        List of validated fact dictionaries.
    """
    if not raw or not raw.strip():
        return []

    text = raw.strip()

    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    if not text:
        return []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []

    if isinstance(parsed, dict):
        parsed = [parsed]

    if not isinstance(parsed, list):
        return []

    valid_facts = []
    for item in parsed:
        if not isinstance(item, dict):
            continue

        fact_text = item.get("fact_text", "")
        if not fact_text or not fact_text.strip():
            continue

        category = str(item.get("category", "personal")).lower().strip()
        if category not in VALID_CATEGORIES:
            category = "personal"

        confidence = item.get("confidence", 0.8)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.8
        confidence = max(0.0, min(1.0, confidence))

        entities = item.get("entities", [])
        if not isinstance(entities, list):
            entities = []
        entities = [str(e) for e in entities if e]

        valid_facts.append({
            "fact_text": fact_text.strip(),
            "category": category,
            "entities": entities,
            "confidence": confidence,
        })

    return valid_facts


async def extract_facts(content: str, max_retries: int = 3) -> list[dict]:
    """Extract facts from content using the local LLM.

    Retries on empty results because thinking models sometimes exhaust
    their token budget on reasoning before producing content.

    Args:
        content: Unstructured text content.
        max_retries: Number of attempts before giving up.

    Returns:
        List of extracted fact dictionaries, or empty list on failure.
    """
    prompt = build_extraction_prompt(content)
    for attempt in range(max_retries):
        try:
            logger.info("extract_facts attempt %d/%d", attempt + 1, max_retries)
            raw = await llm_client.generate(
                prompt=prompt,
                system="Extract facts from the content as a JSON array.",
                max_tokens=1024,
                format=FACT_EXTRACTION_SCHEMA,
            )
            logger.info("LLM returned %d chars", len(raw) if raw else 0)
            if not raw or not raw.strip():
                logger.warning("Empty response from LLM on attempt %d", attempt + 1)
                continue
            facts = parse_extraction_response(raw)
            if facts:
                logger.info("Parsed %d facts on attempt %d", len(facts), attempt + 1)
                return facts
            logger.warning("Parsed 0 facts from %d chars on attempt %d", len(raw), attempt + 1)
        except Exception as e:
            logger.warning("extract_facts attempt %d failed: %s", attempt + 1, e)
    logger.error("extract_facts failed after %d attempts", max_retries)
    return []


async def store_fact(
    fact: dict,
    source_content: str,
    source_type: str,
    metadata: dict | None = None,
) -> int:
    """Store a fact with its embedding in the database.

    Args:
        fact: Fact dictionary with fact_text, category, entities, confidence.
        source_content: Original content the fact was extracted from.
        source_type: Type of source (conversation, email, etc.).
        metadata: Optional additional metadata.

    Returns:
        The database ID of the stored fact.
    """
    embedding = await llm_client.get_embedding_async(fact["fact_text"])

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_facts
            (fact_text, category, entities, confidence, source_content, source_type,
             embedding, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                fact["fact_text"],
                fact["category"],
                fact.get("entities", []),
                fact.get("confidence", 1.0),
                source_content,
                source_type,
                embedding,
                json.dumps(metadata or {}),
            ),
        )
        fact_id: int = cur.fetchone()[0]

    return fact_id


async def search_facts(
    query: str,
    limit: int = 10,
    active_only: bool = True,
) -> list[dict]:
    """Search facts by semantic similarity.

    Args:
        query: Search query text.
        limit: Maximum number of results.
        active_only: If True, only return active (non-superseded) facts.

    Returns:
        List of matching fact dictionaries sorted by similarity.
    """
    embedding = await llm_client.get_embedding_async(query)

    active_clause = "AND is_active = TRUE" if active_only else ""

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            f"""
            SELECT
                id,
                fact_text,
                category,
                entities,
                confidence,
                source_type,
                metadata,
                created_at,
                1 - (embedding <=> %s::vector) as similarity
            FROM memory_facts
            WHERE embedding IS NOT NULL
              {active_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, embedding, limit),
        )
        results = [dict(r) for r in cur.fetchall()]

    return results
