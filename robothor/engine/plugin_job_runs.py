"""Run records for scheduled plugin jobs.

A plugin job is a bare callable APScheduler runs on a cron. Wrapped here, each
execution writes a ``workflow_runs`` row keyed by the job id (``plugin:<name>``)
with ``trigger_type = 'plugin_job'``: ``running`` before the call, then
``completed`` with the duration and the returned value, or ``failed`` with the
exception. ``workflow_runs`` is the table the run listings already read, and its
stale-run reaper and failure detectors already watch it.

Recording is best effort: a database fault is logged and the job still runs.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

TRIGGER_TYPE = "plugin_job"

#: The most of a job's return value, as JSON text, a run row keeps.
RESULT_CAP_CHARS = 16_000


def _context(result: Any) -> str:
    text = json.dumps(result, default=str)
    if len(text) <= RESULT_CAP_CHARS:
        return json.dumps({"result": json.loads(text)})
    return json.dumps({"result": text[:RESULT_CAP_CHARS], "result_truncated": True})


def _start(run_id: str, job_id: str) -> None:
    try:
        from robothor.db.connection import get_connection

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO workflow_runs
                   (id, tenant_id, workflow_id, trigger_type, trigger_detail,
                    status, steps_total, started_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    run_id,
                    DEFAULT_TENANT,
                    job_id,
                    TRIGGER_TYPE,
                    job_id,
                    "running",
                    0,
                    datetime.now(UTC),
                ),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - recording must not stop the job
        logger.warning("Failed to record plugin job run start for %s: %s", job_id, exc)


def _finish(
    run_id: str,
    job_id: str,
    started: float,
    *,
    status: str,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    try:
        from robothor.db.connection import get_connection

        duration_ms = int((time.monotonic() - started) * 1000)
        error_message = f"{type(error).__name__}: {error}" if error is not None else None
        context = _context(result) if error is None else json.dumps({})
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE workflow_runs
                   SET status = %s, completed_at = %s, duration_ms = %s,
                       error_message = %s, context = %s
                   WHERE id = %s""",
                (status, datetime.now(UTC), duration_ms, error_message, context, run_id),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - recording must not stop the job
        logger.warning("Failed to record plugin job run end for %s: %s", job_id, exc)


def recorded(job_id: str, func: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``func`` so each call writes a run row; coroutines stay coroutines."""
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def _async_job(*args: Any, **kwargs: Any) -> Any:
            import asyncio

            run_id = str(uuid.uuid4())
            await asyncio.to_thread(_start, run_id, job_id)
            started = time.monotonic()
            try:
                result = await func(*args, **kwargs)
            except BaseException as exc:
                await asyncio.to_thread(
                    _finish, run_id, job_id, started, status="failed", error=exc
                )
                raise
            await asyncio.to_thread(
                _finish, run_id, job_id, started, status="completed", result=result
            )
            return result

        return _async_job

    @functools.wraps(func)
    def _job(*args: Any, **kwargs: Any) -> Any:
        run_id = str(uuid.uuid4())
        _start(run_id, job_id)
        started = time.monotonic()
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            _finish(run_id, job_id, started, status="failed", error=exc)
            raise
        _finish(run_id, job_id, started, status="completed", result=result)
        return result

    return _job
