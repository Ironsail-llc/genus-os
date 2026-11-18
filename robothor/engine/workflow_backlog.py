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
  it from where it was. An event whose run had not started runs then; one whose
  run had started (and may already have acted: sent a message) is closed with a
  warning, never started again. The claim is a marker set the moment the run
  starts, so under HA a new leader cannot start a run another replica holds.

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
import os
import time
import uuid
from typing import TYPE_CHECKING, Any

from robothor.engine.models import RunStatus
from robothor.events.bus import MAXLEN as BUS_MAXLEN

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from robothor.engine.deferrals import DeferredRuns

logger = logging.getLogger(__name__)

#: Redis key prefix; the tenant and the workflow id complete the key.
KEY_PREFIX = "robothor:workflow:backlog:"

#: Entries one workflow's backlog holds: no more than an event stream holds
#: (``EVENT_BUS_MAXLEN``), the budget Redis's memory is sized for. Each entry
#: is a full copy of an event, and Redis runs without eviction, so a larger
#: backlog could fill it and refuse every write on the instance. Past the cap
#: a new event is dead-lettered, loudly.
MAX_BACKLOG = BUS_MAXLEN

#: Runs of a ``concurrent`` workflow one drainer has in flight at once.
MAX_CONCURRENT_RUNS = 100

#: How long a drainer waits before trying again when something other than the
#: backlog (a cron or manual run) holds a non-concurrent workflow.
LOCK_WAIT_SECONDS = 1.0

#: Under HA, how often a drainer looks again: whether this replica now leads,
#: and for events another replica wrote (its writes wake no one here).
HA_RECHECK_SECONDS = 5.0

#: After a Redis error, how long a drainer waits before it reads again, and how
#: many times a finished entry's delete is tried.
ERROR_RETRY_SECONDS = 5.0
DELETE_ATTEMPTS = 3

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
        # Entry ids this process started and has not yet removed from Redis:
        # a drainer that reads from the start again (a lost and regained
        # lead, a Redis error) skips them instead of running them twice.
        self._inflight: dict[str, set[str]] = {}
        # This engine's claims on the entries it starts: who, and since when.
        self._owner = f"{os.getpid()}:{uuid.uuid4().hex[:12]}"
        # The backlog as Redis last showed it: depth and the oldest entry id.
        # Every replica may write to a backlog that another one drains, so
        # nothing here is counted locally.
        self._seen: dict[str, tuple[int, str | None]] = {}

    def bind(self, redis_client: Any) -> None:
        self._redis = redis_client

    def key(self, workflow_id: str) -> str:
        return backlog_key(self.tenant_id, workflow_id)

    def started_key(self, workflow_id: str) -> str:
        """The hash of entries whose run has started: entry id -> who started it, and when."""
        return self.key(workflow_id) + ":started"

    async def _claim(self, workflow_id: str, msg_id: str) -> bool:
        """Mark the entry's run as started, unless someone already did. Atomic."""
        token = json.dumps({"by": self._owner, "at": round(time.time(), 3)})
        return bool(await self._redis.hsetnx(self.started_key(workflow_id), msg_id, token))

    async def _close(self, workflow_id: str, msg_id: str, fields: dict[str, str], by: str) -> None:
        """Remove an entry whose run started earlier without starting it again."""
        logger.warning(
            "Workflow %s: event %s (backlog entry %s) was already started (%s) and is not "
            "started again; any step it had not finished did not run",
            workflow_id,
            fields.get("id") or "-",
            msg_id,
            by,
        )
        await self._redis.xdel(self.key(workflow_id), msg_id)
        await self._redis.hdel(self.started_key(workflow_id), msg_id)

    def stats(self, workflow_id: str) -> tuple[int, float]:
        """The workflow's backlog depth and the age (seconds) of its oldest entry.

        As Redis showed it at this process's last write, run or look: cheap
        and synchronous, for the summary line.
        """
        depth, oldest = self._seen.get(workflow_id, (0, None))
        if not depth or oldest is None:
            return 0, 0.0
        return depth, entry_age(oldest, time.time())

    async def _refresh(self, workflow_id: str) -> int:
        """Read the backlog's depth and oldest entry from Redis into :meth:`stats`."""
        key = self.key(workflow_id)
        depth = int(await self._redis.xlen(key) or 0)
        head = await self._redis.xrange(key, min="-", max="+", count=1) if depth else []
        self._seen[workflow_id] = (depth, _text(head[0][0]) if head else None)
        return depth

    async def enqueue(
        self, redis_client: Any, wf: Any, stream: str, event_type: str, envelope: dict[str, Any]
    ) -> bool:
        """Write the event to the workflow's backlog. False when the backlog is full.

        Raises when Redis refuses the write: the caller must then not
        acknowledge the event as handled by the backlog.
        """
        if self._redis is None:
            self._redis = redis_client
        depth = int(await self._redis.xlen(self.key(wf.id)) or 0)
        if depth >= MAX_BACKLOG:
            return False
        fields = {k: v for k, v in envelope.items() if k not in _INTERNAL_FIELDS}
        fields[STREAM_FIELD] = stream
        fields[TYPE_FIELD] = event_type
        # Deferred by the rate limit: the limit's free slots are already
        # spoken for by the events waiting ahead of this one. Counted once,
        # here; however long it then waits, it is one deferred run.
        ahead = depth - len(self._inflight.get(wf.id, ()))
        free = self._engine().first_step_free_slots(wf)
        deferred = self._deferred() if free is not None and ahead >= free else None
        await self._redis.xadd(self.key(wf.id), fields)
        # Written: from here on nothing may raise, or the caller would run it twice.
        self._start(wf.id)
        with contextlib.suppress(Exception):
            await self._refresh(wf.id)
        if deferred is not None:
            await deferred.record(wf.id, self._redis)  # logs, never raises
        return True

    async def resume(self, redis_client: Any, workflow_ids: list[str]) -> None:
        """Pick up what a previous engine left in the backlogs of ``workflow_ids``."""
        self._redis = redis_client
        for workflow_id in workflow_ids:
            depth = await self._refresh(workflow_id)
            if depth:
                _, oldest = self.stats(workflow_id)
                logger.info(
                    "Resuming workflow backlog: workflow=%s backlog=%d oldest=%ds",
                    workflow_id,
                    depth,
                    int(oldest),
                )
            # Under HA the leader may be another replica's writes away.
            if depth or _ha():
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
        self._inflight.clear()
        self._seen.clear()

    def _start(self, workflow_id: str) -> None:
        wake = self._wake.setdefault(workflow_id, asyncio.Event())
        wake.set()
        task = self._drainers.get(workflow_id)
        if task is None or task.done():
            self._drainers[workflow_id] = asyncio.create_task(
                self._drain(workflow_id), name=f"workflow-backlog:{workflow_id}"
            )

    async def _drain(self, workflow_id: str) -> None:
        """Start the backlog's runs oldest first, each once the limiter admits it.

        A Redis error is logged and retried after a pause, the read position
        kept: the drain carries on by itself, without waiting for a new event.
        """
        wake = self._wake[workflow_id]
        runs = self._runs.setdefault(workflow_id, set())
        inflight = self._inflight.setdefault(workflow_id, set())
        key = self.key(workflow_id)
        cursor = "-"
        slots: asyncio.Semaphore | None = None
        ha = _ha()
        while True:
            if ha and not self._leader():
                # Another replica leads and runs this backlog. Start over from
                # the oldest entry once this one leads: the other has deleted
                # what it finished, and what this one started is skipped.
                cursor = "-"
                await asyncio.sleep(HA_RECHECK_SECONDS)
                continue
            wake.clear()
            try:
                entries = await self._redis.xrange(key, min=cursor, max="+", count=1)
            except Exception as e:
                logger.warning(
                    "Workflow %s backlog unreadable, retrying in %ds: %s",
                    workflow_id,
                    int(ERROR_RETRY_SECONDS),
                    e,
                )
                await asyncio.sleep(ERROR_RETRY_SECONDS)
                continue
            if not entries:
                if ha:
                    await _wake_or_sleep(wake, HA_RECHECK_SECONDS)
                else:
                    await wake.wait()
                continue
            raw_id, raw = entries[0]
            msg_id = _text(raw_id)
            before, cursor = cursor, "(" + msg_id
            if msg_id in inflight:
                continue  # started here already, still running or not yet removed
            fields = {_text(k): _text(v) for k, v in raw.items()}
            try:
                started = await self._redis.hget(self.started_key(workflow_id), msg_id)
                if started is not None:
                    # Its run started before (a stop or crash cut it off, or the
                    # replica that led then still runs it): never start it again.
                    await self._close(workflow_id, msg_id, fields, _text(started))
                    continue
            except Exception as e:
                logger.warning(
                    "Workflow %s backlog unreadable, retrying in %ds: %s",
                    workflow_id,
                    int(ERROR_RETRY_SECONDS),
                    e,
                )
                cursor = before  # read this entry again
                await asyncio.sleep(ERROR_RETRY_SECONDS)
                continue
            wf = self._engine().get_workflow(workflow_id)
            if wf is None:
                logger.warning(
                    "Workflow %s is no longer loaded: its backlog waits in Redis", workflow_id
                )
                return
            if slots is None:
                slots = asyncio.Semaphore(MAX_CONCURRENT_RUNS if wf.concurrent else 1)
            await slots.acquire()
            stamp = await self._admit(wf)
            inflight.add(msg_id)
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
        if not await self._execute(wf, stream, event_type, fields, stamp, msg_id=msg_id):
            # Another replica started this event's run; it removes the entry.
            self._inflight.get(wf.id, set()).discard(msg_id)
            return
        for attempt in range(DELETE_ATTEMPTS):
            try:
                await self._redis.xdel(self.key(wf.id), msg_id)
            except Exception as e:
                if attempt + 1 == DELETE_ATTEMPTS:
                    # Kept in _inflight, so this process never runs it again;
                    # a restart before Redis is back would.
                    logger.error(
                        "Could not remove a finished entry from %s's backlog: %s", wf.id, e
                    )
                    return
                await asyncio.sleep(ERROR_RETRY_SECONDS)
            else:
                break
        # The entry first, then its started marker: a marker left without an
        # entry is harmless; an entry left without its marker would run again.
        with contextlib.suppress(Exception):
            await self._redis.hdel(self.started_key(wf.id), msg_id)
        self._inflight.get(wf.id, set()).discard(msg_id)
        with contextlib.suppress(Exception):
            await self._refresh(wf.id)

    async def _execute(
        self,
        wf: Any,
        stream: str,
        event_type: str,
        fields: dict[str, Any],
        stamp: float | None,
        *,
        msg_id: str | None = None,
    ) -> bool:
        """Run the workflow for one event; raises only when cancelled.

        With ``msg_id`` (a backlog entry) the run claims the entry the moment it
        starts. False: another engine had already started this event's run.
        """
        engine = self._engine()
        claim: dict[str, bool] = {}

        async def on_start() -> bool:
            try:
                claim["won"] = await self._claim(wf.id, msg_id or "")
            except Exception as e:
                # Unclaimed is unstarted: try again shortly rather than run unclaimed.
                logger.warning("Workflow %s: could not claim a backlog entry: %s", wf.id, e)
                claim["error"] = True
                return False
            return claim["won"]

        try:
            while True:
                claim.clear()
                run = await engine.execute(
                    workflow_id=wf.id,
                    trigger_type="hook",
                    trigger_detail=f"{stream}:{event_type}",
                    initial_context={"event": workflow_event(stream, fields)},
                    user_id=f"service:workflow:{wf.id}",
                    user_role="service",
                    rate_slot=stamp,
                    on_start=on_start if msg_id is not None else None,
                )
                if claim.get("won") is False and not claim.get("error"):
                    engine.release_first_step(wf, stamp)
                    return False
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
        return True


def backlog_stats(
    client: Any, tenant_id: str, *, now: float | None = None
) -> dict[str, tuple[int, float]]:
    """Every non-empty backlog of one tenant: workflow id -> (depth, oldest age). Reads only.

    ``client`` is a synchronous Redis client.
    """
    at = time.time() if now is None else now
    prefix = f"{KEY_PREFIX}{tenant_id}:"
    out: dict[str, tuple[int, float]] = {}
    # Streams only: each backlog's started-marker hash lives under the same prefix.
    for raw_key in client.scan_iter(match=prefix + "*", _type="stream"):
        key = _text(raw_key)
        depth = int(client.xlen(key) or 0)
        if not depth:
            continue
        head = client.xrange(key, min="-", max="+", count=1)
        age = entry_age(_text(head[0][0]), at) if head else 0.0
        out[key[len(prefix) :]] = (depth, age)
    return out
