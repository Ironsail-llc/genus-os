"""Hook workflow backlog: every event a hook workflow takes, kept in Redis and run in order.

A hook event for a workflow is written to that workflow's backlog, a Redis
stream per tenant and workflow, before the hook consumer acknowledges the
event. One drainer per workflow reads the backlog oldest first, waits for the
tool-call rate limiter exactly as long as it takes to admit the run's first
call (no polling, no backoff), starts the run with that slot reserved, and
deletes the entry once the run has finished. So:

* every event runs once, in arrival order, at the limiter's rate: a burst
  queues instead of each event retrying on its own and racing the others;
* waiting is not a failure: there is no retry budget to spend, and nothing is
  dead-lettered for being early;
* an engine that stops keeps its backlog in Redis, and the next start drains
  it from where it was. A run cut off by the stop is started again (the
  at-least-once contract every hook handler already has).

A workflow whose trigger is ``concurrent`` has up to
:data:`MAX_CONCURRENT_RUNS` runs in flight, started in arrival order; any
other workflow runs its backlog one event at a time. Under HA every replica
may write to a backlog but only the leader runs it, so no event runs twice
because two replicas drained it. A non-concurrent
workflow without ``requeue_if_busy`` keeps its old contract: an event that
arrives while a run is in flight is dropped (``EventHooks``), never queued.

:meth:`WorkflowBacklog.stats` gives each workflow's depth and the age of its
oldest entry; the deferral summary line and the doctor's ``deferred_runs``
check report them (:func:`backlog_stats` reads them from another process).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from robothor.engine.models import RunStatus

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from robothor.engine.deferrals import DeferredRuns

logger = logging.getLogger(__name__)

#: Redis key prefix; the tenant and the workflow id complete the key.
KEY_PREFIX = "robothor:workflow:backlog:"

#: Entries one workflow's backlog holds. Past it a new event is dead-lettered,
#: loudly, so a workflow that can never keep up cannot fill Redis.
MAX_BACKLOG = 50_000

#: Runs of a ``concurrent`` workflow one drainer has in flight at once.
MAX_CONCURRENT_RUNS = 100

#: How long a drainer waits before trying again when something other than the
#: backlog (a cron or manual run) holds a non-concurrent workflow.
LOCK_WAIT_SECONDS = 1.0

#: Under HA, how often a drainer looks again: whether this replica now leads,
#: and for events another replica wrote (its writes wake no one here).
HA_RECHECK_SECONDS = 5.0

#: On stop, how long runs in flight get to finish; the rest stay in the backlog.
STOP_DRAIN_SECONDS = 20

# The source stream and event type travel with the entry.
STREAM_FIELD = "_backlog_stream"
TYPE_FIELD = "_backlog_event_type"

# Retry bookkeeping an older engine put on a requeued copy; not part of the event.
_INTERNAL_FIELDS = frozenset(
    {"_retry_agents", "_retry_workflows", "_rate_deferred", "_busy_requeue_count", "_retry_count"}
)


async def _wake_or_sleep(wake: asyncio.Event, seconds: float) -> None:
    """Return when ``wake`` is set or ``seconds`` have passed, whichever is first."""
    waiting = [asyncio.ensure_future(wake.wait()), asyncio.ensure_future(asyncio.sleep(seconds))]
    try:
        await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waiting:
            task.cancel()
        await asyncio.gather(*waiting, return_exceptions=True)


def _ha() -> bool:
    from robothor.engine.leader import ha_leader_enabled

    return ha_leader_enabled()


def _is_leader() -> bool:
    from robothor.engine import leader

    return leader.is_leader()


def backlog_key(tenant_id: str, workflow_id: str) -> str:
    return f"{KEY_PREFIX}{tenant_id}:{workflow_id}"


def _text(v: Any) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


def entry_age(msg_id: str, now: float) -> float:
    """Seconds since the entry was written (a stream id starts with its epoch ms)."""
    return max(now - int(msg_id.split("-", 1)[0]) / 1000, 0.0)


def workflow_event(stream: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """The event as a workflow run's context: the payload parsed when it is JSON."""
    raw = envelope.get("payload", "")
    try:
        payload: Any = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        payload = raw
    return {
        "id": envelope.get("id", ""),
        "type": envelope.get("type", ""),
        "stream": stream,
        "delivery_id": envelope.get("delivery_id", ""),
        "payload": payload,
    }


def _finished(
    runs: set[asyncio.Task[None]], slots: asyncio.Semaphore
) -> Callable[[asyncio.Task[None]], None]:
    """The done callback of one backlog run: forget it and free its slot."""

    def done(task: asyncio.Task[None]) -> None:
        runs.discard(task)
        slots.release()

    return done


class WorkflowBacklog:
    """One engine's per-workflow backlogs and their drainers."""

    def __init__(
        self,
        tenant_id: str,
        engine: Callable[[], Any],
        deferred: Callable[[], DeferredRuns | None] = lambda: None,
        leader: Callable[[], bool] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        # Whether this process may run backlogs (always, unless HA says otherwise).
        self._leader = leader or _is_leader
        self._engine = engine
        self._deferred = deferred
        self._redis: Any = None
        self._drainers: dict[str, asyncio.Task[None]] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._runs: dict[str, set[asyncio.Task[None]]] = {}
        # Entry ids written and not yet finished, oldest first, per workflow,
        # and how many of those have started.
        self._pending: dict[str, dict[str, None]] = {}
        self._started: dict[str, int] = {}

    def bind(self, redis_client: Any) -> None:
        self._redis = redis_client

    def key(self, workflow_id: str) -> str:
        return backlog_key(self.tenant_id, workflow_id)

    def stats(self, workflow_id: str) -> tuple[int, float]:
        """The workflow's backlog depth and the age (seconds) of its oldest entry."""
        pending = self._pending.get(workflow_id)
        if not pending:
            return 0, 0.0
        return len(pending), entry_age(next(iter(pending)), time.time())

    async def enqueue(
        self, redis_client: Any, wf: Any, stream: str, event_type: str, envelope: dict[str, Any]
    ) -> bool:
        """Write the event to the workflow's backlog. False when the backlog is full.

        Raises when Redis refuses the write: the caller must then not
        acknowledge the event as handled by the backlog.
        """
        if self._redis is None:
            self._redis = redis_client
        pending = self._pending.setdefault(wf.id, {})
        if len(pending) >= MAX_BACKLOG:
            return False
        fields = {k: v for k, v in envelope.items() if k not in _INTERNAL_FIELDS}
        fields[STREAM_FIELD] = stream
        fields[TYPE_FIELD] = event_type
        # Deferred by the rate limit: the limit's free slots are already
        # spoken for by the events waiting ahead of this one. Counted once,
        # here; however long it then waits, it is one deferred run.
        ahead = len(pending) - self._started.get(wf.id, 0)
        free = self._engine().first_step_free_slots(wf)
        deferred = self._deferred() if free is not None and ahead >= free else None
        msg_id = _text(await self._redis.xadd(self.key(wf.id), fields))
        # Written: from here on nothing may raise, or the caller would run it twice.
        pending[msg_id] = None
        self._start(wf.id)
        if deferred is not None:
            await deferred.record(wf.id, self._redis)  # logs, never raises
        return True

    async def resume(self, redis_client: Any, workflow_ids: list[str]) -> None:
        """Pick up what a previous engine left in the backlogs of ``workflow_ids``."""
        self._redis = redis_client
        for workflow_id in workflow_ids:
            pending = self._pending.setdefault(workflow_id, {})
            low = "-"
            while True:
                page = await self._redis.xrange(self.key(workflow_id), min=low, max="+", count=500)
                if not page:
                    break
                for msg_id, _ in page:
                    pending.setdefault(_text(msg_id), None)
                low = "(" + _text(page[-1][0])
            if _ha():
                self._start(workflow_id)  # the leader may be another replica's writes away
            if pending:
                depth, oldest = self.stats(workflow_id)
                logger.info(
                    "Resuming workflow backlog: workflow=%s backlog=%d oldest=%ds",
                    workflow_id,
                    depth,
                    int(oldest),
                )
                self._start(workflow_id)

    async def stop(self, timeout: float = STOP_DRAIN_SECONDS) -> None:
        """Stop starting runs; give those in flight ``timeout`` seconds, then cancel them.

        Whatever has not finished stays in Redis for the next start.
        """
        drainers = list(self._drainers.values())
        for task in drainers:
            task.cancel()
        for task in drainers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        runs = [t for group in self._runs.values() for t in group]
        if runs:
            await asyncio.wait(runs, timeout=timeout)
        for task in runs:
            task.cancel()
        await asyncio.gather(*runs, return_exceptions=True)
        self._drainers.clear()
        self._runs.clear()
        self._wake.clear()
        self._pending.clear()
        self._started.clear()

    def _start(self, workflow_id: str) -> None:
        wake = self._wake.setdefault(workflow_id, asyncio.Event())
        wake.set()
        task = self._drainers.get(workflow_id)
        if task is None or task.done():
            self._drainers[workflow_id] = asyncio.create_task(
                self._drain(workflow_id), name=f"workflow-backlog:{workflow_id}"
            )

    async def _drain(self, workflow_id: str) -> None:
        """Start the backlog's runs oldest first, each once the limiter admits it."""
        wake = self._wake[workflow_id]
        runs = self._runs.setdefault(workflow_id, set())
        pending = self._pending.setdefault(workflow_id, {})
        key = self.key(workflow_id)
        cursor = "-"
        slots: asyncio.Semaphore | None = None
        ha = _ha()
        while True:
            if ha and not self._leader():
                # Another replica leads and runs this backlog. Start over from
                # the oldest entry once this one leads: the other has deleted
                # what it finished.
                cursor = "-"
                await asyncio.sleep(HA_RECHECK_SECONDS)
                continue
            wake.clear()
            entries = await self._redis.xrange(key, min=cursor, max="+", count=1)
            if not entries:
                if ha:
                    await _wake_or_sleep(wake, HA_RECHECK_SECONDS)
                else:
                    await wake.wait()
                continue
            raw_id, raw = entries[0]
            msg_id = _text(raw_id)
            cursor = "(" + msg_id
            pending.setdefault(msg_id, None)
            wf = self._engine().get_workflow(workflow_id)
            if wf is None:
                logger.warning(
                    "Workflow %s is no longer loaded: its backlog of %d waits in Redis",
                    workflow_id,
                    len(pending),
                )
                return
            if slots is None:
                slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS if wf.concurrent else 1)
            await slots.acquire()
            stamp = await self._admit(wf)
            self._started[workflow_id] = self._started.get(workflow_id, 0) + 1
            fields = {_text(k): _text(v) for k, v in raw.items()}
            task = asyncio.create_task(
                self._run(wf, msg_id, fields, stamp), name=f"workflow-backlog-run:{workflow_id}"
            )
            runs.add(task)
            task.add_done_callback(_finished(runs, slots))

    async def _admit(self, wf: Any) -> float | None:
        """Wait until the limiter admits the run's first call, then reserve that slot."""
        engine = self._engine()
        while True:
            wait = engine.first_step_rate_wait(wf)
            if wait <= 0:
                return engine.reserve_first_step(wf)  # type: ignore[no-any-return]
            await asyncio.sleep(wait)

    async def run_now(
        self, wf: Any, stream: str, event_type: str, envelope: dict[str, Any]
    ) -> None:
        """Run one event outside the backlog (Redis refused the write). Not ordered, not kept."""
        await self._execute(wf, stream, event_type, dict(envelope), None)

    async def _run(self, wf: Any, msg_id: str, fields: dict[str, str], stamp: float | None) -> None:
        stream = fields.pop(STREAM_FIELD, "")
        event_type = fields.pop(TYPE_FIELD, fields.get("type", ""))
        await self._execute(wf, stream, event_type, fields, stamp)
        try:
            await self._redis.xdel(self.key(wf.id), msg_id)
        except Exception as e:
            logger.warning("Could not remove a finished entry from %s's backlog: %s", wf.id, e)
        if msg_id in self._pending.get(wf.id, {}):
            del self._pending[wf.id][msg_id]
            self._started[wf.id] = max(self._started.get(wf.id, 0) - 1, 0)

    async def _execute(
        self, wf: Any, stream: str, event_type: str, fields: dict[str, Any], stamp: float | None
    ) -> None:
        """Run the workflow for one event; raises only when cancelled."""
        engine = self._engine()
        try:
            while True:
                run = await engine.execute(
                    workflow_id=wf.id,
                    trigger_type="hook",
                    trigger_detail=f"{stream}:{event_type}",
                    initial_context={"event": workflow_event(stream, fields)},
                    user_id=f"service:workflow:{wf.id}",
                    user_role="service",
                    rate_slot=stamp,
                )
                if run.status != RunStatus.SKIPPED or getattr(run, "rate_limited", False):
                    break
                # A cron or manual run holds the workflow: this event waits its
                # turn instead of being dropped, and gives its slot back meanwhile.
                engine.release_first_step(wf, stamp)
                await asyncio.sleep(LOCK_WAIT_SECONDS)
                stamp = await self._admit(wf)
        except asyncio.CancelledError:
            raise  # the engine is stopping: the entry stays for the next start
        except Exception as e:
            logger.error(
                "Workflow %s failed (hook %s:%s): %s", wf.id, stream, event_type, e, exc_info=True
            )


def backlog_stats(
    client: Any, tenant_id: str, *, now: float | None = None
) -> dict[str, tuple[int, float]]:
    """Every non-empty backlog of one tenant: workflow id -> (depth, oldest age). Reads only.

    ``client`` is a synchronous Redis client.
    """
    at = time.time() if now is None else now
    prefix = f"{KEY_PREFIX}{tenant_id}:"
    out: dict[str, tuple[int, float]] = {}
    for raw_key in client.scan_iter(match=prefix + "*"):
        key = _text(raw_key)
        depth = int(client.xlen(key) or 0)
        if not depth:
            continue
        head = client.xrange(key, min="-", max="+", count=1)
        age = entry_age(_text(head[0][0]), at) if head else 0.0
        out[key[len(prefix) :]] = (depth, age)
    return out
