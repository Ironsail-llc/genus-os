"""Continuation uses the original limit, never a new request-length allowance."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.runtime.contracts import StateEnvelope
from robothor.engine.runtime.deadlines import RuntimeDeadlineError
from robothor.engine.runtime.resume_deadline import restore

CTX = ExecutionContext("tenant", "operator", "request")


@pytest.mark.parametrize("value", ["bad", "2026-09-21T00:00:00", 13, {}])
def test_invalid_saved_deadline_denies_resumption(value):
    with pytest.raises(ValueError, match="saved runtime deadline"):
        restore(CTX, {"runtime_context": {"deadline": value}})


def test_deadline_only_tightens_and_legacy_remains_compatible():
    earlier = datetime.now(UTC) + timedelta(seconds=10)
    later = earlier + timedelta(seconds=10)
    saved = {"runtime_context": {"deadline": later.isoformat()}}
    assert restore(replace(CTX, deadline=earlier), saved).deadline == earlier
    assert restore(CTX, saved).deadline == later
    assert restore(CTX, {}) == CTX


def test_new_goal_attempt_retains_its_trusted_limit():
    now = datetime.now(UTC)
    ctx = replace(CTX, goal_id="goal", attempt_id="new", deadline=now + timedelta(seconds=10))
    saved = {
        "runtime_context": {"goal_id": "goal", "attempt_id": "old", "deadline": now.isoformat()}
    }
    assert restore(ctx, saved) == ctx
    saved["runtime_context"]["attempt_id"] = "new"
    assert restore(ctx, saved).deadline == now


@pytest.mark.parametrize("expired", [False, True])
async def test_restored_deadline_bounds_execution_after_checkpoint_load(monkeypatch, expired):
    deadline = datetime.now(UTC) + timedelta(seconds=-1 if expired else 0.08)
    monkeypatch.setattr(
        "robothor.engine.checkpoint.CheckpointManager.load_latest",
        lambda *a, **k: {"runtime_context": {"deadline": deadline.isoformat()}},
    )
    monkeypatch.setattr("robothor.engine.runtime.controls.stopped", lambda *a: False)
    entered = []

    async def execute(**kwargs):
        from robothor.engine.runtime.current import active_context

        assert active_context.get().deadline == deadline
        entered.append(True)
        await asyncio.Event().wait()

    action = AsyncMock(side_effect=execute)
    request = RunRequest(CTX, "main", "Continue", resume_from="saved", checkpoint=StateEnvelope())
    async with asyncio.timeout(1):
        with pytest.raises(RuntimeDeadlineError):
            await CurrentRuntime(action).run(request)
    assert entered == ([] if expired else [True])
