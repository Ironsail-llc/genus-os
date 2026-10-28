"""Hook-triggered workflows: the event reaches the run, and a busy workflow can requeue."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.hooks import (
    BUSY_REQUEUE_DELAY_SECONDS,
    MAX_BUSY_REQUEUES,
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
            # A requeue spawned while running a workflow task
            for c in registry.spawn.call_args_list[len(spawned) :]:
                await c.args[0]
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
