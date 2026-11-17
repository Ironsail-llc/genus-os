"""A burst of hook events for one workflow drains in order, once each, with nothing lost.

A burst of several thousand distinct events for one workflow whose first step
is a rate-limited tool call. Every event must run exactly once, in arrival
order, at the limiter's rate, and a restart in the middle of the drain must
neither lose nor repeat an event.

The burst runs through the real hook consumer (``EventHooks._handle_event``),
the real workflow engine and the real guardrail limiter, over a fake Redis, in
virtual time: every ``asyncio.sleep`` waits on a virtual clock that jumps to the
next wake-up as soon as every task is blocked, so an hour-long drain takes
seconds and its timings are exact.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import importlib
import itertools
import math
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine import dedup
from robothor.engine.hooks import STREAM_PREFIX, EventHooks, retry_stream_key
from robothor.engine.models import RunStatus
from robothor.engine.workflow import WorkflowEngine, parse_workflow

STREAM = f"{STREAM_PREFIX}platform"
EVENT = "platform.order.created"
GROUP = "engine"
START = 1_700_000_000.0

_real_sleep = asyncio.sleep


class VirtualTime:
    """A clock that only moves when every task is waiting on it."""

    def __init__(self) -> None:
        self.now = START
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float, result: Any = None) -> Any:
        if seconds <= 0:
            await _real_sleep(0)
            return result
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._timers, (self.now + seconds, next(self._seq), fut))
        await fut
        return result

    async def settle(self) -> None:
        """Run every ready task until all of them are blocked."""
        loop: Any = asyncio.get_running_loop()
        await _real_sleep(0)
        while loop._ready:  # noqa: SLF001 - exact quiescence for the default loop
            await _real_sleep(0)

    async def run_until(self, done: Any = None, limit: float = 24 * 3600) -> None:
        """Advance virtual time until ``done()`` (or, with no ``done``, until
        nothing waits on the clock); fail if nothing is left to wake first."""
        while True:
            await self.settle()
            if done is not None and done():
                return
            while self._timers and self._timers[0][2].done():
                heapq.heappop(self._timers)
            if not self._timers:
                if done is None:
                    return
                raise AssertionError("stalled: no task is waiting on the clock")
            when = self._timers[0][0]
            if when - START > limit:
                raise AssertionError(f"still running after {limit} virtual seconds")
            self.now = max(self.now, when)
            while self._timers and self._timers[0][0] <= self.now:
                _, _, fut = heapq.heappop(self._timers)
                if not fut.done():
                    fut.set_result(None)


class FakeRedis:
    """The stream, hash and key commands the hook consumer and the backlog use."""

    def __init__(self, vt: VirtualTime) -> None:
        self.vt = vt
        self.streams: dict[str, list[tuple[bytes, dict[bytes, bytes]]]] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.ttls: dict[str, int] = {}
        self.acked: list[bytes] = []
        self._seq = itertools.count()
        self.on_retry: Any = None  # the consumer reading an engine's retry stream

    @staticmethod
    def _b(v: Any) -> bytes:
        return v if isinstance(v, bytes) else str(v).encode()

    async def xadd(self, key: str, fields: dict[Any, Any], **kw: Any) -> bytes:
        msg_id = f"{int(self.vt.now * 1000)}-{next(self._seq)}".encode()
        entry = {self._b(k): self._b(v) for k, v in fields.items()}
        self.streams.setdefault(key, []).append((msg_id, entry))
        if key.endswith(f":retry:{GROUP}") and self.on_retry is not None:
            self.on_retry(key, msg_id, entry)
        return msg_id

    async def xrange(
        self,
        key: str,
        min: str = "-",  # noqa: A002 - redis-py's names
        max: str = "+",  # noqa: A002
        count: int | None = None,
    ) -> list[tuple[bytes, dict[bytes, bytes]]]:
        def _key(msg_id: bytes) -> tuple[int, int]:
            a, b = msg_id.decode().split("-")
            return int(a), int(b)

        out = []
        for msg_id, entry in self.streams.get(key, []):
            if min.startswith("("):
                if _key(msg_id) <= _key(min[1:].encode()):
                    continue
            elif min != "-" and _key(msg_id) < _key(min.encode()):
                continue
            out.append((msg_id, dict(entry)))
            if count is not None and len(out) >= count:
                break
        return out

    async def xdel(self, key: str, *ids: Any) -> int:
        drop = {self._b(i) for i in ids}
        before = len(self.streams.get(key, []))
        self.streams[key] = [e for e in self.streams.get(key, []) if e[0] not in drop]
        return before - len(self.streams[key])

    async def xlen(self, key: str) -> int:
        return len(self.streams.get(key, []))

    async def xack(self, stream: str, group: str, *ids: Any) -> int:
        self.acked.extend(self._b(i) for i in ids)
        return len(ids)

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        h = self.hashes.setdefault(key, {})
        h[field] = h.get(field, 0) + amount
        return h[field]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def scan_iter(self, match: str = "*", **kw: Any) -> Any:
        prefix = match.rstrip("*")
        for key in [k for k in self.streams if k.startswith(prefix)]:
            yield key.encode()

    def dlq(self) -> int:
        return len(self.streams.get(f"{STREAM}:dlq", []))


def _definition(**extra: Any) -> dict[str, Any]:
    trigger: dict[str, Any] = {"type": "hook", "stream": "platform", "event_type": EVENT}
    trigger.update(extra.pop("trigger", {"requeue_if_busy": True}))
    return {
        "id": "order-flow",
        "triggers": [trigger],
        "steps": [
            {
                "id": "claim",
                "type": "tool",
                "tool_name": "create_task",
                "tool_args": {"ref": "{{ event.id }}"},
            }
        ],
        **extra,
    }


class Burst:
    """One engine's hook consumer and workflow engine over a fake Redis, in virtual time."""

    def __init__(self, engine_config: Any, vt: VirtualTime, data: dict[str, Any]) -> None:
        self.vt = vt
        self.config = engine_config
        self.redis = FakeRedis(vt)
        self.redis.on_retry = self._consume_retry
        self.calls: list[tuple[str, float]] = []  # (event id, virtual time) per tool call
        runner = MagicMock()

        async def tool(name: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
            self.calls.append((args["ref"], vt.now))
            return {"ok": True}

        runner.registry.execute = AsyncMock(side_effect=tool)
        engine = WorkflowEngine(engine_config, runner=runner)
        wf = parse_workflow(data)
        engine._workflows[wf.id] = wf
        engine._persist_run_start = MagicMock()  # type: ignore[method-assign]
        engine._persist_run_end = MagicMock()  # type: ignore[method-assign]
        engine._persist_step = MagicMock()  # type: ignore[method-assign]
        engine._persist_step_start = MagicMock()  # type: ignore[method-assign]
        engine._mark_run_running = MagicMock(  # type: ignore[method-assign]
            side_effect=lambda run: setattr(run, "status", RunStatus.RUNNING)
        )
        self.engine = engine
        self.hooks = self.new_hooks()

    def new_hooks(self) -> EventHooks:
        """A consumer wired as ``EventHooks.start`` wires it (an engine (re)start)."""
        hooks = EventHooks(self.config, MagicMock())
        hooks.workflow_engine = self.engine
        hooks._event_triggers = {"platform": []}
        retry = retry_stream_key(STREAM, GROUP)
        hooks._prefixed_to_bare = {STREAM: "platform", retry: "platform"}
        hooks._retry_key = {STREAM: retry, retry: retry}
        hooks._source_key = {retry: STREAM}
        backlog = getattr(hooks, "backlog", None)
        if backlog is not None:
            backlog.bind(self.redis)
        return hooks

    def _consume_retry(self, key: str, msg_id: bytes, entry: dict[bytes, bytes]) -> None:
        asyncio.get_running_loop().create_task(
            self.hooks._dispatch(key, msg_id, entry, self.redis, GROUP)
        )

    async def publish(self, n: int, start: int = 0) -> None:
        """``n`` distinct events read off the stream in order, as the consumer loop reads them."""
        for i in range(start, start + n):
            data = {b"type": EVENT.encode(), b"id": f"evt-{i}".encode(), b"payload": b"{}"}
            await self.hooks._dispatch(STREAM, f"{i + 1}-0".encode(), data, self.redis, GROUP)

    def handled(self) -> list[str]:
        return [ref for ref, _ in self.calls]


@contextlib.contextmanager
def virtual_time() -> Any:
    """Every sleep, the limiter's clock and the backlog's clock on one virtual clock."""
    clock = VirtualTime()
    fake_time = SimpleNamespace(monotonic=clock.monotonic, time=clock.time)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("asyncio.sleep", new=clock.sleep))
        stack.enter_context(patch("robothor.engine.guardrails.time", fake_time))
        with contextlib.suppress(ImportError):
            importlib.import_module("robothor.engine.workflow_backlog")
            stack.enter_context(patch("robothor.engine.workflow_backlog.time", fake_time))
        dedup.clear()
        yield clock
        dedup.clear()


@pytest.fixture
def vt():
    with virtual_time() as clock:
        yield clock


def _max_calls_in_any_minute(times: list[float]) -> int:
    best, lo = 0, 0
    for hi, t in enumerate(times):
        while times[lo] <= t - 60:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


def _expected(n: int, start: int = 0) -> list[str]:
    return [f"evt-{i}" for i in range(start, start + n)]


EVENTS = 3000
LIMIT = 30  # the fleet default


class TestBurstDrains:
    @pytest.mark.asyncio
    async def test_a_burst_runs_every_event_once_in_arrival_order(self, engine_config, vt):
        b = Burst(engine_config, vt, _definition())
        await b.publish(EVENTS)
        await vt.run_until()

        handled = b.handled()
        out_of_order = sum(
            1 for a, c in zip(handled, handled[1:], strict=False) if int(a[4:]) > int(c[4:])
        )
        assert (b.redis.dlq(), len(handled), out_of_order) == (0, EVENTS, 0)
        assert handled == _expected(EVENTS)  # each once, in arrival order
        assert len(set(b.redis.acked)) == EVENTS
        # At the limiter's rate, and no faster.
        times = [t for _, t in b.calls]
        assert _max_calls_in_any_minute(times) <= LIMIT
        drain = times[-1] - START
        minutes = math.ceil(EVENTS / LIMIT)
        assert drain == pytest.approx((minutes - 1) * 60, abs=1)  # the limiter's rate
        # The longest any event waited is the drain itself: no event is
        # starved behind later ones.
        assert max(t - START for t in times) == drain

    @pytest.mark.asyncio
    async def test_a_concurrent_workflow_burst_starts_in_order_at_the_limit(
        self, engine_config, vt
    ):
        data = _definition(trigger={"concurrent": True}, rate_limit_per_minute=60)
        b = Burst(engine_config, vt, data)
        await b.publish(600)
        await vt.run_until()

        assert b.handled() == _expected(600)
        assert b.redis.dlq() == 0
        times = [t for _, t in b.calls]
        assert _max_calls_in_any_minute(times) <= 60
        assert times[-1] - START <= 10 * 60

    @pytest.mark.asyncio
    async def test_a_restart_mid_drain_loses_and_repeats_nothing(self, engine_config, vt):
        b = Burst(engine_config, vt, _definition())
        await b.publish(300)
        await vt.run_until(lambda: len(b.calls) >= 100)

        # The engine stops: its in-memory state is gone, Redis is not.
        await b.hooks.stop_workflow_backlog()  # type: ignore[attr-defined]
        await vt.settle()
        b.hooks = b.new_hooks()
        await b.hooks.resume_workflow_backlog(b.redis)  # type: ignore[attr-defined]
        await b.publish(50, start=300)  # and the stream goes on
        await vt.run_until()

        assert b.handled() == _expected(350)
        assert b.redis.dlq() == 0

    @pytest.mark.asyncio
    async def test_the_backlog_is_visible_while_it_drains(self, engine_config, vt):
        b = Burst(engine_config, vt, _definition())
        await b.publish(120)
        await vt.run_until(lambda: len(b.calls) >= 60)

        depth, oldest = b.hooks.backlog.stats("order-flow")  # type: ignore[attr-defined]
        assert depth == 60
        assert oldest == pytest.approx(vt.now - START, abs=1)


class TestHighAvailability:
    """Replicas share one Redis: each may write to a backlog, only the leader runs it."""

    @pytest.mark.asyncio
    async def test_only_the_leader_runs_the_backlog(self, engine_config, vt, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_HA_LEADER_ENABLED", "1")
        leader = {"now": False}
        with patch("robothor.engine.leader.is_leader", side_effect=lambda: leader["now"]):
            b = Burst(engine_config, vt, _definition())
            await b.publish(5)
            await vt.run_until(lambda: vt.now - START > 120)
            assert b.handled() == []  # a replica keeps them, never runs them
            assert b.hooks.backlog.stats("order-flow")[0] == 5

            leader["now"] = True
            await vt.run_until(lambda: len(b.calls) >= 5)
            await b.hooks.backlog.stop()
        assert b.handled() == _expected(5)

    @pytest.mark.asyncio
    async def test_the_leader_finds_events_another_replica_wrote(
        self, engine_config, vt, monkeypatch
    ):
        monkeypatch.setenv("ROBOTHOR_HA_LEADER_ENABLED", "1")
        with patch("robothor.engine.leader.is_leader", return_value=True):
            leader = Burst(engine_config, vt, _definition())
            await leader.hooks.resume_workflow_backlog(leader.redis)
            await vt.settle()  # it has looked: nothing yet, so it waits
            # Another replica, same Redis, reads the events and writes them.
            replica = leader.new_hooks()
            replica.backlog._leader = lambda: False  # its own process's verdict
            for i in range(3):
                await replica.backlog.enqueue(
                    leader.redis,
                    leader.engine.get_workflow("order-flow"),
                    "platform",
                    EVENT,
                    {"id": f"evt-{i}", "type": EVENT, "payload": "{}"},
                )
            await vt.settle()
            assert leader.handled() == []  # not woken: it has to look
            await vt.run_until(lambda: len(leader.calls) >= 3)
            await replica.backlog.stop()
            await leader.hooks.backlog.stop()
        assert leader.handled() == _expected(3)
