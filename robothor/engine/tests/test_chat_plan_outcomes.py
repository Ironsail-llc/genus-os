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


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.CANCELLED])
async def test_failed_exploration_cannot_publish_partial_plan(
    client,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
    status,
):
    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    run = AgentRun(
        status=status,
        output_text="Perform an unchecked action[PLAN_READY]",
        error_message="Exploration stopped before verification",
    )
    mock_runner.execute = AsyncMock(return_value=run)
    with (
        patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")),
        patch("robothor.engine.chat.save_exchange_async", new_callable=AsyncMock) as exchange,
        patch("robothor.engine.chat.save_plan_state_async", new_callable=AsyncMock) as persist,
    ):
        response = await client.post(
            "/chat/plan/start",
            json={"message": "Prepare a plan", "session_key": "agent:main:primary"},
        )
        state = await client.get("/chat/plan/status?session_key=agent:main:primary")
    done = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ][-1]
    assert "event: plan\n" not in response.text
    assert state.json() == {"active": False, "plan": None}
    assert done["status"] == status.value
    assert run.error_message in done["text"]
    assert "unchecked action" not in done["text"]
    assert run.error_message in _sessions["agent:main:user:bob"].history[-1]["content"]
    assert run.error_message in exchange.call_args.args[2]
    persist.assert_not_called()
    assert mock_runner.execute.await_count == 1


@pytest.mark.parametrize("match", [True, False])
@pytest.mark.parametrize("cache_lost", [True, False, "status_first"])
async def test_outcome_restores_only_its_own_saved_plan(
    client,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
    match,
    cache_lost,
):
    from uuid import uuid4

    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    run = AgentRun(status=RunStatus.COMPLETED, output_text="Check the record[PLAN_READY]")
    mock_runner.execute = AsyncMock(return_value=run)
    with (
        patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")),
        patch("robothor.engine.chat.save_exchange_async", new_callable=AsyncMock),
        patch("robothor.engine.chat.save_plan_state_async", new_callable=AsyncMock) as saved,
    ):
        await client.post("/chat/plan/start", json={"message": "Prepare a plan"})
        if cache_lost:
            _sessions.clear()
        if cache_lost == "status_first":
            assert (await client.get("/chat/plan/status")).json()["active"] is False
        outcome = {
            "terminal": True,
            "state": "completed",
            "text": run.output_text,
            "run_id": run.id if match else str(uuid4()),
            "plan_exploration": True,
        }
        with (
            patch("robothor.engine.chat_recovery.read_outcome", return_value=outcome),
            patch(
                "robothor.engine.chat_store.load_plan_state", return_value=saved.call_args.args[1]
            ),
        ):
            response = await client.get(f"/chat/outcome?request_id={uuid4()}")
            restored = await client.get("/chat/plan/status")
    if match:
        assert restored.json()["plan"]["plan_id"] == response.json()["plan"]["plan_id"]
        assert response.json()["plan"]["exploration_run_id"] == run.id
        assert response.json()["plan"]["status"] == "pending"
    else:
        assert "plan" not in response.json()
    assert mock_runner.execute.await_count == 1


async def test_plan_recovery_waits_for_delivery_and_recovers_without_cache():
    import asyncio
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from robothor.engine.chat_plan_recovery import attach_plan

    auth = _member_auth("bob")
    task = asyncio.create_task(asyncio.Event().wait())
    session = SimpleNamespace(active_request_id="request", active_task=task, active_plan=None)
    outcome = {"terminal": True, "state": "completed", "run_id": "run", "plan_exploration": True}
    try:
        pending = dict(outcome)
        await attach_plan(pending, session, "request", auth, "session")
        assert not pending["terminal"] and pending["delivery_pending"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    saved = {
        "plan_text": "Check the record",
        "original_message": "Prepare a plan",
        "plan_id": "draft",
        "exploration_run_id": "run",
        "status": "pending",
        "created_at": datetime.now(UTC).isoformat(),
    }
    with patch("robothor.engine.chat_store.load_plan_state", return_value=saved) as read:
        await attach_plan(outcome, None, "request", auth, "session")
    assert outcome["plan"] == saved
    read.assert_called_once_with("session", tenant_id=auth.tenant_id)


async def test_unpersisted_plan_is_not_published(client, mock_runner, monkeypatch):  # noqa: F811
    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    mock_runner.execute = AsyncMock(
        return_value=AgentRun(
            status=RunStatus.COMPLETED, output_text="Check the record[PLAN_READY]"
        )
    )
    with (
        patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")),
        patch("robothor.engine.chat.save_exchange_async", new_callable=AsyncMock),
        patch(
            "robothor.engine.chat.save_plan_state_async",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Database unavailable"),
        ) as save,
    ):
        response = await client.post("/chat/plan/start", json={"message": "Prepare a plan"})
        state = await client.get("/chat/plan/status")
    assert save.call_args.kwargs["strict"] is True
    assert "event: plan\n" not in response.text
    assert state.json()["active"] is False


@pytest.mark.parametrize("refusal", ["failed", "rejected", "expired", "other_run"])
async def test_saved_plan_recovery_does_not_revive_ineligible_draft(refusal):
    from datetime import UTC, datetime

    from robothor.engine.chat_plan_recovery import attach_plan

    outcome = {
        "terminal": True,
        "state": "failed" if refusal == "failed" else "completed",
        "run_id": "run",
        "plan_exploration": True,
    }
    saved = {
        "plan_id": "draft",
        "exploration_run_id": "other" if refusal == "other_run" else "run",
        "status": "rejected" if refusal == "rejected" else "pending",
        "created_at": "2000-01-01T00:00:00+00:00"
        if refusal == "expired"
        else datetime.now(UTC).isoformat(),
    }
    with patch("robothor.engine.chat_store.load_plan_state", return_value=saved):
        await attach_plan(outcome, None, "request", _member_auth("bob"), "absent-session")
    assert "plan" not in outcome
    assert "absent-session" not in _sessions


async def test_duplicate_approval_does_not_start_a_second_execution(
    client,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
):
    import asyncio
    from datetime import UTC, datetime

    from robothor.engine.chat import _get_session
    from robothor.engine.models import PlanState

    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    session = _get_session("agent:main:user:bob")
    session.active_plan = PlanState(
        plan_id="plan-once",
        plan_text="Check the task",
        original_message="Check the task",
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
    )
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def execute(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return AgentRun(status=RunStatus.COMPLETED, output_text="Task checked")

    mock_runner.execute = AsyncMock(side_effect=execute)
    with (
        patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")),
        patch("robothor.engine.chat.save_exchange_async", new_callable=AsyncMock),
        patch("robothor.engine.chat.clear_plan_state_async", new_callable=AsyncMock),
    ):
        first = asyncio.create_task(
            client.post("/chat/plan/approve", json={"plan_id": "plan-once"})
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = await client.post("/chat/plan/approve", json={"plan_id": "plan-once"})
        finally:
            release.set()
            response = await first
    assert response.status_code == 200
    assert second.status_code == 409
    assert calls == 1


async def test_retry_reattaches_admission_even_after_pending_plan_is_cleared(
    client,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
):
    from uuid import uuid4

    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    with (
        patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")),
        patch("robothor.engine.chat_plan_claim.already_admitted", return_value=True),
    ):
        response = await client.post(
            "/chat/plan/approve", json={"plan_id": "completed-plan", "request_id": str(uuid4())}
        )
    assert response.status_code == 409
    assert response.json()["request_admitted"] is True
    assert "Checking its recorded result" in response.json()["error"]
    mock_runner.execute.assert_not_called()


@pytest.mark.parametrize("deep", [False, True])
async def test_older_execution_cannot_clear_newer_pending_plan(
    client,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
    deep,
):
    from datetime import UTC, datetime

    from robothor.engine.chat import _get_session
    from robothor.engine.models import PlanState

    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    session = _get_session("agent:main:user:bob")
    session.active_plan = PlanState(
        plan_id="old",
        plan_text="Old plan",
        original_message="Old task",
        deep_plan=deep,
        created_at=datetime.now(UTC).isoformat(),
    )
    newer = PlanState(
        plan_id="new",
        plan_text="New plan",
        original_message="New task",
        created_at=datetime.now(UTC).isoformat(),
    )

    async def execute(**kwargs):
        session.active_plan = newer
        return AgentRun(status=RunStatus.COMPLETED, output_text="Old task finished")

    mock_runner.execute = AsyncMock(side_effect=execute)
    mock_runner.execute_deep = AsyncMock(side_effect=execute)
    with (
        patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")),
        patch("robothor.engine.chat.save_exchange_async", new_callable=AsyncMock),
        patch("robothor.engine.chat.clear_plan_state_async", new_callable=AsyncMock),
    ):
        response = await client.post("/chat/plan/approve", json={"plan_id": "old"})
    assert response.status_code == 200
    assert session.active_plan is newer
    assert newer.status == "pending"


@pytest.mark.parametrize("action", ["reject", "iterate"])
async def test_approved_plan_cannot_be_rejected_or_revised_as_pending(
    client,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
    action,
):
    from datetime import UTC, datetime

    from robothor.engine.chat import _get_session
    from robothor.engine.models import PlanState

    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    session = _get_session("agent:main:user:bob")
    plan = PlanState(
        plan_id="approved",
        plan_text="Original",
        original_message="Original",
        status="approved",
        created_at=datetime.now(UTC).isoformat(),
    )
    session.active_plan = plan
    mock_runner.execute = AsyncMock(
        return_value=AgentRun(status=RunStatus.COMPLETED, output_text="Changed[PLAN_READY]")
    )
    with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
        response = await client.post(
            f"/chat/plan/{action}", json={"plan_id": plan.plan_id, "feedback": "Change it"}
        )
    assert response.status_code == 409
    assert session.active_plan is plan and plan.status == "approved"
    mock_runner.execute.assert_not_called()


async def test_failed_revision_does_not_publish_or_mutate_pending_draft(
    client,  # noqa: F811
    mock_runner,  # noqa: F811
    monkeypatch,
):
    from datetime import UTC, datetime

    from robothor.engine.chat import _get_session
    from robothor.engine.models import PlanState

    monkeypatch.setenv("ROBOTHOR_PER_USER_SESSIONS", "enforce")
    session = _get_session("agent:main:user:bob")
    original = PlanState(
        plan_id="original",
        plan_text="Original",
        original_message="Original",
        created_at=datetime.now(UTC).isoformat(),
    )
    session.active_plan = original
    mock_runner.execute = AsyncMock(
        return_value=AgentRun(
            status=RunStatus.FAILED,
            error_message="Revision could not be verified",
            output_text="Partial[PLAN_READY]",
        )
    )
    with patch("robothor.engine.chat._auth_context", return_value=_member_auth("bob")):
        response = await client.post(
            "/chat/plan/iterate", json={"plan_id": "original", "feedback": "Change it"}
        )
    assert "event: plan\n" not in response.text
    assert session.active_plan is original
    assert original.plan_text == "Original" and original.revision_count == 0
    assert "Revision could not be verified" in response.text


@pytest.mark.parametrize("competing", ["plan", "task"])
async def test_recovery_does_not_replace_session_work_started_during_saved_plan_read(competing):
    import asyncio
    from datetime import UTC, datetime

    from robothor.engine.chat import _get_session
    from robothor.engine.chat_plan_recovery import attach_plan
    from robothor.engine.models import PlanState

    key = f"recovery-race-{competing}"
    session = _get_session(key)
    outcome = {"terminal": True, "state": "completed", "run_id": "run", "plan_exploration": True}
    saved = {
        "plan_text": "Check the record",
        "original_message": "Prepare a plan",
        "plan_id": "saved",
        "exploration_run_id": "run",
        "status": "pending",
        "created_at": datetime.now(UTC).isoformat(),
    }
    newer = PlanState(plan_id="newer", plan_text="New work", original_message="Prepare new work")
    task = asyncio.create_task(asyncio.Event().wait())

    async def read(*args, **kwargs):
        if competing == "plan":
            session.active_plan = newer
        else:
            session.active_task = task
        return saved

    try:
        with patch("asyncio.to_thread", side_effect=read):
            await attach_plan(outcome, session, "request", _member_auth("bob"), key)
        assert "plan" not in outcome
        assert session.active_plan is (newer if competing == "plan" else None)
        if competing == "task":
            assert session.active_task is task
    finally:
        _sessions.pop(key, None)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
