"""The scheduler's tick for workspace schedules (``user_cronjobs`` rows with a ``room_id``).

A room's row is CLAIMED before its run starts (mark-before-fire): the claim
moves ``next_run_at`` only if it still holds the value this tick read, so
another tick or replica that read the same row claims nothing and one run
happens. The run goes to ``robothor.rooms.runs.execute_room_run``, which waits
for the room's own turn lock; never the per-agent dedup lock, which would skip
the room whenever its agent is busy elsewhere. One engine serves one tenant
(``EngineConfig.tenant_id``, docs/event-bus.md), so only that tenant's rows are
read.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["tick_room_cronjobs"]


async def tick_room_cronjobs(tenant_id: str, runner: Any) -> None:
    """Claim and fire every due workspace schedule of ``tenant_id``. Never raises."""
    from robothor.engine import user_cron
    from robothor.rooms import runs

    if runner is not None:
        runs.bind_runner(runner)
    now = datetime.now(UTC)
    try:
        due = await asyncio.to_thread(user_cron.list_due_room_cronjobs, tenant_id, now)
    except Exception as e:  # noqa: BLE001 - the next tick retries
        logger.warning("room schedule tick error: %s", e)
        return
    for job in due:
        try:
            payload = job.get("schedule_payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload)
            next_run = user_cron.compute_next_run(payload, now)
            claimed = await asyncio.to_thread(
                user_cron.claim_room_cronjob,
                job["job_id"],
                due_at=job["next_run_at"],
                next_run_at=next_run,
            )
            if claimed:
                run = runs.execute_room_run(tenant_id, job["room_id"], trigger="schedule")
                runs.spawn(run, name=f"room-schedule-{job['room_id']}")
        except Exception as e:  # noqa: BLE001 - one row must not starve the others
            logger.warning("room schedule %s failed to fire: %s", job.get("job_id"), e)
