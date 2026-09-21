"""Blocked profile reads leave the event loop free and cannot start late work."""

import asyncio
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from robothor.engine.chat_recovery import read_outcome
from robothor.engine.runtime import profile_lookup
from robothor.engine.runtime.chat_control import request_key
from robothor.engine.runtime.current import runtime_entrypoint
from robothor.engine.runtime.deadlines import RuntimeDeadlineError
from robothor.engine.tests.test_runtime_admission_audit import (  # noqa: F401
    audit_store,
    identity,
    private_database,
    records,
)


@pytest.mark.parametrize("cancel", [False, True])
async def test_blocked_lookup_is_interruptible_and_records_original_outcome(
    records,  # noqa: F811
    audit_store,  # noqa: F811
    monkeypatch,
    tmp_path,
    cancel,  # noqa: F811
):
    auth, client = identity(), str(uuid4())
    identifier = request_key(auth, "web:main", client)
    entered, release, finished = Event(), Event(), Event()
    execution = Mock()
    accepted = []

    async def status(event):
        if event.get("event") == "accepted":
            assert not entered.is_set()
            accepted.append(event)

    def blocked(*args):
        entered.set()
        try:
            assert release.wait(3), "test must release its lookup thread"
            return SimpleNamespace(difficulty_class="simple"), ""
        finally:
            finished.set()

    loader = Mock(side_effect=blocked)
    monkeypatch.setattr("robothor.engine.runner.load_agent_config_or_reason", loader)
    monkeypatch.setattr(profile_lookup, "LOOKUP_SECONDS", 0.2)

    class Runner:
        config = SimpleNamespace(tenant_id=auth.tenant_id, manifest_dir=tmp_path)

        @runtime_entrypoint
        async def execute(
            self,
            agent_id,
            message,
            user_id="",
            tenant_id=None,
            correlation_id=None,
            trigger_type=None,
            agent_config=None,
            on_status=None,
        ):
            execution()
            pytest.fail("interrupted profile must never start execution")

    task = asyncio.create_task(
        Runner().execute(
            "main",
            "Do the work",
            user_id=auth.user_id,
            correlation_id=identifier,
            trigger_type="webchat",
            on_status=status,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        # The event loop reached this assertion while the filesystem worker is
        # still blocked, so unrelated requests/Stop can make progress.
        assert not finished.is_set() and not task.done()
        assert len(accepted) == 1
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeDeadlineError):
            await task
        result = read_outcome(auth, "web:main", client)
        assert result["state"] == ("cancelled" if cancel else "timeout")
        assert result["terminal"] and not result["verified"] and not result["effects"]
        assert "before execution began" in result["text"]
        execution.assert_not_called()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    execution.assert_not_called()
    loader.assert_called_once()


async def test_lookup_error_is_not_misreported_as_runtime_expiry(monkeypatch):
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock

    from robothor.engine.runtime.contracts import ExecutionContext, RunRequest

    loader = Mock(side_effect=TimeoutError("filesystem-specific timeout"))
    audit = AsyncMock()
    monkeypatch.setattr("robothor.engine.runner.load_agent_config_or_reason", loader)
    monkeypatch.setattr("robothor.engine.runtime.admission_audit.record_timeout", audit)
    request = RunRequest(ExecutionContext("tenant", "operator", "request"), "main", "Do the work")
    with pytest.raises(TimeoutError, match="filesystem-specific timeout"):
        await profile_lookup.lookup(request, "fixture", datetime.now(UTC))
    audit.assert_not_awaited()


async def test_cancellation_during_acceptance_delivery_is_audited(
    records,  # noqa: F811
    audit_store,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    from robothor.engine.runtime.acceptance import before_lookup
    from robothor.engine.runtime.contracts import ExecutionContext, RunRequest

    auth, client = identity(), str(uuid4())
    entered = asyncio.Event()
    loader = Mock(side_effect=AssertionError("Cancelled acceptance must not load a profile"))
    monkeypatch.setattr("robothor.engine.runner.load_agent_config_or_reason", loader)

    async def stalled(event):
        entered.set()
        await asyncio.Event().wait()

    request = RunRequest(
        ExecutionContext(auth.tenant_id, auth.user_id, request_key(auth, "web:main", client)),
        "main",
        "Do the work",
        {"trigger_type": "webchat", "on_status": stalled},
    )
    task = asyncio.create_task(before_lookup(request))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = read_outcome(auth, "web:main", client)
    assert result["terminal"] and result["state"] == "cancelled"
    assert "before execution began" in result["text"]
    loader.assert_not_called()
