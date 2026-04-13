"""
Fact Extraction Layer for Genus OS Memory System.

Extracts structured facts from unstructured content using a local LLM,
then stores them with vector embeddings in PostgreSQL for semantic search.

Architecture:
    Content -> LLM extraction -> Parse JSON -> Store with embedding -> pgvector search

Dependencies:
    - robothor.llm.ollama for LLM generation and embeddings
    - PostgreSQL with pgvector for storage and search
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
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
    return f"""Extract specific, memorable facts from the following content.

Rules:
- Each fact MUST be a complete sentence with a subject and predicate
- Each fact MUST reference at least one specific named entity (person, organization, place, project, technology)
- Each fact MUST be specific to this content — NOT generic knowledge anyone would know
- Include temporal context when present (dates, "yesterday", "next week", etc.)
- Categorize each fact: decision (someone decided X), preference (someone prefers X), event (X happened), contact (relationship info), project (work/technical), personal (personal life), technical (system/code)

Skip:
- Greetings, filler, partial sentences
- Generic statements ("X is a company", "X is available", "meetings are important")
- Single words or numbers without context
- Facts that don't mention any specific person, organization, or project by name

Content:
{content}"""


def parse_extraction_response(raw: str) -> list[dict[str, Any]]:
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

        valid_facts.append(
            {
                "fact_text": fact_text.strip(),
                "category": category,
                "entities": entities,
                "confidence": confidence,
            }
        )

    # Hard quality filters — reject garbage before it enters the database
    filtered = []
    for fact in valid_facts:
        text = fact["fact_text"]

        # Too short to be meaningful
        if len(text) < 15:
            logger.debug("Rejected (too short): %s", text[:50])
            continue

        # No entities — can't be a specific fact
        if not fact["entities"]:
            logger.debug("Rejected (no entities): %s", text[:50])
            continue

        # Too low confidence
        if fact["confidence"] < 0.3:
            logger.debug("Rejected (low confidence %.2f): %s", fact["confidence"], text[:50])
            continue

        # Single word/number
        if re.match(r"^\s*\w+\s*$", text):
            logger.debug("Rejected (single word): %s", text[:50])
            continue

        # Generic patterns that add no value
        generic_patterns = [
            r"^.{1,30}\s+is\s+a\s+(company|person|tool|platform|service|technology)\b",
            r"^.{1,30}\s+is\s+available\b",
            r"^(Hello|Hi|Hey|Thanks|Thank you|Bye|Goodbye)\b",
        ]
        is_generic = False
        for pattern in generic_patterns:
            if re.match(pattern, text, re.IGNORECASE):
                logger.debug("Rejected (generic pattern): %s", text[:50])
                is_generic = True
                break
        if is_generic:
            continue

        filtered.append(fact)

    if len(filtered) < len(valid_facts):
        logger.info("Quality filter: %d/%d facts passed", len(filtered), len(valid_facts))

    return filtered


async def extract_facts(content: str, max_retries: int = 3) -> list[dict[str, Any]]:
    """Extract facts from content using the local LLM.

    Retries on empty results because thinking models sometimes exhaust
    their token budget on reasoning before producing content.

    Hard-capped at 45s total to prevent Ollama hangs from blocking agent runs.

    Args:
        content: Unstructured text content.
        max_retries: Number of attempts before giving up.

    Returns:
        List of extracted fact dictionaries, or empty list on failure.
    """
    try:
        return await asyncio.wait_for(_extract_facts_inner(content, max_retries), timeout=45.0)
    except TimeoutError:
        logger.warning("extract_facts hard timeout (45s) — returning empty")
        return []


async def _extract_facts_inner(
    content: str,
    max_retries: int,
) -> list[dict[str, Any]]:
    """Inner implementation of extract_facts (no timeout wrapper)."""
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
    fact: dict[str, Any],
    source_content: str,
    source_type: str,
    metadata: dict[str, Any] | None = None,
    *,
    tenant_id: str = "",
) -> int:
    """Store a fact with its embedding in the database.

    Args:
        fact: Fact dictionary with fact_text, category, entities, confidence.
        source_content: Original content the fact was extracted from.
        source_type: Type of source (conversation, email, etc.).
        metadata: Optional additional metadata.
        tenant_id: Tenant scope for data isolation.

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
             embedding, metadata, tenant_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                tenant_id or DEFAULT_TENANT,
            ),
        )
        fact_id: int = cur.fetchone()[0]

    return fact_id


async def store_facts_batch(
    facts: list[dict[str, Any]],
    source_content: str,
    source_type: str,
    metadata: dict[str, Any] | None = None,
    *,
    tenant_id: str = "",
) -> list[int]:
    """Store multiple facts with batch-embedded vectors.

    Embeds all fact texts in a single Ollama call, then inserts each fact
    with its pre-computed embedding.

    Args:
        facts: List of fact dicts with fact_text, category, entities, confidence.
        source_content: Original content the facts were extracted from.
        source_type: Type of source (conversation, email, etc.).
        metadata: Optional additional metadata.
        tenant_id: Tenant scope for data isolation.

    Returns:
        List of database IDs for the stored facts.
    """
    if not facts:
        return []

    texts = [f["fact_text"] for f in facts]
    embeddings = await llm_client.get_embeddings_batch_async(texts)

    with get_connection() as conn:
        cur = conn.cursor()
        ids = []

        for fact, embedding in zip(facts, embeddings, strict=True):
            cur.execute(
                """
                INSERT INTO memory_facts
                (fact_text, category, entities, confidence, source_content, source_type,
                 embedding, metadata, tenant_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    tenant_id or DEFAULT_TENANT,
                ),
            )
            ids.append(cur.fetchone()[0])

    logger.info("store_facts_batch: stored %d facts with batch embeddings", len(ids))
    return ids


async def search_insights(
    query: str,
    limit: int = 5,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Search cross-domain insights by vector similarity.

    Args:
        query: Search query text.
        limit: Maximum number of results.
        tenant_id: Tenant scope for data isolation.

    Returns:
        List of matching insight dictionaries sorted by similarity.
    """
    embedding = await llm_client.get_embedding_async(query)

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, insight_text, source_fact_ids, categories, entities,
                   created_at, metadata,
                   1 - (embedding <=> %s::vector) as similarity
            FROM memory_insights
            WHERE is_active = TRUE AND embedding IS NOT NULL
              AND tenant_id = %s
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, tenant_id or DEFAULT_TENANT, embedding, limit),
        )
        results = [dict(r) for r in cur.fetchall()]

    for r in results:
        r["source"] = "insight"

    return results


async def search_facts(
    query: str,
    limit: int = 10,
    active_only: bool = True,
    use_reranker: bool = False,
    expand_entities: bool = False,
    include_insights: bool = False,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Hybrid search: vector similarity + BM25 keyword matching with RRF fusion.

    Pipeline:
        1. Vector search: top 30 by cosine similarity (semantic)
        2. BM25 search: top 30 by ts_rank (keyword)
        3. Reciprocal Rank Fusion: score = 1/(60+rank_vector) + 1/(60+rank_bm25)
        4. Optional: entity-graph expansion for associated facts
        5. Optional: reranker (cross-encoder) for precision

    Args:
        query: Search query text.
        limit: Maximum number of results.
        active_only: If True, only return active (non-superseded) facts.
        use_reranker: If True, run reranker on candidates.
        expand_entities: If True, pull related entity facts.

    Returns:
        List of matching fact dictionaries sorted by relevance.
    """
    embedding = await llm_client.get_embedding_async(query)

    active_clause = "AND is_active = TRUE" if active_only else ""
    fetch_limit = max(30, limit * 3)
    _tenant = tenant_id or DEFAULT_TENANT

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Vector search
        cur.execute(
            f"""
            SELECT id, fact_text, category, entities, confidence, source_type,
                   metadata, created_at,
                   1 - (embedding <=> %s::vector) as similarity
            FROM memory_facts
            WHERE embedding IS NOT NULL AND tenant_id = %s {active_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            (embedding, _tenant, embedding, fetch_limit),
        )
        vector_results = [dict(r) for r in cur.fetchall()]

        # BM25 keyword search
        cur.execute(
            f"""
            SELECT id, fact_text, category, entities, confidence, source_type,
                   metadata, created_at,
                   ts_rank(tsv, plainto_tsquery('english', %s)) as bm25_score
            FROM memory_facts
            WHERE tsv @@ plainto_tsquery('english', %s) AND tenant_id = %s
              {active_clause}
            ORDER BY ts_rank(tsv, plainto_tsquery('english', %s)) DESC
            LIMIT %s
            """,
            (query, query, _tenant, query, fetch_limit),
        )
        bm25_results = [dict(r) for r in cur.fetchall()]

    # Reciprocal Rank Fusion
    vector_ranks = {r["id"]: rank for rank, r in enumerate(vector_results)}
    bm25_ranks = {r["id"]: rank for rank, r in enumerate(bm25_results)}

    all_ids = set(vector_ranks.keys()) | set(bm25_ranks.keys())
    all_results_by_id: dict[int, dict[str, Any]] = {}
    for r in vector_results + bm25_results:
        if r["id"] not in all_results_by_id:
            all_results_by_id[r["id"]] = r

    k = 60  # RRF constant
    rrf_scores: dict[int, float] = {}
    for fact_id in all_ids:
        score = 0.0
        if fact_id in vector_ranks:
            score += 1.0 / (k + vector_ranks[fact_id])
        if fact_id in bm25_ranks:
            score += 1.0 / (k + bm25_ranks[fact_id])
        rrf_scores[fact_id] = score

    sorted_ids = sorted(rrf_scores, key=lambda k: rrf_scores[k], reverse=True)
    candidates = []
    for fact_id in sorted_ids:
        r = all_results_by_id[fact_id]
        r["rrf_score"] = round(rrf_scores[fact_id], 6)
        candidates.append(r)

    # Entity-graph expansion (best-effort)
    if expand_entities and candidates:
        try:
            from robothor.memory.entities import get_entity

            mentioned_entities: set[str] = set()
            for r in candidates[:5]:
                for e in r.get("entities") or []:
                    mentioned_entities.add(e)

            expansion_ids = {r["id"] for r in candidates}
            for entity_name in list(mentioned_entities)[:3]:
                entity = await get_entity(entity_name, tenant_id=_tenant)
                if entity and entity.get("relations"):
                    for rel in entity["relations"][:3]:
                        related_name = rel.get("target") or rel.get("source", "")
                        if related_name:
                            with get_connection() as conn:
                                cur = conn.cursor(cursor_factory=RealDictCursor)
                                cur.execute(
                                    """
                                    SELECT id, fact_text, category, entities, confidence,
                                           source_type, metadata, created_at, importance_score
                                    FROM memory_facts
                                    WHERE is_active = TRUE AND tenant_id = %s
                                      AND %s = ANY(entities)
                                      AND importance_score > 0.5
                                      AND id != ALL(%s)
                                    ORDER BY importance_score DESC, created_at DESC
                                    LIMIT 2
                                    """,
                                    (_tenant, related_name, list(expansion_ids)),
                                )
                                for r in cur.fetchall():
                                    r = dict(r)
                                    r["rrf_score"] = 0.005
                                    r["source"] = "entity_expansion"
                                    candidates.append(r)
                                    expansion_ids.add(r["id"])
        except Exception:
            pass  # Entity expansion is best-effort

    # Optional reranker pass
    if use_reranker and candidates:
        try:
            from brain.memory_system.reranker import rerank_with_fallback

            for c in candidates:
                c["content"] = c.get("fact_text", "")
            reranked: list[dict[str, Any]] = await rerank_with_fallback(
                query, candidates, top_k=limit
            )
            if include_insights:
                try:
                    insights = await search_insights(query, limit=3, tenant_id=tenant_id)
                    reranked.extend(insights)
                except Exception:
                    pass
            return reranked
        except Exception:
            pass  # Fall through to return without reranker

    result = candidates[:limit]

    # Append cross-domain insights if requested
    if include_insights:
        try:
            insights = await search_insights(query, limit=3, tenant_id=tenant_id)
            result.extend(insights)
        except Exception:
            pass  # Insight search is best-effort

    return result


def get_memory_stats(tenant_id: str = "") -> dict[str, Any]:
    """Get memory system statistics from the facts-based memory system.

    Args:
        tenant_id: Tenant scope for data isolation.

    Returns counts for total facts, active facts, superseded facts,
    scored facts, entities, and relations.
    """
    _tenant = tenant_id or DEFAULT_TENANT

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_facts WHERE tenant_id = %s",
            (_tenant,),
        )
        total_facts = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_facts WHERE is_active = TRUE AND tenant_id = %s",
            (_tenant,),
        )
        active_facts = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_facts "
            "WHERE is_active = FALSE AND superseded_by IS NOT NULL AND tenant_id = %s",
            (_tenant,),
        )
        superseded_count = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_facts "
            "WHERE importance_score != 0.5 AND is_active = TRUE AND tenant_id = %s",
            (_tenant,),
        )
        scored_count = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_entities WHERE tenant_id = %s",
            (_tenant,),
        )
        entity_count = cur.fetchone()["count"]

        cur.execute(
            "SELECT COUNT(*) as count FROM memory_relations WHERE tenant_id = %s",
            (_tenant,),
        )
        relation_count = cur.fetchone()["count"]

    return {
        "total_facts": total_facts,
        "active_facts": active_facts,
        "superseded_count": superseded_count,
        "scored_count": scored_count,
        "entity_count": entity_count,
        "relation_count": relation_count,
    }


def search_facts_compat(
    query: str,
    limit: int = 10,
    tenant_id: str = "",
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Sync compatibility wrapper for search_facts, matching the old tiers API.

    Maps fact fields to the RAG pipeline's expected format:
        fact_text -> content, source_type -> content_type, adds tier: "facts"

    Args:
        query: Search query text.
        limit: Maximum number of results.
        tenant_id: Tenant scope for data isolation.

    Returns:
        List of result dicts with 'content', 'content_type', 'tier' keys.
    """
    import asyncio

    results = asyncio.run(search_facts(query, limit=limit, tenant_id=tenant_id))
    compat_results = [
        {
            **r,
            "content": r.get("fact_text", ""),
            "content_type": r.get("source_type", "unknown"),
            "tier": "facts",
            "similarity": r.get("rrf_score", 0.0),
        }
        for r in results
    ]
    return compat_results
