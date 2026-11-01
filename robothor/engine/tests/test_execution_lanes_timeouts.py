"""Time queued for a lane slot is not run time.

The scheduler wraps a cron run in a safety ``asyncio.timeout`` (the agent's
timeout plus grace), and a workflow wraps its steps in ``wf.timeout_seconds``.
If the lane slot were taken inside those clocks, a healthy run queued behind a
burst of background work would be cancelled for waiting, and the scheduler's
timeout would count toward its circuit breaker. So both take the slot first.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine import lanes
from robothor.engine.lanes import ExecutionLanes, Lane


@pytest.fixture
def lanes_on(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_EXECUTION_LANES", "1")
    monkeypatch.delenv("ROBOTHOR_DISABLE_ALL_RIPS", raising=False)


@pytest.fixture
def pool(monkeypatch):
    pool = ExecutionLanes(interactive_limit=0, background_limit=1, yield_ms=0)
    monkeypatch.setattr(lanes, "_LANES", pool)
    yield pool
    monkeypatch.setattr(lanes, "_LANES", None)


@pytest.fixture
def clock_entries(monkeypatch):
    """Record the background lane's state each time a timeout clock starts."""
    real_timeout = asyncio.timeout
    entries: list[dict[str, int]] = []

    def recording_timeout(delay):
        snap = lanes.get_lanes().snapshot()["background"]
        entries.append({"active": snap["active"], "waiting": snap["waiting"]})
        return real_timeout(delay)

    monkeypatch.setattr(asyncio, "timeout", recording_timeout)
    return entries


def _scheduler(monkeypatch, execute):
    import robothor.engine.scheduler as sched_mod
    from robothor.engine.scheduler import CronScheduler

    s = CronScheduler.__new__(CronScheduler)
    s.config = MagicMock(tenant_id="default")
    s.runner = MagicMock()
    monkeypatch.setattr("robothor.engine.leader.is_leader", lambda: True)
    monkeypatch.setattr(sched_mod, "try_acquire", AsyncMock(return_value=True))
    monkeypatch.setattr(sched_mod, "release", AsyncMock())
    monkeypatch.setattr(sched_mod, "admit", lambda *a, **k: True)
    monkeypatch.setattr(sched_mod, "register", lambda *a, **k: None)
    monkeypatch.setattr(sched_mod, "complete", lambda *a, **k: None)
    s._circuit_breaker_tripped = MagicMock(return_value=False)
    s._record_timeout = MagicMock()
    s._execute_and_deliver = execute
    return s


class TestTheSchedulerClockStartsAfterTheSlot:
    async def test_a_queued_cron_run_does_not_start_its_safety_clock(
        self, lanes_on, pool, clock_entries, monkeypatch
    ):
        ran = asyncio.Event()

        async def execute(*args, **kwargs):
            ran.set()

        s = _scheduler(monkeypatch, execute)
        agent = MagicMock(timeout_seconds=1)
        await pool.acquire(Lane.BACKGROUND)  # the lane is full
        task = asyncio.create_task(s._run_scheduled("example-agent", "example-agent", agent, "c"))
        await asyncio.sleep(0.05)
        assert clock_entries == [], "the safety clock started while the run was queued"
        assert pool.snapshot()["background"]["waiting"] == 1

        pool.release(Lane.BACKGROUND)
        await asyncio.wait_for(task, timeout=1.0)
        assert ran.is_set()
        # The clock started with this run already holding the slot.
        assert clock_entries[0] == {"active": 1, "waiting": 0}
        s._record_timeout.assert_not_called()
        assert pool.snapshot()["background"]["active"] == 0

    async def test_the_run_inside_takes_no_second_slot(self, lanes_on, pool, monkeypatch):
        seen: list[int] = []

        class FakeRunner:
            @lanes.lane_gated
            async def execute(self, agent_id, message, trigger_type=None):
                seen.append(pool.snapshot()["background"]["active"])

        runner = FakeRunner()

        async def execute(*args, **kwargs):
            from robothor.engine.models import TriggerType

            await runner.execute("example-agent", "m", trigger_type=TriggerType.CRON)

        s = _scheduler(monkeypatch, execute)
        await asyncio.wait_for(
            s._run_scheduled("example-agent", "example-agent", MagicMock(timeout_seconds=1), "c"),
            timeout=1.0,
        )
        assert seen == [1]

    async def test_flag_off_takes_no_slot(self, pool, clock_entries, monkeypatch):
        monkeypatch.delenv("ROBOTHOR_EXECUTION_LANES", raising=False)

        async def execute(*args, **kwargs):
            return None

        s = _scheduler(monkeypatch, execute)
        await pool.acquire(Lane.BACKGROUND)
        await asyncio.wait_for(
            s._run_scheduled("example-agent", "example-agent", MagicMock(timeout_seconds=1), "c"),
            timeout=1.0,
        )
        assert clock_entries == [{"active": 1, "waiting": 0}]
        pool.release(Lane.BACKGROUND)


class TestTheWorkflowClockStartsAfterTheSlot:
    async def test_a_queued_workflow_does_not_start_its_timeout(
        self, lanes_on, pool, clock_entries, tmp_path
    ):
        from robothor.engine.config import EngineConfig
        from robothor.engine.models import WorkflowDef, WorkflowStepDef, WorkflowStepType
        from robothor.engine.workflow import WorkflowEngine

        engine = WorkflowEngine(
            EngineConfig(workspace=tmp_path, manifest_dir=tmp_path / "agents"), runner=MagicMock()
        )
        step = WorkflowStepDef(id="t", type=WorkflowStepType.TRANSFORM, transform_expr="value")
        engine._workflows["test-wf"] = WorkflowDef(
            id="test-wf", name="Test WF", steps=[step], timeout_seconds=1
        )
        engine._persist_run_start = MagicMock()
        engine._persist_run_end = MagicMock()
        engine._persist_step = MagicMock()

        await pool.acquire(Lane.BACKGROUND)
        task = asyncio.create_task(engine.execute("test-wf"))
        await asyncio.sleep(0.05)
        assert clock_entries == [], "the workflow timeout started while it was queued"

        pool.release(Lane.BACKGROUND)
        run = await asyncio.wait_for(task, timeout=1.0)
        assert run.status.value == "completed"
        assert clock_entries[0] == {"active": 1, "waiting": 0}


class TestZeroMeansUncappedOnlyForInteractive:
    def test_a_background_limit_of_zero_still_bounds_to_one(self, lanes_on, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_MAX_CONCURRENT_BACKGROUND", "0")
        monkeypatch.setattr(lanes, "_LANES", None)
        assert lanes.get_lanes().snapshot()["background"]["limit"] == 1
        monkeypatch.setattr(lanes, "_LANES", None)
