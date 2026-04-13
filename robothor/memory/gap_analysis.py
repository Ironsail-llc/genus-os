"""
Knowledge Gap Analysis for Genus OS Memory System.

Identifies gaps in the knowledge graph and fact store that the
Curiosity Engine agent can target for self-directed research.

Gap categories:
    - Orphaned entities: mentioned once, no connections
    - Low-confidence facts: uncertain information needing verification
    - Entity type imbalances: thin coverage in some entity categories
    - Thin clusters: well-mentioned but poorly connected entities
    - Uncertainty signals: recent agent outputs expressing uncertainty
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

# Alias for the agent_runs DB (same database in Genus OS).
get_engine_connection = get_connection

# Threshold below which a type is considered imbalanced relative to the dominant type.
_IMBALANCE_RATIO = 0.10


async def find_orphaned_entities(
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find entities with exactly 1 mention and zero relations."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT e.id, e.name, e.entity_type, e.mention_count
            FROM memory_entities e
            LEFT JOIN memory_relations r
                ON (r.source_entity_id = e.id OR r.target_entity_id = e.id)
            WHERE e.mention_count = 1
            GROUP BY e.id, e.name, e.entity_type, e.mention_count
            HAVING COUNT(r.id) = 0
            ORDER BY e.last_seen DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]


async def find_low_confidence_facts(
    threshold: float = 0.5,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find active facts with confidence below the threshold."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, fact_text, confidence, category, entities, created_at
            FROM memory_facts
            WHERE is_active = TRUE
              AND confidence < %s
            ORDER BY confidence ASC
            LIMIT %s
            """,
            (threshold, limit),
        )
        return [dict(row) for row in cur.fetchall()]


async def find_entity_type_imbalances() -> list[dict[str, Any]]:
    """Find entity types that are underrepresented relative to the dominant type."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT entity_type, COUNT(*) AS count
            FROM memory_entities
            GROUP BY entity_type
            ORDER BY count DESC
            """,
        )
        rows = [dict(row) for row in cur.fetchall()]

    if not rows:
        return []

    dominant_count = rows[0]["count"]
    if dominant_count == 0:
        return []

    imbalanced = []
    for row in rows:
        ratio = row["count"] / dominant_count
        if ratio < _IMBALANCE_RATIO:
            imbalanced.append(
                {
                    "entity_type": row["entity_type"],
                    "count": row["count"],
                    "ratio": round(ratio, 3),
                    "dominant_type": rows[0]["entity_type"],
                    "dominant_count": dominant_count,
                }
            )

    return imbalanced


async def find_thin_entity_clusters(
    min_mentions: int = 3,
    max_relations: int = 1,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find entities mentioned frequently but with few graph connections."""
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


async def find_uncertainty_signals(
    hours_back: int = 168,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Find recent agent outputs that express uncertainty."""
    with get_engine_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT agent_id,
                   SUBSTRING(output_text FROM 1 FOR 200) AS output_snippet,
                   created_at
            FROM agent_runs
            WHERE created_at > NOW() - INTERVAL '%s hours'
              AND status = 'completed'
              AND (
                  output_text ILIKE '%%I don''t know%%'
                  OR output_text ILIKE '%%I''m not sure%%'
                  OR output_text ILIKE '%%I don''t have information%%'
                  OR output_text ILIKE '%%I couldn''t find%%'
                  OR output_text ILIKE '%%no data available%%'
                  OR output_text ILIKE '%%uncertain%%'
              )
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (hours_back, limit),
        )
        return [dict(row) for row in cur.fetchall()]


async def analyze_knowledge_gaps() -> dict[str, Any]:
    """Run all gap detection analyses and return structured results."""
    return {
        "orphaned_entities": await find_orphaned_entities(),
        "low_confidence_facts": await find_low_confidence_facts(),
        "type_imbalances": await find_entity_type_imbalances(),
        "thin_clusters": await find_thin_entity_clusters(),
        "uncertainty_signals": await find_uncertainty_signals(),
    }
