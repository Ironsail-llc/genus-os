"""
Phase 2b — Contact 360 linkage for agent runs.

Two responsibilities:

  1. ``resolve_run_person_id`` — pure resolver. Given (trigger_type,
     trigger_detail), parse the chat_id out of "chat:<id>[|sender:<n>]" and
     look it up via contact_identifiers. Returns the matching person_id or
     None. Used by the runner just before persisting an agent_runs row.

  2. ``emit_run_timeline_activity`` — best-effort write into timeline_activity
     so the agent_run shows up on the contact's unified feed. No-op when the
     run has no person_id (e.g. cron jobs).

Both helpers are tiny on purpose: they're the contract the runner depends
on, and they're easy to unit-test against the test DB.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

if TYPE_CHECKING:
    from robothor.engine.models import AgentRun

logger = logging.getLogger(__name__)


_TELEGRAM_TRIGGERS = {"telegram", "chat"}


def _parse_chat_id(trigger_detail: str | None) -> str | None:
    """Extract '<chat_id>' from 'chat:<chat_id>[|sender:<name>]'."""
    if not trigger_detail or not trigger_detail.startswith("chat:"):
        return None
    head = trigger_detail.split("|", 1)[0]
    rest = head.split(":", 1)[1] if ":" in head else None
    return rest or None


def resolve_run_person_id(
    *,
    trigger_type: str,
    trigger_detail: str | None,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Look up the contact_identifiers row that matches a telegram trigger
    and return person_id (UUID string) or None. Cron/hook/manual triggers
    return None — no contact context."""
    tt = trigger_type.value if hasattr(trigger_type, "value") else str(trigger_type)
    if tt.lower() not in _TELEGRAM_TRIGGERS:
        return None
    chat_id = _parse_chat_id(trigger_detail)
    if not chat_id:
        return None
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ci.person_id
                  FROM contact_identifiers ci
                  JOIN crm_people p ON p.id = ci.person_id
                 WHERE ci.channel = 'telegram'
                   AND ci.identifier = %s
                   AND p.deleted_at IS NULL
                 LIMIT 1
                """,
                (chat_id,),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 — best-effort lookup
        logger.debug("resolve_run_person_id failed: %s", e)
        return None
    if not row:
        return None
    pid = row[0] if not isinstance(row, dict) else row.get("person_id")
    return str(pid) if pid is not None else None


def emit_run_timeline_activity(run: AgentRun) -> None:
    """Insert a timeline_activity row for an agent_run, if the run is linked
    to a person. Idempotent on (tenant, source_table, source_id)."""
    if not getattr(run, "person_id", None):
        return
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            status_val = run.status.value if hasattr(run.status, "value") else str(run.status)
            trigger_val = (
                run.trigger_type.value
                if hasattr(run.trigger_type, "value")
                else str(run.trigger_type)
            )
            title = f"{run.agent_id} ({status_val})"
            snippet = (run.output_text or "")[:200] if run.output_text else None
            metadata = {
                "status": status_val,
                "duration_ms": run.duration_ms,
                "trigger_type": trigger_val,
            }
            cur.execute(
                """
                INSERT INTO timeline_activity
                    (tenant_id, person_id, occurred_at, activity_type,
                     source_table, source_id, agent_id, title, snippet, metadata)
                VALUES (%s, %s, NOW(), 'agent_run', 'agent_runs', %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (tenant_id, source_table, source_id) DO NOTHING
                """,
                (
                    run.tenant_id or DEFAULT_TENANT,
                    run.person_id,
                    run.id,
                    run.agent_id,
                    title,
                    snippet,
                    _json(metadata),
                ),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("timeline_activity emit failed for run %s: %s", run.id, e)


def _json(d: dict[str, Any]) -> str:
    import json

    def _safe(o: Any) -> Any:
        if hasattr(o, "isoformat"):
            return o.isoformat()
        return str(o)

    return json.dumps(d, default=_safe)
