"""The confirmation binding must bind ONE draft, ONCE, for ITS creator.

Round-1 review probe. A bare "yes" was permanently hijacked into a calendar
replay with no model call: the terminal reply carried the operation marker, so
the binding re-armed itself every turn and never cleared.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from robothor.engine.calendar_operations import confirmation_id
from robothor.engine.routine_request import (
    TOOL,
    bind_confirmation,
    confirmed_response,
    finish_confirmation,
)

OP = str(uuid4())


@pytest.fixture
def calendar_enabled(monkeypatch):
    from robothor.settings import get_settings, reset_settings

    monkeypatch.setenv("ROBOTHOR_CALENDAR_OPERATIONS_ENABLED", "true")
    reset_settings()
    assert get_settings().engine.calendar_operations_enabled is True
    yield
    reset_settings()


def session(**run):
    return SimpleNamespace(
        run=SimpleNamespace(
            **{
                "is_benchmark": False,
                "tools_provided": [TOOL],
                "tenant_id": "fixture",
                "user_id": "owner",
                "agent_id": "main",
                **run,
            }
        ),
        messages=[],
        was_interrupted=False,
        _interrupt_requested=False,
        consume_interrupt=lambda: None,
    )


def history(operation_id=OP):
    return [{"role": "assistant", "content": "Draft ready.\n\nCalendar operation: " + operation_id}]


@pytest.fixture
def row(monkeypatch):
    """The stored operation this binding would replay."""
    stored = {"status": "draft"}

    def load_operation(operation_id, tenant, user, agent):
        if (tenant, user, agent) != ("fixture", "owner", "main"):
            return None
        return dict(stored, id=operation_id)

    monkeypatch.setattr(
        "robothor.engine.calendar_operations.load_operation", load_operation, raising=True
    )
    return stored


async def test_a_draft_binds(calendar_enabled, row):
    live = session()
    await bind_confirmation(live, "yes", history())
    assert live.routine_operation_id == OP


@pytest.mark.parametrize("status", ["completed", "blocked", "executing"])
async def test_only_a_draft_binds(calendar_enabled, row, status):
    """An already finished operation must never be replayed by a bare 'yes'."""
    row["status"] = status
    live = session()
    await bind_confirmation(live, "yes", history())
    assert getattr(live, "routine_operation_id", None) is None
    assert live.messages == []


async def test_a_bystander_cannot_confirm_someone_elses_draft(calendar_enabled, row):
    """In a Telegram group an unregistered sender is telegram:{chat_id}."""
    live = session(user_id="telegram:-100999")
    await bind_confirmation(live, "yes", history())
    assert getattr(live, "routine_operation_id", None) is None


async def test_plan_mode_plans_instead_of_executing(calendar_enabled, row):
    live = session()
    await bind_confirmation(live, "yes", history(), readonly_mode=True)
    assert getattr(live, "routine_operation_id", None) is None


async def test_the_terminal_reply_never_re_arms_the_binding(calendar_enabled, row):
    """The marker on the final reply made the trap permanent: every later
    'yes' matched it again, with no model call and no way out."""
    live = session()
    live.routine_operation_id = OP
    live.run.steps = [
        SimpleNamespace(
            tool_name=TOOL,
            tool_output={
                "status": "updated",
                "verification": "verified",
                "added": ["sam@example.com"],
                "invitations_requested": True,
                "operation_id": OP,
                "calendar": {"kind": "operator", "id": "alice@example.com"},
            },
        )
    ]
    live.was_interrupted = False
    live._interrupt_requested = False
    await finish_confirmation(live, None)
    reply = live.messages[-1]["content"]
    assert OP not in reply
    assert confirmation_id("yes", [{"role": "assistant", "content": reply}]) is None


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"calendar": {"kind": "own", "id": "bot@example.com"}}, "assistant's OWN calendar"),
        ({"calendar": {"kind": "operator", "id": "alice@example.com"}}, "operator's calendar"),
        ({}, "was not reported"),
    ],
)
async def test_every_reply_names_the_calendar_it_changed(result, expected):
    """The verifier that would catch a wrong-calendar claim is disabled on
    this path, and this instance has put an itinerary on the WRONG calendar
    and reported it as done. So the reply says whose calendar, always."""
    live = session()
    live.run.steps = [
        SimpleNamespace(
            tool_name=TOOL,
            tool_output={
                "status": "updated",
                "verification": "verified",
                "added": ["sam@example.com"],
                "invitations_requested": True,
                **result,
            },
        )
    ]
    await finish_confirmation(live, None)
    reply = live.messages[-1]["content"]
    assert expected in reply
    if result:
        assert result["calendar"]["id"] in reply


def test_the_kill_switch_requires_a_restart_because_settings_are_cached(monkeypatch):
    """`restart_required=False` is a promise the platform cannot keep here:
    settings are read once per process and `reset_settings` has no production
    caller, so the documented kill switch does nothing until a restart."""
    from robothor.settings import get_settings
    from robothor.settings.registry import field_index

    assert field_index()["ROBOTHOR_CALENDAR_OPERATIONS_ENABLED"]["restart_required"] is True

    before = get_settings().engine.calendar_operations_enabled
    monkeypatch.setenv("ROBOTHOR_CALENDAR_OPERATIONS_ENABLED", str(not before).lower())
    assert get_settings().engine.calendar_operations_enabled is before


def test_the_confirmed_call_is_synthesised_only_once():
    """A second loop iteration must reach the model, not replay the write."""
    live = session()
    live.routine_operation_id = OP
    confirmed_response(live)
    assert not live.routine_operation_id
