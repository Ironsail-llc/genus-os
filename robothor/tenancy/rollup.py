"""How each tenant in an organisation is doing, one entry per tenant.

A parent tenant's owner asks the parent's agent "how are the companies
doing?". Every tool filters on the run's own tenant, so the honest answer used
to be "I can only see the parent". This module answers it for a list of tenant
ids the caller has already decided may be read: the ``org_overview`` engine
tool (``robothor/engine/tools/handlers/org.py``) and, later, the dashboard.

Authorisation is the CALLER's job. This module reads whatever tenants it is
given, each one inside ``tenant_scope(tenant)``, so with row-level security on
every query for a tenant is limited to that tenant's rows at the database.

Rules the shape of the result follows:

* A tenant with no runs in seven days is ``no_activity``, with the time of its
  last run. Zero failures because nothing ran is not health.
* One tenant's read failing does not fail the rollup: that tenant is reported
  with ``status: "error"`` and the rest are still answered.
* The fleet-health benchmark break-out reads EVERY tenant's benchmark rows on
  purpose (``analytics.get_fleet_health``). It is skipped here: repeating an
  all-tenant query once per company would both cost N times and put the same
  cross-tenant numbers in every company's entry.

Synchronous and blocking: call it off the event loop. It must not import the
engine's tool modules, because the dashboard bridge imports it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.db.connection import get_connection, tenant_scope

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

#: How late an enabled schedule's next run may be before it counts as overdue.
#: The scheduler stamps next_run_at when a run starts, so a few minutes of lag
#: is ordinary; a quarter of an hour is not.
SCHEDULE_GRACE_MINUTES = 15

#: How many of the oldest overdue tasks each entry names.
OLDEST_OVERDUE_LIMIT = 5

#: How many failure clusters each entry carries.
TOP_FAILURES_LIMIT = 5


def tenant_rollup(
    tenant_ids: Iterable[str],
    *,
    now: datetime | None = None,
    own_tenant: str | None = None,
    own_scope: Any = None,
) -> dict[str, Any]:
    """The rollup for each of ``tenant_ids``, in the order given, once each.

    ``own_scope`` is the caller's data scope (``identity.scope.DataScope``) in
    its OWN tenant, ``own_tenant``: the overdue tasks that entry names are read
    through it, as ``list_tasks`` would read them for that caller. Counts stay
    unscoped, as ``list_tasks_summary`` shows them. A caller that may read a
    child tenant is an owner, admin or unattended run, which no scope narrows.
    """
    as_of = now or datetime.now(UTC)
    meta = _tenant_meta()
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tenant_id in tenant_ids:
        if not tenant_id or tenant_id in seen:
            continue
        seen.add(tenant_id)
        info = meta.get(tenant_id, {})
        head = {
            "tenant_id": tenant_id,
            "display_name": info.get("displayName") or tenant_id,
            "parent_id": info.get("parentTenantId"),
        }
        try:
            with tenant_scope(tenant_id):
                scope = own_scope if tenant_id == own_tenant else None
                entries.append({**head, **_one_tenant(tenant_id, as_of, scope)})
        except Exception as exc:  # noqa: BLE001 - one company must not sink the rest
            logger.warning("tenant rollup for %s failed: %s", tenant_id, type(exc).__name__)
            entries.append(
                {
                    **head,
                    "status": "error",
                    "error": f"could not read this tenant ({type(exc).__name__})",
                }
            )
    return {"as_of": as_of.isoformat(), "tenants": entries}


def _tenant_meta() -> dict[str, dict[str, Any]]:
    """Name and parent of every tenant. ``crm_tenants`` carries no RLS."""
    from robothor.crm.dal import list_tenants

    try:
        return {t["id"]: t for t in list_tenants(active_only=False) if "id" in t}
    except Exception as exc:  # noqa: BLE001 - names are decoration; ids still answer
        logger.warning("tenant names unavailable: %s", type(exc).__name__)
        return {}


def _one_tenant(tenant_id: str, as_of: datetime, scope: Any = None) -> dict[str, Any]:
    """Every figure for one tenant. Runs inside that tenant's scope."""
    from robothor.engine.analytics import get_failure_patterns, get_fleet_health
    from robothor.engine.tracking import list_schedules

    day = get_fleet_health(days=1, tenant_id=tenant_id, include_benchmark=False)["fleet_totals"]
    week = get_fleet_health(days=7, tenant_id=tenant_id, include_benchmark=False)["fleet_totals"]
    patterns = get_failure_patterns(hours=24, tenant_id=tenant_id).get("patterns") or []
    schedules = list_schedules(tenant_id=tenant_id)
    last = _last_activity_at(tenant_id)

    stuck = [
        {
            "agent_id": s["agent_id"],
            "consecutive_errors": int(s.get("consecutive_errors") or 0),
            "last_status": s.get("last_status"),
        }
        for s in schedules
        if int(s.get("consecutive_errors") or 0) > 0
    ]
    overdue_schedules = _overdue_schedules(schedules, as_of)
    tasks = _tasks(tenant_id, as_of, scope)

    entry: dict[str, Any] = {
        "runs_24h": int(day.get("total_runs") or 0),
        "failures_24h": int(day.get("failed") or 0),
        "success_rate_24h": day.get("success_rate"),
        "cost_usd_24h": float(day.get("total_cost_usd") or 0.0),
        "runs_7d": int(week.get("total_runs") or 0),
        "failures_7d": int(week.get("failed") or 0),
        "success_rate_7d": week.get("success_rate"),
        "cost_usd_7d": float(week.get("total_cost_usd") or 0.0),
        "top_failures": patterns[:TOP_FAILURES_LIMIT],
        "agents_with_consecutive_errors": stuck,
        "overdue_schedules": overdue_schedules,
        "tasks": tasks,
        "last_activity_at": last.isoformat() if last else None,
    }
    entry["status"] = _status(entry)
    return entry


def _status(entry: dict[str, Any]) -> str:
    if entry["runs_7d"] == 0:
        return "no_activity"
    if (
        entry["failures_24h"]
        or entry["agents_with_consecutive_errors"]
        or entry["overdue_schedules"]
        or entry["tasks"]["overdue"]
    ):
        return "attention"
    return "ok"


def _overdue_schedules(schedules: list[dict[str, Any]], as_of: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in schedules:
        next_run = _as_datetime(s.get("next_run_at"))
        if not s.get("enabled") or next_run is None:
            continue
        late = (as_of - next_run).total_seconds() / 60
        if late > SCHEDULE_GRACE_MINUTES:
            out.append(
                {
                    "agent_id": s["agent_id"],
                    "next_run_at": next_run.isoformat(),
                    "overdue_minutes": int(late),
                }
            )
    return out


def _tasks(tenant_id: str, as_of: datetime, scope: Any = None) -> dict[str, Any]:
    from robothor.crm.dal import list_tasks

    counts = _task_counts(tenant_id)
    oldest = list_tasks(overdue=True, limit=OLDEST_OVERDUE_LIMIT, tenant_id=tenant_id, scope=scope)
    return {
        "open": counts["open"],
        "overdue": counts["overdue"],
        "oldest_overdue": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "status": t.get("status"),
                "priority": t.get("priority") or "normal",
                "assignedToAgent": t.get("assignedToAgent"),
                "dueAt": t.get("dueAt"),
                "overdueDays": _days_between(t.get("dueAt"), as_of),
                "ageDays": _days_between(t.get("createdAt"), as_of),
            }
            for t in oldest
        ],
    }


def _task_counts(tenant_id: str) -> dict[str, int]:
    """Open and overdue task counts, with the predicates ``dal.list_tasks`` uses."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE status IS DISTINCT FROM 'DONE'),
                COUNT(*) FILTER (WHERE status IS DISTINCT FROM 'DONE' AND due_at < NOW())
            FROM crm_tasks
            WHERE tenant_id = %s AND deleted_at IS NULL
            """,
            (tenant_id,),
        )
        row = cur.fetchone() or (0, 0)
    return {"open": int(row[0] or 0), "overdue": int(row[1] or 0)}


def _last_activity_at(tenant_id: str) -> datetime | None:
    """When this tenant last started a run, however long ago."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(created_at) FROM agent_runs WHERE tenant_id = %s", (tenant_id,))
        row = cur.fetchone()
    return _as_datetime(row[0]) if row else None


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


def _days_between(value: Any, as_of: datetime) -> int | None:
    then = _as_datetime(value)
    return None if then is None else int((as_of - then).total_seconds() // 86400)
