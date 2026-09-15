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
from robothor.engine.schedule_reconcile import KIND_AGENT, KIND_HEARTBEAT, KIND_WORKER
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

#: The engine's three job kinds, imported so the two sides cannot drift.
#: ``schedule_reconcile`` composes ``<id>``, ``<id>:heartbeat`` and
#: ``<id>:worker``; everything here that parses or builds one of those ids goes
#: through :func:`_split_job_id` or :func:`_jobs_of`.
_JOB_SUFFIXES = frozenset({KIND_HEARTBEAT, KIND_WORKER})


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


def _split_job_id(job_id: str) -> tuple[str, str]:
    """``("main:heartbeat")`` → ``("main", "heartbeat")``; a bare id → kind ``agent``.

    The inverse of what ``schedule_reconcile`` composes. Kept as one function so
    the two directions cannot drift: everything that reads an
    ``agent_schedules`` row and everything that writes a route parameter comes
    through here.
    """
    agent_id, _, suffix = job_id.partition(":")
    kind = suffix if suffix in _JOB_SUFFIXES else KIND_AGENT
    return agent_id, kind


def _jobs_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Every job one manifest declares, in the engine's own ids.

    This is the whole of the "which automations exist" question, and getting it
    wrong is what made the primary agent invisible. ``schedule_reconcile``
    derives up to THREE jobs from one manifest — ``<id>`` from ``schedule.cron``,
    ``<id>:heartbeat`` from ``heartbeat.cron``, ``<id>:worker`` from
    ``worker.cron`` — and the scheduler writes each of those as an
    ``agent_schedules.agent_id``. ``_summary`` reads ``schedule.cron`` only, so
    an agent scheduled purely by heartbeat and worker (the shape of the primary
    agent on a real instance) had neither a "cron" nor a matching schedule row
    and was dropped from both arms of the listing.

    One card per JOB rather than one per manifest, because the job is the unit
    everything else here is keyed by: the schedule row, the circuit breaker
    (``scheduler.py`` trips per dedup key) and therefore the reset.
    """
    summary = agent_manifests._summary(document)
    agent_id = summary["id"]
    if not agent_id:
        return []

    delivery = agent_manifests._block(document, "delivery")
    base = {
        **summary,
        "agent_id": agent_id,
        "delivery_channel": delivery.get("channel") or "",
        "delivery_to": delivery.get("to") or "",
    }

    jobs: list[dict[str, Any]] = []
    if summary["cron"]:
        # The form's FORM_OWNED_PATHS covers schedule.cron/schedule.timezone and
        # nothing else, so this is the only kind whose schedule a PATCH can
        # actually change. The others say so rather than offering a form that
        # posts and alters nothing.
        jobs.append({**base, "id": agent_id, "kind": KIND_AGENT, "editable": True})

    for kind in (KIND_HEARTBEAT, KIND_WORKER):
        block = agent_manifests._block(document, kind)
        cron = block.get("cron")
        if not cron:
            continue
        sub_delivery = agent_manifests._block(block, "delivery")
        jobs.append(
            {
                **base,
                "id": f"{agent_id}:{kind}",
                "kind": kind,
                "editable": False,
                "name": f"{base['name'] or agent_id} · {kind}",
                "cron": str(cron),
                "timezone": block.get("timezone") or summary["timezone"],
                "delivery": sub_delivery.get("mode") or base["delivery"],
                "delivery_channel": sub_delivery.get("channel") or base["delivery_channel"],
                "delivery_to": sub_delivery.get("to") or base["delivery_to"],
            }
        )
    return jobs


def _manifests() -> list[dict[str, Any]]:
    """Every job every readable manifest declares.

    One unreadable manifest is skipped rather than 500ing the page, the same
    bargain ``list_manifests`` already makes — and a manifest the loader could
    not parse at all still reaches the operator, as a degraded card built from
    its schedule row (see ``_automations``).
    """
    scan = agent_manifests._scan()
    rows: list[dict[str, Any]] = []
    for document in scan.manifests:
        try:
            rows.extend(_jobs_of(document))
        except Exception as error:  # noqa: BLE001 — one manifest, not the page
            logger.warning(
                "Could not summarise %s for automations: %s",
                sanitize_log(str(document.get("id") or "")),
                type(error).__name__,
            )
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


def _latest_runs(agent_ids: list[str], tenant_id: str) -> dict[str, dict[str, Any]]:
    """The newest run of each of these agents, for this tenant, keyed by agent.

    ``DISTINCT ON`` rather than a per-agent ``LIMIT 1``: one statement for the
    whole page instead of one per card, and the alternative — fetching every
    run and keeping the first — reads the entire table to answer a question
    about twenty rows.

    Scoped in the statement for the same reason the schedule query is, and it
    is not decoration: RLS on ``agent_runs`` engages only when the connection
    carries a scope, and the bridge sets none — so without this predicate a
    foreign tenant's NEWER run wins the ``DISTINCT ON`` and its delivery status
    and verification verdict are rendered as this automation's.
    """
    if not agent_ids:
        return {}
    rows = _query(
        f"SELECT DISTINCT ON (agent_id) {_RUN_COLUMNS} FROM agent_runs "
        "WHERE agent_id = ANY(%s) AND tenant_id = %s "
        "ORDER BY agent_id, started_at DESC NULLS LAST",
        (list(agent_ids), tenant_id),
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
        "agent_id": manifest["agent_id"],
        "kind": manifest["kind"],
        "editable": bool(manifest.get("editable")),
        "manifest_unreadable": bool(manifest.get("manifest_unreadable")),
        "name": manifest.get("name") or manifest["id"],
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


def _degraded(job_id: str) -> dict[str, Any]:
    """A card for a job whose manifest the loader could not read.

    The engine keeps firing a job it already holds — a blocked reconcile prunes
    nothing — so dropping the row as "no manifest" makes a still-running,
    still-failing automation disappear from the one screen built to notice
    that. This is the 2026-08-24 manifest outage in miniature: the file broke,
    the agent kept going, and every surface went quiet.

    Everything on it comes from the schedule row. ``editable`` is false: there
    is nothing to offer an operator a form for until the YAML parses again.
    """
    agent_id, kind = _split_job_id(job_id)
    return {
        "id": job_id,
        "agent_id": agent_id,
        "kind": kind,
        "name": job_id,
        "description": "",
        "cron": "",
        "timezone": "",
        "enabled": True,
        "delivery": "",
        "delivery_channel": "",
        "delivery_to": "",
        "editable": False,
        "manifest_unreadable": True,
    }


def _automations(tenant_id: str) -> list[dict[str, Any]]:
    """Every job that has a declared cron or a row the engine is holding.

    Both directions matter. A manifest job with no row yet is an automation the
    scheduler has not reconciled — it belongs on screen, saying so. A row with
    no manifest job is an automation still firing from a file that will not
    parse — it belongs on screen even more.
    """
    schedules = {str(row.get("agent_id") or ""): row for row in _schedule_rows(tenant_id)}
    manifests = _manifests()

    # A manifest job with neither a cron nor a schedule row is not an
    # automation: it is an agent that only runs when something triggers it, and
    # it has its own screen.
    listed = [
        manifest for manifest in manifests if manifest.get("cron") or manifest["id"] in schedules
    ]
    known = {manifest["id"] for manifest in listed}
    listed.extend(_degraded(job_id) for job_id in schedules if job_id and job_id not in known)

    threshold = _threshold()
    latest = _latest_runs([manifest["id"] for manifest in listed], tenant_id)
    rows = [
        _compose(manifest, schedules.get(manifest["id"], {}), latest.get(manifest["id"]), threshold)
        for manifest in listed
    ]
    rows.sort(key=lambda row: row["id"])
    return rows


def _safe_job_id(job_id: object) -> str:
    """A job id the engine could have written, or a 422 naming the rule it broke.

    ``agent_manifests._safe_id`` is the kebab rule for a MANIFEST id and refuses
    the colon outright, which made ``main:heartbeat`` — the row an operator most
    often needs to reset — unreachable through this route. So: the agent half
    still goes through that same validator (which is what refuses ``..``, a
    slash, an uppercase letter or anything path-shaped), and the suffix is
    checked against the engine's own two kinds rather than a pattern. A third
    colon, or a suffix the engine never derives, is not a job id.
    """
    raw = str(job_id)
    agent_part, separator, suffix = raw.partition(":")
    validated = agent_manifests._safe_id(agent_part)
    if not separator:
        return validated
    if suffix not in _JOB_SUFFIXES:
        raise HTTPException(
            status_code=422,
            detail=f"expected an agent id, or one ending :{' or :'.join(sorted(_JOB_SUFFIXES))}",
        )
    return f"{validated}:{suffix}"


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
    agent_id = _safe_job_id(agent_id)
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
