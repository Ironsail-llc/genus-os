"""Tool failures over a window, grouped by tool, agent and normalized error.

Three tables record a failed call, and this is the one place that reads them
together, for one tenant:

``tool``           ``agent_tool_events`` rows that did not succeed (the
                   benchmark sandbox's expected refusals are left out, as the
                   degradation detector leaves them out).
``denied``         ``audit_log`` ``agent.tool_call`` rows with status
                   ``denied`` (RBAC and allowlist denials).
``workflow_step``  ``workflow_run_steps`` rows with status ``failed``.

Each error is redacted and normalized (:func:`normalize_error`) so the same
failure about a different record groups as one. ``genus engine failures``
prints the result.
"""

from __future__ import annotations

import re
from typing import Any

from robothor.constants import SANDBOX_DENIED_ERROR_TYPE

__all__ = ["collect", "normalize_error"]

_UUID = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE
)
_HEX = re.compile(r"\b(?:0x)?[0-9a-f]{8,}\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")

_Row = tuple[str, str, Any, int, Any]


def normalize_error(text: Any) -> str:
    """``text`` redacted, with ids and numbers collapsed, at most 200 characters."""
    from robothor.engine.log_fields import short_reason

    clean = short_reason(text, limit=400)
    if not clean:
        return "(no message)"
    clean = _UUID.sub("<id>", clean)
    clean = _HEX.sub("<id>", clean)
    clean = _NUMBER.sub("<n>", clean)
    return clean[:200]


def _query(sql: str, params: tuple[Any, ...]) -> list[_Row]:
    from robothor.db.connection import get_connection

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return [tuple(r) for r in cur.fetchall()]  # type: ignore[misc]


def _tool_event_rows(hours: int, tenant_id: str) -> list[_Row]:
    return _query(
        """
        SELECT e.tool_name, r.agent_id, COALESCE(e.error_message, e.error_type),
               COUNT(*), MAX(e.created_at)
        FROM agent_tool_events e
        JOIN agent_runs r ON r.id = e.run_id
        WHERE NOT e.success
          AND e.created_at > NOW() - make_interval(hours => %s)
          AND r.tenant_id = %s
          AND (e.error_type IS NULL OR e.error_type <> %s)
        GROUP BY 1, 2, 3
        """,
        (hours, tenant_id, SANDBOX_DENIED_ERROR_TYPE),
    )


def _denied_rows(hours: int, tenant_id: str) -> list[_Row]:
    return _query(
        """
        SELECT action, actor, details->>'error', COUNT(*), MAX(timestamp)
        FROM audit_log
        WHERE event_type = 'agent.tool_call'
          AND status = 'denied'
          AND timestamp > NOW() - make_interval(hours => %s)
          AND tenant_id = %s
        GROUP BY 1, 2, 3
        """,
        (hours, tenant_id),
    )


def _workflow_step_rows(hours: int, tenant_id: str) -> list[_Row]:
    return _query(
        """
        SELECT COALESCE(s.tool_name, s.step_id), w.workflow_id, s.error_message,
               COUNT(*), MAX(COALESCE(s.completed_at, s.created_at))
        FROM workflow_run_steps s
        JOIN workflow_runs w ON w.id = s.run_id
        WHERE s.status = 'failed'
          AND s.created_at > NOW() - make_interval(hours => %s)
          AND w.tenant_id = %s
        GROUP BY 1, 2, 3
        """,
        (hours, tenant_id),
    )


def collect(*, hours: int, tenant_id: str) -> list[dict[str, Any]]:
    """Every failure group in the window, largest first.

    A group is ``{source, tool, agent, workflow, error, count, last_seen}``;
    ``agent`` is empty for a workflow step and ``workflow`` for the others.
    """
    groups: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    sources = (
        ("tool", _tool_event_rows),
        ("denied", _denied_rows),
        ("workflow_step", _workflow_step_rows),
    )
    for source, fetch in sources:
        for tool, who, error, count, last_seen in fetch(hours, tenant_id):
            agent, workflow = (
                ("", str(who or "")) if source == "workflow_step" else (str(who or ""), "")
            )
            key = (source, str(tool or ""), agent, workflow, normalize_error(error))
            group = groups.setdefault(
                key,
                {
                    "source": source,
                    "tool": key[1],
                    "agent": agent,
                    "workflow": workflow,
                    "error": key[4],
                    "count": 0,
                    "last_seen": None,
                },
            )
            group["count"] += int(count or 0)
            if last_seen is not None and (
                group["last_seen"] is None or last_seen > group["last_seen"]
            ):
                group["last_seen"] = last_seen
    return sorted(groups.values(), key=lambda g: (-g["count"], g["source"], g["tool"]))
