"""Concurrent hook workflows run in a lane of their own.

A ``concurrent: true`` hook workflow only hands each event on to something with
its own locking, so its runs are meant to overlap. Taking the background
lane's slot, which the fleet keeps to two, queued the third overlapping event
behind two long background runs and undid that. A non-concurrent workflow is
background work like any other and still waits for a background slot.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine import dedup, lanes
from robothor.engine.lanes import Lane
from robothor.engine.models import RunStatus
from robothor.engine.workflow import WorkflowEngine, parse_workflow

EVENT = "platform.message.created"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("ROBOTHOR_HA_DEDUP_ENABLED", raising=False)
    monkeypatch.delenv("ROBOTHOR_LANE_HOOKS_LIMIT", raising=False)
    monkeypatch.delenv("ROBOTHOR_MAX_CONCURRENT_BACKGROUND", raising=False)
    monkeypatch.setattr(lanes, "_LANES", None)
    dedup.clear()
    yield
    dedup.clear()
    monkeypatch.setattr(lanes, "_LANES", None)


def _definition(workflow_id: str, *, concurrent: bool) -> dict[str, Any]:
    return {
        "id": workflow_id,
        "triggers": [
            {"type": "hook", "stream": "platform", "event_type": EVENT, "concurrent": concurrent}
        ],
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


async def _run(engine: WorkflowEngine, workflow_id: str, event_id: str) -> Any:
    return await engine.execute(
        workflow_id,
        trigger_type="hook",
        initial_context={"event": {"id": event_id}},
        user_id=f"service:workflow:{workflow_id}",
        user_role="service",
    )


async def _hold_background(holding: asyncio.Event, release: asyncio.Event, count: list[int]):
    """A long background run: holds a background slot until released."""
    with lanes.detached():
        async with lanes.run_slot(Lane.BACKGROUND):
            count.append(1)
            if len(count) == 2:
                holding.set()
            await release.wait()


async def _saturate_background() -> tuple[list[asyncio.Task[None]], asyncio.Event]:
    holding, release, count = asyncio.Event(), asyncio.Event(), []
    holders = [asyncio.create_task(_hold_background(holding, release, count)) for _ in range(2)]
    await asyncio.wait_for(holding.wait(), timeout=2)
    return holders, release


@pytest.mark.asyncio
async def test_three_overlapping_hook_events_run_while_background_is_full(engine_config):
    holders, release = await _saturate_background()
    inside: list[str] = []
    all_inside = asyncio.Event()

    async def tool(name: str, args: dict[str, Any], **_: Any) -> dict[str, Any]:
        inside.append(args["ref"])
        if len(inside) == 3:
            all_inside.set()
        await asyncio.wait_for(all_inside.wait(), timeout=2)
        return {"ok": True}

    data = _definition("example-dispatch", concurrent=True)
    engine = _engine(engine_config, data, AsyncMock(side_effect=tool))
    try:
        runs = await asyncio.wait_for(
            asyncio.gather(*(_run(engine, "example-dispatch", f"evt-{i}") for i in range(3))),
            timeout=5,
        )
    finally:
        release.set()
        await asyncio.gather(*holders)

    assert [r.status for r in runs] == [RunStatus.COMPLETED] * 3
    assert sorted(inside) == ["evt-0", "evt-1", "evt-2"]


@pytest.mark.asyncio
async def test_a_non_concurrent_workflow_still_waits_for_a_background_slot(engine_config):
    holders, release = await _saturate_background()
    started = asyncio.Event()

    async def tool(name: str, args: dict[str, Any], **_: Any) -> dict[str, Any]:
        started.set()
        return {"ok": True}

    data = _definition("example-serial", concurrent=False)
    engine = _engine(engine_config, data, AsyncMock(side_effect=tool))
    run = asyncio.create_task(_run(engine, "example-serial", "evt-0"))
    try:
        await asyncio.sleep(0.3)
        assert not started.is_set()  # queued behind the two background runs
    finally:
        release.set()
        await asyncio.gather(*holders)
    assert (await asyncio.wait_for(run, timeout=2)).status == RunStatus.COMPLETED


def test_the_hooks_lane_has_its_own_bound_of_eight_by_default():
    assert Lane("hooks") is Lane.HOOKS
    assert lanes.get_lanes().snapshot()["hooks"]["limit"] == 8


def test_the_hooks_lane_bound_is_a_setting(monkeypatch):
    from robothor.settings import reset_settings

    monkeypatch.setenv("ROBOTHOR_LANE_HOOKS_LIMIT", "3")
    reset_settings()
    try:
        assert lanes.get_lanes().snapshot()["hooks"]["limit"] == 3
    finally:
        monkeypatch.delenv("ROBOTHOR_LANE_HOOKS_LIMIT")
        reset_settings()
