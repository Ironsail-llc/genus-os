"""Hook workflow runs deferred by the tool-call rate limit are counted and summarised.

A hook event whose run cannot start at once, because the limit's free slots
are already spoken for by the events waiting ahead of it in the workflow's
backlog, is a deferred run (``test_workflow_burst_drain.py`` pins the drain).
Each is counted once, per workflow and per minute window in Redis, where the
doctor reads it from another process, and one summary line per workflow,
carrying the backlog still waiting, is logged when the window closes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.deferrals import (
    WINDOW_SECONDS,
    DeferredRuns,
    recent_deferrals,
    window_key,
)
from robothor.engine.hooks import STREAM_PREFIX, EventHooks
from robothor.engine.tests.test_workflow_burst_drain import (
    START,
    Burst,
    VirtualTime,
    virtual_time,
)

STREAM = f"{STREAM_PREFIX}platform"
EVENT = "platform.order.created"
WORKFLOW = "example-workflow"
#: The engine_config fixture's tenant.
TENANT = "test-tenant"
#: A wall-clock instant ten seconds into a minute window.
WINDOW_START = 1_699_999_980
T0 = WINDOW_START + 10


def _label(start: int) -> str:
    return datetime.fromtimestamp(start, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeAsyncRedis:
    """The few hash commands the counter uses, against a dict."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, int]] = {}
        self.ttls: dict[str, int] = {}

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        bucket = self.hashes.setdefault(key, {})
        bucket[field] = bucket.get(field, 0) + amount
        return bucket[field]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True


class FakeSyncRedis:
    """What the doctor's synchronous client sees of the same data."""

    def __init__(self, hashes: dict[str, dict[str, int]]) -> None:
        self.hashes = hashes

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        return {f.encode(): str(v).encode() for f, v in self.hashes.get(key, {}).items()}


@pytest.fixture
def vt():
    with virtual_time() as clock:
        yield clock


def _definition(limit: int) -> dict[str, Any]:
    return {
        "id": WORKFLOW,
        "rate_limit_per_minute": limit,
        "triggers": [
            {"type": "hook", "stream": "platform", "event_type": EVENT, "requeue_if_busy": True}
        ],
        # A write: create_task changes state wherever it runs.
        "steps": [
            {
                "id": "claim",
                "type": "tool",
                "tool_name": "create_task",
                "tool_args": {"ref": "{{ event.id }}"},
            }
        ],
    }


class _Harness(Burst):
    """A burst through the real hook consumer and backlog, counted on the virtual clock."""

    def __init__(self, engine_config: Any, vt: VirtualTime, limit: int) -> None:
        super().__init__(engine_config, vt, _definition(limit))
        self.hooks.deferred_runs = DeferredRuns(
            TENANT, clock=vt.time, backlog=self.hooks.backlog.stats
        )

    async def trigger(self, n: int, start: int = 0) -> None:
        await self.publish(n, start)
        await self.vt.settle()

    def effects(self) -> list[str]:
        return self.handled()

    def counted(self) -> int:
        return sum(sum(bucket.values()) for bucket in self.redis.hashes.values())


def _summaries(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "deferred=" in r.getMessage()]


def _line(deferred: int, start: int, backlog: int, oldest: int) -> str:
    return (
        f"Workflow runs deferred by the tool-call rate limit: workflow={WORKFLOW} "
        f"deferred={deferred} window={_label(start)} backlog={backlog} oldest={oldest}s"
    )


class TestCountedPerWindow:
    @pytest.mark.asyncio
    async def test_a_burst_past_the_limit_is_counted_and_summarised_once(
        self, engine_config, vt, caplog
    ):
        h = _Harness(engine_config, vt, limit=10)
        with caplog.at_level(logging.INFO):
            await h.trigger(25)
            # Still inside the window: nothing summarised yet.
            h.hooks.deferred_runs.flush()
            assert _summaries(caplog) == []

            vt.now = WINDOW_START + WINDOW_SECONDS + 1  # the window closes
            h.hooks.deferred_runs.flush()

        assert h.redis.hashes[window_key(TENANT, WINDOW_START)] == {WORKFLOW: 15}
        # The line says what is still waiting: 15 events, the oldest since the burst.
        assert _summaries(caplog) == [_line(15, WINDOW_START, 15, int(vt.now - START))]
        # One line per window, not one per run: no deferral logs its own line.
        per_run = [
            r
            for r in caplog.records
            if r.levelno >= logging.INFO
            and ("Requeueing" in r.getMessage() or "deferred:" in r.getMessage())
        ]
        assert per_run == []

    @pytest.mark.asyncio
    async def test_every_deferred_event_runs_once_in_order(self, engine_config, vt):
        h = _Harness(engine_config, vt, limit=10)
        await h.trigger(25)
        assert h.effects() == [f"evt-{i}" for i in range(10)]
        await vt.run_until()
        assert h.effects() == [f"evt-{i}" for i in range(25)]
        assert h.redis.dlq() == 0

    @pytest.mark.asyncio
    async def test_a_second_flush_logs_nothing_more(self, engine_config, vt, caplog):
        h = _Harness(engine_config, vt, limit=1)
        await h.trigger(3)
        vt.now = WINDOW_START + WINDOW_SECONDS
        with caplog.at_level(logging.INFO):
            h.hooks.deferred_runs.flush()
            h.hooks.deferred_runs.flush()
        assert len(_summaries(caplog)) == 1

    @pytest.mark.asyncio
    async def test_the_next_deferral_closes_the_previous_window(self, engine_config, vt, caplog):
        h = _Harness(engine_config, vt, limit=1)
        with caplog.at_level(logging.INFO):
            await h.trigger(3)  # two deferred in the first window
            vt.now = WINDOW_START + WINDOW_SECONDS + 5  # the limiter's minute is not over
            await h.trigger(1, start=3)  # one more, in the next window
        assert _summaries(caplog) == [_line(2, WINDOW_START, 3, int(vt.now - START))]
        assert h.redis.hashes[window_key(TENANT, WINDOW_START + WINDOW_SECONDS)] == {WORKFLOW: 1}

    @pytest.mark.asyncio
    async def test_the_counter_outlives_the_doctors_look_back(self, engine_config, vt):
        from robothor.engine.deferrals import LOOKBACK_SECONDS

        h = _Harness(engine_config, vt, limit=1)
        await h.trigger(2)
        ttl = h.redis.ttls[window_key(TENANT, WINDOW_START)]
        assert ttl >= LOOKBACK_SECONDS + WINDOW_SECONDS


class TestNothingDeferred:
    @pytest.mark.asyncio
    async def test_a_window_with_no_deferral_logs_nothing(self, engine_config, vt, caplog):
        h = _Harness(engine_config, vt, limit=10)
        with caplog.at_level(logging.INFO):
            await h.trigger(5)
            vt.now = WINDOW_START + WINDOW_SECONDS + 1
            h.hooks.deferred_runs.flush()
        assert _summaries(caplog) == []
        assert h.redis.hashes == {}


class TestCountedOnce:
    @pytest.mark.asyncio
    async def test_a_deferred_run_that_later_runs_is_not_counted_again(self, engine_config, vt):
        h = _Harness(engine_config, vt, limit=10)
        await h.trigger(11)
        assert h.counted() == 1
        # A minute later the limiter has room and the waiting event runs.
        await vt.run_until()
        assert h.effects()[-1] == "evt-10"
        assert h.counted() == 1

    @pytest.mark.asyncio
    async def test_an_event_that_waits_several_windows_is_counted_once(
        self, engine_config, vt, caplog
    ):
        # Limit 10, 25 events: the last waits through two more windows. Still
        # one deferred run per event that could not start at once.
        h = _Harness(engine_config, vt, limit=10)
        await h.trigger(25)
        await vt.run_until()
        assert h.counted() == 15
        assert h.redis.hashes == {window_key(TENANT, WINDOW_START): {WORKFLOW: 15}}

    @pytest.mark.asyncio
    async def test_a_deferred_write_is_counted_and_made_once_later(self, engine_config, vt):
        h = _Harness(engine_config, vt, limit=2)
        await h.trigger(3)
        assert h.counted() == 1
        # The deferred event ran no step yet, so its write has not happened.
        assert h.effects() == ["evt-0", "evt-1"]

        await vt.run_until()
        assert h.effects() == ["evt-0", "evt-1", "evt-2"]
        assert h.counted() == 1


class TestRedisTrouble:
    @pytest.mark.asyncio
    async def test_a_counter_that_cannot_be_written_still_runs_every_event(self, engine_config, vt):
        h = _Harness(engine_config, vt, limit=1)
        h.redis.hincrby = AsyncMock(side_effect=ConnectionError("down"))  # type: ignore[method-assign]
        await h.trigger(2)
        await vt.run_until()
        assert h.effects() == ["evt-0", "evt-1"]


class TestReadBack:
    def test_recent_deferrals_sums_the_windows_in_the_look_back(self):
        hashes = {
            window_key(TENANT, WINDOW_START): {WORKFLOW: 15, "other-workflow": 2},
            window_key(TENANT, WINDOW_START - WINDOW_SECONDS): {WORKFLOW: 5},
            # Older than an hour: outside the look-back.
            window_key(TENANT, WINDOW_START - 3600 - WINDOW_SECONDS): {WORKFLOW: 900},
        }
        counts = recent_deferrals(FakeSyncRedis(hashes), TENANT, now=T0, lookback=3600)
        assert counts == {WORKFLOW: 20, "other-workflow": 2}

    def test_reading_twice_reads_the_same_count(self):
        hashes = {window_key(TENANT, WINDOW_START): {WORKFLOW: 15}}
        client = FakeSyncRedis(hashes)
        first = recent_deferrals(client, TENANT, now=T0, lookback=3600)
        second = recent_deferrals(client, TENANT, now=T0, lookback=3600)
        assert first == second == {WORKFLOW: 15}


class FakeSharedRedis(FakeAsyncRedis):
    """One store that several tenants' engines and doctors share."""

    def hgetall(self, key: str) -> dict[bytes, bytes]:
        return FakeSyncRedis(self.hashes).hgetall(key)


class TestOneStoreSeveralTenants:
    @pytest.mark.asyncio
    async def test_a_tenant_reads_only_its_own_deferrals(self):
        shared = FakeSharedRedis()
        engine_a = DeferredRuns("tenant-a", clock=_Clock(T0))
        engine_b = DeferredRuns("tenant-b", clock=_Clock(T0))
        for _ in range(3):
            await engine_b.record("orders-sync", shared)
        await engine_a.record("orders-sync", shared)

        assert recent_deferrals(shared, "tenant-a", now=T0) == {"orders-sync": 1}
        assert recent_deferrals(shared, "tenant-b", now=T0) == {"orders-sync": 3}

    @pytest.mark.asyncio
    async def test_the_engine_counts_under_its_own_tenant(self, engine_config, vt):
        h = _Harness(engine_config, vt, limit=1)
        h.hooks.deferred_runs = EventHooks(engine_config, MagicMock()).deferred_runs
        await h.trigger(2)
        (key,) = h.redis.hashes
        assert key.startswith(window_key(TENANT, 0).rsplit(":", 1)[0] + ":")
        assert h.redis.hashes[key] == {WORKFLOW: 1}
