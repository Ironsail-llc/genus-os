"""Operator-scoped, read-only view of an external n8n, from what the poller stored.

The poller (``robothor/integrations/n8n``) writes ``n8n_workflows``,
``n8n_executions`` and ``n8n_sync_state``; this router only reads them. It never
calls n8n itself, so a slow or down n8n cannot slow the dashboard.

Honesty rules, the same as ``system_health``: a monitor that is switched off
says ``not_configured``; one that has never polled, or has not succeeded for
three poll intervals, says ``unknown`` rather than a stale green; and a
database error degrades to ``unknown`` with the error named — the route never
500s.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, TypeVar

import psycopg2.extras
from deps import get_tenant_id
from fastapi import APIRouter, Depends, Request

from robothor.db.connection import get_connection
from routers._operator import require_operator

if TYPE_CHECKING:
    from collections.abc import Callable

router = APIRouter(prefix="/api/integrations/n8n", tags=["integrations"])

_MAX_LIMIT = 100
_DEFAULT_POLL_SECONDS = 300

T = TypeVar("T")


def _n8n_settings() -> tuple[bool, str, int]:
    """(enabled, base_url, poll_seconds) from the settings the poller declares.

    Guarded with ``getattr`` so the bridge imports and answers
    ``not_configured`` on an install whose settings do not declare them yet.
    """
    from robothor.settings import get_settings

    integrations = getattr(get_settings(), "integrations", None)
    enabled = bool(getattr(integrations, "n8n_enabled", False))
    base_url = str(getattr(integrations, "n8n_base_url", "") or "").rstrip("/")
    poll = getattr(integrations, "n8n_poll_seconds", None) or _DEFAULT_POLL_SECONDS
    return enabled, base_url, int(poll)


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def status_for(
    sync_state: dict[str, Any] | None, poll_seconds: int, rows: list[dict[str, Any]]
) -> str:
    """ok | warn | unknown for an ENABLED monitor (``not_configured`` is the route's).

    unknown — never polled, never succeeded, or the last success is older than
    three poll intervals. warn — any workflow failed in the last 24 h, or the
    last poll recorded an error. Otherwise ok.
    """
    if not sync_state:
        return "unknown"
    last_ok = _as_datetime(sync_state.get("last_ok_at"))
    if last_ok is None:
        return "unknown"
    if datetime.now(UTC) - last_ok > timedelta(seconds=3 * poll_seconds):
        return "unknown"
    if sync_state.get("last_error") or any((r.get("failures_24h") or 0) > 0 for r in rows):
        return "warn"
    return "ok"


def _rows(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _safe(fn: Callable[[], T]) -> tuple[T | None, str | None]:
    try:
        return fn(), None
    except Exception as exc:  # a probe failure must never 500 or read as healthy
        return None, str(exc)


def _sync_state(tenant_id: str) -> dict[str, Any] | None:
    rows = _rows(
        "SELECT last_poll_at, last_ok_at, last_error FROM n8n_sync_state "
        "WHERE tenant_id = %s LIMIT 1",
        (tenant_id,),
    )
    return rows[0] if rows else None


def _workflow_rows(tenant_id: str) -> list[dict[str, Any]]:
    return _rows(
        "SELECT w.workflow_id, w.name, w.active, w.tags, "
        "COUNT(e.execution_id) FILTER (WHERE e.started_at > now() - interval '24 hours') "
        "AS failures_24h, "
        "COUNT(e.execution_id) FILTER (WHERE e.started_at > now() - interval '7 days') "
        "AS failures_7d, "
        "MAX(e.started_at) AS last_failure_at, "
        "(ARRAY_AGG(e.error_message ORDER BY e.started_at DESC) "
        "FILTER (WHERE e.execution_id IS NOT NULL))[1] AS last_error "
        "FROM n8n_workflows w "
        "LEFT JOIN n8n_executions e ON e.tenant_id = w.tenant_id "
        "AND e.workflow_id = w.workflow_id AND e.status IN ('error', 'crashed') "
        "AND e.started_at > now() - interval '7 days' "
        "WHERE w.tenant_id = %s "
        "GROUP BY w.workflow_id, w.name, w.active, w.tags "
        "ORDER BY failures_24h DESC, w.name",
        (tenant_id,),
    )


def _execution_rows(tenant_id: str, workflow_id: str, limit: int) -> list[dict[str, Any]]:
    return _rows(
        "SELECT execution_id, status, mode, started_at, stopped_at, error_message "
        "FROM n8n_executions WHERE tenant_id = %s AND workflow_id = %s "
        "ORDER BY started_at DESC NULLS LAST LIMIT %s",
        (tenant_id, workflow_id, limit),
    )


def _workflow_out(row: dict[str, Any], base_url: str) -> dict[str, Any]:
    return {
        "workflow_id": row["workflow_id"],
        "name": row.get("name") or row["workflow_id"],
        "active": bool(row.get("active")),
        "tags": list(row.get("tags") or []),
        "failures_24h": int(row.get("failures_24h") or 0),
        "failures_7d": int(row.get("failures_7d") or 0),
        "last_failure_at": _iso(row.get("last_failure_at")),
        "last_error": row.get("last_error"),
        "url": f"{base_url}/workflow/{row['workflow_id']}",
    }


@router.get("/workflows")
def list_workflows(request: Request, tenant_id: str = Depends(get_tenant_id)) -> dict[str, Any]:
    require_operator(request)
    enabled, base_url, poll_seconds = _n8n_settings()
    body: dict[str, Any] = {
        "status": "not_configured",
        "base_url": base_url,
        "last_poll_at": None,
        "last_ok_at": None,
        "last_error": None,
        "workflows": [],
    }
    if not enabled:
        return body

    sync, sync_error = _safe(lambda: _sync_state(tenant_id))
    rows, rows_error = _safe(lambda: _workflow_rows(tenant_id))
    db_error = sync_error or rows_error
    if db_error:
        body.update(status="unknown", last_error=db_error)
        return body

    rows = rows or []
    body.update(
        status=status_for(sync, poll_seconds, rows),
        last_poll_at=_iso((sync or {}).get("last_poll_at")),
        last_ok_at=_iso((sync or {}).get("last_ok_at")),
        last_error=(sync or {}).get("last_error"),
        workflows=[_workflow_out(r, base_url) for r in rows],
    )
    return body


@router.get("/workflows/{workflow_id}/executions")
def workflow_executions(
    workflow_id: str,
    request: Request,
    limit: int = 20,
    tenant_id: str = Depends(get_tenant_id),
) -> dict[str, Any]:
    require_operator(request)
    _, base_url, _ = _n8n_settings()
    limit = max(1, min(limit, _MAX_LIMIT))
    rows, _error = _safe(lambda: _execution_rows(tenant_id, workflow_id, limit))
    return {
        "executions": [
            {
                "execution_id": r["execution_id"],
                "status": r.get("status"),
                "mode": r.get("mode"),
                "started_at": _iso(r.get("started_at")),
                "stopped_at": _iso(r.get("stopped_at")),
                "error_message": r.get("error_message"),
                "url": f"{base_url}/workflow/{workflow_id}/executions/{r['execution_id']}",
            }
            for r in rows or []
        ]
    }
