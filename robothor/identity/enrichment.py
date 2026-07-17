"""CRM/memory-graph enrichment for a resolved ``IdentityContext``.

Given a resolved identity's ``person_id``, pulls together the context an
agent needs to actually recognize who it's talking to: CRM affiliation
(company, job title), the memory graph's relationship lines (bridged via
``contact_identifiers.memory_entity_id``), and cross-channel activity history
(``robothor.crm.dal.get_person_summary``).

Never raises: any DB error is logged and swallowed — enrichment is a nice-to-
have layered on top of identity resolution, and must never be able to break
the auth path that already succeeded in ``resolve_identity``.
"""

from __future__ import annotations

import logging
import time

from psycopg2.extras import RealDictCursor

from robothor.crm.dal import get_person_summary
from robothor.db.connection import get_connection
from robothor.identity.context import EnrichedIdentity, IdentityContext

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60
_RELATIONSHIP_LIMIT = 5

_cache: dict[tuple[str, str], tuple[EnrichedIdentity | None, float]] = {}


def enrich_identity(ctx: IdentityContext) -> EnrichedIdentity | None:
    """Build an ``EnrichedIdentity`` for ``ctx.person_id``.

    Returns ``None`` when ``ctx.person_id`` is unset (no CRM link to enrich
    from) or when enrichment fails for any reason.
    """
    if not ctx.person_id:
        return None

    cache_key = (ctx.person_id, ctx.tenant_id)
    cached = _cache.get(cache_key)
    if cached is not None:
        value, expires_at = cached
        if time.monotonic() < expires_at:
            return value
        del _cache[cache_key]

    try:
        result = _build_enrichment(ctx.person_id, ctx.tenant_id)
    except Exception:
        logger.exception(
            "enrich_identity: enrichment failed for person_id=%s tenant_id=%s",
            ctx.person_id,
            ctx.tenant_id,
        )
        result = None

    _cache[cache_key] = (result, time.monotonic() + _CACHE_TTL_SECONDS)
    return result


def clear_cache() -> None:
    """Clear the enrichment cache."""
    _cache.clear()


def _build_enrichment(person_id: str, tenant_id: str) -> EnrichedIdentity:
    job_title: str | None = None
    company: str | None = None
    relationships: tuple[str, ...] = ()

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute(
            """
            SELECT p.job_title, c.name AS company_name
              FROM crm_people p
              LEFT JOIN crm_companies c ON c.id = p.company_id
             WHERE p.id = %s AND p.tenant_id = %s AND p.deleted_at IS NULL
            """,
            (person_id, tenant_id),
        )
        person_row = cur.fetchone()
        if person_row:
            job_title = person_row.get("job_title") or None
            company = person_row.get("company_name") or None

        cur.execute(
            """
            SELECT memory_entity_id
              FROM contact_identifiers
             WHERE person_id = %s AND tenant_id = %s AND memory_entity_id IS NOT NULL
             LIMIT 1
            """,
            (person_id, tenant_id),
        )
        entity_row = cur.fetchone()

        if entity_row:
            entity_id = entity_row["memory_entity_id"]
            cur.execute(
                """
                SELECT r.relation_type,
                       me.name AS other_name
                  FROM memory_relations r
                  JOIN memory_entities me
                    ON me.id = CASE WHEN r.source_entity_id = %s
                                     THEN r.target_entity_id
                                     ELSE r.source_entity_id END
                 WHERE (r.source_entity_id = %s OR r.target_entity_id = %s)
                   AND r.tenant_id = %s
                 ORDER BY r.confidence DESC
                 LIMIT %s
                """,
                (entity_id, entity_id, entity_id, tenant_id, _RELATIONSHIP_LIMIT),
            )
            relationships = tuple(
                f"{row['relation_type']} → {row['other_name']}" for row in cur.fetchall()
            )

    summary = get_person_summary(person_id, tenant_id=tenant_id)
    counts: dict[str, int] = dict(summary.get("counts") or {})
    last_touched_raw = summary.get("last_touched_at")
    last_touched_at: str | None = None
    if last_touched_raw is not None and hasattr(last_touched_raw, "isoformat"):
        last_touched_at = last_touched_raw.isoformat()

    return EnrichedIdentity(
        company=company,
        job_title=job_title,
        relationships=relationships,
        last_touched_at=last_touched_at,
        activity_counts=counts,
    )
