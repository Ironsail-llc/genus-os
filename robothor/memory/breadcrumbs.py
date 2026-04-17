"""
Cross-request agent breadcrumbs.

Agents persist mid-task state as breadcrumbs that survive beyond a single
run. On the next run, the runner injects the latest breadcrumbs into
warmup context so the agent picks up where it left off. Breadcrumbs have
a 7-day TTL. Heavily-accessed breadcrumbs (>=3 reads) are promoted to
first-class memory_facts during nightly maintenance.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 7
PROMOTION_ACCESS_THRESHOLD = 3


def leave_breadcrumb(
    agent_id: str,
    content: dict[str, Any] | str,
    run_id: str | None = None,
    ttl_days: int = DEFAULT_TTL_DAYS,
    tenant_id: str | None = None,
) -> int:
    """Persist a breadcrumb for this agent.

    `content` can be a string (wrapped as {"note": content}) or a dict.
    Returns the breadcrumb id.
    """
    tid = tenant_id or DEFAULT_TENANT
    payload = content if isinstance(content, dict) else {"note": str(content)}
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_breadcrumbs
                (tenant_id, agent_id, run_id, content, expires_at)
            VALUES (%s, %s, %s, %s::jsonb, NOW() + (%s || ' days')::INTERVAL)
            RETURNING id
            """,
            (tid, agent_id, run_id, json.dumps(payload), str(ttl_days)),
        )
        return int(cur.fetchone()[0])


def load_recent_breadcrumbs(
    agent_id: str,
    limit: int = 5,
    tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load the most recent non-expired breadcrumbs for an agent.

    Bumps access_count on each returned row so heavily-read breadcrumbs can
    be promoted to memory_facts in nightly maintenance.
    """
    tid = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, content, created_at, expires_at, access_count
            FROM agent_breadcrumbs
            WHERE tenant_id = %s
              AND agent_id = %s
              AND expires_at > NOW()
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (tid, agent_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]

        if rows:
            ids = [r["id"] for r in rows]
            cur.execute(
                """
                UPDATE agent_breadcrumbs
                SET access_count = access_count + 1
                WHERE id = ANY(%s)
                """,
                (ids,),
            )
    return rows


def format_breadcrumbs_for_warmup(breadcrumbs: list[dict[str, Any]]) -> str:
    """Render breadcrumbs as a warmup-friendly text block.

    Empty list returns an empty string so callers can skip injection cleanly.
    """
    if not breadcrumbs:
        return ""
    lines = ["# Breadcrumbs from prior runs"]
    for bc in breadcrumbs:
        created = bc.get("created_at")
        ts = (
            created.strftime("%Y-%m-%d %H:%M")
            if isinstance(created, datetime)
            else str(created or "?")
        )
        content = bc.get("content")
        if isinstance(content, str):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                content = json.loads(content)
        if isinstance(content, dict):
            note = content.get("note") or json.dumps(content, default=str)
        else:
            note = str(content)
        lines.append(f"- [{ts}] {note}")
    return "\n".join(lines[:10])


def prune_expired_breadcrumbs(tenant_id: str | None = None) -> int:
    """Delete breadcrumbs past their expires_at. Returns number deleted.

    ``tenant_id`` bounds the sweep. Nightly maintenance must pass it so a
    multi-tenant instance doesn't prune every tenant on every run. When
    ``None`` (manual global GC), sweeps across all tenants.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        if tenant_id is None:
            cur.execute("DELETE FROM agent_breadcrumbs WHERE expires_at <= NOW()")
        else:
            cur.execute(
                "DELETE FROM agent_breadcrumbs WHERE tenant_id = %s AND expires_at <= NOW()",
                (tenant_id,),
            )
        return int(cur.rowcount)


async def promote_hot_breadcrumbs(
    access_threshold: int = PROMOTION_ACCESS_THRESHOLD,
    tenant_id: str | None = None,
) -> dict[str, int]:
    """Promote heavily-accessed breadcrumbs to memory_facts.

    Any breadcrumb read at least `access_threshold` times becomes a fact
    (category=procedural, source_type=breadcrumb). The original breadcrumb
    is then deleted to avoid double-surfacing.
    """
    tid = tenant_id or DEFAULT_TENANT
    promoted = 0
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT id, agent_id, content
            FROM agent_breadcrumbs
            WHERE tenant_id = %s
              AND access_count >= %s
              AND expires_at > NOW()
            """,
            (tid, access_threshold),
        )
        hot = [dict(r) for r in cur.fetchall()]

    if not hot:
        return {"promoted": 0}

    from robothor.memory.facts import store_fact

    for bc in hot:
        try:
            content = bc["content"]
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    content = {"note": content}
            note = content.get("note") if isinstance(content, dict) else None
            if not note:
                note = json.dumps(content, default=str)[:500]

            fact = {
                "fact_text": f"[{bc['agent_id']}] {note}",
                "category": "procedural",
                "entities": [],
                "confidence": 0.7,
            }
            await store_fact(
                fact,
                source_content=json.dumps(content, default=str)[:1000],
                source_type="breadcrumb",
                metadata={"breadcrumb_id": bc["id"], "agent_id": bc["agent_id"]},
                tenant_id=tid,
            )
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM agent_breadcrumbs WHERE id = %s", (bc["id"],))
            promoted += 1
        except Exception as e:
            logger.warning("breadcrumb promotion failed for id=%s: %s", bc["id"], e)

    return {"promoted": promoted}
