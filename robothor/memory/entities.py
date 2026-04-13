"""
Entity Graph for Genus OS Memory System.

Maintains a knowledge graph of named entities (people, projects,
technologies, etc.) and their relationships, extracted from stored facts.

Architecture:
    Content -> LLM entity extraction -> upsert entities -> add relations -> query graph
"""

from __future__ import annotations

import json
import logging
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
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


async def extract_entities(text: str) -> dict[str, Any]:
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


async def upsert_entity(
    name: str,
    entity_type: str,
    aliases: list[str] | None = None,
    *,
    tenant_id: str = "",
) -> int:
    """Insert or update an entity, incrementing mention count on conflict.

    Args:
        name: Entity name.
        entity_type: One of person, project, organization, technology, location, event.
        aliases: Optional list of alternative names.
        tenant_id: Tenant scope for data isolation.

    Returns:
        Entity ID.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    entity_type = entity_type.lower()
    if entity_type not in VALID_ENTITY_TYPES:
        entity_type = "technology"

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_entities (name, entity_type, aliases, tenant_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name, entity_type) DO UPDATE
            SET mention_count = memory_entities.mention_count + 1,
                last_seen = NOW()
            RETURNING id
            """,
            (name, entity_type, aliases or [], _tenant),
        )
        entity_id: int = cur.fetchone()[0]

    return entity_id


async def add_relation(
    source_id: int,
    target_id: int,
    relation_type: str,
    fact_id: int | None = None,
    confidence: float = 1.0,
    *,
    tenant_id: str = "",
) -> int:
    """Add a relationship between two entities.

    Args:
        source_id: Source entity ID.
        target_id: Target entity ID.
        relation_type: Type of relationship (e.g., 'uses', 'works_at').
        fact_id: Optional ID of the fact this relation was derived from.
        confidence: Confidence score for the relation.
        tenant_id: Tenant scope for data isolation.

    Returns:
        Relation ID.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_relations (source_entity_id, target_entity_id, relation_type, fact_id, confidence, tenant_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_entity_id, target_entity_id, relation_type) DO UPDATE
            SET confidence = GREATEST(memory_relations.confidence, EXCLUDED.confidence)
            RETURNING id
            """,
            (source_id, target_id, relation_type, fact_id, confidence, _tenant),
        )
        rel_id: int = cur.fetchone()[0]

    return rel_id


async def get_entity(name: str, *, tenant_id: str = "") -> dict[str, Any] | None:
    """Look up an entity and all its relationships.

    Args:
        name: Entity name (case-insensitive).
        tenant_id: Tenant scope for data isolation.

    Returns:
        Dict with entity info and relations, or None if not found.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            "SELECT * FROM memory_entities WHERE lower(name) = lower(%s) AND tenant_id = %s",
            (name, _tenant),
        )
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
            WHERE r.source_entity_id = %s AND r.tenant_id = %s
            """,
            (entity["id"], _tenant),
        )
        outgoing = [dict(r) for r in cur.fetchall()]

        # Get incoming relations
        cur.execute(
            """
            SELECT r.*, e.name as source_name, e.entity_type as source_type
            FROM memory_relations r
            JOIN memory_entities e ON r.source_entity_id = e.id
            WHERE r.target_entity_id = %s AND r.tenant_id = %s
            """,
            (entity["id"], _tenant),
        )
        incoming = [dict(r) for r in cur.fetchall()]

    entity["relations"] = outgoing + incoming
    return entity


async def get_all_about(entity_name: str, *, tenant_id: str = "") -> dict[str, Any]:
    """Get everything known about an entity: entity info, facts, and relations.

    Args:
        entity_name: Entity name to look up.
        tenant_id: Tenant scope for data isolation.

    Returns:
        Dict with 'entity', 'facts', and 'relations'.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    entity = await get_entity(entity_name, tenant_id=tenant_id)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, fact_text, category, confidence, created_at
            FROM memory_facts
            WHERE %s = ANY(entities)
              AND is_active = TRUE
              AND tenant_id = %s
            ORDER BY created_at DESC
            LIMIT 50
            """,
            (entity_name, _tenant),
        )
        facts = [dict(r) for r in cur.fetchall()]

    return {
        "entity": entity,
        "facts": facts,
        "relations": entity["relations"] if entity else [],
    }


async def extract_and_store_entities(
    content: str,
    fact_id: int | None = None,
    *,
    tenant_id: str = "",
) -> dict[str, Any]:
    """Extract entities and relations from content and store them.

    Args:
        content: Text to extract entities from.
        fact_id: Optional fact ID to link relations to.
        tenant_id: Tenant scope for data isolation.

    Returns:
        Dict with counts of entities and relations stored.
    """
    extracted = await extract_entities(content)

    entity_ids = {}
    for e in extracted["entities"]:
        eid = await upsert_entity(e["name"], e["type"], tenant_id=tenant_id)
        entity_ids[e["name"]] = eid

    relations_stored = 0
    for r in extracted["relations"]:
        src_id = entity_ids.get(r["source"])
        tgt_id = entity_ids.get(r["target"])
        if src_id and tgt_id:
            await add_relation(src_id, tgt_id, r["relation"], fact_id=fact_id, tenant_id=tenant_id)
            relations_stored += 1

    return {
        "entities_stored": len(extracted["entities"]),
        "relations_stored": relations_stored,
    }


async def extract_entities_batch(fact_ids: list[int], *, tenant_id: str = "") -> dict[str, Any]:
    """Batch-extract entities from multiple facts in a single LLM call.

    Instead of one LLM call per fact, this concatenates all fact texts
    and makes one extraction call, then links results back.

    Args:
        fact_ids: List of fact IDs to extract entities from.
        tenant_id: Tenant scope for data isolation.

    Returns:
        Dict with total entities and relations stored.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    if not fact_ids:
        return {"entities_stored": 0, "relations_stored": 0}

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT id, fact_text FROM memory_facts WHERE id = ANY(%s) AND tenant_id = %s",
            (fact_ids, _tenant),
        )
        facts = {r["id"]: r["fact_text"] for r in cur.fetchall()}

    if not facts:
        return {"entities_stored": 0, "relations_stored": 0}

    combined = "\n".join(f"[Fact {fid}]: {text}" for fid, text in facts.items())

    extracted = await extract_entities(combined)

    entity_ids = {}
    for e in extracted["entities"]:
        eid = await upsert_entity(e["name"], e["type"], tenant_id=tenant_id)
        entity_ids[e["name"]] = eid

    relations_stored = 0
    ref_fact_id = fact_ids[0] if fact_ids else None
    for r in extracted["relations"]:
        src_id = entity_ids.get(r["source"])
        tgt_id = entity_ids.get(r["target"])
        if src_id and tgt_id:
            await add_relation(
                src_id, tgt_id, r["relation"], fact_id=ref_fact_id, tenant_id=tenant_id
            )
            relations_stored += 1

    return {
        "entities_stored": len(extracted["entities"]),
        "relations_stored": relations_stored,
    }


# ── Cross-Fact Relationship Inference ────────────────────────────────────────

RELATION_INFERENCE_SCHEMA = {
    "type": "object",
    "properties": {
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "relation": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["source", "target", "relation", "confidence"],
            },
        },
    },
    "required": ["relations"],
}

MAX_INFERRED_CONFIDENCE = 0.7


async def find_underconnected_entities(
    min_mentions: int = 2,
    max_relations: int = 1,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Find entities mentioned multiple times but with few graph connections."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT e.id, e.name, e.entity_type, e.mention_count,
                   COUNT(r.id) AS relation_count
            FROM memory_entities e
            LEFT JOIN memory_relations r
                ON (r.source_entity_id = e.id OR r.target_entity_id = e.id)
            WHERE e.mention_count >= %s
            GROUP BY e.id, e.name, e.entity_type, e.mention_count
            HAVING COUNT(r.id) <= %s
            ORDER BY e.mention_count DESC
            LIMIT %s
            """,
            (min_mentions, max_relations, limit),
        )
        return [dict(row) for row in cur.fetchall()]


async def find_cooccurring_entity_pairs(
    entity_ids: list[int],
) -> list[dict[str, Any]]:
    """Find pairs of entities that appear in the same facts."""
    if not entity_ids:
        return []

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            WITH target_entities AS (
                SELECT id, name FROM memory_entities WHERE id = ANY(%s)
            ),
            entity_facts AS (
                SELECT te.id AS entity_id, te.name AS entity_name, f.id AS fact_id, f.fact_text
                FROM target_entities te
                JOIN memory_facts f ON te.name = ANY(f.entities)
                WHERE f.is_active = TRUE
            )
            SELECT
                a.entity_id AS entity_a_id, a.entity_name AS entity_a_name,
                b.entity_id AS entity_b_id, b.entity_name AS entity_b_name,
                COUNT(DISTINCT a.fact_id) AS shared_fact_count,
                ARRAY_AGG(DISTINCT a.fact_id) AS shared_fact_ids
            FROM entity_facts a
            JOIN entity_facts b ON a.fact_id = b.fact_id AND a.entity_id < b.entity_id
            GROUP BY a.entity_id, a.entity_name, b.entity_id, b.entity_name
            HAVING COUNT(DISTINCT a.fact_id) >= 1
            ORDER BY COUNT(DISTINCT a.fact_id) DESC
            """,
            (entity_ids,),
        )
        return [dict(row) for row in cur.fetchall()]


async def infer_relations(
    pairs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use LLM to infer relationship types between co-occurring entity pairs.

    Inferred relations are stored with confidence capped at MAX_INFERRED_CONFIDENCE.
    """
    if not pairs:
        return []

    stored = []
    for pair in pairs:
        try:
            facts_text = "\n".join(f"- {t}" for t in pair.get("shared_facts_text", []))
            prompt = (
                f"Given these two entities and the facts they share, "
                f"what is their relationship?\n\n"
                f"Entity A: {pair['entity_a_name']}\nEntity B: {pair['entity_b_name']}\n\n"
                f"Shared facts:\n{facts_text}\n\n"
                f"Return the relationship(s) between them. "
                f"Use simple verb phrases (works_at, manages, uses, collaborates_with, belongs_to, etc.)."
            )

            raw = await llm_client.generate(
                prompt=prompt,
                system="Infer entity relationships from shared facts.",
                max_tokens=512,
                format=RELATION_INFERENCE_SCHEMA,
            )

            parsed = json.loads(raw.strip())
            relations = parsed.get("relations", [])
            if not isinstance(relations, list):
                continue

            name_to_id = {
                pair["entity_a_name"]: pair["entity_a_id"],
                pair["entity_b_name"]: pair["entity_b_id"],
            }

            for rel in relations:
                src_id = name_to_id.get(rel.get("source", ""))
                tgt_id = name_to_id.get(rel.get("target", ""))
                rel_type = rel.get("relation", "")
                if not (src_id and tgt_id and rel_type):
                    continue

                confidence = min(float(rel.get("confidence", 0.6)), MAX_INFERRED_CONFIDENCE)
                fact_ref = pair["shared_fact_ids"][0] if pair.get("shared_fact_ids") else None
                rel_id = await add_relation(src_id, tgt_id, rel_type, fact_ref, confidence)
                stored.append(
                    {
                        "relation_id": rel_id,
                        "source": rel.get("source"),
                        "target": rel.get("target"),
                        "relation_type": rel_type,
                        "confidence": confidence,
                    }
                )

        except (json.JSONDecodeError, Exception):
            logger.warning(
                "Relationship inference failed for pair %s <-> %s",
                pair.get("entity_a_name"),
                pair.get("entity_b_name"),
                exc_info=True,
            )
            continue

    return stored
