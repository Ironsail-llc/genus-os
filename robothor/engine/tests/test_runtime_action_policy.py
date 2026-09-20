"""Confirmed simple operations are bounded from admission, not after setup."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest, action_policy
from robothor.engine.runtime.deadlines import RuntimeDeadlineError


@pytest.fixture
def confirmed(monkeypatch):
    monkeypatch.setattr(
        "robothor.settings.get_settings",
        lambda: SimpleNamespace(engine=SimpleNamespace(calendar_operations_enabled=True)),
    )
    return RunRequest(
        ExecutionContext("tenant", "operator", "request"),
        "main",
        "Go",
        {
            "conversation_history": [
                {
                    "role": "assistant",
                    "content": "Calendar operation: 00000000-0000-0000-0000-000000000001",
                }
            ]
        },
    )


def test_confirmation_receives_total_sixty_second_deadline(confirmed):
    before = datetime.now(UTC)
    bounded = action_policy.apply_action_deadline(confirmed)
    assert 59 < (bounded.context.deadline - before).total_seconds() <= 60.1
    assert confirmed.context.deadline is None
    assert bounded.options == confirmed.options


@pytest.mark.parametrize("seconds", [-1, 10, 120])
def test_confirmation_never_extends_existing_deadline(confirmed, seconds):
    deadline = datetime.now(UTC) + timedelta(seconds=seconds)
    request = replace(confirmed, context=replace(confirmed.context, deadline=deadline))
    bounded = action_policy.apply_action_deadline(request)
    assert bounded.context.deadline <= deadline
    if seconds <= 60:
        assert bounded.context.deadline == deadline


@pytest.mark.parametrize(
    "options",
    [
        {"readonly_mode": True},
        {"deep_plan": True},
        {"spawn_context": SimpleNamespace(parent_run_id="parent")},
        {"agent_config": SimpleNamespace(is_benchmark=True)},
        {"conversation_history": []},
        {"conversation_history": [{"role": "user", "content": "Go"}]},
    ],
)
def test_unrelated_or_special_modes_keep_existing_policy(confirmed, options):
    request = replace(confirmed, options={**confirmed.options, **options})
    assert action_policy.apply_action_deadline(request) is request


@pytest.mark.parametrize("message", ["Do a long research task", "Yes, and do something else"])
def test_unrecognized_requests_keep_existing_policy(confirmed, message):
    request = replace(confirmed, message=message)
    assert action_policy.apply_action_deadline(request) is request


def test_disabled_feature_does_not_change_deadline(confirmed, monkeypatch):
    monkeypatch.setattr(
        "robothor.settings.get_settings",
        lambda: SimpleNamespace(engine=SimpleNamespace(calendar_operations_enabled=False)),
    )
    assert action_policy.apply_action_deadline(confirmed) is confirmed


async def test_automatically_assigned_deadline_includes_stalled_admission(confirmed, monkeypatch):
    monkeypatch.setattr(action_policy, "SIMPLE_ACTION_SECONDS", 0.02)
    execute = AsyncMock()

    async def stalled(event):
        await asyncio.Event().wait()

    with pytest.raises(RuntimeDeadlineError):
        await asyncio.wait_for(CurrentRuntime(execute).run(confirmed, stalled), 0.3)
    execute.assert_not_awaited()


def test_background_goal_and_resume_keep_existing_policy(confirmed):
    goal = replace(
        confirmed, context=replace(confirmed.context, goal_id="goal", attempt_id="attempt")
    )
    assert action_policy.apply_action_deadline(goal) is goal
    resumed = replace(confirmed, resume_from="existing-run")
    assert action_policy.apply_action_deadline(resumed) is resumed
