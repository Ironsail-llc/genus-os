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
import re
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.memory import generation

logger = logging.getLogger(__name__)

#: Relations returned per direction by get_entity. The payload reaches the model
#: in full — tracking.py's 4000-char cap is persistence-only — and the operator's
#: own node carries 5,207 edges. Fixing resolution without bounding the result
#: would trade a uselessly small answer for one that blows out the context
#: window on 211 calls a day.
MAX_RELATIONS_RETURNED = 50

# Junk-entity guard. Deliberately conservative: it must reject only names that
# cannot be a real entity in any language, because a false positive silently
# drops a real node out of the graph. Two rules, nothing more:
#   * a canonical dashed UUID — the extractor occasionally echoes a row id back
#     as an entity "name" ("3f7c1e9a-…"), which is never a person or project;
#   * fewer than two characters after stripping — "", "  ", "x" carry no
#     meaning and collide with every other one-character fragment.
# Short real names ("AI", "R2", "3M") and partial hex strings stay valid.
_UUID_NAME_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

MIN_ENTITY_NAME_LENGTH = 2

# Relations are inserted in chunks; a chunk that Postgres rejects is retried
# row by row so one bad row costs one relation, not the whole batch.
RELATION_CHUNK_SIZE = 500

_RELATION_SAVEPOINT = "robothor_relations_chunk"


def is_junk_entity_name(name: str | None) -> bool:
    """Whether ``name`` is unusable as an entity name (see ``_UUID_NAME_RE``)."""
    if not isinstance(name, str):
        return True
    stripped = name.strip()
    if len(stripped) < MIN_ENTITY_NAME_LENGTH:
        return True
    return bool(_UUID_NAME_RE.match(stripped))


def _is_valid_entity_id(value: Any) -> bool:
    """Whether ``value`` can be an entity id: a non-negative int (0 included).

    ``bool`` is an ``int`` subclass and is never a real id, so it is rejected.
    Negative values catch any surviving ``-1`` sentinel — the truthy sentinel
    whose FK violation used to destroy an entire relation batch.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


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
        raw = await generation.generate(
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
) -> int | None:
    """Insert or update an entity, incrementing mention count on conflict.

    Args:
        name: Entity name.
        entity_type: One of person, project, organization, technology, location, event.
        aliases: Optional list of alternative names.
        tenant_id: Tenant scope for data isolation.

    Returns:
        The entity ID, or ``None`` when the name is junk (see
        :func:`is_junk_entity_name`) and no row was written. ``None`` — never a
        numeric sentinel like ``-1``, which is truthy and would sail through a
        caller's ``if entity_id:`` guard straight into a relation insert that
        the ``memory_relations`` foreign key then rejects, taking the whole
        batch with it. Callers MUST test ``is not None`` (id 0 is a valid id).
    """
    _tenant = tenant_id or DEFAULT_TENANT
    if is_junk_entity_name(name):
        logger.warning(
            "skipping junk entity name %r (type=%s, tenant=%s)", name, entity_type, _tenant
        )
        return None
    name = name.strip()
    entity_type = entity_type.lower()
    if entity_type not in VALID_ENTITY_TYPES:
        entity_type = "technology"

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_entities (name, entity_type, aliases, tenant_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, name, entity_type) DO UPDATE
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
            ON CONFLICT (tenant_id, source_entity_id, target_entity_id, relation_type) DO UPDATE
            SET confidence = GREATEST(memory_relations.confidence, EXCLUDED.confidence)
            RETURNING id
            """,
            (source_id, target_id, relation_type, fact_id, confidence, _tenant),
        )
        rel_id: int = cur.fetchone()[0]

    return rel_id


_RELATION_INSERT_SQL = """
    INSERT INTO memory_relations
        (source_entity_id, target_entity_id, relation_type, fact_id, confidence, tenant_id)
    VALUES %s
    ON CONFLICT (tenant_id, source_entity_id, target_entity_id, relation_type) DO UPDATE
    SET confidence = GREATEST(memory_relations.confidence, EXCLUDED.confidence)
"""


def _insert_relation_chunk(cur: Any, chunk: list[tuple[Any, ...]]) -> bool:
    """Insert one chunk inside a SAVEPOINT. Returns False if Postgres rejected it.

    Without the savepoint a rejected statement aborts the surrounding
    transaction, so every later chunk would fail too. The savepoint is always
    released so a long batch cannot grow an unbounded savepoint stack.
    """
    from psycopg2.extras import execute_values

    cur.execute(f"SAVEPOINT {_RELATION_SAVEPOINT}")
    try:
        execute_values(cur, _RELATION_INSERT_SQL, chunk)
    except psycopg2.Error as exc:
        cur.execute(f"ROLLBACK TO SAVEPOINT {_RELATION_SAVEPOINT}")
        cur.execute(f"RELEASE SAVEPOINT {_RELATION_SAVEPOINT}")
        # A multi-row chunk failing is the interesting event; the row-by-row
        # retries that follow are summarised once by the caller instead of
        # emitting one WARNING each.
        log = logger.warning if len(chunk) > 1 else logger.debug
        log("memory_relations rejected a chunk of %d row(s): %s", len(chunk), exc)
        return False
    cur.execute(f"RELEASE SAVEPOINT {_RELATION_SAVEPOINT}")
    return True


async def add_relations_batch(
    rows: list[tuple[int, int, str, int | None, float]],
    *,
    tenant_id: str = "",
) -> int:
    """Insert many relations in as few round-trips as possible (fixes the N+1 in
    the extract paths). ``rows`` are ``(source_id, target_id, relation_type,
    fact_id, confidence)``. Same upsert semantics as :func:`add_relation`.

    One bad row cannot cost the batch. Rows whose endpoints are not valid entity
    ids are dropped before the insert, and a chunk that Postgres still rejects
    (a stale id, an entity deleted between upsert and insert) is retried row by
    row. Every drop is logged at WARNING with a count — ``memory_relations`` has
    foreign keys to ``memory_entities(id)``, so a single unusable row used to
    make Postgres reject the whole ``execute_values`` statement while
    ``ingestion.py`` swallowed the error, losing the batch silently.

    Returns the number of rows actually inserted/updated.
    """
    if not rows:
        return 0

    _tenant = tenant_id or DEFAULT_TENANT
    values: list[tuple[Any, ...]] = []
    invalid = 0
    for source_id, target_id, relation_type, fact_id, confidence in rows:
        if not (_is_valid_entity_id(source_id) and _is_valid_entity_id(target_id)):
            invalid += 1
            continue
        if not isinstance(relation_type, str) or not relation_type.strip():
            invalid += 1
            continue
        values.append((source_id, target_id, relation_type, fact_id, confidence, _tenant))

    if invalid:
        logger.warning(
            "dropped %d of %d relation row(s) with missing or invalid entity ids "
            "(tenant=%s) — the remaining %d are still being stored",
            invalid,
            len(rows),
            _tenant,
            len(values),
        )
    if not values:
        return 0

    inserted = 0
    rejected = 0
    with get_connection() as conn:
        cur = conn.cursor()
        for start in range(0, len(values), RELATION_CHUNK_SIZE):
            chunk = values[start : start + RELATION_CHUNK_SIZE]
            if _insert_relation_chunk(cur, chunk):
                inserted += len(chunk)
                continue
            for row in chunk:
                if _insert_relation_chunk(cur, [row]):
                    inserted += 1
                else:
                    rejected += 1

    if rejected:
        logger.warning(
            "postgres rejected %d of %d relation row(s) (tenant=%s); %d stored",
            rejected,
            len(values),
            _tenant,
            inserted,
        )
    return inserted


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

        # `memory_entities` is unique on (tenant_id, name, entity_type), so one
        # name can be several rows. Without ORDER BY, fetchone() took whichever
        # Postgres offered: "Philip D'Agostino" resolved to a 1-relation `event`
        # node while the operator's real 5,207-relation `person` node sat beside
        # it. Among rows sharing a name, the one carrying the relationships is
        # the one the caller means.
        cur.execute(
            """
            SELECT e.*,
                   (SELECT count(*) FROM memory_relations r
                     WHERE r.tenant_id = e.tenant_id
                       AND (r.source_entity_id = e.id OR r.target_entity_id = e.id)
                   ) AS _degree
            FROM memory_entities e
            WHERE lower(e.name) = lower(%s) AND e.tenant_id = %s
            ORDER BY _degree DESC, e.id ASC
            LIMIT 1
            """,
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
            ORDER BY r.id DESC
            LIMIT %s
            """,
            (entity["id"], _tenant, MAX_RELATIONS_RETURNED),
        )
        outgoing = [dict(r) for r in cur.fetchall()]

        # Get incoming relations
        cur.execute(
            """
            SELECT r.*, e.name as source_name, e.entity_type as source_type
            FROM memory_relations r
            JOIN memory_entities e ON r.source_entity_id = e.id
            WHERE r.target_entity_id = %s AND r.tenant_id = %s
            ORDER BY r.id DESC
            LIMIT %s
            """,
            (entity["id"], _tenant, MAX_RELATIONS_RETURNED),
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


async def _store_entities(
    extracted_entities: list[dict[str, Any]],
    *,
    tenant_id: str = "",
) -> dict[str, int]:
    """Upsert extracted entities, returning ``{name: entity_id}`` for the STORED ones.

    A junk name yields ``None`` from :func:`upsert_entity` and is simply absent
    from the mapping, so it can never become a relation endpoint. The result's
    length is the honest "entities stored" count.
    """
    entity_ids: dict[str, int] = {}
    skipped = 0
    for e in extracted_entities:
        entity_id = await upsert_entity(e["name"], e["type"], tenant_id=tenant_id)
        if entity_id is None:
            skipped += 1
            continue
        entity_ids[e["name"]] = entity_id
    if skipped:
        logger.warning(
            "entity extraction skipped %d of %d unusable entity name(s)",
            skipped,
            len(extracted_entities),
        )
    return entity_ids


def _relation_rows(
    relations: list[dict[str, Any]],
    entity_ids: dict[str, int],
    fact_id: int | None,
) -> list[tuple[int, int, str, int | None, float]]:
    """Build relation rows, keeping only those whose endpoints were stored.

    The membership test is ``is not None``, not truthiness: entity id 0 is a
    valid id, and a truthy sentinel must never pass for one.
    """
    rows: list[tuple[int, int, str, int | None, float]] = []
    dropped = 0
    for r in relations:
        source_id = entity_ids.get(r["source"])
        target_id = entity_ids.get(r["target"])
        if source_id is None or target_id is None:
            dropped += 1
            continue
        rows.append((source_id, target_id, r["relation"], fact_id, 1.0))
    if dropped:
        logger.info(
            "dropped %d of %d extracted relation(s) with an unstored endpoint",
            dropped,
            len(relations),
        )
    return rows


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

    entity_ids = await _store_entities(extracted["entities"], tenant_id=tenant_id)
    rel_rows = _relation_rows(extracted["relations"], entity_ids, fact_id)
    relations_stored = await add_relations_batch(rel_rows, tenant_id=tenant_id)

    return {
        "entities_stored": len(entity_ids),
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

    entity_ids = await _store_entities(extracted["entities"], tenant_id=tenant_id)
    ref_fact_id = fact_ids[0] if fact_ids else None
    rel_rows = _relation_rows(extracted["relations"], entity_ids, ref_fact_id)
    relations_stored = await add_relations_batch(rel_rows, tenant_id=tenant_id)

    return {
        "entities_stored": len(entity_ids),
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

            raw = await generation.generate(
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
