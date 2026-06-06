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
from typing import Any

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
        return fire_at if (fire_at and fire_at > after) else None
    if kind == "cron":
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(str(schedule["expression"]))
        return trigger.get_next_fire_time(None, after)
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
    """Enabled jobs whose next_run_at is due (<= now)."""
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
