"""
Entity Graph for Robothor Memory System.

Maintains a knowledge graph of named entities (people, projects,
technologies, etc.) and their relationships, extracted from stored facts.

Architecture:
    Content -> LLM entity extraction -> upsert entities -> add relations -> query graph
"""

from __future__ import annotations

import json
import logging

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client

logger = logging.getLogger(__name__)

VALID_ENTITY_TYPES = [
    "person",
    "project",
    "organization",
    "technology",
    "location",
    "event",
]

# JSON schema for entity extraction structured output.
ENTITY_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {
                        "type": "string",
                        "enum": VALID_ENTITY_TYPES,
                    },
                },
                "required": ["name", "type"],
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "relation": {"type": "string"},
                },
                "required": ["source", "target", "relation"],
            },
        },
    },
    "required": ["entities", "relations"],
}


async def extract_entities(text: str) -> dict:
    """Extract entities and relations from text using the LLM.

    Args:
        text: Content to extract entities from.

    Returns:
        Dict with 'entities' list and 'relations' list.
    """
    if not text or not text.strip():
        return {"entities": [], "relations": []}

    prompt = f"""Extract named entities (proper nouns, specific names) and their relationships from this text. Relations should be simple verb phrases (uses, works_at, manages, built_with, etc.).

Text: {text}"""

    try:
        raw = await llm_client.generate(
            prompt=prompt,
            system="Extract entities and relations from the text.",
            max_tokens=2048,
            format=ENTITY_EXTRACTION_SCHEMA,
        )

        parsed = json.loads(raw.strip())

        entities = parsed.get("entities", [])
        if not isinstance(entities, list):
            entities = []
        entities = [e for e in entities if isinstance(e, dict) and e.get("name") and e.get("type")]
        for e in entities:
            if e["type"].lower() not in VALID_ENTITY_TYPES:
                e["type"] = "technology"
            else:
                e["type"] = e["type"].lower()

        relations = parsed.get("relations", [])
        if not isinstance(relations, list):
            relations = []
        relations = [
            r
            for r in relations
            if isinstance(r, dict) and r.get("source") and r.get("target") and r.get("relation")
        ]

        return {"entities": entities, "relations": relations}

    except (json.JSONDecodeError, Exception):
        return {"entities": [], "relations": []}


async def upsert_entity(name: str, entity_type: str, aliases: list[str] | None = None) -> int:
    """Insert or update an entity, incrementing mention count on conflict.

    Args:
        name: Entity name.
        entity_type: One of person, project, organization, technology, location, event.
        aliases: Optional list of alternative names.

    Returns:
        Entity ID.
    """
    entity_type = entity_type.lower()
    if entity_type not in VALID_ENTITY_TYPES:
        entity_type = "technology"

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_entities (name, entity_type, aliases)
            VALUES (%s, %s, %s)
            ON CONFLICT (name, entity_type) DO UPDATE
            SET mention_count = memory_entities.mention_count + 1,
                last_seen = NOW()
            RETURNING id
            """,
            (name, entity_type, aliases or []),
        )
        entity_id: int = cur.fetchone()[0]

    return entity_id


async def add_relation(
    source_id: int,
    target_id: int,
    relation_type: str,
    fact_id: int | None = None,
    confidence: float = 1.0,
) -> int:
    """Add a relationship between two entities.

    Args:
        source_id: Source entity ID.
        target_id: Target entity ID.
        relation_type: Type of relationship (e.g., 'uses', 'works_at').
        fact_id: Optional ID of the fact this relation was derived from.
        confidence: Confidence score for the relation.

    Returns:
        Relation ID.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, fact_id, confidence)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_entity_id, target_entity_id, relation_type) DO UPDATE
            SET confidence = GREATEST(memory_relations.confidence, EXCLUDED.confidence)
            RETURNING id
            """,
            (source_id, target_id, relation_type, fact_id, confidence),
        )
        rel_id: int = cur.fetchone()[0]

    return rel_id


async def get_entity(name: str) -> dict | None:
    """Look up an entity and all its relationships.

    Args:
        name: Entity name (case-insensitive).

    Returns:
        Dict with entity info and relations, or None if not found.
    """
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT * FROM memory_entities WHERE lower(name) = lower(%s)", (name,))
        entity = cur.fetchone()

        if not entity:
            return None

        entity = dict(entity)

        # Get outgoing relations
        cur.execute(
            """
            SELECT r.*, e.name as target_name, e.entity_type as target_type
            FROM memory_relations r
            JOIN memory_entities e ON r.target_entity_id = e.id
            WHERE r.source_entity_id = %s
            """,
            (entity["id"],),
        )
        outgoing = [dict(r) for r in cur.fetchall()]

        # Get incoming relations
        cur.execute(
            """
            SELECT r.*, e.name as source_name, e.entity_type as source_type
            FROM memory_relations r
            JOIN memory_entities e ON r.source_entity_id = e.id
            WHERE r.target_entity_id = %s
            """,
            (entity["id"],),
        )
        incoming = [dict(r) for r in cur.fetchall()]

    entity["relations"] = outgoing + incoming
    return entity


async def get_all_about(entity_name: str) -> dict:
    """Get everything known about an entity: entity info, facts, and relations.

    Args:
        entity_name: Entity name to look up.

    Returns:
        Dict with 'entity', 'facts', and 'relations'.
    """
    entity = await get_entity(entity_name)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, fact_text, category, confidence, created_at
            FROM memory_facts
            WHERE %s = ANY(entities)
              AND is_active = TRUE
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (entity_name,),
        )
        facts = [dict(r) for r in cur.fetchall()]

    return {
        "entity": entity,
        "facts": facts,
        "relations": entity["relations"] if entity else [],
    }


async def extract_and_store_entities(content: str, fact_id: int | None = None) -> dict:
    """Extract entities and relations from content and store them.

    Args:
        content: Text to extract entities from.
        fact_id: Optional fact ID to link relations to.

    Returns:
        Dict with counts of entities and relations stored.
    """
    extracted = await extract_entities(content)

    entity_ids = {}
    for e in extracted["entities"]:
        eid = await upsert_entity(e["name"], e["type"])
        entity_ids[e["name"]] = eid

    relations_stored = 0
    for r in extracted["relations"]:
        src_id = entity_ids.get(r["source"])
        tgt_id = entity_ids.get(r["target"])
        if src_id and tgt_id:
            await add_relation(src_id, tgt_id, r["relation"], fact_id=fact_id)
            relations_stored += 1

    return {
        "entities_stored": len(extracted["entities"]),
        "relations_stored": relations_stored,
    }


async def extract_entities_batch(fact_ids: list[int]) -> dict:
    """Batch-extract entities from multiple facts in a single LLM call.

    Instead of one LLM call per fact, this concatenates all fact texts
    and makes one extraction call, then links results back.

    Args:
        fact_ids: List of fact IDs to extract entities from.

    Returns:
        Dict with total entities and relations stored.
    """
    if not fact_ids:
        return {"entities_stored": 0, "relations_stored": 0}

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT id, fact_text FROM memory_facts WHERE id = ANY(%s)", (fact_ids,))
        facts = {r["id"]: r["fact_text"] for r in cur.fetchall()}

    if not facts:
        return {"entities_stored": 0, "relations_stored": 0}

    combined = "\n".join(f"[Fact {fid}]: {text}" for fid, text in facts.items())

    extracted = await extract_entities(combined)

    entity_ids = {}
    for e in extracted["entities"]:
        eid = await upsert_entity(e["name"], e["type"])
        entity_ids[e["name"]] = eid

    relations_stored = 0
    ref_fact_id = fact_ids[0] if fact_ids else None
    for r in extracted["relations"]:
        src_id = entity_ids.get(r["source"])
        tgt_id = entity_ids.get(r["target"])
        if src_id and tgt_id:
            await add_relation(src_id, tgt_id, r["relation"], fact_id=ref_fact_id)
            relations_stored += 1

    return {
        "entities_stored": len(extracted["entities"]),
        "relations_stored": relations_stored,
    }
