"""Simple interactive actions are bounded from admission, not after setup."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest, action_policy
from robothor.engine.runtime.deadlines import RuntimeDeadlineError


@pytest.fixture
def simple():
    return RunRequest(
        ExecutionContext("tenant", "operator", "request"),
        "main",
        "Create the requested task",
        {
            "trigger_type": "webchat",
            "agent_config": SimpleNamespace(difficulty_class="simple", is_benchmark=False),
        },
    )


def test_a_bare_yes_after_an_old_calendar_draft_gets_no_special_treatment():
    """A bare "Go" used to be bound to the "Calendar operation: <id>" line in
    the previous assistant turn — from anyone in the chat. The binding is gone,
    so such a message is just a message."""
    request = RunRequest(
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
    assert action_policy.apply_action_deadline(request) is request


@pytest.mark.parametrize("trigger", ["webchat", "telegram"])
def test_explicit_simple_interactive_profile_is_bounded(simple, trigger):
    request = replace(simple, options={**simple.options, "trigger_type": trigger})
    bounded = action_policy.apply_action_deadline(request)
    assert bounded.context.deadline is not None
    assert 59 < (bounded.context.deadline - datetime.now(UTC)).total_seconds() <= 60.1
    assert request.context.deadline is None
    assert bounded.options == request.options


@pytest.mark.parametrize("seconds", [-1, 10, 120])
def test_the_deadline_never_extends_an_existing_one(simple, seconds):
    deadline = datetime.now(UTC) + timedelta(seconds=seconds)
    request = replace(simple, context=replace(simple.context, deadline=deadline))
    bounded = action_policy.apply_action_deadline(request)
    assert bounded.context.deadline <= deadline
    if seconds <= 60:
        assert bounded.context.deadline == deadline


@pytest.mark.parametrize(
    "changes",
    [
        {"trigger_type": "event"},
        {"trigger_type": "manual"},
        {"agent_config": SimpleNamespace(difficulty_class="complex")},
        {"agent_config": SimpleNamespace(difficulty_class="")},
        {"agent_config": SimpleNamespace(difficulty_class="simple", is_benchmark=True)},
        {"readonly_mode": True},
        {"deep_plan": True},
        {"spawn_context": SimpleNamespace(parent_run_id="parent")},
    ],
)
def test_simple_profile_deadline_does_not_change_other_modes(simple, changes):
    request = replace(simple, options={**simple.options, **changes})
    assert action_policy.apply_action_deadline(request) is request


def test_background_goal_and_resume_keep_existing_policy(simple):
    goal = replace(simple, context=replace(simple.context, goal_id="goal", attempt_id="attempt"))
    assert action_policy.apply_action_deadline(goal) is goal
    resumed = replace(simple, resume_from="existing-run")
    assert action_policy.apply_action_deadline(resumed) is resumed


async def test_simple_profile_deadline_covers_admission_and_preserves_shorter_limit(
    simple, monkeypatch
):
    monkeypatch.setattr(action_policy, "SIMPLE_ACTION_SECONDS", 0.02)
    execute = AsyncMock()

    async def stalled(event):
        await asyncio.Event().wait()

    with pytest.raises(RuntimeDeadlineError):
        await asyncio.wait_for(CurrentRuntime(execute).run(simple, stalled), 0.3)
    execute.assert_not_awaited()
    deadline = datetime.now(UTC) - timedelta(seconds=1)
    shorter = replace(simple, context=replace(simple.context, deadline=deadline))
    assert action_policy.apply_action_deadline(shorter).context.deadline == deadline


def test_delegated_simple_profile_retains_parent_policy(simple):
    request = replace(simple, context=replace(simple.context, parent_id="parent"))
    assert action_policy.apply_action_deadline(request) is request
