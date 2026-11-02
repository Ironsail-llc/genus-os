"""A hook workflow whose trigger sets ``concurrent: true`` runs alongside itself.

By default one run of a workflow is in flight at a time: an event that lands
while it runs is skipped as "already running" (and requeued after a pause
when the trigger sets ``requeue_if_busy``). A dispatcher workflow that only
hands each event on to something with its own locking gains nothing from
that and loses seconds on every event that arrives close to another, so its
trigger can opt out of the run lock.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine import dedup
from robothor.engine.hooks import STREAM_PREFIX, EventHooks
from robothor.engine.models import RunStatus
from robothor.engine.workflow import WorkflowEngine, parse_workflow

STREAM = f"{STREAM_PREFIX}platform"
EVENT = "platform.message.created"


@pytest.fixture(autouse=True)
def _clean_locks(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_HA_DEDUP_ENABLED", raising=False)
    dedup.clear()
    yield
    dedup.clear()


def _definition(**trigger: Any) -> dict[str, Any]:
    return {
        "id": "example-dispatch",
        "triggers": [{"type": "hook", "stream": "platform", "event_type": EVENT, **trigger}],
        "steps": [
            {
                "id": "dispatch",
                "type": "tool",
                "tool_name": "create_task",
                "tool_args": {"ref": "{{ event.id }}"},
            }
        ],
    }


def _engine(engine_config: Any, data: dict[str, Any], tool: Any) -> WorkflowEngine:
    runner = MagicMock()
    runner.registry.execute = tool
    engine = WorkflowEngine(engine_config, runner=runner)
    wf = parse_workflow(data)
    engine._workflows[wf.id] = wf
    engine._persist_run_start = MagicMock()  # type: ignore[method-assign]
    engine._persist_run_end = MagicMock()  # type: ignore[method-assign]
    engine._persist_step = MagicMock()  # type: ignore[method-assign]
    engine._persist_step_start = MagicMock()  # type: ignore[method-assign]
    return engine


def _hooks(engine_config: Any, engine: WorkflowEngine) -> tuple[EventHooks, list[Any]]:
    """Hooks wired to the real engine, recording every run ``execute`` returns."""
    hooks = EventHooks(engine_config, MagicMock())
    hooks.workflow_engine = engine
    hooks._delayed_requeue = AsyncMock()  # type: ignore[method-assign]
    runs: list[Any] = []
    real_execute = engine.execute

    async def execute(*args: Any, **kwargs: Any) -> Any:
        runs.append(await real_execute(*args, **kwargs))
        return runs[-1]

    engine.execute = execute  # type: ignore[method-assign]
    return hooks, runs


def _event(event_id: str) -> dict[str, Any]:
    return {"id": event_id, "type": EVENT, "payload": "{}"}


async def _deliver(hooks: EventHooks, event_id: str) -> None:
    wf = hooks.workflow_engine.get_workflow("example-dispatch")
    await hooks._run_workflow(wf, "platform", EVENT, _event(event_id), AsyncMock(), STREAM)


class TestConcurrentWorkflow:
    @pytest.mark.asyncio
    async def test_two_events_run_at_the_same_time(self, engine_config):
        # The tool step returns only once both runs are inside it: if the
        # second run were held back by the first, the barrier would never fill.
        inside: list[str] = []
        both_inside = asyncio.Event()

        async def tool(name: str, args: dict[str, Any], **_: Any) -> dict[str, Any]:
            inside.append(args["ref"])
            if len(inside) == 2:
                both_inside.set()
            await asyncio.wait_for(both_inside.wait(), timeout=2)
            return {"ok": True}

        engine = _engine(engine_config, _definition(concurrent=True), AsyncMock(side_effect=tool))
        hooks, runs = _hooks(engine_config, engine)

        await asyncio.gather(_deliver(hooks, "evt-1"), _deliver(hooks, "evt-2"))

        assert [r.status for r in runs] == [RunStatus.COMPLETED, RunStatus.COMPLETED]
        assert sorted(inside) == ["evt-1", "evt-2"]
        assert engine._persist_run_start.call_count == 2  # both runs recorded
        hooks._delayed_requeue.assert_not_awaited()  # neither skipped nor requeued

    @pytest.mark.asyncio
    async def test_a_concurrent_run_ignores_a_held_run_lock(self, engine_config):
        engine = _engine(
            engine_config, _definition(concurrent=True), AsyncMock(return_value={"ok": True})
        )
        assert await dedup.try_acquire("workflow:example-dispatch")
        run = await engine.execute(
            "example-dispatch",
            trigger_type="hook",
            initial_context={"event": {"id": "evt-1"}},
            user_id="service:workflow:example-dispatch",
            user_role="service",
        )
        assert run.status == RunStatus.COMPLETED
        # It neither took nor released the lock someone else holds.
        assert dedup.is_running("workflow:example-dispatch")

    @pytest.mark.asyncio
    async def test_the_rate_limit_still_defers_a_concurrent_run(self, engine_config):
        data = {**_definition(concurrent=True), "rate_limit_per_minute": 1}
        engine = _engine(engine_config, data, AsyncMock(return_value={"ok": True}))
        hooks, runs = _hooks(engine_config, engine)

        await _deliver(hooks, "evt-1")
        await _deliver(hooks, "evt-2")

        assert runs[0].status == RunStatus.COMPLETED
        assert runs[1].status == RunStatus.SKIPPED
        assert runs[1].rate_limited is True
        hooks._delayed_requeue.assert_awaited_once()


class TestNonConcurrentWorkflow:
    @pytest.mark.asyncio
    async def test_a_duplicate_is_still_skipped(self, engine_config):
        first_inside = asyncio.Event()
        finish = asyncio.Event()

        async def tool(name: str, args: dict[str, Any], **_: Any) -> dict[str, Any]:
            first_inside.set()
            await asyncio.wait_for(finish.wait(), timeout=2)
            return {"ok": True}

        engine = _engine(engine_config, _definition(), AsyncMock(side_effect=tool))
        hooks, runs = _hooks(engine_config, engine)

        first = asyncio.create_task(_deliver(hooks, "evt-1"))
        await asyncio.wait_for(first_inside.wait(), timeout=2)
        await _deliver(hooks, "evt-2")
        finish.set()
        await first

        skipped, completed = runs
        assert skipped.status == RunStatus.SKIPPED
        assert "already running" in (skipped.error_message or "")
        assert completed.status == RunStatus.COMPLETED
        assert engine._persist_run_start.call_count == 1


class TestConcurrentField:
    def test_defaults_to_false(self):
        wf = parse_workflow(_definition())
        assert wf.triggers[0].concurrent is False
        assert wf.concurrent is False

    def test_parsed_from_yaml(self):
        wf = parse_workflow(_definition(concurrent=True))
        assert wf.triggers[0].concurrent is True
        assert wf.concurrent is True

    def test_concurrent_with_requeue_if_busy_is_rejected(self):
        with pytest.raises(ValueError, match="concurrent.*requeue_if_busy"):
            parse_workflow(_definition(concurrent=True, requeue_if_busy=True))

    def test_concurrent_with_requeue_if_busy_on_another_trigger_is_rejected(self):
        data = _definition(concurrent=True)
        data["triggers"].append(
            {"type": "hook", "stream": "platform", "event_type": "other", "requeue_if_busy": True}
        )
        with pytest.raises(ValueError, match="concurrent.*requeue_if_busy"):
            parse_workflow(data)

    def test_a_workflow_that_fails_validation_is_not_loaded(self, engine_config, tmp_path):
        import yaml

        (tmp_path / "example-dispatch.yaml").write_text(
            yaml.safe_dump(_definition(concurrent=True, requeue_if_busy=True))
        )
        engine = WorkflowEngine(engine_config, runner=MagicMock())
        assert engine.load_workflows(tmp_path) == 0
        assert engine.get_workflow("example-dispatch") is None
