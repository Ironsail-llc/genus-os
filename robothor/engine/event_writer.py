"""One ordered, off-loop writer for audit and guardrail event rows.

Every tool call writes an audit row and every guardrail that fires writes an
event row. Both used to INSERT and commit on the caller's thread — on the
engine's event loop — so one slow database stalled every other session's
streaming, heartbeats and tool calls for as long as each row took.

Rows are now queued here and written by a single task that hops to a worker
thread once per batch: whatever queued while the previous batch was being
written, up to ``MAX_BATCH`` rows. There is no deliberate delay, so under light
load a row is written as soon as the thread is free.

Contract:

- **Order.** Rows are written in the order they were submitted. One batch is in
  flight at a time, and every write, queued or inline, holds one lock.
- **Context.** Each row runs in a copy of the ``contextvars`` its caller had
  when it submitted (tenant scope, run context), not the writer task's.
- **flush()** returns once every row submitted before it has been written. The
  run's persist task flushes at run end; the daemon calls ``shutdown()`` after
  the task-registry drain, which flushes and stops the task.
- **No loop, no queue.** Called with no running loop (the CLI, a worker thread
  already off the loop) a submit writes inline before returning, behind any
  rows still queued — today's behaviour for those callers.
- **Never raises.** A failing row is logged at ERROR and the rows behind it are
  still written.

Loss window: nothing queued is lost on a normal shutdown. On a hard crash
(SIGKILL, OOM) the rows still queued are lost — at most the rows submitted
while the in-flight batch was being written, i.e. one batch's write time
(single-digit milliseconds per row on a healthy database).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Rows written per thread hop.
MAX_BATCH = 100

_Item = tuple[contextvars.Context, Callable[..., Any], tuple[Any, ...], dict[str, Any]]


class EventWriter:
    """FIFO of blocking writes, drained off the event loop by one task."""

    def __init__(self, max_batch: int = MAX_BATCH) -> None:
        self._max_batch = max_batch
        self._pending: deque[_Item] = deque()
        self._lock = threading.RLock()
        self._submitted = 0  # rows queued from a loop (loop thread only)
        self._written = 0  # queued rows taken off the queue (under the lock)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake: asyncio.Event | None = None
        self._progress: asyncio.Event | None = None
        self._closed = False

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
        """Queue ``fn(*args, **kwargs)``; write inline when there is no loop."""
        item: _Item = (contextvars.copy_context(), fn, args, kwargs)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or (self._closed and loop is self._loop):
            with self._lock:
                self._write_pending(limit=None)
                _write(item)
            return
        self._pending.append(item)
        self._submitted += 1
        self._ensure_task(loop).set()

    async def flush(self) -> None:
        """Return once every row submitted before this call is written."""
        loop = asyncio.get_running_loop()
        target = self._submitted
        while self._written < target:
            wake = self._ensure_task(loop)
            progress = self._progress
            wake.set()
            assert progress is not None
            await progress.wait()

    async def shutdown(self) -> None:
        """Flush, stop the task, and write any later submit inline."""
        self._ensure_task(asyncio.get_running_loop())
        await self.flush()
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with self._lock:
            self._write_pending(limit=None)

    # ── internals ────────────────────────────────────────────────────────

    def _ensure_task(self, loop: asyncio.AbstractEventLoop) -> asyncio.Event:
        if self._loop is not loop:
            # A new loop (a test, a second asyncio.run): rows the old one left
            # are still at the head of the queue and the new task writes them.
            self._loop, self._task, self._closed = loop, None, False
            self._wake, self._progress = asyncio.Event(), asyncio.Event()
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._run(), name="event-writer")
        assert self._wake is not None
        return self._wake

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._pending:
                await asyncio.to_thread(self._write_batch)
                self._signal_progress()
            self._signal_progress()

    def _signal_progress(self) -> None:
        progress, self._progress = self._progress, asyncio.Event()
        if progress is not None:
            progress.set()

    def _write_batch(self) -> None:
        with self._lock:
            self._write_pending(limit=self._max_batch)

    def _write_pending(self, limit: int | None) -> None:
        """Write queued rows in order (caller holds the lock).

        Each row leaves the queue only as it is written, so a loop that dies
        mid-batch leaves exactly the unwritten rows behind for the next one.
        """
        count = 0
        while self._pending and (limit is None or count < limit):
            _write(self._pending.popleft())
            self._written += 1
            count += 1


def _write(item: _Item) -> None:
    ctx, fn, args, kwargs = item
    try:
        ctx.run(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
        logger.error("event writer: a queued write failed: %s", exc)


_writer = EventWriter()


def get_event_writer() -> EventWriter:
    """The engine's one writer (one queue, so one order)."""
    return _writer
