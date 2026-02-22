"""
Cross-Channel Ingestion Pipeline for Robothor Memory System.

Accepts content from multiple channels (discord, email, cli, api, telegram)
and runs it through fact extraction and conflict resolution.

Architecture:
    Content + channel -> extract_facts -> resolve_and_store (each fact) -> result
"""

from __future__ import annotations

import json
import logging

from robothor.db.connection import get_connection
from robothor.memory.conflicts import resolve_and_store
from robothor.memory.entities import extract_entities_batch
from robothor.memory.facts import extract_facts, store_fact

logger = logging.getLogger(__name__)

VALID_CHANNELS = [
    "discord",
    "email",
    "cli",
    "api",
    "telegram",
    "gchat",
    "voice",
    "mcp",
    "camera",
    "conversation",
    "crm",
]


async def ingest_content(
    content: str,
    source_channel: str,
    content_type: str,
    metadata: dict | None = None,
) -> dict:
    """Ingest content from any channel, extracting and storing facts.

    Args:
        content: The raw content to ingest.
        source_channel: Channel the content came from (discord, email, etc.).
        content_type: Type of content (conversation, email, decision, etc.).
        metadata: Optional channel-specific metadata.

    Returns:
        Dict with ingestion results including facts processed.

    Raises:
        ValueError: If content is empty.
    """
    if not content or not content.strip():
        raise ValueError("Content cannot be empty")

    logger.info("Extracting facts from %s content (%d chars) via %s", content_type, len(content), source_channel)
    facts = await extract_facts(content)
    logger.info("Extracted %d facts", len(facts))

    stored_ids = []
    skipped = 0

    for fact in facts:
        try:
            result = await resolve_and_store(
                fact=fact,
                source_content=content,
                source_type=content_type,
            )
            if result.get("new_id"):
                stored_ids.append(result["new_id"])
                _set_source_channel(result["new_id"], source_channel, metadata)
                logger.info("Stored fact %d: %s", result["new_id"], fact["fact_text"][:80])
            else:
                skipped += 1
                logger.info("Skipped fact (dedup): %s", fact["fact_text"][:80])
        except Exception as e:
            logger.warning("Conflict resolution failed (%s), storing directly", e)
            fact_id = await store_fact(fact, content, content_type, metadata)
            stored_ids.append(fact_id)
            _set_source_channel(fact_id, source_channel, metadata)

    # Run batch entity extraction on all stored facts
    entity_results: dict = {"entities_stored": 0, "relations_stored": 0}
    if stored_ids:
        try:
            entity_results = await extract_entities_batch(stored_ids)
            logger.info(
                "Entity extraction: %d entities, %d relations",
                entity_results.get("entities_stored", 0),
                entity_results.get("relations_stored", 0),
            )
        except Exception as e:
            logger.warning("Entity extraction failed: %s", e)

    return {
        "source_channel": source_channel,
        "content_type": content_type,
        "facts_processed": len(stored_ids),
        "facts_skipped": skipped,
        "fact_ids": stored_ids,
        "entities_stored": entity_results.get("entities_stored", 0),
        "relations_stored": entity_results.get("relations_stored", 0),
    }


def _set_source_channel(fact_id: int, source_channel: str, metadata: dict | None = None) -> None:
    """Update a fact's source_channel and merge metadata."""
    with get_connection() as conn:
        cur = conn.cursor()
        if metadata:
            cur.execute(
                """
                UPDATE memory_facts
                SET source_channel = %s,
                    metadata = metadata || %s
                WHERE id = %s
                """,
                (source_channel, json.dumps(metadata), fact_id),
            )
        else:
            cur.execute(
                """
                UPDATE memory_facts
                SET source_channel = %s
                WHERE id = %s
                """,
                (source_channel, fact_id),
            )
