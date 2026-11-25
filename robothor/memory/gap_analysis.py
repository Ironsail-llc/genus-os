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

Every analysis reads one tenant: ``tenant_id``, defaulting to DEFAULT_TENANT.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

# Alias for the agent_runs DB (same database in Genus OS).
get_engine_connection = get_connection

# Threshold below which a type is considered imbalanced relative to the dominant type.
_IMBALANCE_RATIO = 0.10


async def find_orphaned_entities(
    limit: int = 20,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Find entities with exactly 1 mention and zero relations."""
    _tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT e.id, e.name, e.entity_type, e.mention_count
            FROM memory_entities e
            LEFT JOIN memory_relations r
                ON (r.source_entity_id = e.id OR r.target_entity_id = e.id)
               AND r.tenant_id = e.tenant_id
            WHERE e.mention_count = 1
              AND e.tenant_id = %s
            GROUP BY e.id, e.name, e.entity_type, e.mention_count
            HAVING COUNT(r.id) = 0
            ORDER BY e.last_seen DESC
            LIMIT %s
            """,
            (_tenant, limit),
        )
        return [dict(row) for row in cur.fetchall()]


async def find_low_confidence_facts(
    threshold: float = 0.5,
    limit: int = 20,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Find active facts with confidence below the threshold."""
    _tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, fact_text, confidence, category, entities, created_at
            FROM memory_facts
            WHERE is_active = TRUE
              AND confidence < %s
              AND tenant_id = %s
            ORDER BY confidence ASC
            LIMIT %s
            """,
            (threshold, _tenant, limit),
        )
        return [dict(row) for row in cur.fetchall()]


async def find_entity_type_imbalances(*, tenant_id: str = "") -> list[dict[str, Any]]:
    """Find entity types that are underrepresented relative to the dominant type."""
    _tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT entity_type, COUNT(*) AS count
            FROM memory_entities
            WHERE tenant_id = %s
            GROUP BY entity_type
            ORDER BY count DESC
            """,
            (_tenant,),
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
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Find entities mentioned frequently but with few graph connections."""
    _tenant = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT e.id, e.name, e.entity_type, e.mention_count,
                   COUNT(r.id) AS relation_count
            FROM memory_entities e
            LEFT JOIN memory_relations r
                ON (r.source_entity_id = e.id OR r.target_entity_id = e.id)
               AND r.tenant_id = e.tenant_id
            WHERE e.mention_count >= %s
              AND e.tenant_id = %s
            GROUP BY e.id, e.name, e.entity_type, e.mention_count
            HAVING COUNT(r.id) <= %s
            ORDER BY e.mention_count DESC
            LIMIT %s
            """,
            (min_mentions, _tenant, max_relations, limit),
        )
        return [dict(row) for row in cur.fetchall()]


async def find_uncertainty_signals(
    hours_back: int = 168,
    limit: int = 20,
    *,
    tenant_id: str = "",
) -> list[dict[str, Any]]:
    """Find recent agent outputs that express uncertainty."""
    _tenant = tenant_id or DEFAULT_TENANT
    with get_engine_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT agent_id,
                   SUBSTRING(output_text FROM 1 FOR 200) AS output_snippet,
                   created_at
            FROM agent_runs
            WHERE created_at > NOW() - INTERVAL '%s hours'
              AND tenant_id = %s
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
            (hours_back, _tenant, limit),
        )
        return [dict(row) for row in cur.fetchall()]


async def analyze_knowledge_gaps(tenant_id: str = "") -> dict[str, Any]:
    """Run all gap detection analyses for one tenant and return structured results."""
    _tenant = tenant_id or DEFAULT_TENANT
    return {
        "orphaned_entities": await find_orphaned_entities(tenant_id=_tenant),
        "low_confidence_facts": await find_low_confidence_facts(tenant_id=_tenant),
        "type_imbalances": await find_entity_type_imbalances(tenant_id=_tenant),
        "thin_clusters": await find_thin_entity_clusters(tenant_id=_tenant),
        "uncertainty_signals": await find_uncertainty_signals(tenant_id=_tenant),
    }


async def get_memory_density_metrics(tenant_id: str = "") -> dict[str, Any]:
    """Summary gap metrics for reactive curiosity decisions.

    Returns counts of orphans, low-confidence facts, imbalances, thin clusters,
    plus a boolean `should_spawn` signal set when enough gaps accumulate to
    warrant an out-of-cycle curiosity-engine run.
    """
    _tenant = tenant_id or DEFAULT_TENANT
    orphans = await find_orphaned_entities(limit=50, tenant_id=_tenant)
    low_conf = await find_low_confidence_facts(limit=50, tenant_id=_tenant)
    imbalances = await find_entity_type_imbalances(tenant_id=_tenant)
    thin = await find_thin_entity_clusters(limit=50, tenant_id=_tenant)
    uncertainty = await find_uncertainty_signals(limit=50, tenant_id=_tenant)

    orphan_count = len(orphans)
    low_conf_count = len(low_conf)
    thin_count = len(thin)
    imbalance_count = len(imbalances)
    uncertainty_count = len(uncertainty)

    # Heuristic: spawn reactively when the gap surface is meaningfully large.
    # Any single dimension can trip it independently — orphans/thin/uncertainty
    # are all signals that curiosity should run sooner rather than waiting
    # for the Sunday cron.
    should_spawn = (
        orphan_count >= 20
        or thin_count >= 15
        or uncertainty_count >= 10
        or low_conf_count >= 30
        or imbalance_count >= 3
    )

    return {
        "orphan_count": orphan_count,
        "low_confidence_count": low_conf_count,
        "thin_cluster_count": thin_count,
        "imbalance_count": imbalance_count,
        "uncertainty_count": uncertainty_count,
        "should_spawn": should_spawn,
    }
