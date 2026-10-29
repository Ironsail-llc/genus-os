"""The shape the bridge serves for the n8n section, and the status it shows.

Shared by the bridge routes (``GET /api/integrations/n8n/workflows`` and
``GET /api/integrations/n8n/workflows/{id}/executions``) and the agent tools,
so a row means the same thing on the dashboard and in an agent's answer::

    workflows:  {status, base_url, last_poll_at, last_ok_at, last_error,
                 workflows: [{workflow_id, name, active, tags, failures_24h,
                              failures_7d, last_failure_at, last_error, url}]}
    executions: {executions: [{execution_id, status, mode, started_at,
                               stopped_at, error_message, url}]}

Times are ISO-8601 strings (or null). ``status`` is one of ``ok``, ``warn``,
``unknown`` or ``not_configured`` -- see :func:`status_for`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from robothor.integrations.n8n.client import instance_root

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from robothor.integrations.n8n.store import N8nStore
    from robothor.settings.model import GenusSettings

__all__ = [
    "MIN_POLL_SECONDS",
    "STALE_INTERVALS",
    "Status",
    "execution_row",
    "effective_poll_seconds",
    "execution_url",
    "executions_payload",
    "is_configured",
    "status_for",
    "workflow_row",
    "workflow_url",
    "workflows_payload",
]

Status = Literal["ok", "warn", "unknown", "not_configured"]

#: How many poll intervals may pass before the snapshot counts as stale.
STALE_INTERVALS = 3
#: The shortest poll interval the poller honours, whatever the setting says.
MIN_POLL_SECONDS = 30


def effective_poll_seconds(poll_seconds: int) -> int:
    """The interval the poller actually sleeps for ``n8n_poll_seconds``."""
    return max(MIN_POLL_SECONDS, int(poll_seconds))


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def is_configured(settings: GenusSettings) -> bool:
    """The monitor is on and knows where n8n is (the key is the doctor's question)."""
    group = settings.integrations
    return bool(group.n8n_enabled and (group.n8n_base_url or "").strip())


def workflow_url(base_url: str, workflow_id: str) -> str:
    return f"{instance_root(base_url)}/workflow/{workflow_id}"


def execution_url(base_url: str, workflow_id: str, execution_id: str) -> str:
    return f"{workflow_url(base_url, workflow_id)}/executions/{execution_id}"


def workflow_row(row: Mapping[str, Any], base_url: str) -> dict[str, Any]:
    """One workflow as the contract shows it."""
    wid = str(row["workflow_id"])
    return {
        "workflow_id": wid,
        "name": row.get("name") or "",
        "active": bool(row.get("active")),
        "tags": [str(t) for t in row.get("tags") or []],
        "failures_24h": int(row.get("failures_24h") or 0),
        "failures_7d": int(row.get("failures_7d") or 0),
        "last_failure_at": _iso(row.get("last_failure_at")),
        "last_error": row.get("last_error"),
        "url": workflow_url(base_url, wid),
    }


def execution_row(row: Mapping[str, Any], base_url: str) -> dict[str, Any]:
    """One failed execution as the contract shows it."""
    eid = str(row["execution_id"])
    return {
        "execution_id": eid,
        "status": row.get("status") or "error",
        "mode": row.get("mode") or "",
        "started_at": _iso(row.get("started_at")),
        "stopped_at": _iso(row.get("stopped_at")),
        "error_message": row.get("error_message"),
        "url": execution_url(base_url, str(row.get("workflow_id") or ""), eid),
    }


def status_for(
    sync_state: Any,
    poll_seconds: int,
    rows: Iterable[Mapping[str, Any]],
    *,
    configured: bool = True,
    now: datetime | None = None,
) -> Status:
    """The one-word status of the n8n section.

    ``not_configured`` when the monitor is off; ``unknown`` when it has never
    polled successfully or the last success is older than
    :data:`STALE_INTERVALS` poll intervals; ``warn`` when any workflow failed
    in the last 24 hours or the latest poll failed; otherwise ``ok``.
    ``sync_state`` is an :class:`~robothor.integrations.n8n.store.N8nSyncState`
    or a mapping with the same fields.
    """
    if not configured:
        return "not_configured"
    last_ok = _field(sync_state, "last_ok_at")
    if not isinstance(last_ok, datetime):
        return "unknown"
    current = now or datetime.now(UTC)
    if current - last_ok > timedelta(
        seconds=STALE_INTERVALS * effective_poll_seconds(poll_seconds)
    ):
        return "unknown"
    if _field(sync_state, "last_error") or any(int(r.get("failures_24h") or 0) for r in rows):
        return "warn"
    return "ok"


async def workflows_payload(
    store: N8nStore,
    tenant_id: str,
    *,
    base_url: str,
    poll_seconds: int,
    configured: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The whole ``GET /api/integrations/n8n/workflows`` response."""
    if not configured:
        return {
            "status": "not_configured",
            "base_url": base_url,
            "last_poll_at": None,
            "last_ok_at": None,
            "last_error": None,
            "workflows": [],
        }
    state = await store.get_sync_state(tenant_id)
    rows = await store.list_workflows(tenant_id, now=now)
    return {
        "status": status_for(state, poll_seconds, rows, now=now),
        "base_url": instance_root(base_url),
        "last_poll_at": _iso(_field(state, "last_poll_at")),
        "last_ok_at": _iso(_field(state, "last_ok_at")),
        "last_error": _field(state, "last_error"),
        "workflows": [workflow_row(r, base_url) for r in rows],
    }


async def executions_payload(
    store: N8nStore,
    tenant_id: str,
    workflow_id: str,
    *,
    base_url: str,
    limit: int = 20,
) -> dict[str, Any]:
    """The whole ``GET /api/integrations/n8n/workflows/{id}/executions`` response."""
    rows = await store.list_executions(tenant_id, workflow_id, max(1, min(int(limit), 100)))
    return {"executions": [execution_row(r, base_url) for r in rows]}
