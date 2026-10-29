"""Operator-scoped, read-only view of an external n8n, from what the poller stored.

The poller (``robothor/integrations/n8n``) writes ``n8n_workflows``,
``n8n_executions`` and ``n8n_sync_state``; this router only reads them. It never
calls n8n itself, so a slow or down n8n cannot slow the dashboard.

Honesty rules, the same as ``system_health``: a monitor that is switched off,
or switched on without a base URL, says ``not_configured``; one that has never
polled, or has not succeeded for three poll intervals, says ``unknown`` rather
than a stale green; and a database error degrades to ``unknown`` with the error
named (on the executions route, an ``error`` beside the empty list) — the route
never 500s.

Each workflow in the listing carries ``workflow_id``, ``name``, ``active``,
``tags``, ``failures_24h``, ``failures_7d``, ``last_failure_at``,
``last_error``, ``last_failure_url`` (the newest failed execution in n8n) and
``url`` (the workflow in n8n). Both links are absolute, or ``""`` when no base
URL is configured: never a path relative to the dashboard.

What counts as configured, where links point and when a snapshot is stale are
the poller's rules, read from ``robothor.integrations.n8n.contract`` so the
badge never disagrees with what the poller does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar

import psycopg2.extras
from deps import get_tenant_id
from fastapi import APIRouter, Depends, Request

from robothor.db.connection import get_connection
from robothor.integrations.n8n import contract
from robothor.integrations.n8n.client import instance_root
from routers._operator import require_operator

if TYPE_CHECKING:
    from collections.abc import Callable

router = APIRouter(prefix="/api/integrations/n8n", tags=["integrations"])

_MAX_LIMIT = 100

T = TypeVar("T")


def _n8n_settings() -> tuple[bool, str, int]:
    """(configured, instance_root, poll_seconds) by the poller's own rules.

    Configured means switched on AND a base URL (``contract.is_configured``):
    the poller refuses to run without one. The root has no ``/api/v1``, and the
    interval is the one the poller actually sleeps for.
    """
    from robothor.settings import get_settings

    settings = get_settings()
    integrations = settings.integrations
    return (
        contract.is_configured(settings),
        instance_root(str(integrations.n8n_base_url or "")),
        contract.effective_poll_seconds(integrations.n8n_poll_seconds),
    )


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
    """ok | warn | unknown for a configured monitor (``contract.status_for``).

    Times read back from the database may be strings; they are parsed first.
    """
    state = dict(sync_state or {})
    state["last_ok_at"] = _as_datetime(state.get("last_ok_at"))
    return contract.status_for(state, poll_seconds, rows)


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
        "FILTER (WHERE e.execution_id IS NOT NULL))[1] AS last_error, "
        "(ARRAY_AGG(e.execution_id ORDER BY e.started_at DESC) "
        "FILTER (WHERE e.execution_id IS NOT NULL))[1] AS last_failed_execution_id "
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


def _workflow_url(root: str, workflow_id: str) -> str:
    """An absolute link into n8n, or ``""`` — never a path relative to the Helm."""
    return contract.workflow_url(root, workflow_id) if root else ""


def _execution_url(root: str, workflow_id: str, execution_id: str) -> str:
    return contract.execution_url(root, workflow_id, execution_id) if root else ""


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
        # The execution that failed last, so the operator lands on the failure
        # rather than the workflow. ``contract.workflow_row()`` emits the same
        # field; the row is built here only so ``name`` can fall back to the id.
        "last_failure_url": (
            _execution_url(base_url, row["workflow_id"], str(failed_id))
            if (failed_id := row.get("last_failed_execution_id"))
            else ""
        ),
        "url": _workflow_url(base_url, row["workflow_id"]),
    }


@router.get("/workflows")
def list_workflows(request: Request, tenant_id: str = Depends(get_tenant_id)) -> dict[str, Any]:
    require_operator(request)
    body: dict[str, Any] = {
        "status": "not_configured",
        "base_url": "",
        "last_poll_at": None,
        "last_ok_at": None,
        "last_error": None,
        "workflows": [],
    }
    settings, settings_error = _safe(_n8n_settings)
    if settings is None:
        # A config the bridge cannot read is not "off" and not healthy.
        body.update(status="unknown", last_error=settings_error)
        return body
    enabled, base_url, poll_seconds = settings
    body["base_url"] = base_url
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
    settings, _settings_error = _safe(_n8n_settings)
    if settings is None or not settings[0]:
        return {"executions": []}
    base_url = settings[1]
    limit = max(1, min(limit, _MAX_LIMIT))
    rows, db_error = _safe(lambda: _execution_rows(tenant_id, workflow_id, limit))
    if db_error:
        # An outage must not read as a clean record of no failures.
        return {"executions": [], "error": db_error}
    return {
        "executions": [
            {
                "execution_id": r["execution_id"],
                "status": r.get("status"),
                "mode": r.get("mode"),
                "started_at": _iso(r.get("started_at")),
                "stopped_at": _iso(r.get("stopped_at")),
                "error_message": r.get("error_message"),
                "url": _execution_url(base_url, workflow_id, r["execution_id"]),
            }
            for r in rows or []
        ]
    }
