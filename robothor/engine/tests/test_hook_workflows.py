"""Hook-triggered workflows: the event reaches the run, and a busy workflow can requeue."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.hooks import (
    BUSY_REQUEUE_DELAY_SECONDS,
    MAX_BUSY_REQUEUES,
    REPLAY_MAX_AGE_SECONDS,
    REPLAY_MAX_DELIVERIES,
    STREAM_PREFIX,
    EventHooks,
)
from robothor.engine.models import RunStatus, WorkflowTriggerDef
from robothor.engine.workflow import _render_template, parse_workflow

STREAM = f"{STREAM_PREFIX}platform"
EVENT = "platform.order.created"


def _wf(requeue: bool = False) -> MagicMock:
    wf = MagicMock()
    wf.id = "order-flow"
    wf.triggers = [
        WorkflowTriggerDef(
            type="hook", stream="platform", event_type=EVENT, requeue_if_busy=requeue
        )
    ]
    return wf


def _run(status: RunStatus) -> MagicMock:
    run = MagicMock()
    run.status = status
    return run


def _hooks(engine_config, wf, status=RunStatus.COMPLETED, agents=()):
    hooks = EventHooks(engine_config, MagicMock())
    hooks._event_triggers = {
        "platform": [{"event_type": EVENT, "agent_id": a, "message": "m"} for a in agents]
    }
    hooks._prefixed_to_bare = {STREAM: "platform"}
    hooks.runner.execute = AsyncMock(return_value=MagicMock())
    hooks.workflow_engine = MagicMock()
    hooks.workflow_engine.get_workflows_for_event.return_value = [wf]
    hooks.workflow_engine.execute = AsyncMock(return_value=_run(status))
    return hooks


async def _fire(hooks, data, redis=None):
    """Handle one event, then run every task it spawned (sleeps skipped)."""
    redis = redis or AsyncMock()
    registry = MagicMock()
    with (
        patch("robothor.engine.hooks.try_acquire", return_value=True),
        patch("robothor.engine.hooks.release", new_callable=AsyncMock),
        patch("robothor.engine.hooks.deliver", new_callable=AsyncMock),
        patch("robothor.engine.config.load_agent_config", return_value=None),
        patch("robothor.engine.hooks.get_task_registry", return_value=registry),
    ):
        await hooks._handle_event(STREAM, b"1-0", data, redis, "engine")
        spawned = [c.args[0] for c in registry.spawn.call_args_list]
        with patch("robothor.engine.hooks.asyncio.sleep", new_callable=AsyncMock) as sleep:
            for coro in spawned:
                await coro
    return redis, registry, sleep


class TestEventReachesTheWorkflow:
    @pytest.mark.asyncio
    async def test_event_passed_as_initial_context(self, engine_config):
        hooks = _hooks(engine_config, _wf())
        data = {
            b"id": b"evt-1",
            b"type": EVENT.encode(),
            b"delivery_id": b"dlv-1",
            b"payload": b'{"data": {"ticket": {"id": "t-42"}}}',
        }
        await _fire(hooks, data)
        kwargs = hooks.workflow_engine.execute.call_args.kwargs
        assert kwargs["initial_context"] == {
            "event": {
                "id": "evt-1",
                "type": EVENT,
                "stream": "platform",
                "delivery_id": "dlv-1",
                "payload": {"data": {"ticket": {"id": "t-42"}}},
            }
        }
        assert kwargs["trigger_type"] == "hook"

    @pytest.mark.asyncio
    async def test_non_json_payload_kept_as_text(self, engine_config):
        hooks = _hooks(engine_config, _wf())
        await _fire(hooks, {b"type": EVENT.encode(), b"payload": b"not json"})
        event = hooks.workflow_engine.execute.call_args.kwargs["initial_context"]["event"]
        assert event["payload"] == "not json"
        assert event["delivery_id"] == ""

    def test_a_missing_optional_field_renders_empty_without_a_warning(self, caplog):
        context = {"steps": {}, "event": {"payload": {"data": {"ticket": {"id": "t-42"}}}}}
        with caplog.at_level("WARNING", logger="robothor.engine.workflow"):
            rendered = _render_template("{{ event.payload.data.message.senderType }}", context)
        assert rendered == ""
        assert not [r for r in caplog.records if "Template eval failed" in r.getMessage()]

    def test_step_args_can_template_the_event(self):
        context = {"steps": {}, "event": {"payload": {"data": {"ticket": {"id": "t-42"}}}}}
        assert _render_template("{{ event.payload.data.ticket.id }}", context) == "t-42"


class TestBusyWorkflowRequeue:
    @pytest.mark.asyncio
    async def test_busy_workflow_dropped_by_default(self, engine_config):
        hooks = _hooks(engine_config, _wf(), status=RunStatus.SKIPPED)
        redis, _registry, _sleep = await _fire(hooks, {b"type": EVENT.encode()})
        redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_busy_workflow_requeued_for_that_workflow_only(self, engine_config):
        hooks = _hooks(engine_config, _wf(requeue=True), status=RunStatus.SKIPPED)
        redis, _registry, sleep = await _fire(hooks, {b"type": EVENT.encode(), b"payload": b"{}"})
        sleep.assert_awaited_once_with(BUSY_REQUEUE_DELAY_SECONDS)
        stream, copy = redis.xadd.call_args.args[:2]
        assert stream == STREAM
        assert copy["_retry_workflows"] == "order-flow"
        assert copy["_busy_requeue_count"] == "1"
        assert "_retry_agents" not in copy
        assert copy["type"] == EVENT

    @pytest.mark.asyncio
    async def test_completed_workflow_not_requeued(self, engine_config):
        hooks = _hooks(engine_config, _wf(requeue=True), status=RunStatus.COMPLETED)
        redis, _registry, _sleep = await _fire(hooks, {b"type": EVENT.encode()})
        redis.xadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_exhausted_goes_to_dlq(self, engine_config):
        hooks = _hooks(engine_config, _wf(requeue=True), status=RunStatus.SKIPPED)
        data = {b"type": EVENT.encode(), b"_busy_requeue_count": str(MAX_BUSY_REQUEUES).encode()}
        redis, _registry, sleep = await _fire(hooks, data)
        sleep.assert_not_awaited()
        assert redis.xadd.call_args.args[0] == f"{STREAM}:dlq"

    @pytest.mark.asyncio
    async def test_workflow_copy_runs_that_workflow_and_no_agents(self, engine_config):
        hooks = _hooks(engine_config, _wf(requeue=True), agents=["audit"])
        data = {b"type": EVENT.encode(), b"_retry_workflows": b"order-flow"}
        await _fire(hooks, data)
        hooks.runner.execute.assert_not_called()
        hooks.workflow_engine.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_workflow_copy_skips_other_workflows(self, engine_config):
        hooks = _hooks(engine_config, _wf(requeue=True))
        data = {b"type": EVENT.encode(), b"_retry_workflows": b"another-flow"}
        await _fire(hooks, data)
        hooks.workflow_engine.execute.assert_not_called()


class TestTriggerParsing:
    def test_requeue_if_busy_parsed_from_yaml(self):
        wf = parse_workflow(
            {
                "id": "order-flow",
                "triggers": [
                    {
                        "type": "hook",
                        "stream": "platform",
                        "event_type": EVENT,
                        "requeue_if_busy": True,
                    },
                    {"type": "cron", "cron": "0 3 * * *"},
                ],
                "steps": [{"id": "noop", "type": "noop"}],
            }
        )
        assert [t.requeue_if_busy for t in wf.triggers] == [True, False]


class TestConsumerLoopNotBlocked:
    """A long agent run must not hold up other agents, workflows or streams."""

    @staticmethod
    def _hooks(engine_config, agents):
        hooks = EventHooks(engine_config, MagicMock())
        hooks._event_triggers = {
            "platform": [{"event_type": EVENT, "agent_id": a, "message": "m"} for a in agents]
        }
        hooks._prefixed_to_bare = {STREAM: "platform"}
        return hooks

    @staticmethod
    def _patches():
        return (
            patch("robothor.engine.hooks.try_acquire", new_callable=AsyncMock, return_value=True),
            patch("robothor.engine.hooks.release", new_callable=AsyncMock),
            patch("robothor.engine.hooks.deliver", new_callable=AsyncMock),
            patch("robothor.engine.config.load_agent_config", return_value=None),
        )

    @pytest.mark.asyncio
    async def test_dispatch_returns_before_the_agent_run_finishes(self, engine_config):
        import asyncio

        gate = asyncio.Event()
        hooks = self._hooks(engine_config, ["slow-agent"])

        async def execute(**kwargs):
            await gate.wait()
            return _run(RunStatus.COMPLETED)

        hooks.runner.execute = AsyncMock(side_effect=execute)
        redis = AsyncMock()
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            await asyncio.wait_for(
                hooks._dispatch(STREAM, b"1-0", {b"type": EVENT.encode()}, redis, "engine"), 1
            )
            redis.xack.assert_not_awaited()  # still in flight, not lost
            gate.set()
            await hooks.drain()
        redis.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_other_agent_runs_while_one_is_blocked(self, engine_config):
        import asyncio

        gate = asyncio.Event()
        ran: list[str] = []
        hooks = EventHooks(engine_config, MagicMock())
        hooks._event_triggers = {
            "platform": [{"event_type": EVENT, "agent_id": "slow-agent", "message": "m"}],
            "other": [{"event_type": "other.ping", "agent_id": "quick-agent", "message": "m"}],
        }
        hooks._prefixed_to_bare = {STREAM: "platform", f"{STREAM_PREFIX}other": "other"}

        async def execute(**kwargs):
            if kwargs["agent_id"] == "slow-agent":
                await gate.wait()
            ran.append(kwargs["agent_id"])
            return _run(RunStatus.COMPLETED)

        hooks.runner.execute = AsyncMock(side_effect=execute)
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            await hooks._dispatch(STREAM, b"1-0", {b"type": EVENT.encode()}, AsyncMock(), "g")
            await hooks._dispatch(
                f"{STREAM_PREFIX}other", b"2-0", {b"type": b"other.ping"}, AsyncMock(), "g"
            )
            for _ in range(50):
                if ran:
                    break
                await asyncio.sleep(0.01)
            assert ran == ["quick-agent"]
            gate.set()
            await hooks.drain()
        assert ran == ["quick-agent", "slow-agent"]

    @pytest.mark.asyncio
    async def test_same_agent_events_wait_in_order_instead_of_dropping(self, engine_config):
        import asyncio

        gate = asyncio.Event()
        seen: list[str] = []
        hooks = self._hooks(engine_config, ["order-desk"])

        async def execute(**kwargs):
            seen.append(kwargs["message"])
            if len(seen) == 1:
                await gate.wait()
            return _run(RunStatus.COMPLETED)

        hooks.runner.execute = AsyncMock(side_effect=execute)
        hooks._event_triggers["platform"][0]["include_payload"] = True
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            for i in range(3):
                data = {b"type": EVENT.encode(), b"payload": f'{{"n": {i}}}'.encode()}
                await hooks._dispatch(STREAM, f"{i}-0".encode(), data, AsyncMock(), "g")
            await asyncio.sleep(0.05)
            assert len(seen) == 1  # the others wait their turn
            gate.set()
            await hooks.drain()
        assert [f'"n":{i}' in m for i, m in enumerate(seen)] == [True, True, True]

    @pytest.mark.asyncio
    async def test_workflows_dispatched_before_the_events_agents_run(self, engine_config):
        import asyncio

        gate = asyncio.Event()
        hooks = self._hooks(engine_config, ["slow-agent"])

        async def execute(**kwargs):
            await gate.wait()
            return _run(RunStatus.COMPLETED)

        hooks.runner.execute = AsyncMock(side_effect=execute)
        hooks.workflow_engine = MagicMock()
        hooks.workflow_engine.get_workflows_for_event.return_value = [_wf()]
        hooks.workflow_engine.execute = AsyncMock(return_value=_run(RunStatus.COMPLETED))
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            await hooks._dispatch(STREAM, b"1-0", {b"type": EVENT.encode()}, AsyncMock(), "g")
            for _ in range(50):
                if hooks.workflow_engine.execute.await_count:
                    break
                await asyncio.sleep(0.01)
            hooks.workflow_engine.execute.assert_awaited_once()
            gate.set()
            await hooks.drain()


class TestBusyRequeueBackoff:
    """A busy requeue backs off, so an event outlasts a long run instead of an hour's DLQ."""

    def test_delay_starts_at_the_base_and_grows_to_a_cap(self):
        from robothor.engine.hooks import busy_requeue_delay

        delays = [busy_requeue_delay(n) for n in range(MAX_BUSY_REQUEUES)]
        assert delays[0] == BUSY_REQUEUE_DELAY_SECONDS
        assert delays == sorted(delays)
        assert max(delays) == 60

    def test_budget_outlasts_a_long_scheduled_run(self):
        from robothor.engine.hooks import busy_requeue_delay

        total = sum(busy_requeue_delay(n) for n in range(MAX_BUSY_REQUEUES))
        assert 45 * 60 <= total <= 75 * 60

    @pytest.mark.asyncio
    async def test_requeue_waits_the_delay_for_its_count(self, engine_config):
        from robothor.engine.hooks import busy_requeue_delay

        hooks = _hooks(engine_config, _wf(requeue=True), status=RunStatus.SKIPPED)
        data = {b"type": EVENT.encode(), b"_busy_requeue_count": b"35"}
        _redis, _registry, sleep = await _fire(hooks, data)
        sleep.assert_awaited_once_with(busy_requeue_delay(35))


class TestDurabilityAndLimits:
    """Events in flight survive a restart; hook agent runs respect the fleet's limit."""

    @staticmethod
    def _patches():
        return TestConsumerLoopNotBlocked._patches()

    @staticmethod
    def _pending_redis(entries, claimed):
        """Redis with ``entries`` (id, deliveries) pending and ``claimed`` id -> data on XCLAIM."""
        redis = AsyncMock()
        pending = [
            {"message_id": mid, "times_delivered": n, "consumer": b"engine-test"}
            for mid, n in entries
        ]
        redis.xpending_range = AsyncMock(side_effect=[pending, []])

        async def xclaim(stream, group, consumer, min_idle_time, message_ids):
            return [(m, claimed[m]) for m in message_ids if claimed.get(m)]

        redis.xclaim = AsyncMock(side_effect=xclaim)
        return redis

    @staticmethod
    def _id(seconds_ago: float) -> bytes:
        import time

        return f"{int((time.time() - seconds_ago) * 1000)}-0".encode()

    @pytest.mark.asyncio
    async def test_start_replays_a_recent_pending_entry(self, engine_config):
        hooks = EventHooks(engine_config, MagicMock())
        hooks._dispatch = AsyncMock()
        fresh = self._id(60)
        redis = self._pending_redis([(fresh, 1)], {fresh: {b"type": EVENT.encode()}})
        await hooks._replay_pending(redis, "engine", "engine-test", [STREAM])
        hooks._dispatch.assert_awaited_once()
        assert hooks._dispatch.call_args.args[:2] == (STREAM, fresh)
        assert redis.xpending_range.call_args_list[0].kwargs["consumername"] == "engine-test"

    @pytest.mark.asyncio
    async def test_a_stale_pending_entry_is_dead_lettered_not_replayed(self, engine_config):
        hooks = EventHooks(engine_config, MagicMock())
        hooks._dispatch = AsyncMock()
        old = self._id(REPLAY_MAX_AGE_SECONDS + 600)
        redis = self._pending_redis([(old, 1)], {old: {b"type": EVENT.encode()}})
        await hooks._replay_pending(redis, "engine", "engine-test", [STREAM])
        hooks._dispatch.assert_not_awaited()
        assert redis.xadd.call_args.args[0] == f"{STREAM}:dlq"
        redis.xack.assert_awaited_once_with(STREAM, "engine", old)

    @pytest.mark.asyncio
    async def test_an_entry_delivered_too_often_is_dead_lettered(self, engine_config):
        hooks = EventHooks(engine_config, MagicMock())
        hooks._dispatch = AsyncMock()
        poison = self._id(60)
        redis = self._pending_redis(
            [(poison, REPLAY_MAX_DELIVERIES)], {poison: {b"type": EVENT.encode()}}
        )
        await hooks._replay_pending(redis, "engine", "engine-test", [STREAM])
        hooks._dispatch.assert_not_awaited()
        assert redis.xadd.call_args.args[0] == f"{STREAM}:dlq"
        redis.xack.assert_awaited_once_with(STREAM, "engine", poison)

    @pytest.mark.asyncio
    async def test_a_trimmed_pending_entry_is_acknowledged(self, engine_config):
        hooks = EventHooks(engine_config, MagicMock())
        hooks._dispatch = AsyncMock()
        gone = self._id(60)
        redis = self._pending_redis([(gone, 1)], {})
        await hooks._replay_pending(redis, "engine", "engine-test", [STREAM])
        hooks._dispatch.assert_not_awaited()
        redis.xack.assert_awaited_once_with(STREAM, "engine", gone)

    @pytest.mark.asyncio
    async def test_no_replay_under_ha(self, engine_config, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_HA_LEADER_ENABLED", "true")
        hooks = EventHooks(engine_config, MagicMock())
        hooks._dispatch = AsyncMock()
        fresh = self._id(60)
        redis = self._pending_redis([(fresh, 1)], {fresh: {b"type": EVENT.encode()}})
        await hooks._replay_pending(redis, "engine", "engine-test", [STREAM])
        hooks._dispatch.assert_not_awaited()
        redis.xpending_range.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_waits_for_in_flight_events_then_leaves_the_rest_pending(
        self, engine_config
    ):
        import asyncio

        hooks = EventHooks(engine_config, MagicMock())
        finished = asyncio.Event()

        async def quick():
            await asyncio.sleep(0)
            finished.set()

        async def stuck():
            await asyncio.Event().wait()

        quick_task = asyncio.create_task(quick())
        stuck_task = asyncio.create_task(stuck())
        hooks._in_flight = {quick_task, stuck_task}
        await hooks._stop_in_flight(timeout=0.05)
        assert finished.is_set()
        assert stuck_task.cancelled()

    @pytest.mark.asyncio
    async def test_hook_agent_runs_capped_by_max_concurrent_agents(self, engine_config):
        import asyncio

        gate = asyncio.Event()
        started: list[str] = []
        agents = [f"agent-{i}" for i in range(4)]
        hooks = TestConsumerLoopNotBlocked._hooks(engine_config, agents)

        async def execute(**kwargs):
            started.append(kwargs["agent_id"])
            await gate.wait()
            return _run(RunStatus.COMPLETED)

        hooks.runner.execute = AsyncMock(side_effect=execute)
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            await hooks._dispatch(STREAM, b"1-0", {b"type": EVENT.encode()}, AsyncMock(), "g")
            await asyncio.sleep(0.05)
            assert len(started) == engine_config.max_concurrent_agents
            gate.set()
            await hooks.drain()
        assert sorted(started) == agents

    @pytest.mark.asyncio
    async def test_agents_on_one_event_do_not_wait_for_each_other(self, engine_config):
        import asyncio

        gate = asyncio.Event()
        ran: list[str] = []
        hooks = TestConsumerLoopNotBlocked._hooks(engine_config, ["slow-agent", "quick-agent"])

        async def execute(**kwargs):
            if kwargs["agent_id"] == "slow-agent":
                await gate.wait()
            ran.append(kwargs["agent_id"])
            return _run(RunStatus.COMPLETED)

        hooks.runner.execute = AsyncMock(side_effect=execute)
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            await hooks._dispatch(STREAM, b"1-0", {b"type": EVENT.encode()}, AsyncMock(), "g")
            await asyncio.sleep(0.05)
            assert ran == ["quick-agent"]
            gate.set()
            await hooks.drain()
        assert ran == ["quick-agent", "slow-agent"]


class TestAbsentFieldIsVisible:
    def test_an_absent_field_is_logged_at_debug(self, caplog):
        context = {"steps": {}, "event": {"payload": {"data": {}}}}
        with caplog.at_level("DEBUG", logger="robothor.engine.workflow"):
            assert _render_template("{{ event.payload.data.message.id }}", context) == ""
        assert any(r.levelname == "DEBUG" and "message" in r.getMessage() for r in caplog.records)
