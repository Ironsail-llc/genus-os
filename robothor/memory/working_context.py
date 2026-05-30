"""Working-context refresher — regenerates the live operational snapshot.

The ``working_context`` block was previously only *appended* to by the
pre-compression ``[REMEMBER]`` hook (``engine/context.py``), so it accreted
stale items and never reflected *today's* state — operators saw days-old
context. This rebuilds the block deterministically from current open tasks,
recent high-signal facts, and active intents, **replacing** stale content
within a recency window. No LLM call — fast, cheap, and predictable.

Called from the autoDream maintenance pass so it refreshes through the day.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection
from robothor.memory.blocks import write_block

logger = logging.getLogger(__name__)

_BLOCK = "working_context"
_OPEN_STATUSES = ("TODO", "IN_PROGRESS", "REVIEW")
_MAX_TASKS = 12
_MAX_FACTS = 8
_FACT_WINDOW_DAYS = 3
_MAX_INTENTS = 5

# Scheduler run-artifact titles (runner.py mints one crm_task per run) — never
# operational signal, always noise. Excluded from the live snapshot.
_ARTIFACT_SUFFIXES = ("cron run", "workflow run", "sub_agent run", "hook run", "manual run")


def _open_tasks(cur: Any, tenant_id: str) -> list[dict[str, Any]]:
    artifact_clause = " ".join(f"AND title NOT LIKE '%%: {s}'" for s in _ARTIFACT_SUFFIXES)
    cur.execute(
        f"""
        SELECT title, status, COALESCE(next_action, '') AS next_action, updated_at
        FROM crm_tasks
        WHERE tenant_id = %s AND status = ANY(%s) AND deleted_at IS NULL
          {artifact_clause}
        ORDER BY updated_at DESC NULLS LAST
        LIMIT %s
        """,
        (tenant_id, list(_OPEN_STATUSES), _MAX_TASKS),
    )
    return [dict(r) for r in cur.fetchall()]


def _recent_facts(cur: Any, tenant_id: str) -> list[dict[str, Any]]:
    cutoff = datetime.now(UTC) - timedelta(days=_FACT_WINDOW_DAYS)
    cur.execute(
        """
        SELECT fact_text, category
        FROM memory_facts
        WHERE tenant_id = %s AND is_active AND created_at >= %s
          AND importance_score >= 0.6
          AND COALESCE(source_channel, '') <> 'camera'
          AND fact_text NOT ILIKE '%%camera%%'
          AND fact_text NOT ILIKE '%%in the image%%'
        ORDER BY importance_score DESC, created_at DESC
        LIMIT %s
        """,
        (tenant_id, cutoff, _MAX_FACTS),
    )
    return [dict(r) for r in cur.fetchall()]


def _active_intents(cur: Any, tenant_id: str) -> list[dict[str, Any]]:
    try:
        cur.execute(
            """
            SELECT title FROM memory_intents
            WHERE tenant_id = %s AND status = 'active'
            ORDER BY priority ASC, last_advanced_at ASC NULLS FIRST
            LIMIT %s
            """,
            (tenant_id, _MAX_INTENTS),
        )
        return [dict(r) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001 — memory_intents may not exist on older schemas
        return []


def _render(
    tasks: list[dict[str, Any]], facts: list[dict[str, Any]], intents: list[dict[str, Any]]
) -> str:
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Working context — refreshed {ts}", ""]

    lines.append(f"## Open tasks ({len(tasks)})")
    if tasks:
        for t in tasks:
            na = f" → {t['next_action']}" if t.get("next_action") else ""
            lines.append(f"- [{t['status']}] {t['title']}{na}")
    else:
        lines.append("- (none open)")

    lines.append("")
    lines.append(f"## Recent context (last {_FACT_WINDOW_DAYS}d, high-signal)")
    if facts:
        lines.extend(f"- ({f['category']}) {f['fact_text']}" for f in facts)
    else:
        lines.append("- (no recent high-importance facts)")

    if intents:
        lines.append("")
        lines.append("## Standing intents")
        lines.extend(f"- {i['title']}" for i in intents)

    return "\n".join(lines)


def refresh_working_context(tenant_id: str = "") -> dict[str, Any]:
    """Rebuild the working_context block from current live state. Returns stats."""
    tid = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        tasks = _open_tasks(cur, tid)
        facts = _recent_facts(cur, tid)
        intents = _active_intents(cur, tid)

    content = _render(tasks, facts, intents)
    write_block(_BLOCK, content, tenant_id=tid)
    stats = {"tasks": len(tasks), "facts": len(facts), "intents": len(intents)}
    logger.info("refresh_working_context: %s", stats)
    return stats
