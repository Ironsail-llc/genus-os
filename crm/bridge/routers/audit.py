"""Audit API — programmatic access to audit logs and guardrail events.

Provides REST endpoints for querying the audit_log and agent_guardrail_events
tables. Protected by Cloudflare Access + RBAC middleware.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/events")
async def get_audit_events(
    since: str | None = Query(None, description="ISO timestamp lower bound"),
    until: str | None = Query(None, description="ISO timestamp upper bound"),
    event_type: str | None = Query(None, description="Filter by event_type"),
    actor: str | None = Query(None, description="Filter by actor (agent_id)"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Query audit log events with filters."""
    try:
        from robothor.audit.logger import query_log

        events = query_log(
            limit=limit,
            event_type=event_type,
            actor=actor,
            since=since,
        )

        # Apply 'until' filter in Python (query_log doesn't support it natively)
        if until:
            events = [e for e in events if e.get("timestamp", "") <= until]

        return {"events": events, "count": len(events)}
    except Exception as e:
        logger.error("Audit events query failed: %s", e)
        return {"events": [], "count": 0, "error": str(e)}


@router.get("/guardrails")
async def get_guardrail_events(
    since: str | None = Query(None, description="ISO timestamp lower bound"),
    until: str | None = Query(None, description="ISO timestamp upper bound"),
    policy: str | None = Query(None, description="Filter by guardrail_name"),
    action: str | None = Query(None, description="Filter by action (blocked/warned/allowed)"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """Query guardrail events."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()

            query = (
                "SELECT id, run_id, step_number, guardrail_name, action, "
                "tool_name, reason, created_at "
                "FROM agent_guardrail_events WHERE 1=1"
            )
            params: list[Any] = []

            if since:
                query += " AND created_at >= %s"
                params.append(since)
            if until:
                query += " AND created_at <= %s"
                params.append(until)
            if policy:
                query += " AND guardrail_name = %s"
                params.append(policy)
            if action:
                query += " AND action = %s"
                params.append(action)

            query += " ORDER BY created_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(query, params)
            rows = cur.fetchall()

        events = [
            {
                "id": str(r[0]),
                "run_id": str(r[1]) if r[1] else None,
                "step_number": r[2],
                "guardrail_name": r[3],
                "action": r[4],
                "tool_name": r[5],
                "reason": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
            }
            for r in rows
        ]

        return {"events": events, "count": len(events)}
    except Exception as e:
        logger.error("Guardrail events query failed: %s", e)
        return {"events": [], "count": 0, "error": str(e)}


@router.get("/stats")
async def get_audit_stats(
    hours: int = Query(24, ge=1, le=720, description="Rolling window in hours"),
) -> dict[str, Any]:
    """Aggregated audit statistics for the given time window."""
    try:
        from robothor.audit.logger import stats as audit_stats

        base_stats = audit_stats()

        # Add guardrail-specific stats
        guardrail_stats = _get_guardrail_stats(hours)

        return {
            "audit_log": base_stats,
            "guardrails": guardrail_stats,
            "window_hours": hours,
        }
    except Exception as e:
        logger.error("Audit stats failed: %s", e)
        return {"error": str(e)}


def _get_guardrail_stats(hours: int) -> dict[str, Any]:
    """Get guardrail event statistics for the given time window."""
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()

            cur.execute(
                """
                SELECT
                    guardrail_name,
                    action,
                    COUNT(*) as count
                FROM agent_guardrail_events
                WHERE created_at >= now() - interval '%s hours'
                GROUP BY guardrail_name, action
                ORDER BY count DESC
                """,
                (hours,),
            )
            rows = cur.fetchall()

        by_policy: dict[str, dict[str, int]] = {}
        total = 0
        for name, action, count in rows:
            by_policy.setdefault(name, {})[action] = count
            total += count

        return {
            "total_events": total,
            "by_policy": by_policy,
        }
    except Exception as e:
        logger.warning("Guardrail stats failed: %s", e)
        return {"total_events": 0, "error": str(e)}
