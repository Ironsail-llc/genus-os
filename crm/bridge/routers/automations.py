"""One card per scheduled agent, with run truth: ran, delivered, completed.

Before this route the Helm could tell an operator that an automation EXISTS
(the manifest list) and, on a different screen, that some run somewhere had a
status (the Runs list). Neither answered the only question an operator actually
asks about a nightly agent: *did last night's one happen, did the result reach
me, and was it any good*. Those three facts live in three different places and
nothing joined them.

So this router joins them, and the join is the whole of what it is:

``_manifests``
    What the automation IS — name, cron, timezone, enabled, delivery target.
    Read through ``agent_manifests._scan`` / ``_summary``, imported rather than
    re-derived: a second reader of the manifest directory is a second set of
    rules about what a manifest means, and the two would drift.
``_schedule_rows``
    What the scheduler BELIEVES — ``next_run_at`` and ``consecutive_errors``,
    which exist nowhere else. Tenant-scoped in the statement, not merely by
    RLS, so the scoping is visible in the diff that would break it.
``_latest_runs``
    What actually HAPPENED — the newest ``agent_runs`` row per agent, carrying
    the delivery and verification columns the Runs list was already storing and
    never showing.

Two properties this file is built around:

* **The manifest is the source of truth, and the schedule row is the fallback**
  (CLAUDE.md rule 4). A manifest whose cron was removed but whose schedule row
  the engine still holds is listed with the row's cron, because an automation
  that is still firing is not made invisible by the fact that somebody meant to
  stop it.
* **The breaker threshold is imported, never copied.** ``breaker_tripped`` is a
  claim about what the scheduler will do next, and a local ``5`` would be a
  claim about what it did in August. See ``_threshold``.

Every route here is a plain ``def``: the work is psycopg2 and a directory scan,
both of which belong in FastAPI's worker threadpool rather than on the loop
serving every other request (``tests/test_route_concurrency.py``).
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg2.extras
from fastapi import APIRouter, HTTPException, Request

from robothor.db.connection import get_connection
from robothor.engine.sanitize import sanitize_log
from routers import agent_manifests
from routers._audit import audited
from routers._operator import PLATFORM_TENANT, require_operator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/automations", tags=["automations"])

#: The columns of the newest run that answer "delivered" and "completed".
#:
#: ``delivery_status`` and ``outcome_assessment`` were already on the Runs
#: listing; ``delivered_at``, ``delivery_channel`` and ``verified_status`` were
#: being written by the engine and read by nothing. All five are here because
#: the three columns on the card are three different questions — a run that
#: completed with ``delivery_status = 'failed'`` is the case the single status
#: pill was quietly reporting as green.
_RUN_COLUMNS = (
    "agent_id, id, started_at, status, duration_ms, delivery_status, delivered_at, "
    "delivery_channel, delivery_mode, verified_status, outcome_assessment"
)


def _threshold() -> int:
    """How many consecutive errors the scheduler skips an agent after.

    Imported from the scheduler rather than copied: this number is the
    scheduler's own policy, it has moved before, and a stale copy here would
    paint a card green while the engine had already stopped running it.

    Lazily, because importing the scheduler drags in APScheduler and the whole
    engine job registry — a cost the bridge should pay once, on the first
    request that needs the number, and never at import time.
    """
    from robothor.engine.scheduler import CIRCUIT_BREAKER_THRESHOLD

    return int(CIRCUIT_BREAKER_THRESHOLD)


def _query(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def _isoformat(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: (value.isoformat() if hasattr(value, "isoformat") else value)
        for key, value in row.items()
    }


def _manifests() -> list[dict[str, Any]]:
    """The manifest half of every row.

    ``_summary`` is the fleet-list shape and carries the delivery MODE only;
    the channel and the address live on the document, and the card names them
    because "delivered" with no channel is not an answer. One unreadable
    manifest is skipped rather than 500ing the page, the same bargain
    ``list_manifests`` already makes.
    """
    scan = agent_manifests._scan()
    rows: list[dict[str, Any]] = []
    for document in scan.manifests:
        try:
            summary = agent_manifests._summary(document)
        except Exception as error:  # noqa: BLE001 — one manifest, not the page
            logger.warning(
                "Could not summarise %s for automations: %s",
                sanitize_log(str(document.get("id") or "")),
                type(error).__name__,
            )
            continue
        delivery = agent_manifests._block(document, "delivery")
        summary["delivery_channel"] = delivery.get("channel") or ""
        summary["delivery_to"] = delivery.get("to") or ""
        rows.append(summary)
    return rows


def _schedule_rows(tenant_id: str) -> list[dict[str, Any]]:
    """What the scheduler holds for this tenant.

    Scoped in the statement as well as by RLS. ``agent_schedules`` is written
    by the engine process, whose connection is not the operator's, and a
    join that relied on the policy alone would silently widen the day somebody
    ran this route on a connection without one.
    """
    rows = _query(
        "SELECT agent_id, enabled, cron_expr, timezone, next_run_at, consecutive_errors, "
        "delivery_mode, delivery_channel, delivery_to FROM agent_schedules "
        "WHERE tenant_id = %s",
        (tenant_id,),
    )
    return [_isoformat(row) for row in rows]


def _latest_runs(agent_ids: list[str]) -> dict[str, dict[str, Any]]:
    """The newest run of each of these agents, keyed by agent.

    ``DISTINCT ON`` rather than a per-agent ``LIMIT 1``: one statement for the
    whole page instead of one per card, and the alternative — fetching every
    run and keeping the first — reads the entire table to answer a question
    about twenty rows.
    """
    if not agent_ids:
        return {}
    rows = _query(
        f"SELECT DISTINCT ON (agent_id) {_RUN_COLUMNS} FROM agent_runs "
        "WHERE agent_id = ANY(%s) ORDER BY agent_id, started_at DESC NULLS LAST",
        (list(agent_ids),),
    )
    return {str(row["agent_id"]): _isoformat(row) for row in rows}


def _last_run(run: dict[str, Any] | None) -> dict[str, Any] | None:
    """One run, as the three columns of the card read it.

    ``agent_id`` is dropped: it is the join key, already the id of the card it
    is sitting on, and repeating it invites a reader to trust the copy.
    """
    if run is None:
        return None
    return {
        "id": str(run.get("id") or ""),
        "started_at": run.get("started_at"),
        "status": run.get("status"),
        "duration_ms": run.get("duration_ms"),
        "delivery_status": run.get("delivery_status"),
        "delivered_at": run.get("delivered_at"),
        "delivery_channel": run.get("delivery_channel"),
        "delivery_mode": run.get("delivery_mode"),
        "verified_status": run.get("verified_status"),
        "outcome_assessment": run.get("outcome_assessment"),
    }


def _compose(
    manifest: dict[str, Any],
    schedule: dict[str, Any],
    run: dict[str, Any] | None,
    threshold: int,
) -> dict[str, Any]:
    """One card. Manifest first, schedule row as the fallback.

    The fallback direction is the point. The manifest is what the operator
    edits and what the engine will load on its next reconcile; the schedule row
    is what it loaded last. Where they disagree the manifest is the answer for
    a field the operator can change (cron, timezone, enabled) — except when the
    manifest has nothing to say, which is how a cron the operator deleted but
    the engine is still firing stays visible instead of reading as "no schedule".
    """
    errors = int(schedule.get("consecutive_errors") or 0)
    return {
        "id": manifest["id"],
        "name": manifest.get("name") or manifest["id"],
        "kind": "agent",
        "description": manifest.get("description") or "",
        "cron": manifest.get("cron") or schedule.get("cron_expr") or "",
        "timezone": manifest.get("timezone") or schedule.get("timezone") or "",
        "enabled": bool(manifest.get("enabled", schedule.get("enabled", True))),
        "next_run_at": schedule.get("next_run_at"),
        "last_run": _last_run(run),
        "consecutive_errors": errors,
        "breaker_tripped": errors >= threshold,
        "breaker_threshold": threshold,
        "delivery": {
            "mode": manifest.get("delivery") or schedule.get("delivery_mode") or "none",
            "channel": manifest.get("delivery_channel") or schedule.get("delivery_channel") or "",
            "to": manifest.get("delivery_to") or schedule.get("delivery_to") or "",
        },
    }


def _automations(tenant_id: str) -> list[dict[str, Any]]:
    manifests = _manifests()
    schedules = {str(row.get("agent_id") or ""): row for row in _schedule_rows(tenant_id)}

    # A manifest with neither a cron nor a schedule row is not an automation:
    # it is an agent that only runs when something triggers it, and it has its
    # own screen. A schedule row with no manifest is dropped the other way —
    # there is no name, no delivery target and nothing an operator could edit.
    listed = [
        manifest for manifest in manifests if manifest.get("cron") or manifest["id"] in schedules
    ]
    threshold = _threshold()
    latest = _latest_runs([manifest["id"] for manifest in listed])
    rows = [
        _compose(manifest, schedules.get(manifest["id"], {}), latest.get(manifest["id"]), threshold)
        for manifest in listed
    ]
    rows.sort(key=lambda row: row["id"])
    return rows


def _reset_breaker(agent_id: str, tenant_id: str) -> bool:
    """Zero one agent's error count. ``False`` when no such row for this tenant."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE agent_schedules SET consecutive_errors = 0, updated_at = NOW() "
                "WHERE agent_id = %s AND tenant_id = %s",
                (agent_id, tenant_id),
            )
            return bool(cur.rowcount > 0)


@router.get("")
def list_automations(request: Request) -> dict[str, Any]:
    """Every scheduled agent, with what its last run did.

    ``require_operator`` on a GET for the same reason the fleet listing has
    one: this enumerates the appliance's schedule and its delivery targets,
    which is not member data.
    """
    require_operator(request)
    rows = _automations(PLATFORM_TENANT)
    return {"automations": rows, "count": len(rows)}


@router.post("/{agent_id}/reset-breaker")
def reset_breaker(agent_id: str, request: Request) -> dict[str, Any]:
    """Let a tripped automation run again.

    The scheduler stops running an agent after ``CIRCUIT_BREAKER_THRESHOLD``
    consecutive failures and offers no way back except editing the table by
    hand — which is the ssh session this whole surface exists to remove. It is
    audited because it is an act that makes an agent start doing things again,
    and the trail has to name who decided that.
    """
    require_operator(request)
    agent_id = agent_manifests._safe_id(agent_id)
    if not _reset_breaker(agent_id, PLATFORM_TENANT):
        audited(
            request,
            "automation.reset_breaker",
            action=agent_id,
            status="error",
            reason="no_schedule_row",
        )
        raise HTTPException(status_code=404, detail="no schedule for that automation")
    audited(request, "automation.reset_breaker", action=agent_id)
    return {"id": agent_id, "consecutive_errors": 0}
