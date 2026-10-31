"""Interactive turns and background work run in separate lanes.

Every background loop the daemon starts (scheduler, hooks, watchdog, autodream,
curator, curiosity, capacity) shares the event loop's one default thread pool
with a person's chat turn, and hook runs share one fleet-wide semaphore. A burst
of scheduled runs therefore delays a reply: the turn's blocking calls queue for
a worker behind blocking background work.

Behind ``ROBOTHOR_EXECUTION_LANES`` each lane gets its own concurrency limit and
its own thread executor. With the flag off nothing changes.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from robothor.engine import lanes
from robothor.engine.lanes import ExecutionLanes, Lane
from robothor.engine.models import TriggerType


@pytest.fixture
def lanes_on(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EXECUTION_LANES", "1")
    monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_RIPS", raising=False)
    yield


@pytest.fixture
def lanes_off(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_EXECUTION_LANES", raising=False)
    yield


@pytest.fixture(autouse=True)
def fresh_lanes(monkeypatch):
    """The real lanes object is a process-wide singleton."""
    monkeypatch.setattr(lanes, "_LANES", None)
    yield
    monkeypatch.setattr(lanes, "_LANES", None)


async def _hold(pool: ExecutionLanes, lane: Lane, started: asyncio.Event, release: asyncio.Event):
    async with pool.slot(lane):
        started.set()
        await release.wait()


class TestTriggersMapToLanes:
    @pytest.mark.parametrize(
        "trigger",
        [
            TriggerType.TELEGRAM,
            TriggerType.WEBCHAT,
            TriggerType.SLACK,
            TriggerType.CHANNEL,
            TriggerType.MANUAL,
        ],
    )
    def test_a_person_waiting_is_interactive(self, trigger):
        assert lanes.lane_for_trigger(trigger) is Lane.INTERACTIVE

    @pytest.mark.parametrize(
        "trigger",
        [
            TriggerType.CRON,
            TriggerType.HOOK,
            TriggerType.WORKFLOW,
            TriggerType.EVENT,
            TriggerType.WEBHOOK,
            TriggerType.SUB_AGENT,
        ],
    )
    def test_nobody_waiting_is_background(self, trigger):
        assert lanes.lane_for_trigger(trigger) is Lane.BACKGROUND


class TestInteractiveIsNotQueuedBehindBackground:
    async def test_interactive_slot_within_100ms_while_background_is_saturated(self, lanes_on):
        pool = ExecutionLanes(interactive_limit=3, background_limit=2, yield_ms=200)
        release = asyncio.Event()
        holders = []
        for _ in range(2):
            started = asyncio.Event()
            holders.append(asyncio.create_task(_hold(pool, Lane.BACKGROUND, started, release)))
            await started.wait()
        assert pool.snapshot()["background"]["active"] == 2

        t0 = time.perf_counter()
        async with pool.slot(Lane.INTERACTIVE):
            waited = time.perf_counter() - t0
        assert waited < 0.1, f"interactive waited {waited * 1000:.1f} ms"

        release.set()
        await asyncio.gather(*holders)

    async def test_an_interactive_run_starts_within_100ms_through_the_run_gate(
        self, lanes_on, monkeypatch
    ):
        pool = ExecutionLanes(interactive_limit=3, background_limit=2, yield_ms=200)
        monkeypatch.setattr(lanes, "_LANES", pool)
        release = asyncio.Event()
        started_at: dict[str, float] = {}

        class FakeRunner:
            @lanes.lane_gated
            async def execute(self, agent_id, message, trigger_type=TriggerType.MANUAL):
                started_at[agent_id] = time.perf_counter()
                if trigger_type is not TriggerType.WEBCHAT:
                    await release.wait()
                return agent_id

        runner = FakeRunner()
        background = [
            asyncio.create_task(runner.execute(f"bg-{i}", "sweep", trigger_type=TriggerType.CRON))
            for i in range(2)
        ]
        while pool.snapshot()["background"]["active"] < 2:
            await asyncio.sleep(0.001)

        t0 = time.perf_counter()
        await runner.execute("example-agent", "hello", trigger_type=TriggerType.WEBCHAT)
        assert started_at["example-agent"] - t0 < 0.1

        release.set()
        await asyncio.gather(*background)

    def test_the_real_runner_execute_is_gated(self):
        from robothor.engine.runner import AgentRunner

        assert getattr(AgentRunner.execute, "__lane_gated__", False) is True


class TestBackgroundLaneIsBounded:
    async def test_a_third_background_run_waits_for_a_free_slot(self, lanes_on):
        pool = ExecutionLanes(interactive_limit=3, background_limit=2, yield_ms=200)
        release = asyncio.Event()
        holders = []
        for _ in range(2):
            started = asyncio.Event()
            holders.append(asyncio.create_task(_hold(pool, Lane.BACKGROUND, started, release)))
            await started.wait()

        third_started = asyncio.Event()
        third = asyncio.create_task(_hold(pool, Lane.BACKGROUND, third_started, asyncio.Event()))
        await asyncio.sleep(0.05)
        assert not third_started.is_set()
        assert pool.snapshot()["background"]["waiting"] == 1

        release.set()
        await asyncio.wait_for(third_started.wait(), timeout=1.0)
        third.cancel()
        await asyncio.gather(*holders, third, return_exceptions=True)
        assert pool.snapshot()["background"]["active"] == 0

    async def test_a_nested_run_takes_no_second_slot(self, lanes_on, monkeypatch):
        """A run that starts a sub-agent must not wait on its own lane."""
        pool = ExecutionLanes(interactive_limit=1, background_limit=1, yield_ms=200)
        monkeypatch.setattr(lanes, "_LANES", pool)
        seen: list[int] = []

        class FakeRunner:
            @lanes.lane_gated
            async def execute(self, agent_id, message, trigger_type=TriggerType.MANUAL):
                seen.append(pool.snapshot()["background"]["active"])
                if agent_id == "parent":
                    await self.execute("child", "sub", trigger_type=TriggerType.SUB_AGENT)
                return agent_id

        await asyncio.wait_for(
            FakeRunner().execute("parent", "go", trigger_type=TriggerType.CRON), timeout=1.0
        )
        assert seen == [1, 1]

    async def test_a_slot_is_released_when_the_run_raises(self, lanes_on):
        pool = ExecutionLanes(interactive_limit=1, background_limit=1, yield_ms=200)
        with pytest.raises(RuntimeError):
            async with pool.slot(Lane.BACKGROUND):
                raise RuntimeError("boom")
        assert pool.snapshot()["background"]["active"] == 0


class TestBackgroundYieldsToAWaitingInteractiveTurn:
    async def test_background_waits_at_most_the_yield_window(self, lanes_on):
        pool = ExecutionLanes(interactive_limit=1, background_limit=2, yield_ms=100)
        release = asyncio.Event()
        started = asyncio.Event()
        holder = asyncio.create_task(_hold(pool, Lane.INTERACTIVE, started, release))
        await started.wait()
        waiter = asyncio.create_task(_hold(pool, Lane.INTERACTIVE, asyncio.Event(), release))
        while pool.snapshot()["interactive"]["waiting"] < 1:
            await asyncio.sleep(0.001)

        t0 = time.perf_counter()
        async with pool.slot(Lane.BACKGROUND):
            waited = time.perf_counter() - t0
        assert 0.08 <= waited < 0.3, f"background waited {waited * 1000:.1f} ms"

        release.set()
        await asyncio.gather(holder, waiter)

    async def test_background_does_not_wait_when_nobody_is_waiting(self, lanes_on):
        pool = ExecutionLanes(interactive_limit=1, background_limit=2, yield_ms=500)
        t0 = time.perf_counter()
        async with pool.slot(Lane.BACKGROUND):
            waited = time.perf_counter() - t0
        assert waited < 0.05


class TestSeparateExecutors:
    async def test_saturated_background_executor_does_not_delay_interactive_to_thread(
        self, lanes_on
    ):
        loop = asyncio.get_running_loop()
        executor = lanes.LaneExecutor(interactive_workers=2, background_workers=2)
        loop.set_default_executor(executor)
        try:
            with lanes.in_lane(Lane.BACKGROUND):
                blockers = [
                    asyncio.ensure_future(asyncio.to_thread(time.sleep, 0.5)) for _ in range(4)
                ]
            await asyncio.sleep(0.01)

            with lanes.in_lane(Lane.INTERACTIVE):
                t0 = time.perf_counter()
                await asyncio.to_thread(lambda: None)
                waited = time.perf_counter() - t0
            assert waited < 0.1, f"interactive to_thread waited {waited * 1000:.1f} ms"
            await asyncio.gather(*blockers)
        finally:
            executor.shutdown(wait=True)

    async def test_untagged_work_runs_on_the_background_executor(self, lanes_on):
        loop = asyncio.get_running_loop()
        executor = lanes.LaneExecutor(interactive_workers=1, background_workers=1)
        loop.set_default_executor(executor)
        try:
            import threading

            name = await asyncio.to_thread(lambda: threading.current_thread().name)
            assert "background" in name
            with lanes.in_lane(Lane.INTERACTIVE):
                name = await asyncio.to_thread(lambda: threading.current_thread().name)
            assert "interactive" in name
        finally:
            executor.shutdown(wait=True)

    async def test_install_sets_the_lane_executor_when_on(self, lanes_on):
        loop = asyncio.new_event_loop()
        try:
            assert lanes.install_lane_executor(loop) is True
            assert isinstance(loop._default_executor, lanes.LaneExecutor)
        finally:
            loop.close()


class TestFlagOffIsToday:
    def test_the_flag_defaults_off(self, lanes_off):
        assert lanes.lanes_enabled() is False

    async def test_no_lane_slot_is_taken(self, lanes_off, monkeypatch):
        pool = ExecutionLanes(interactive_limit=1, background_limit=1, yield_ms=200)
        monkeypatch.setattr(lanes, "_LANES", pool)
        seen: list[int] = []

        class FakeRunner:
            @lanes.lane_gated
            async def execute(self, agent_id, message, trigger_type=TriggerType.MANUAL):
                snap = pool.snapshot()
                seen.append(snap["background"]["active"] + snap["interactive"]["active"])
                return agent_id

        runner = FakeRunner()
        await asyncio.gather(
            *[runner.execute(f"a{i}", "m", trigger_type=TriggerType.CRON) for i in range(3)]
        )
        assert seen == [0, 0, 0]

    def test_install_leaves_the_default_executor_alone(self, lanes_off):
        loop = asyncio.new_event_loop()
        try:
            before = ThreadPoolExecutor(max_workers=1)
            loop.set_default_executor(before)
            assert lanes.install_lane_executor(loop) is False
            assert loop._default_executor is before
        finally:
            loop.close()

    async def test_hook_runs_keep_the_single_fleet_semaphore(self, lanes_off):
        from robothor.engine.hooks import EventHooks

        config = SimpleNamespace(max_concurrent_agents=2)
        hooks = EventHooks(config, runner=None)
        sem = hooks._agent_runs
        await sem.acquire()
        await sem.acquire()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sem.acquire(), timeout=0.05)
        sem.release()
        sem.release()


class TestOccupancyIsVisible:
    def test_snapshot_reports_each_lane(self, lanes_on):
        snap = ExecutionLanes(interactive_limit=3, background_limit=2, yield_ms=200).snapshot()
        assert snap["enabled"] is True
        assert snap["interactive"] == {"active": 0, "waiting": 0, "limit": 3}
        assert snap["background"] == {"active": 0, "waiting": 0, "limit": 2}

    def test_limits_come_from_the_environment(self, lanes_on, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MAX_CONCURRENT_INTERACTIVE", "4")
        monkeypatch.setenv("ROBOTHOR_MAX_CONCURRENT_BACKGROUND", "1")
        snap = lanes.get_lanes().snapshot()
        assert snap["interactive"]["limit"] == 4
        assert snap["background"]["limit"] == 1

    def test_default_limits(self, lanes_on, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_MAX_CONCURRENT_INTERACTIVE", raising=False)
        monkeypatch.delenv("ROBOTHOR_MAX_CONCURRENT_BACKGROUND", raising=False)
        snap = lanes.get_lanes().snapshot()
        assert snap["interactive"]["limit"] == 3
        assert snap["background"]["limit"] == 2

    async def test_prometheus_gauges_track_occupancy(self, lanes_on):
        from prometheus_client import REGISTRY

        pool = ExecutionLanes(interactive_limit=3, background_limit=2, yield_ms=200)
        async with pool.slot(Lane.BACKGROUND):
            assert REGISTRY.get_sample_value("robothor_lane_active", {"lane": "background"}) == 1
        assert REGISTRY.get_sample_value("robothor_lane_active", {"lane": "background"}) == 0
        assert REGISTRY.get_sample_value("robothor_lane_waiting", {"lane": "interactive"}) == 0

    def test_health_has_the_execution_lanes_block(self, lanes_on):
        from robothor.engine.tests.test_health_execution_mode import _health

        block = _health()["execution_lanes"]
        assert block["enabled"] is True
        assert set(block) >= {"enabled", "interactive", "background"}
        assert block["background"]["limit"] >= 1
