"""n8n monitor tools: read the snapshot the poller keeps, never n8n itself.

``n8n_list_workflows`` and ``n8n_recent_failures`` answer from the tables the
n8n poller (:mod:`robothor.integrations.n8n.poller`) fills, so they cost no
API call, work while n8n is down, and show exactly what the dashboard shows.
Both are read-only and tenant-scoped through ``ctx.tenant_id``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext
    from robothor.integrations.n8n.store import N8nStore

HANDLERS: dict[str, Any] = {}

DEFAULT_HOURS = 24
#: The snapshot keeps 30 days of failures; a longer window would only look complete.
MAX_HOURS = 720
MAX_FAILURES = 100

_NOT_CONFIGURED = (
    "n8n monitoring is not configured on this instance (ROBOTHOR_N8N_ENABLED and "
    "ROBOTHOR_N8N_BASE_URL). Nothing is known about n8n workflows."
)


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _store() -> N8nStore:
    from robothor.integrations.n8n.store import PgN8nStore

    return PgN8nStore()


def _settings() -> Any:
    from robothor.settings import get_settings

    return get_settings()


def _hours(value: Any) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return DEFAULT_HOURS
    return max(1, min(hours, MAX_HOURS))


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _not_configured() -> dict[str, Any]:
    return {"status": "not_configured", "message": _NOT_CONFIGURED}


@_handler("n8n_list_workflows")
async def _n8n_list_workflows(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """The n8n workflows with their failure counts, as of the last poll."""
    from robothor.integrations.n8n.contract import is_configured, workflows_payload

    settings = _settings()
    if not is_configured(settings):
        return {**_not_configured(), "workflows": [], "count": 0}
    group = settings.integrations
    payload = await workflows_payload(
        _store(),
        ctx.tenant_id,
        base_url=group.n8n_base_url,
        poll_seconds=group.n8n_poll_seconds,
        configured=True,
    )
    active_only = _flag(args.get("active_only"), True)
    query = str(args.get("query") or "").strip().lower()
    workflows = [
        w
        for w in payload["workflows"]
        if (w["active"] or not active_only)
        and (
            not query
            or query in w["name"].lower()
            or any(query in tag.lower() for tag in w["tags"])
        )
    ]
    return {**payload, "workflows": workflows, "count": len(workflows)}


@_handler("n8n_recent_failures")
async def _n8n_recent_failures(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Failed n8n executions in the last ``hours``, newest first."""
    from robothor.integrations.n8n.contract import execution_row, is_configured

    settings = _settings()
    hours = _hours(args.get("hours", DEFAULT_HOURS))
    if not is_configured(settings):
        return {**_not_configured(), "hours": hours, "failures": [], "count": 0}
    base_url = settings.integrations.n8n_base_url
    workflow_id = str(args.get("workflow_id") or "").strip() or None
    store = _store()
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await store.list_executions(ctx.tenant_id, workflow_id, MAX_FAILURES, since=since)
    names = {w["workflow_id"]: w["name"] for w in await store.list_workflows(ctx.tenant_id)}
    failures = [
        {
            **execution_row(row, base_url),
            "workflow_id": row["workflow_id"],
            "workflow_name": names.get(row["workflow_id"], row["workflow_id"]),
        }
        for row in rows
    ]
    state = await store.get_sync_state(ctx.tenant_id)
    last_ok = state.last_ok_at.isoformat() if state and state.last_ok_at else None
    return {
        "hours": hours,
        "failures": failures,
        "count": len(failures),
        "last_ok_at": last_ok,
        "truncated": len(failures) >= MAX_FAILURES,
    }
