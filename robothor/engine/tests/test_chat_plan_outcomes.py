"""Approved-plan chat must not present partial success as a terminal success."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine.chat import _sessions
from robothor.engine.models import AgentRun, RunStatus
from robothor.engine.tests.test_chat_per_user_sessions import (
    _member_auth,
    chat_app,  # noqa: F401
    client,  # noqa: F401
    mock_runner,  # noqa: F401
)


@pytest.mark.parametrize("deep", [False, True])
@pytest.mark.parametrize("partial", ["Everything is done.", ""])
@pytest.mark.parametrize(
    "status", [RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED, RunStatus.COMPLETED]
)
async def test_approved_plan_reports_terminal_outcome_in_stream_history_and_storage(
    client,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
    deep,
    status,
    partial,
):
    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    initial = AgentRun(status=RunStatus.COMPLETED, output_text="Check the result[PLAN_READY]")
    terminal = AgentRun(status=status, output_text=partial)
    if status != RunStatus.COMPLETED:
        terminal.error_message = (
            "Outcome unresolved; reconcile the dispatched action before retrying"
        )
    mock_runner.execute = AsyncMock(side_effect=[initial] if deep else [initial, terminal])
    mock_runner.execute_deep = AsyncMock(return_value=terminal)
    with (
        patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")),
        patch("robothor.engine.chat.save_exchange_async", new_callable=AsyncMock) as persist,
        patch("robothor.engine.chat.clear_plan_state_async", new_callable=AsyncMock),
    ):
        await client.post(
            "/chat/plan/start",
            json={"session_key": "agent:main:primary", "message": "Do the work", "deep_plan": deep},
        )
        persist.reset_mock()
        session = _sessions["agent:main:user:bob"]
        response = await client.post(
            "/chat/plan/approve",
            json={"session_key": "agent:main:primary", "plan_id": session.active_plan.plan_id},
        )
    data = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    done = data[-1]
    expected = terminal.output_text if status == RunStatus.COMPLETED else terminal.error_message
    assert expected in done["text"]
    assert done["status"] == status.value
    if expected:
        assert expected in session.history[-1]["content"]
        assert expected in persist.call_args.args[2]
    else:
        assert done["text"] == ""
        persist.assert_not_called()
    if status != RunStatus.COMPLETED:
        assert "Everything is done" not in done["text"]
        assert "event: deep_result" not in response.text
    assert session.active_plan is None
