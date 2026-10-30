"""A workflow's tool-call rate limit: its own ceiling, and a burst slows down.

Every workflow's tool steps share one per-minute guardrail keyed
``workflow:<id>``. It was fixed at the fleet default (30/min) with no way to
raise it, and a step it blocked failed the run outright, so a burst of hook
events lost every run past the ceiling. The limit is now set per workflow and
a blocked step waits in place until the limiter admits it, within the
workflow's deadline. Only a hook run blocked at its first step, before it has
done anything, is deferred and its event requeued instead: replaying an event
re-runs every step, so a run that has already acted must never be replayed.
"""

from __future__ import annotations

from collections import deque
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.hooks import MAX_BUSY_REQUEUES, STREAM_PREFIX, EventHooks
from robothor.engine.models import RunStatus
from robothor.engine.workflow import WorkflowEngine, parse_workflow

STREAM = f"{STREAM_PREFIX}platform"
EVENT = "platform.order.created"


def _definition(**extra: Any) -> dict[str, Any]:
    step: dict[str, Any] = {"id": "claim", "type": "tool", "tool_name": "create_task"}
    step.update(extra.pop("step", {}))
    return {
        "id": "order-flow",
        "triggers": [{"type": "hook", "stream": "platform", "event_type": EVENT}],
        "steps": [step],
        **extra,
    }


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    c = _Clock()
    with patch("robothor.engine.guardrails.time.monotonic", c):
        yield c


@pytest.fixture
def no_dedup():
    with (
        patch("robothor.engine.dedup.try_acquire", new=AsyncMock(return_value=True)),
        patch("robothor.engine.dedup.release", new=AsyncMock()),
    ):
        yield


def _engine(engine_config: Any, data: dict[str, Any]) -> WorkflowEngine:
    runner = MagicMock()
    runner.registry.execute = AsyncMock(return_value={"ok": True})
    engine = WorkflowEngine(engine_config, runner=runner)
    wf = parse_workflow(data)
    engine._workflows[wf.id] = wf
    engine._persist_run_start = MagicMock()  # type: ignore[method-assign]
    engine._persist_run_end = MagicMock()  # type: ignore[method-assign]
    engine._persist_step = MagicMock()  # type: ignore[method-assign]
    engine._persist_step_start = MagicMock()  # type: ignore[method-assign]
    return engine


async def _run(
    engine: WorkflowEngine, trigger: str = "hook", *, requeue: bool = False, event: str = "evt"
) -> Any:
    kwargs: dict[str, Any] = {"requeue_on_rate_limit": True} if requeue else {}
    return await engine.execute(
        "order-flow",
        trigger_type=trigger,
        initial_context={"event": {"id": event}},
        user_id="service:workflow:order-flow",
        user_role="service",
        **kwargs,
    )


def _tool(step_id: str) -> dict[str, Any]:
    """A write step whose effect names the event and the step, so repeats show."""
    return {
        "id": step_id,
        "type": "tool",
        "tool_name": "create_task",
        "tool_args": {"ref": "{{ event.id }}:" + step_id},
    }


def _three_steps(**extra: Any) -> dict[str, Any]:
    return {**_definition(), "steps": [_tool("s1"), _tool("s2"), _tool("s3")], **extra}


def _parallel(**extra: Any) -> dict[str, Any]:
    return {
        **_definition(),
        "steps": [
            _tool("s1"),
            {"id": "fan", "type": "parallel", "parallel_steps": [_tool("a"), _tool("b")]},
        ],
        **extra,
    }


def _effects(engine: WorkflowEngine) -> list[str]:
    return [c.args[1]["ref"] for c in engine.runner.registry.execute.await_args_list]


@pytest.fixture
def waits(clock):
    """asyncio.sleep that lets the limiter's minute pass instead of waiting it."""
    seen: list[float] = []

    async def wait(seconds: float) -> None:
        seen.append(seconds)
        clock.now += seconds

    with patch("robothor.engine.workflow.asyncio.sleep", side_effect=wait):
        yield seen


# ── the YAML field ───────────────────────────────────────────────────────


class TestRateLimitField:
    def test_defaults_to_the_fleet_default(self):
        assert parse_workflow(_definition()).rate_limit_per_minute == 0

    def test_parsed_from_yaml(self):
        wf = parse_workflow(_definition(rate_limit_per_minute=120))
        assert wf.rate_limit_per_minute == 120

    @pytest.mark.parametrize("bad", [0, -1, "fast", 1.5, True])
    def test_rejects_anything_but_a_positive_integer(self, bad):
        with pytest.raises(ValueError, match="rate_limit_per_minute"):
            parse_workflow(_definition(rate_limit_per_minute=bad))

    @pytest.mark.asyncio
    async def test_a_higher_limit_lets_more_calls_through(self, engine_config, clock, no_dedup):
        # 35 calls in one minute: over the fleet default of 30, under 40.
        engine = _engine(engine_config, _definition(rate_limit_per_minute=40))
        runs = [await _run(engine) for _ in range(35)]
        assert {r.status for r in runs} == {RunStatus.COMPLETED}

    @pytest.mark.asyncio
    async def test_a_lower_limit_is_enforced(self, engine_config, clock, no_dedup):
        engine = _engine(engine_config, _definition(rate_limit_per_minute=2))
        runs = [await _run(engine, requeue=True) for _ in range(3)]
        assert [r.status for r in runs[:2]] == [RunStatus.COMPLETED] * 2
        assert runs[2].rate_limited is True
        assert engine.runner.registry.execute.await_count == 2
        assert engine.runner.registry.execute.await_args.args[0] == "create_task"

    @pytest.mark.asyncio
    async def test_each_workflow_has_its_own_limit(self, engine_config, clock, no_dedup):
        engine = _engine(engine_config, _definition(rate_limit_per_minute=1))
        other = parse_workflow({**_definition(), "id": "other-flow"})
        engine._workflows[other.id] = other
        assert (await _run(engine)).status == RunStatus.COMPLETED
        # order-flow is at its ceiling; other-flow is on the fleet default.
        assert (await _run(engine, requeue=True)).rate_limited is True
        run = await engine.execute(
            "other-flow", trigger_type="hook", user_id="service:workflow:other-flow"
        )
        assert run.status == RunStatus.COMPLETED


# ── a blocked step waits in place ────────────────────────────────────────


class TestWaitInPlace:
    @pytest.mark.asyncio
    async def test_a_later_step_waits_and_earlier_steps_run_once(
        self, engine_config, clock, no_dedup, waits
    ):
        # Limit 2, three writes: s3 is blocked after s1 and s2 acted. Even for
        # a hook run (whose caller requeues), it must wait, never replay.
        engine = _engine(engine_config, _three_steps(rate_limit_per_minute=2))
        run = await _run(engine, requeue=True)
        assert run.status == RunStatus.COMPLETED
        assert _effects(engine) == ["evt:s1", "evt:s2", "evt:s3"]
        assert not run.rate_limited

    @pytest.mark.asyncio
    async def test_the_wait_backs_off_from_5_to_30_seconds(
        self, engine_config, clock, no_dedup, waits
    ):
        engine = _engine(engine_config, _three_steps(rate_limit_per_minute=2))
        await _run(engine)
        assert waits[:4] == [5, 10, 20, 30]
        assert set(waits[4:]) <= {30}

    @pytest.mark.asyncio
    async def test_a_blocked_step_past_the_deadline_fails_visibly(
        self, engine_config, clock, no_dedup
    ):
        # The limiter never frees (the clock is frozen): the workflow deadline
        # ends the wait and the run says which step it was waiting in.
        import asyncio

        real_sleep = asyncio.sleep

        async def tick(seconds: float) -> None:
            await real_sleep(0.01)

        engine = _engine(engine_config, _three_steps(rate_limit_per_minute=2, timeout_seconds=1))
        with patch("robothor.engine.workflow.asyncio.sleep", side_effect=tick):
            run = await _run(engine)
        assert run.status == RunStatus.TIMEOUT
        assert "s3" in (run.error_message or "")
        assert _effects(engine) == ["evt:s1", "evt:s2"]

    @pytest.mark.asyncio
    async def test_a_run_nothing_requeues_waits_at_its_first_step(
        self, engine_config, clock, no_dedup, waits
    ):
        # A cron (or agent, or manual) run has no requeue: it waits too.
        engine = _engine(engine_config, _definition(rate_limit_per_minute=1))
        await _run(engine, trigger="cron")
        run = await _run(engine, trigger="cron")
        assert run.status == RunStatus.COMPLETED
        assert waits

    @pytest.mark.asyncio
    async def test_a_resumed_run_waits_and_never_defers(
        self, engine_config, clock, no_dedup, waits
    ):
        # The approval driver resumes a run; nothing requeues it.
        from robothor.engine.models import WorkflowRun

        engine = _engine(engine_config, _definition(rate_limit_per_minute=1))
        await _run(engine)
        suspended = WorkflowRun(
            workflow_id="order-flow",
            trigger_type="hook",
            status=RunStatus.AWAITING_APPROVAL,
            context={"steps": {}, "event": {"id": "evt"}},
        )
        with patch.object(engine, "_load_suspended_run", return_value=(suspended, "claim")):
            run = await engine.resume_run(suspended.id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED
        assert not run.rate_limited

    @pytest.mark.asyncio
    async def test_a_blocked_parallel_branch_waits(self, engine_config, clock, no_dedup, waits):
        # Limit 2: s1 and one branch run, the other branch waits; nothing repeats.
        engine = _engine(engine_config, _parallel(rate_limit_per_minute=2))
        run = await _run(engine, requeue=True)
        assert run.status == RunStatus.COMPLETED
        assert sorted(_effects(engine)) == ["evt:a", "evt:b", "evt:s1"]


# ── the first step defers instead ────────────────────────────────────────


class TestFirstStepDefers:
    @pytest.mark.asyncio
    async def test_a_hook_run_blocked_at_its_first_step_is_deferred(
        self, engine_config, clock, no_dedup, waits
    ):
        # on_failure: skip must not swallow it: the step did nothing.
        engine = _engine(
            engine_config, _definition(rate_limit_per_minute=1, step={"on_failure": "skip"})
        )
        await _run(engine, requeue=True)
        run = await _run(engine, requeue=True)
        assert run.status == RunStatus.SKIPPED
        assert run.rate_limited is True
        assert "rate limit" in (run.error_message or "").lower()
        assert not waits  # it gives the worker back rather than holding it

    @pytest.mark.asyncio
    async def test_a_parallel_first_step_waits_not_defers(
        self, engine_config, clock, no_dedup, waits
    ):
        # A sibling branch may already have acted, so a branch never defers.
        data = {**_parallel(rate_limit_per_minute=1)}
        data["steps"] = data["steps"][1:]
        engine = _engine(engine_config, data)
        run = await _run(engine, requeue=True)
        assert run.status == RunStatus.COMPLETED
        assert sorted(_effects(engine)) == ["evt:a", "evt:b"]


# ── a burst of hook events ───────────────────────────────────────────────


async def _burst(
    engine_config: Any, clock: _Clock, data: dict[str, Any], events: int
) -> tuple[Any, list[Any]]:
    engine = _engine(engine_config, data)
    wf = engine.get_workflow("order-flow")
    hooks = EventHooks(engine_config, MagicMock())
    hooks.workflow_engine = engine

    runs: list[Any] = []
    real_execute = engine.execute

    async def execute(*args: Any, **kwargs: Any) -> Any:
        run = await real_execute(*args, **kwargs)
        runs.append(run)
        return run

    engine.execute = execute  # type: ignore[method-assign]
    queue: deque[dict[str, Any]] = deque(
        {"id": f"evt-{i}", "type": EVENT, "payload": "{}"} for i in range(events)
    )

    async def requeue(redis: Any, stream: str, data: dict[str, Any], delay: int = 0) -> None:
        assert stream == STREAM
        clock.now += delay
        queue.append(data)

    hooks._delayed_requeue = requeue  # type: ignore[method-assign]
    redis = AsyncMock()
    for _ in range(events * MAX_BUSY_REQUEUES):
        if not queue:
            break
        await hooks._run_workflow(wf, "platform", EVENT, queue.popleft(), redis, STREAM)
    assert not queue
    redis.xadd.assert_not_called()  # nothing dead-lettered
    return engine, runs


class TestBurst:
    @pytest.mark.asyncio
    async def test_a_burst_slows_down_instead_of_failing(
        self, engine_config, clock, no_dedup, waits
    ):
        engine, runs = await _burst(engine_config, clock, _definition(rate_limit_per_minute=2), 6)
        assert len([r for r in runs if r.status == RunStatus.COMPLETED]) == 6
        assert not [r for r in runs if r.status == RunStatus.FAILED]
        assert engine.runner.registry.execute.await_count == 6
        assert clock.now - 1000.0 >= 120  # six calls at two a minute

    @pytest.mark.asyncio
    async def test_a_burst_of_multi_step_runs_acts_exactly_once(
        self, engine_config, clock, no_dedup, waits
    ):
        from collections import Counter

        engine, runs = await _burst(engine_config, clock, _three_steps(rate_limit_per_minute=2), 5)
        assert len([r for r in runs if r.status == RunStatus.COMPLETED]) == 5
        assert not [r for r in runs if r.status == RunStatus.FAILED]
        counts = Counter(_effects(engine))
        expected = {f"evt-{i}:{s}" for i in range(5) for s in ("s1", "s2", "s3")}
        assert set(counts) == expected
        assert set(counts.values()) == {1}
        # A deferred run never acted: every skipped attempt stopped at s1.
        for run in runs:
            if run.status == RunStatus.SKIPPED:
                assert [r.step_id for r in run.step_results] == ["s1"]
