"""Hook workflow runs deferred by the tool-call rate limit are counted and summarised.

A hook run whose first step the rate limit would block is deferred and its
event requeued (``test_workflow_rate_limit.py`` pins that). A burst used to log
one line per deferral and leave nothing an operator could read afterwards. Now
each deferral is counted per workflow and per minute window in Redis, where the
doctor reads it from another process, and one summary line per workflow is
logged when the window closes. The requeue itself is unchanged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.deferrals import (
    WINDOW_SECONDS,
    DeferredRuns,
    recent_deferrals,
    window_key,
)
from robothor.engine.hooks import STREAM_PREFIX, EventHooks
from robothor.engine.models import RunStatus
from robothor.engine.workflow import WorkflowEngine, parse_workflow

STREAM = f"{STREAM_PREFIX}platform"
EVENT = "platform.order.created"
WORKFLOW = "example-workflow"
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
def limiter_clock():
    # Only the limiter's clock: patching time.monotonic itself would freeze
    # the event loop's clock too, and with it every asyncio timeout.
    c = _Clock(1000.0)
    with patch("robothor.engine.guardrails.time", SimpleNamespace(monotonic=c)):
        yield c


@pytest.fixture
def no_dedup():
    with (
        patch("robothor.engine.dedup.try_acquire", new=AsyncMock(return_value=True)),
        patch("robothor.engine.dedup.release", new=AsyncMock()),
    ):
        yield


def _engine(engine_config: Any, limit: int) -> WorkflowEngine:
    runner = MagicMock()
    runner.registry.execute = AsyncMock(return_value={"ok": True})
    engine = WorkflowEngine(engine_config, runner=runner)
    wf = parse_workflow(
        {
            "id": WORKFLOW,
            "rate_limit_per_minute": limit,
            "triggers": [{"type": "hook", "stream": "platform", "event_type": EVENT}],
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
    )
    engine._workflows[wf.id] = wf
    engine._persist_run_start = MagicMock()  # type: ignore[method-assign]
    engine._persist_run_end = MagicMock()  # type: ignore[method-assign]
    engine._persist_step = MagicMock()  # type: ignore[method-assign]
    engine._persist_step_start = MagicMock()  # type: ignore[method-assign]
    return engine


class _Harness:
    def __init__(self, engine_config: Any, limit: int) -> None:
        self.engine = _engine(engine_config, limit)
        self.wf = self.engine.get_workflow(WORKFLOW)
        self.clock = _Clock(T0)
        self.redis = FakeAsyncRedis()
        self.hooks = EventHooks(engine_config, MagicMock())
        self.hooks.workflow_engine = self.engine
        self.hooks.deferred_runs = DeferredRuns(clock=self.clock)
        self.requeued: list[dict[str, Any]] = []
        self.runs: list[Any] = []
        real_execute = self.engine.execute

        async def execute(*args: Any, **kwargs: Any) -> Any:
            run = await real_execute(*args, **kwargs)
            self.runs.append(run)
            return run

        self.engine.execute = execute  # type: ignore[method-assign]

        async def requeue(_redis: Any, stream: str, data: dict[str, Any], delay: int = 0) -> None:
            assert stream == STREAM
            self.requeued.append(data)

        self.hooks._delayed_requeue = requeue  # type: ignore[method-assign]

    async def fire(self, envelope: dict[str, Any]) -> None:
        await self.hooks._run_workflow(self.wf, "platform", EVENT, envelope, self.redis, STREAM)

    async def trigger(self, n: int, start: int = 0) -> None:
        for i in range(start, start + n):
            await self.fire({"id": f"evt-{i}", "type": EVENT, "payload": "{}"})

    def effects(self) -> list[str]:
        return [c.args[1]["ref"] for c in self.engine.runner.registry.execute.await_args_list]

    def counted(self) -> int:
        return sum(sum(bucket.values()) for bucket in self.redis.hashes.values())


def _summaries(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "deferred=" in r.getMessage()]


class TestCountedPerWindow:
    @pytest.mark.asyncio
    async def test_a_burst_past_the_limit_is_counted_and_summarised_once(
        self, engine_config, limiter_clock, no_dedup, caplog
    ):
        h = _Harness(engine_config, limit=10)
        with caplog.at_level(logging.INFO):
            await h.trigger(25)
            # Still inside the window: nothing summarised yet.
            h.hooks.deferred_runs.flush()
            assert _summaries(caplog) == []

            h.clock.now = WINDOW_START + WINDOW_SECONDS + 1  # the window closes
            h.hooks.deferred_runs.flush()

        assert h.redis.hashes[window_key(WINDOW_START)] == {WORKFLOW: 15}
        assert _summaries(caplog) == [
            f"Workflow runs deferred by the tool-call rate limit: "
            f"workflow={WORKFLOW} deferred=15 window={_label(WINDOW_START)}"
        ]
        # One line per window, not one per run: no deferral logs its own line.
        per_run = [
            r
            for r in caplog.records
            if r.levelno >= logging.INFO
            and ("Requeueing" in r.getMessage() or "deferred:" in r.getMessage())
        ]
        assert per_run == []

    @pytest.mark.asyncio
    async def test_requeueing_is_unchanged(self, engine_config, limiter_clock, no_dedup):
        h = _Harness(engine_config, limit=10)
        await h.trigger(25)
        assert [r.status for r in h.runs].count(RunStatus.COMPLETED) == 10
        assert [r.rate_limited for r in h.runs].count(True) == 15
        assert [e["id"] for e in h.requeued] == [f"evt-{i}" for i in range(10, 25)]
        assert {e["_busy_requeue_count"] for e in h.requeued} == {"1"}

    @pytest.mark.asyncio
    async def test_a_second_flush_logs_nothing_more(
        self, engine_config, limiter_clock, no_dedup, caplog
    ):
        h = _Harness(engine_config, limit=1)
        await h.trigger(3)
        h.clock.now = WINDOW_START + WINDOW_SECONDS
        with caplog.at_level(logging.INFO):
            h.hooks.deferred_runs.flush()
            h.hooks.deferred_runs.flush()
        assert len(_summaries(caplog)) == 1

    @pytest.mark.asyncio
    async def test_the_next_deferral_closes_the_previous_window(
        self, engine_config, limiter_clock, no_dedup, caplog
    ):
        h = _Harness(engine_config, limit=1)
        with caplog.at_level(logging.INFO):
            await h.trigger(3)  # two deferred in the first window
            h.clock.now = WINDOW_START + WINDOW_SECONDS + 5
            await h.trigger(1, start=3)  # one more, in the next window
        assert _summaries(caplog) == [
            f"Workflow runs deferred by the tool-call rate limit: "
            f"workflow={WORKFLOW} deferred=2 window={_label(WINDOW_START)}"
        ]
        assert h.redis.hashes[window_key(WINDOW_START + WINDOW_SECONDS)] == {WORKFLOW: 1}

    @pytest.mark.asyncio
    async def test_the_counter_outlives_the_doctors_look_back(
        self, engine_config, limiter_clock, no_dedup
    ):
        from robothor.engine.deferrals import LOOKBACK_SECONDS

        h = _Harness(engine_config, limit=1)
        await h.trigger(2)
        ttl = h.redis.ttls[window_key(WINDOW_START)]
        assert ttl >= LOOKBACK_SECONDS + WINDOW_SECONDS


class TestNothingDeferred:
    @pytest.mark.asyncio
    async def test_a_window_with_no_deferral_logs_nothing(
        self, engine_config, limiter_clock, no_dedup, caplog
    ):
        h = _Harness(engine_config, limit=10)
        with caplog.at_level(logging.INFO):
            await h.trigger(5)
            h.clock.now = WINDOW_START + WINDOW_SECONDS + 1
            h.hooks.deferred_runs.flush()
        assert _summaries(caplog) == []
        assert h.redis.hashes == {}


class TestCountedOnce:
    @pytest.mark.asyncio
    async def test_a_deferred_run_that_later_runs_is_not_counted_again(
        self, engine_config, limiter_clock, no_dedup
    ):
        h = _Harness(engine_config, limit=10)
        await h.trigger(11)
        assert h.counted() == 1
        (requeued,) = h.requeued

        # A minute later the limiter has room and the requeued event runs.
        limiter_clock.now += 61
        h.clock.now = WINDOW_START + WINDOW_SECONDS + 1
        await h.fire(requeued)
        assert h.runs[-1].status == RunStatus.COMPLETED
        assert h.counted() == 1

    @pytest.mark.asyncio
    async def test_a_deferred_write_is_counted_and_never_made(
        self, engine_config, limiter_clock, no_dedup
    ):
        h = _Harness(engine_config, limit=2)
        await h.trigger(3)
        assert h.counted() == 1
        # The deferred attempt ran no step, so its write did not happen.
        assert h.effects() == ["evt-0", "evt-1"]

        limiter_clock.now += 61
        await h.fire(h.requeued[0])
        assert h.effects() == ["evt-0", "evt-1", "evt-2"]
        assert h.counted() == 1


class TestRedisTrouble:
    @pytest.mark.asyncio
    async def test_a_counter_that_cannot_be_written_still_requeues(
        self, engine_config, limiter_clock, no_dedup
    ):
        h = _Harness(engine_config, limit=1)
        h.redis.hincrby = AsyncMock(side_effect=ConnectionError("down"))  # type: ignore[method-assign]
        await h.trigger(2)
        assert [e["id"] for e in h.requeued] == ["evt-1"]


class TestReadBack:
    def test_recent_deferrals_sums_the_windows_in_the_look_back(self):
        hashes = {
            window_key(WINDOW_START): {WORKFLOW: 15, "other-workflow": 2},
            window_key(WINDOW_START - WINDOW_SECONDS): {WORKFLOW: 5},
            # Older than an hour: outside the look-back.
            window_key(WINDOW_START - 3600 - WINDOW_SECONDS): {WORKFLOW: 900},
        }
        counts = recent_deferrals(FakeSyncRedis(hashes), now=T0, lookback=3600)
        assert counts == {WORKFLOW: 20, "other-workflow": 2}

    def test_reading_twice_reads_the_same_count(self):
        hashes = {window_key(WINDOW_START): {WORKFLOW: 15}}
        client = FakeSyncRedis(hashes)
        first = recent_deferrals(client, now=T0, lookback=3600)
        second = recent_deferrals(client, now=T0, lookback=3600)
        assert first == second == {WORKFLOW: 15}
