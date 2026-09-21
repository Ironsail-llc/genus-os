"""Existing classification restricts simple chat execution without expanding authority."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from robothor.engine.runtime import ExecutionContext, RunRequest
from robothor.engine.runtime import classified_deadline as policy
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.deadlines import RuntimeDeadlineError, enclosing_deadline_reason


def request(**options):
    return RunRequest(
        ExecutionContext("tenant", "operator", "request"),
        "main",
        "Create a task",
        {"trigger_type": "webchat", **options},
    )


def config(difficulty=""):
    return SimpleNamespace(difficulty_class=difficulty, is_benchmark=False)


PLAN = SimpleNamespace(success=True, difficulty="simple")
ROUTE = SimpleNamespace(difficulty="complex")


@pytest.mark.parametrize("override", ["", "simple", "moderate", "complex"])
def test_planner_classification_respects_manifest_override(override):
    started = datetime.now(UTC) - timedelta(seconds=20)
    with policy.admission(request(), started):
        deadline = policy._deadline(config(override), ROUTE, PLAN)
    assert deadline == (started + timedelta(seconds=60) if override in ("", "simple") else None)


@pytest.mark.parametrize(
    "variant", ["goal", "child", "resume", "readonly_mode", "deep_plan", "cron"]
)
def test_noninteractive_or_long_work_does_not_get_simple_deadline(variant):
    req = request()
    if variant == "goal":
        req = replace(req, context=replace(req.context, goal_id="goal", attempt_id="attempt"))
    elif variant == "child":
        req = replace(req, context=replace(req.context, parent_id="parent"))
    elif variant == "resume":
        req = replace(req, resume_from="run")
    else:
        req = request(**({"trigger_type": "cron"} if variant == "cron" else {variant: True}))
    with policy.admission(req):
        assert policy._deadline(config(), ROUTE, PLAN) is None


async def test_classification_cancels_stalled_execution_and_keeps_shorter_native_limit(monkeypatch):
    monkeypatch.setattr("robothor.engine.runtime.action_policy.SIMPLE_ACTION_SECONDS", 0.03)
    saved = Mock()
    monkeypatch.setattr(policy, "_persist", saved)
    req = request()
    token = active_context.set(req.context)
    try:
        with policy.admission(req), pytest.raises(TimeoutError) as error:
            async with asyncio.timeout(1) as window:
                await policy.apply(SimpleNamespace(run=object()), window, config(), ROUTE, PLAN)
                assert window.when() - asyncio.get_running_loop().time() < 0.04
                await asyncio.Event().wait()
        assert "Runtime deadline expired" in enclosing_deadline_reason(error.value)
        saved.assert_called_once()
        active_context.set(req.context)
        with policy.admission(req):
            async with asyncio.timeout(0.01) as window:
                before = window.when()
                await policy.apply(SimpleNamespace(run=object()), window, config(), ROUTE, PLAN)
                assert window.when() == before
    finally:
        active_context.reset(token)


async def test_late_classification_does_not_start_a_fresh_sixty_seconds(monkeypatch):
    saved = Mock()
    monkeypatch.setattr(policy, "_persist", saved)
    req = request()
    token = active_context.set(req.context)
    try:
        with policy.admission(req, datetime.now(UTC) - timedelta(seconds=61)):
            with pytest.raises(RuntimeDeadlineError):
                async with asyncio.timeout(None) as window:
                    await policy.apply(SimpleNamespace(run=object()), window, config(), ROUTE, PLAN)
        saved.assert_not_called()
    finally:
        active_context.reset(token)
