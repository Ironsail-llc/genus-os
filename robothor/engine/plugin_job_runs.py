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

import asyncio
import functools
import inspect
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

TRIGGER_TYPE = "plugin_job"

#: The most of a job's return value, as JSON text, a run row keeps.
RESULT_CAP_CHARS = 16_000


def _without_nul(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_without_nul(v) for v in value]
    if isinstance(value, dict):
        return {_without_nul(k): _without_nul(v) for k, v in value.items()}
    return value


def _context(result: Any) -> str:
    try:
        # allow_nan=False: Postgres JSONB rejects NaN and Infinity.
        text = json.dumps(result, default=str, allow_nan=False)
    except (TypeError, ValueError):  # a circular reference, a non-finite float
        text = json.dumps(repr(result))
    if "\\u0000" in text:  # Postgres JSONB rejects the NUL escape
        text = json.dumps(_without_nul(json.loads(text)))
    if len(text) <= RESULT_CAP_CHARS:
        return json.dumps({"result": json.loads(text)})
    return json.dumps({"result": text[:RESULT_CAP_CHARS], "result_truncated": True})


class _Run:
    """One execution of one plugin job, and the row that records it."""

    def __init__(self, job_id: str, tenant_id: str) -> None:
        self.job_id = job_id
        self.tenant_id = tenant_id
        self.run_id = str(uuid.uuid4())
        self.started = time.monotonic()

    def start(self) -> None:
        self._write(
            """INSERT INTO workflow_runs
               (id, tenant_id, workflow_id, trigger_type, trigger_detail,
                status, steps_total, started_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                self.run_id,
                self.tenant_id,
                self.job_id,
                TRIGGER_TYPE,
                self.job_id,
                "running",
                0,
                datetime.now(UTC),
            ),
        )
        self.started = time.monotonic()

    def completed(self, result: Any) -> None:
        self._finish("completed", None, _context(result))

    def cancelled(self) -> None:
        self._finish("cancelled", "Cancelled: engine shutdown mid-run", json.dumps({}))

    def failed(self, error: BaseException) -> None:
        self._finish("failed", f"{type(error).__name__}: {error}", json.dumps({}))

    def _finish(self, status: str, error_message: str | None, context: str) -> None:
        self._write(
            """UPDATE workflow_runs
               SET status = %s, completed_at = %s, duration_ms = %s,
                   error_message = %s, context = %s
               WHERE id = %s""",
            (
                status,
                datetime.now(UTC),
                int((time.monotonic() - self.started) * 1000),
                error_message,
                context,
                self.run_id,
            ),
        )

    def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        try:
            from robothor.db.connection import get_connection

            with get_connection() as conn:
                conn.cursor().execute(sql, params)
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - recording must not stop the job
            logger.warning("Failed to record a run of plugin job %s: %s", self.job_id, exc)


def recorded(job_id: str, func: Callable[..., Any], *, tenant_id: str) -> Callable[..., Any]:
    """Wrap ``func`` so each call writes a run row; coroutines stay coroutines.

    A coroutine job is awaited by AsyncIOScheduler on the event loop, so its
    row writes go to a thread; a plain job already runs in the scheduler's
    thread pool and writes inline. The job's exception is re-raised either
    way, so the scheduler's own error handling is unchanged.
    """
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def _async_job(*args: Any, **kwargs: Any) -> Any:
            run = _Run(job_id, tenant_id)
            await asyncio.to_thread(run.start)
            try:
                result = await func(*args, **kwargs)
            except asyncio.CancelledError:
                # Engine shutdown: recorded as a workflow's would be, and
                # written inline, since the executor may already be gone.
                run.cancelled()
                raise
            except BaseException as exc:
                await asyncio.to_thread(run.failed, exc)
                raise
            await asyncio.to_thread(run.completed, result)
            return result

        return _async_job

    @functools.wraps(func)
    def _job(*args: Any, **kwargs: Any) -> Any:
        run = _Run(job_id, tenant_id)
        run.start()
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            run.failed(exc)
            raise
        run.completed(result)
        return result

    return _job
