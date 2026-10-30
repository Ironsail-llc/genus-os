"""A workflow's tool-call rate limit: its own ceiling, and a burst slows down.

Every workflow's tool steps share one per-minute guardrail keyed
``workflow:<id>``. It was fixed at the fleet default (30/min) with no way to
raise it, and a step it blocked failed the run outright, so a burst of hook
events lost every run past the ceiling. The limit is now set per workflow,
a blocked step honours its retries, and a hook run blocked with no retry
left is requeued on the busy-requeue budget instead of failing.
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


async def _run(engine: WorkflowEngine, trigger: str = "hook", *, requeue: bool = False) -> Any:
    kwargs: dict[str, Any] = {"requeue_on_rate_limit": True} if requeue else {}
    return await engine.execute(
        "order-flow",
        trigger_type=trigger,
        user_id="service:workflow:order-flow",
        user_role="service",
        **kwargs,
    )


def _parallel(**extra: Any) -> dict[str, Any]:
    branch = {"type": "tool", "tool_name": "create_task", "on_failure": "skip"}
    return {
        "id": "order-flow",
        "triggers": [{"type": "hook", "stream": "platform", "event_type": EVENT}],
        "steps": [
            {
                "id": "fan",
                "type": "parallel",
                "on_failure": "skip",
                "parallel_steps": [{"id": "a", **branch}, {"id": "b", **branch}],
            }
        ],
        **extra,
    }


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
        runs = [await _run(engine) for _ in range(3)]
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
        assert (await _run(engine)).rate_limited is True
        run = await engine.execute(
            "other-flow", trigger_type="hook", user_id="service:workflow:other-flow"
        )
        assert run.status == RunStatus.COMPLETED


# ── a blocked step ───────────────────────────────────────────────────────


class TestBlockedStep:
    @pytest.mark.asyncio
    async def test_a_blocked_step_retries_with_its_backoff(self, engine_config, clock, no_dedup):
        engine = _engine(
            engine_config, _definition(rate_limit_per_minute=1, step={"retry_count": 1})
        )
        assert (await _run(engine)).status == RunStatus.COMPLETED

        async def wait(seconds: float) -> None:
            clock.now += seconds

        with patch("robothor.engine.workflow.asyncio.sleep", side_effect=wait) as sleep:
            run = await _run(engine)
        assert run.status == RunStatus.COMPLETED
        sleep.assert_awaited_once_with(60)

    @pytest.mark.asyncio
    async def test_a_run_its_caller_requeues_is_deferred_not_failed(
        self, engine_config, clock, no_dedup
    ):
        # on_failure: skip must not swallow it either: the step did nothing.
        engine = _engine(
            engine_config, _definition(rate_limit_per_minute=1, step={"on_failure": "skip"})
        )
        await _run(engine)
        run = await _run(engine, requeue=True)
        assert run.status == RunStatus.SKIPPED
        assert run.rate_limited is True
        assert "rate limit" in (run.error_message or "").lower()

    @pytest.mark.asyncio
    async def test_a_hook_run_no_caller_requeues_fails(self, engine_config, clock, no_dedup):
        # Being hook-triggered is not enough: only a caller that requeues may defer.
        engine = _engine(engine_config, _definition(rate_limit_per_minute=1))
        await _run(engine)
        run = await _run(engine)
        assert run.status == RunStatus.FAILED
        assert run.rate_limited is True

    @pytest.mark.asyncio
    async def test_a_resumed_hook_run_fails_visibly(self, engine_config, clock, no_dedup):
        # The approval driver resumes a run; nothing requeues it, so a block
        # after the approval must fail, not read as a benign skip.
        from robothor.engine.models import WorkflowRun

        engine = _engine(engine_config, _definition(rate_limit_per_minute=1))
        await _run(engine)
        suspended = WorkflowRun(
            workflow_id="order-flow",
            trigger_type="hook",
            status=RunStatus.AWAITING_APPROVAL,
            context={"steps": {}},
        )
        with patch.object(engine, "_load_suspended_run", return_value=(suspended, "claim")):
            run = await engine.resume_run(suspended.id)
        assert run is not None
        assert run.status == RunStatus.FAILED
        assert run.rate_limited is True

    @pytest.mark.asyncio
    async def test_a_blocked_cron_run_still_fails(self, engine_config, clock, no_dedup):
        # Nothing requeues a cron run, so it must stay visible (and page).
        engine = _engine(engine_config, _definition(rate_limit_per_minute=1))
        await _run(engine, trigger="cron")
        with patch.object(engine, "_notify_run_failure") as notify:
            run = await _run(engine, trigger="cron")
        assert run.status == RunStatus.FAILED
        assert run.rate_limited is True
        notify.assert_called_once()


# ── a burst of hook events ───────────────────────────────────────────────


class TestBurst:
    @pytest.mark.asyncio
    async def test_a_burst_slows_down_instead_of_failing(self, engine_config, clock, no_dedup):
        engine = _engine(engine_config, _definition(rate_limit_per_minute=2))
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
            {"id": f"evt-{i}", "type": EVENT, "payload": "{}"} for i in range(6)
        )

        async def requeue(redis: Any, stream: str, data: dict[str, Any], delay: int = 0) -> None:
            assert stream == STREAM
            clock.now += delay
            queue.append(data)

        hooks._delayed_requeue = requeue  # type: ignore[method-assign]
        redis = AsyncMock()

        for _ in range(6 * MAX_BUSY_REQUEUES):
            if not queue:
                break
            envelope = queue.popleft()
            await hooks._run_workflow(wf, "platform", EVENT, envelope, redis, STREAM)

        assert not queue
        redis.xadd.assert_not_called()  # nothing dead-lettered
        completed = [r for r in runs if r.status == RunStatus.COMPLETED]
        assert len(completed) == 6
        assert not [r for r in runs if r.status == RunStatus.FAILED]
        assert engine.runner.registry.execute.await_count == 6
        assert clock.now - 1000.0 >= 120  # six calls at two a minute


# ── inside a parallel step ───────────────────────────────────────────────


class TestParallelBranch:
    @pytest.mark.asyncio
    async def test_a_blocked_branch_defers_the_run(self, engine_config, clock, no_dedup):
        # Limit 1, two branches: one is blocked. Neither the branch's nor the
        # parallel step's on_failure: skip may swallow it.
        engine = _engine(engine_config, _parallel(rate_limit_per_minute=1))
        run = await _run(engine, requeue=True)
        assert run.status == RunStatus.SKIPPED
        assert run.rate_limited is True
        blocked = [r for r in run.step_results if r.step_id in {"a", "b"} and r.rate_limited]
        assert len(blocked) == 1
        assert blocked[0].status.value == "failed"

    @pytest.mark.asyncio
    async def test_a_blocked_branch_fails_a_run_nothing_requeues(
        self, engine_config, clock, no_dedup
    ):
        engine = _engine(engine_config, _parallel(rate_limit_per_minute=1))
        run = await _run(engine)
        assert run.status == RunStatus.FAILED
        assert run.rate_limited is True
