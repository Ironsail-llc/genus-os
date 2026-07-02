"""
Procedural Memory — the skill library.

Agents record reusable "how to do X" patterns as procedures. A procedure
carries name, description, ordered steps, prerequisites, applicability tags,
and outcome counts (success/failure). When facing a new task, agents query
`find_applicable_procedures(task)` and reuse a proven playbook rather than
re-deriving from scratch.

Schema lives in `memory_procedures` (migration 041).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.llm import ollama as llm_client
from robothor.memory.vector_tuning import apply_hnsw_session

logger = logging.getLogger(__name__)


def _confidence_from_counts(success: int, failure: int) -> float:
    """Wilson-ish confidence: favors procedures with more evidence of success.

    Starts at 0.5 with no data, climbs toward 1.0 with successes, drops
    toward 0.0 with failures. One failure is costly but recoverable.
    """
    total = success + failure
    if total == 0:
        return 0.5
    raw = success / total
    # Shrink toward 0.5 when evidence is thin (bayesian smoothing).
    weight = total / (total + 4)
    return round(0.5 * (1 - weight) + raw * weight, 3)


async def record_procedure(
    name: str,
    steps: list[str],
    description: str = "",
    prerequisites: list[str] | None = None,
    applicable_tags: list[str] | None = None,
    created_by_agent: str = "unknown",
    tenant_id: str | None = None,
) -> int:
    """Create or update a procedure.

    Idempotent on (tenant_id, name) — calling twice with the same name
    overwrites steps/description but preserves success/failure history.
    Returns the procedure id.
    """
    tid = tenant_id or DEFAULT_TENANT
    desc_for_embed = description or name
    embed_input = f"{name}\n{desc_for_embed}\n" + "\n".join(steps[:20])
    try:
        embedding = await llm_client.get_embedding_async(embed_input)
    except Exception as e:
        logger.warning("record_procedure: embedding failed (%s), storing without", e)
        embedding = None

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO memory_procedures
                (tenant_id, name, description, steps, prerequisites,
                 applicable_tags, embedding, created_by_agent)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, name)
            DO UPDATE SET
                description = EXCLUDED.description,
                steps = EXCLUDED.steps,
                prerequisites = EXCLUDED.prerequisites,
                applicable_tags = EXCLUDED.applicable_tags,
                embedding = EXCLUDED.embedding,
                updated_at = NOW()
            RETURNING id
            """,
            (
                tid,
                name,
                description,
                steps,
                prerequisites or [],
                applicable_tags or [],
                embedding,
                created_by_agent,
            ),
        )
        proc_id: int = cur.fetchone()[0]
        return proc_id


async def find_applicable_procedures(
    task_description: str,
    tags: list[str] | None = None,
    limit: int = 3,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Find procedures relevant to a task via semantic search + tag filter.

    Tag filter is ANDed with semantic similarity — if `tags` is provided,
    only procedures whose `applicable_tags` intersect are returned.
    Results are sorted by cosine similarity to `task_description`.
    """
    tid = tenant_id or DEFAULT_TENANT
    try:
        embedding = await llm_client.get_embedding_async(task_description)
    except Exception as e:
        logger.warning("find_applicable_procedures: embed failed (%s)", e)
        return []

    tag_clause = ""
    params: list[Any] = [embedding, tid]
    if tags:
        tag_clause = "AND applicable_tags && %s"
        params.append(tags)
    params.extend([embedding, limit])

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        apply_hnsw_session(cur)
        cur.execute(
            f"""
            SELECT id, name, description, steps, prerequisites, applicable_tags,
                   success_count, failure_count, confidence, last_used_at,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM memory_procedures
            WHERE is_active = TRUE
              AND tenant_id = %s
              AND embedding IS NOT NULL
              {tag_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


async def report_procedure_outcome(
    procedure_id: int,
    success: bool,
    notes: str = "",
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Record the outcome of applying a procedure.

    Updates success_count or failure_count, recomputes confidence, bumps
    last_used_at. Returns the new counts + confidence.
    """
    tid = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT success_count, failure_count FROM memory_procedures
            WHERE id = %s AND tenant_id = %s
            FOR UPDATE
            """,
            (procedure_id, tid),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"procedure {procedure_id} not found for tenant {tid}")
        new_success = row["success_count"] + (1 if success else 0)
        new_failure = row["failure_count"] + (0 if success else 1)
        new_conf = _confidence_from_counts(new_success, new_failure)

        cur.execute(
            """
            UPDATE memory_procedures
            SET success_count = %s,
                failure_count = %s,
                confidence = %s,
                last_used_at = NOW(),
                updated_at = NOW()
            WHERE id = %s
            """,
            (new_success, new_failure, new_conf, procedure_id),
        )

    if notes:
        logger.info(
            "procedure %d outcome=%s notes=%s",
            procedure_id,
            "success" if success else "failure",
            notes[:200],
        )

    return {
        "procedure_id": procedure_id,
        "success_count": new_success,
        "failure_count": new_failure,
        "confidence": new_conf,
        "last_used_at": datetime.now(UTC).isoformat(),
    }


async def get_procedure(
    procedure_id: int,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """Fetch one procedure by id."""
    tid = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, name, description, steps, prerequisites, applicable_tags,
                   success_count, failure_count, confidence, created_by_agent,
                   last_used_at, created_at, updated_at
            FROM memory_procedures
            WHERE id = %s AND tenant_id = %s AND is_active = TRUE
            """,
            (procedure_id, tid),
        )
        row = cur.fetchone()
        return dict(row) if row else None
