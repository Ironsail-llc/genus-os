"""User-authored cron jobs (Rip 8) — runtime self-scheduling.

Backs the ``register_user_cron`` tool: an agent (or the operator) turns a
natural-language schedule into a ``cron_parse.parse_schedule()`` payload and
persists a row in ``user_cronjobs`` (migration 070). The scheduler's periodic
tick fires due jobs and advances ``next_run_at``.

DB access follows the tracking.py pattern (get_connection, best-effort).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from psycopg2.extras import RealDictCursor

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)


def _serialize_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    """JSON-safe copy of a parse_schedule() dict (datetime → isoformat)."""
    out = dict(schedule)
    fire_at = out.get("fire_at")
    if isinstance(fire_at, datetime):
        out["fire_at"] = fire_at.isoformat()
    return out


def compute_next_run(schedule: dict[str, Any], after: datetime) -> datetime | None:
    """Next fire time strictly after ``after``, or None if the job is exhausted.

    interval → after + every_seconds; once → the fixed instant (None once past);
    cron → APScheduler's next cron time.
    """
    kind = schedule.get("kind")
    if kind == "interval":
        return after + timedelta(seconds=int(schedule["every_seconds"]))
    if kind == "once":
        fire_at = schedule.get("fire_at")
        if isinstance(fire_at, str):
            fire_at = datetime.fromisoformat(fire_at)
        if not fire_at:
            # Malformed is different from late: there is no moment to schedule.
            return None
        # A one-shot whose moment has passed fires on the next tick rather
        # than being discarded. The previous `fire_at > after` dropped it to
        # None, create_user_cronjob wrote next_run_at=NULL, the poller (which
        # selects next_run_at <= now) never saw it, and the caller still got a
        # job_id back — so "remind me at 3pm" asked at 3:01pm returned a
        # confirmation and did nothing, with no error and no log line.
        # The request was late, not wrong.
        return max(after, fire_at)
    if kind == "cron":
        from robothor.engine.crontab import cron_trigger

        # A room's schedule carries its time zone. A plain user cron carries
        # none and keeps APScheduler's default (the host's zone), so no
        # existing job moves.
        trigger = cron_trigger(str(schedule["expression"]), timezone=schedule.get("tz") or None)
        return cast("datetime | None", trigger.get_next_fire_time(None, after))
    return None


def create_user_cronjob(
    *,
    agent_id: str,
    prompt: str,
    schedule: dict[str, Any],
    tenant_id: str = DEFAULT_TENANT,
    created_by_session: str | None = None,
    max_fires: int | None = None,
) -> dict[str, Any]:
    """Insert a user cronjob row. Returns ``{job_id, next_run_at}``."""
    now = datetime.now(UTC)
    next_run = compute_next_run(schedule, now)
    job_id = f"ucron-{uuid.uuid4().hex[:12]}"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_cronjobs (
                job_id, tenant_id, created_by_session, agent_id,
                schedule_kind, schedule_payload, prompt, enabled,
                next_run_at, max_fires
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s)
            """,
            (
                job_id,
                tenant_id,
                created_by_session,
                agent_id,
                schedule["kind"],
                json.dumps(_serialize_schedule(schedule)),
                prompt,
                next_run,
                max_fires,
            ),
        )
    return {"job_id": job_id, "next_run_at": next_run.isoformat() if next_run else None}


def list_due_cronjobs(
    tenant_id: str = DEFAULT_TENANT, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Enabled jobs whose next_run_at is due (<= now).

    A room's schedule row is not one of them: a room's run is a run in that room
    as its run-as user, which this generic tick does not know how to start.
    """
    now = now or datetime.now(UTC)
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT * FROM user_cronjobs
            WHERE enabled = TRUE
              AND next_run_at IS NOT NULL
              AND next_run_at <= %s
              AND tenant_id = %s
              AND room_id IS NULL
            ORDER BY next_run_at
            """,
            (now, tenant_id),
        )
        return [dict(r) for r in cur.fetchall()]


def mark_cronjob_fired(job_id: str, *, next_run_at: datetime | None, disable: bool = False) -> None:
    """Advance a job after it fires: bump fire_count, set next_run_at, maybe disable."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE user_cronjobs
            SET last_run_at = NOW(),
                fire_count = fire_count + 1,
                next_run_at = %s,
                enabled = CASE WHEN %s THEN FALSE ELSE enabled END,
                updated_at = NOW()
            WHERE job_id = %s
            """,
            (next_run_at, disable, job_id),
        )


# ── A room's schedule (robothor.rooms.definition) ───────────────────────────


def get_room_cronjob(tenant_id: str, room_id: str) -> dict[str, Any] | None:
    """The schedule row of a room, or None. ``run_as_user_id`` as a string."""
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM user_cronjobs WHERE tenant_id = %s AND room_id = %s "
            "ORDER BY created_at LIMIT 1",
            (tenant_id, room_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["room_id"] = str(out["room_id"])
    out["run_as_user_id"] = str(out["run_as_user_id"]) if out.get("run_as_user_id") else None
    return out


def sync_room_cronjob(
    *,
    tenant_id: str,
    room_id: str,
    agent_id: str,
    prompt: str,
    schedule: dict[str, Any] | None,
    run_as_user_id: str | None,
    enabled: bool,
) -> dict[str, Any] | None:
    """Make a room's one schedule row match the room.

    ``schedule`` None (the room has no schedule) disables the row, if there is
    one, and returns None. Otherwise the row is created or rewritten, and its
    next run is computed from now when it is enabled (so a resumed room does not
    fire for the runs it missed) and cleared when it is not. A row that keeps
    the same schedule and stays enabled keeps its next run. Returns
    ``{job_id, next_run_at, enabled}``.
    """
    now = datetime.now(UTC)
    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if schedule is None:
            cur.execute(
                "UPDATE user_cronjobs SET enabled = FALSE, next_run_at = NULL, updated_at = NOW() "
                "WHERE tenant_id = %s AND room_id = %s",
                (tenant_id, room_id),
            )
            return None
        next_run = compute_next_run(schedule, now) if enabled else None
        # One statement against the partial unique index of migration 170, so
        # concurrent syncs of a room with no row yet still leave exactly one.
        cur.execute(
            """
            INSERT INTO user_cronjobs (
                job_id, tenant_id, room_id, agent_id, schedule_kind, schedule_payload,
                prompt, enabled, next_run_at, run_as_user_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, room_id) WHERE room_id IS NOT NULL DO UPDATE SET
                agent_id = EXCLUDED.agent_id,
                schedule_kind = EXCLUDED.schedule_kind,
                schedule_payload = EXCLUDED.schedule_payload,
                prompt = EXCLUDED.prompt,
                enabled = EXCLUDED.enabled,
                -- Same schedule, still running: keep the next run, so editing
                -- the instructions does not push an interval schedule back.
                next_run_at = CASE
                    WHEN user_cronjobs.schedule_payload = EXCLUDED.schedule_payload
                     AND user_cronjobs.enabled AND EXCLUDED.enabled
                     AND user_cronjobs.next_run_at IS NOT NULL
                    THEN user_cronjobs.next_run_at
                    ELSE EXCLUDED.next_run_at
                END,
                run_as_user_id = EXCLUDED.run_as_user_id,
                updated_at = NOW()
            RETURNING job_id, next_run_at
            """,
            (
                f"ucron-{uuid.uuid4().hex[:12]}",
                tenant_id,
                room_id,
                agent_id,
                schedule["kind"],
                json.dumps(_serialize_schedule(schedule)),
                prompt or "",
                bool(enabled),
                next_run,
                run_as_user_id,
            ),
        )
        row = cur.fetchone()
        job_id, next_run = row["job_id"], row["next_run_at"]
    return {
        "job_id": job_id,
        "next_run_at": next_run.isoformat() if next_run else None,
        "enabled": bool(enabled),
    }
