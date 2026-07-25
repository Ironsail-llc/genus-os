"""Durable queue for deferred memory writes.

``store_memory`` extracts facts with an LLM on the request path. Measured with
the model warm that is ~23s; the production p50 is 63.5s because the 32B
generation model is almost never resident, and 15 of 121 calls over 30 days
were killed at the 120s tool wall. The fleet wrote itself a fallback skill
because of it.

Deferring extraction fixes the latency and introduces a worse failure mode: a
write the agent was told had succeeded, silently lost.
``robothor/engine/task_registry.py`` is the right executor — chat.py and
telegram.py already defer memory writes through it — but ``daemon.py`` drains
it with a 10-second budget against a ~60-second job under ``Restart=always``,
so every deploy would destroy in-flight work with no record.

So the row comes first and the spawn second. The row answers the only question
that matters after an interruption: was a write promised and not completed?

Re-processing is safe: ``MEMORY_WRITE_DEDUP`` is live and migration 078 gives a
partial unique index on active ``(tenant_id, content_hash)``, so a duplicate
extraction produces no duplicate rows.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.db.connection import get_connection

logger = logging.getLogger(__name__)

# How long a job may sit in `running` before the sweeper assumes its process
# died. Comfortably above a slow cold-load extraction so a merely-slow job is
# not double-processed.
DEFAULT_STALE_AFTER_MINUTES = 15
DEFAULT_MAX_ATTEMPTS = 3


def async_write_enabled() -> bool:
    """Whether store_memory defers extraction. Off by default.

    Deferring removes read-after-write, which is an observable contract change
    for agents, so it ships behind a flag and is promoted on evidence like every
    other rollout here.
    """
    raw = os.environ.get("MEMORY_ASYNC_WRITE", "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def enqueue_write(
    content: str,
    *,
    content_type: str = "conversation",
    tenant_id: str = "",
    agent_id: str | None = None,
    run_id: str | None = None,
) -> int:
    """Record the promise. Returns the job id.

    Deliberately synchronous against the database and deliberately first: if the
    row were written after extraction, a crash during extraction would leave no
    evidence a write had been promised.
    """
    tid = tenant_id or DEFAULT_TENANT
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO memory_write_jobs (tenant_id, content, content_type, agent_id, run_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (tid, content, content_type, agent_id, run_id),
        )
        job_id = int(cur.fetchone()[0])
        conn.commit()
    return job_id


def _finish(job_id: int, status: str, *, fact_ids: list[int] | None = None, error: str = "") -> None:
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE memory_write_jobs SET status=%s, fact_ids=%s, error=%s, updated_at=NOW() "
            "WHERE id=%s",
            (status, fact_ids, error[:2000] or None, job_id),
        )
        conn.commit()


async def process_write_job(job_id: int) -> bool:
    """Run one job to completion. Returns True on success.

    Never raises: this runs detached, so an exception would surface only in the
    application log. The failure is recorded on the row instead, where the
    sweeper and any operator surface can see it.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE memory_write_jobs SET status='running', attempts = attempts + 1, "
            "updated_at=NOW() WHERE id=%s RETURNING content, content_type, tenant_id",
            (job_id,),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        logger.warning("memory write job %s vanished before processing", job_id)
        return False

    content, content_type, tenant_id = row

    try:
        # Imported here so tests can patch robothor.memory.facts.extract_facts
        # and have the patch take effect.
        from robothor.memory import facts as facts_mod

        extracted = await facts_mod.extract_facts(content)
        if not extracted:
            extracted = [
                {
                    "fact_text": content,
                    "category": "personal",
                    "entities": [],
                    "confidence": 0.5,
                    "metadata": {"extraction": "fallback_raw"},
                }
            ]
        fact_ids = await facts_mod.store_facts_batch(
            extracted, content, content_type, tenant_id=tenant_id
        )
    except Exception as e:
        logger.warning("memory write job %s failed: %s", job_id, e)
        _finish(job_id, "failed", error=str(e))
        return False

    _finish(job_id, "done", fact_ids=list(fact_ids or []))
    return True


async def sweep_stale_jobs(
    *,
    stale_after_minutes: int = DEFAULT_STALE_AFTER_MINUTES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    limit: int = 20,
) -> list[int]:
    """Re-run jobs abandoned by a dead process. Returns the ids processed.

    This is the deploy case. task_registry's 10-second drain cannot finish a
    ~60-second extraction, so a restart leaves jobs stranded in `running` while
    the agent has already been told the write succeeded.

    ``max_attempts`` bounds a poison payload: a job that always fails must not
    loop forever against a local LLM.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM memory_write_jobs "
            "WHERE status IN ('pending','running') "
            "  AND attempts < %s "
            "  AND updated_at < NOW() - make_interval(mins => %s) "
            "ORDER BY updated_at LIMIT %s",
            (max_attempts, stale_after_minutes, limit),
        )
        stale = [int(r[0]) for r in cur.fetchall()]

    processed: list[int] = []
    for job_id in stale:
        if await process_write_job(job_id):
            processed.append(job_id)
        else:
            # Still counts as swept — it was reclaimed and given an attempt.
            processed.append(job_id)
    if processed:
        logger.info("memory write sweeper reclaimed %d job(s): %s", len(processed), processed)
    return processed


async def job_status(job_id: int) -> dict[str, Any] | None:
    """Look up one job, for the opt-in status tool and for operator debugging."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, status, attempts, fact_ids, error, created_at, updated_at "
            "FROM memory_write_jobs WHERE id=%s",
            (job_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "job_id": row[0],
        "status": row[1],
        "attempts": row[2],
        "fact_ids": list(row[3] or []),
        "error": row[4],
        "created_at": row[5],
        "updated_at": row[6],
    }
