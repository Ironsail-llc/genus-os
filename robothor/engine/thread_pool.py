"""Thread pool — ordered view of open multi-beat work for the main heartbeat.

A "thread" is a ``crm_tasks`` row tagged ``thread`` that represents ongoing work
spanning multiple heartbeat cycles — research questions, stuck experiments,
broken agents, overdue reports, PR follow-through. This module reads the pool;
creation is inline via ``update_task(tags=[..., 'thread'])`` from heartbeat
instructions.

Priority order (first → last):
1. Philip-blocked: ``requires_human`` AND status = REVIEW
2. SLA-breached: ``sla_deadline_at < now``
3. Most-escalated: ``escalation_count`` desc
4. Oldest-touched: ``COALESCE(updated_at, created_at)`` asc

Injected into Main's warmup via ``register_agent_context_hook``. Never raises —
a failing hook silently returns ``None``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)

MAX_THREADS = 8
MAX_TITLE_CHARS = 60
MAX_LINE_CHARS = 140


@dataclass(frozen=True)
class Thread:
    """One thread-tagged open task with derived status signals."""

    id: str
    title: str
    status: str
    priority: str
    age_days: int
    stale_days: int
    requires_human: bool
    sla_breached: bool
    escalation_count: int
    open_children: int
    total_children: int
    assigned_to_agent: str | None

    @property
    def short_id(self) -> str:
        return self.id[:8]


_LIST_SQL = """
SELECT
  t.id::text,
  t.title,
  t.status,
  COALESCE(t.priority, 'normal'),
  t.requires_human,
  COALESCE(t.escalation_count, 0),
  t.assigned_to_agent,
  GREATEST(0, EXTRACT(DAY FROM (NOW() - t.created_at))::int) AS age_days,
  GREATEST(0, EXTRACT(DAY FROM (NOW() - COALESCE(t.updated_at, t.created_at)))::int) AS stale_days,
  (t.sla_deadline_at IS NOT NULL AND t.sla_deadline_at < NOW()) AS sla_breached,
  (
    SELECT COUNT(*) FROM crm_tasks c
    WHERE c.parent_task_id = t.id AND c.deleted_at IS NULL
  ) AS total_children,
  (
    SELECT COUNT(*) FROM crm_tasks c
    WHERE c.parent_task_id = t.id AND c.deleted_at IS NULL AND c.status != 'DONE'
  ) AS open_children
FROM crm_tasks t
WHERE t.deleted_at IS NULL
  AND t.tenant_id = %s
  AND t.status != 'DONE'
  AND 'thread' = ANY(t.tags)
ORDER BY
  CASE WHEN t.requires_human AND t.status = 'REVIEW' THEN 0 ELSE 1 END,
  CASE WHEN t.sla_deadline_at IS NOT NULL AND t.sla_deadline_at < NOW() THEN 0 ELSE 1 END,
  COALESCE(t.escalation_count, 0) DESC,
  COALESCE(t.updated_at, t.created_at) ASC
LIMIT %s
"""


def list_threads(tenant_id: str = DEFAULT_TENANT, limit: int = MAX_THREADS) -> list[Thread]:
    """Return open thread-tagged tasks ordered by staleness priority.

    Fast enough (<50ms on typical fleet) to run in a 100ms warmup hook budget.
    """
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(_LIST_SQL, (tenant_id, limit))
        rows = cur.fetchall()

    return [
        Thread(
            id=r[0],
            title=r[1] or "(untitled)",
            status=r[2],
            priority=r[3],
            requires_human=bool(r[4]),
            escalation_count=int(r[5]),
            assigned_to_agent=r[6],
            age_days=int(r[7]),
            stale_days=int(r[8]),
            sla_breached=bool(r[9]),
            total_children=int(r[10]),
            open_children=int(r[11]),
        )
        for r in rows
    ]


def format_thread_pool(threads: list[Thread]) -> str:
    """Format threads as a compact text block for warmup injection.

    Always returns a non-empty string — an empty pool produces a prompt to
    promote ongoing work; a non-empty pool produces one line per thread.
    """
    if not threads:
        return (
            "--- THREAD POOL ---\n"
            "(empty) No open threads tagged `thread`. If you notice multi-heartbeat "
            "work slipping through the cracks (research, stuck experiments, overdue "
            "reports, stale PRs), promote it: update_task(id, tags=[..., 'thread'])."
        )

    lines = ["--- THREAD POOL — advance, close, escalate, or propose at least one per beat ---"]
    for t in threads:
        title = t.title
        if len(title) > MAX_TITLE_CHARS:
            title = title[: MAX_TITLE_CHARS - 1] + "…"

        markers: list[str] = []
        if t.requires_human and t.status == "REVIEW":
            markers.append("🧑PHILIP")
        if t.sla_breached:
            markers.append("⏰SLA")
        if t.escalation_count > 0:
            markers.append(f"↑{t.escalation_count}")
        if t.total_children:
            done = t.total_children - t.open_children
            markers.append(f"kids:{done}/{t.total_children}")

        marker_str = " ".join(markers)
        assignee = f" @{t.assigned_to_agent}" if t.assigned_to_agent else ""
        line = (
            f"[{t.short_id}][{t.status}][{t.stale_days}d] {title}"
            + (f"  {marker_str}" if marker_str else "")
            + assignee
        )
        if len(line) > MAX_LINE_CHARS:
            line = line[: MAX_LINE_CHARS - 1] + "…"
        lines.append(line)
    return "\n".join(lines)


def _thread_pool_context(config: AgentConfig) -> str | None:
    """Agent context hook — inject thread pool view for main agent heartbeat.

    Returns None for any agent other than main so other agents' warmups aren't
    cluttered. Swallows all exceptions to match the warmup contract.
    """
    if config.id != "main":
        return None
    try:
        tenant_id = os.environ.get("ROBOTHOR_TENANT_ID", "") or DEFAULT_TENANT
        threads = list_threads(tenant_id=tenant_id)
        return format_thread_pool(threads)
    except Exception as exc:
        logger.debug("Thread pool hook failed: %s", exc)
        return None
