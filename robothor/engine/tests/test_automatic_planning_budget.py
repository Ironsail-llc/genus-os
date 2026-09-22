"""Optional planning cannot consume the ordinary interactive action window."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.runtime import ExecutionContext, RunRequest
from robothor.engine.runtime import automatic_planning as policy
from robothor.engine.runtime.classified_deadline import admission
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.deadlines import RuntimeDeadlineError, require_time


def request():
    return RunRequest(
        ExecutionContext("tenant", "operator", "request"),
        "main",
        "Work",
        {"trigger_type": "webchat"},
    )


def config(**changes):
    return SimpleNamespace(
        planning_enabled=False,
        planning_model="",
        difficulty_class="",
        is_benchmark=False,
        **changes,
    )


@pytest.mark.parametrize(
    "variant",
    ["chat", "explicit", "goal", "child", "resume", "cron", "approved", "planning_model"],
)
def test_catalogue_routing_changes_only_optional_ordinary_chat_planning(variant):
    from robothor.engine.run_lifecycle import RunLifecycleMixin

    req, cfg = request(), config()
    if variant == "explicit":
        cfg.planning_enabled = True
    elif variant == "planning_model":
        cfg.planning_model = "selected-planner"
    elif variant == "goal":
        req = replace(req, context=replace(req.context, goal_id="goal", attempt_id="attempt"))
    elif variant == "child":
        req = replace(req, context=replace(req.context, parent_id="parent"))
    elif variant == "resume":
        req = replace(req, resume_from="prior")
    elif variant == "cron":
        req = replace(req, options={"trigger_type": "cron"})
    elif variant == "approved":
        req = replace(req, options={**req.options, "trigger_detail": "plan-exec:chat"})
    token = active_context.set(req.context)
    try:
        with admission(req):
            route = RunLifecycleMixin._apply_routing(None, cfg, "Create one task.", 104)
            assert route.difficulty == ("moderate" if variant == "chat" else "complex")
            assert RunLifecycleMixin._should_plan(None, cfg, route) is (variant != "chat")
    finally:
        active_context.reset(token)


@pytest.mark.parametrize("suppress", [False, True])
async def test_expired_optional_plan_is_not_used_and_execution_can_continue(monkeypatch, suppress):
    monkeypatch.setattr(policy, "AUTOMATIC_PLAN_SECONDS", 0.01)
    req = request()
    token = active_context.set(req.context)
    finished = []

    async def generate():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress:
                raise
            with pytest.raises(RuntimeDeadlineError):
                require_time()
            return "Late plan must not be accepted"
        finally:
            finished.append(True)

    try:
        with admission(req):
            assert await policy.run(config(), generate) is None
            assert finished == [True]
            assert active_context.get() == req.context
            assert require_time() is None
    finally:
        active_context.reset(token)


@pytest.mark.parametrize(
    "variant",
    [
        "explicit",
        "planning_model",
        "difficulty",
        "goal",
        "child",
        "resume",
        "readonly",
        "deep",
        "cron",
        "benchmark",
        "approved_plan",
        "active_goal",
        "active_child",
    ],
)
async def test_explicit_planning_and_long_work_keep_their_policy(monkeypatch, variant):
    monkeypatch.setattr(policy, "AUTOMATIC_PLAN_SECONDS", 0.001)
    req, cfg = request(), config()
    if variant == "explicit":
        cfg.planning_enabled = True
    elif variant == "planning_model":
        cfg.planning_model = "selected-planner"
    elif variant == "difficulty":
        cfg.difficulty_class = "complex"
    elif variant == "goal":
        req = replace(req, context=replace(req.context, goal_id="goal", attempt_id="attempt"))
    elif variant == "child":
        req = replace(req, context=replace(req.context, parent_id="parent"))
    elif variant == "resume":
        req = replace(req, resume_from="old")
    elif variant == "readonly":
        req = replace(req, options={**req.options, "readonly_mode": True})
    elif variant == "deep":
        req = replace(req, options={**req.options, "deep_plan": True})
    elif variant == "cron":
        req = replace(req, options={"trigger_type": "cron"})
    elif variant == "benchmark":
        cfg.is_benchmark = True
    elif variant == "approved_plan":
        req = replace(req, options={**req.options, "trigger_detail": "plan-exec:session"})

    async def generate():
        await asyncio.sleep(0.01)
        return "Kept plan"

    context = req.context
    if variant == "active_goal":
        context = replace(context, goal_id="goal", attempt_id="attempt")
    elif variant == "active_child":
        context = replace(context, parent_id="parent")
    token = active_context.set(context)
    try:
        with admission(req):
            assert await policy.run(cfg, generate) == "Kept plan"
    finally:
        active_context.reset(token)


async def test_outer_deadline_does_not_become_optional_planning_success():
    req = request()
    req = replace(
        req, context=replace(req.context, deadline=datetime.now(UTC) - timedelta(seconds=1))
    )
    generate = AsyncMock()
    token = active_context.set(req.context)
    try:
        with admission(req), pytest.raises(RuntimeDeadlineError):
            await policy.run(config(), generate)
        generate.assert_not_called()
    finally:
        active_context.reset(token)


async def test_fresh_plan_and_unrelated_timeout_keep_their_meaning():
    req = request()
    token = active_context.set(req.context)
    try:
        with admission(req):
            assert await policy.run(config(), AsyncMock(return_value="Fresh")) == "Fresh"
            with pytest.raises(TimeoutError, match="Provider transport"):
                await policy.run(
                    config(), AsyncMock(side_effect=TimeoutError("Provider transport"))
                )
    finally:
        active_context.reset(token)
