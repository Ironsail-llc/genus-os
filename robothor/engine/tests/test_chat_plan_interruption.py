"""Approved work consults durable outcomes even when the runner raises."""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

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
@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("saved", [False, True])
async def test_plan_exception_delivers_only_owned_saved_outcome(
    client,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
    deep,
    cancelled,
    saved,  # noqa: F811
):
    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    initial = AgentRun(status=RunStatus.COMPLETED, output_text="Create task[PLAN_READY]")
    error = asyncio.CancelledError() if cancelled else RuntimeError("Connection lost")
    mock_runner.execute = AsyncMock(side_effect=[initial] if deep else [initial, error])
    mock_runner.execute_deep = AsyncMock(side_effect=error)
    outcome = {
        "text": "The task was created. Any remaining work is not confirmed.",
        "run_id": str(uuid4()),
        "status": "cancelled",
        "audit_outcome": True,
    }
    read = Mock(return_value=outcome if saved else None)
    with (
        patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")),
        patch("robothor.engine.chat.save_exchange_async", new_callable=AsyncMock),
        patch("robothor.engine.chat_store.save_exchange_async", new_callable=AsyncMock) as persist,
        patch("robothor.engine.chat_delivery._saved_request", read),
        patch("robothor.engine.chat_plan_claim.clear_claim") as retire,
    ):
        await client.post("/chat/plan/start", json={"message": "Create task", "deep_plan": deep})
        session = _sessions["agent:main:user:bob"]
        plan = session.active_plan
        response = await client.post("/chat/plan/approve", json={"plan_id": plan.plan_id})
        await asyncio.sleep(0)
        again = await client.post("/chat/plan/approve", json={"plan_id": plan.plan_id})
    assert again.status_code in {404, 409}
    assert mock_runner.execute.await_count == (1 if deep else 2)
    assert mock_runner.execute_deep.await_count == (1 if deep else 0)
    read.assert_called_once()
    assert read.call_args.args[0] == session.active_request_id
    assert read.call_args.args[1].user_id == "bob"
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    if saved:
        assert events[-1] == {**outcome, "aborted": cancelled}
        assert session.history[-1]["content"] == outcome["text"]
        assert persist.call_args.args[2] == outcome["text"]
        assert session.active_plan is None
        retire.assert_called_once()
    else:
        assert session.active_plan.status == "approved"
        retire.assert_not_called()
        persist.assert_not_called()
        assert "The task was created" not in response.text
