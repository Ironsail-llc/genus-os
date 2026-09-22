"""Trusted runtime deadlines bound execution and later provider/tool dispatch."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from robothor.engine.models import AgentRun, RunStatus
from robothor.engine.runtime import CurrentRuntime, ExecutionContext, RunRequest
from robothor.engine.runtime.current import active_context
from robothor.engine.runtime.deadlines import RuntimeDeadlineError
from robothor.engine.tests.test_runner import runner  # noqa: F401


def request(seconds):
    return RunRequest(
        ExecutionContext(
            "tenant", "owner", "request", deadline=datetime.now(UTC) + timedelta(seconds=seconds)
        ),
        "main",
        "Perform the authorized action",
    )


async def test_expired_admission_never_starts_execution():
    execute = AsyncMock()
    with pytest.raises(RuntimeDeadlineError, match="new dispatch denied"):
        await CurrentRuntime(execute).run(request(-1))
    execute.assert_not_awaited()
    assert active_context.get() is None


async def test_deadline_cancels_inflight_execution_and_preserves_cleanup():
    cleaned = asyncio.Event()

    async def execute(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with pytest.raises(RuntimeDeadlineError, match="execution cancelled"):
        await CurrentRuntime(execute).run(request(0.02))
    assert cleaned.is_set()
    assert active_context.get() is None


async def test_chat_background_admission_preserves_trusted_host_deadline():
    from types import SimpleNamespace

    from robothor.engine.runtime.chat_control import start

    outer = request(0.05).context
    session = SimpleNamespace(active_request_id=None, active_task=None)
    auth = SimpleNamespace(tenant_id=outer.tenant_id, user_id=outer.principal_id)
    cleaned = asyncio.Event()

    async def execute(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    async def work():
        admitted = active_context.get()
        assert admitted.deadline == outer.deadline
        assert admitted.request_id != outer.request_id
        return await CurrentRuntime(execute).run(RunRequest(admitted, "main", "fixture"))

    token = active_context.set(outer)
    try:
        with pytest.raises(RuntimeDeadlineError, match="execution cancelled"):
            await asyncio.wait_for(start(session, work, auth, "web:main"), 1)
        assert cleaned.is_set()
        assert active_context.get() is outer
    finally:
        active_context.reset(token)


@pytest.mark.parametrize("tenant,principal", [("other", "owner"), ("tenant", "other")])
async def test_chat_does_not_borrow_another_principals_deadline(tenant, principal):
    from types import SimpleNamespace

    from robothor.engine.runtime.chat_control import start

    outer = request(-1).context
    auth = SimpleNamespace(tenant_id=tenant, user_id=principal)

    async def work():
        current = active_context.get()
        assert (current.tenant_id, current.principal_id) == (tenant, principal)
        assert current.deadline is None and current.goal_id is None

    token = active_context.set(outer)
    try:
        await start(SimpleNamespace(), work, auth, "web:main")
    finally:
        active_context.reset(token)


async def test_cancellation_resistance_cannot_dispatch_or_report_success():
    from robothor.engine.request_budget import bounded_completion
    from robothor.engine.tools.dispatch import ToolContext, _runtime_denial

    provider = AsyncMock()

    async def execute(**kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            with pytest.raises(RuntimeDeadlineError, match="new dispatch denied"):
                await bounded_completion(provider, model="fixture", messages=[])
            denial = await _runtime_denial("fixture_write", {}, ToolContext(tenant_id="tenant"))
            assert "deadline expired" in denial["error"]
            return AgentRun(status=RunStatus.COMPLETED)

    with pytest.raises(RuntimeDeadlineError, match="cancellation was suppressed"):
        await CurrentRuntime(execute).run(request(0.02))
    provider.assert_not_awaited()


async def test_unrelated_timeout_is_not_relabelled_as_runtime_expiry():
    failure = TimeoutError("provider connection timed out")
    with pytest.raises(TimeoutError) as caught:
        await CurrentRuntime(AsyncMock(side_effect=failure)).run(request(30))
    assert caught.value is failure


@pytest.mark.usefixtures("_mock_run_persistence")
@pytest.mark.parametrize("denied_before_timer", [False, True])
async def test_native_runner_unwinds_provider_wait_at_host_deadline(
    request, sample_agent_config, monkeypatch, denied_before_timer
):
    import litellm

    from robothor.engine.runtime.activity import runs

    # Request the shared native runner fixture without substituting an execution loop.
    engine = request.getfixturevalue("runner")
    persisted = MagicMock()
    monkeypatch.setattr(engine, "_persist_run_sync", persisted)
    entered = asyncio.Event()

    async def provider(**kwargs):
        entered.set()
        if denied_before_timer:
            raise RuntimeDeadlineError("Runtime deadline expired at dispatch")
        await asyncio.Event().wait()

    monkeypatch.setattr(litellm, "acompletion", provider)
    context = ExecutionContext(
        "test-tenant",
        "owner",
        "deadline-request",
        deadline=datetime.now(UTC) + timedelta(seconds=1),
    )
    with pytest.raises(RuntimeDeadlineError):
        await CurrentRuntime(engine.execute).run(
            RunRequest(
                context,
                "test-agent",
                "A synthetic request",
                options={"agent_config": sample_agent_config},
            )
        )
    assert entered.is_set()
    assert not runs("test-tenant")
    await asyncio.sleep(0)
    assert persisted.called
    assert persisted.call_args.args[0].status == RunStatus.CANCELLED
    if denied_before_timer:
        assert persisted.call_args.args[0].error_message == "Runtime deadline expired at dispatch"


async def test_nested_deadline_uses_one_owner_and_cannot_extend_parent(monkeypatch):
    from robothor.engine.runtime.deadlines import execute_before_deadline, require_time
    from robothor.engine.workflow_budget import propagates_to_caller

    real_timeout = asyncio.timeout
    created = []

    def timeout(seconds):
        created.append(seconds)
        return real_timeout(seconds)

    monkeypatch.setattr(asyncio, "timeout", timeout)
    parent, child = request(0.02).context, request(60).context

    async def nested():
        async def child_work():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                with pytest.raises(RuntimeDeadlineError):
                    require_time(child)
                return "late response"

        return await execute_before_deadline(child, child_work)

    with pytest.raises(RuntimeDeadlineError):
        await execute_before_deadline(parent, nested)
    assert len(created) == 1
    assert propagates_to_caller(RuntimeDeadlineError("expired"))


async def test_delegated_run_records_the_parent_deadline_in_its_execution_identity():
    from robothor.engine.runtime.current import run_identity

    parent = request(30).context
    child = request(60).context

    async def child_execute(**kwargs):
        assert active_context.get().deadline == parent.deadline
        run = AgentRun(tenant_id="tenant", status=RunStatus.COMPLETED)
        assert run_identity(run)["deadline"] == parent.deadline.isoformat()
        return run

    async def parent_execute(**kwargs):
        result = await CurrentRuntime(child_execute).run(RunRequest(child, "child", "Go"))
        return result.run

    await CurrentRuntime(parent_execute).run(RunRequest(parent, "parent", "Go"))
    assert active_context.get() is None


async def test_shared_retry_helpers_do_not_retry_host_expiry():
    from robothor.engine.retry import retry_async, retry_sync

    expired = RuntimeDeadlineError("host expired")
    asynchronous = AsyncMock(side_effect=expired)
    synchronous = MagicMock(side_effect=expired)
    with pytest.raises(RuntimeDeadlineError):
        await retry_async(asynchronous, max_attempts=3)
    with pytest.raises(RuntimeDeadlineError):
        retry_sync(synchronous, max_attempts=3)
    asynchronous.assert_awaited_once()
    synchronous.assert_called_once()


async def test_native_manifest_cap_remains_separate_from_adapter_owned_deadline():
    from types import SimpleNamespace

    from robothor.engine.runtime.setup import bounded_timeout

    async def execute(**kwargs):
        assert bounded_timeout(30, SimpleNamespace()) == 30
        assert bounded_timeout(None, SimpleNamespace()) is None
        assert bounded_timeout(120, SimpleNamespace(routine_operation_id="confirmed")) == 60
        return AgentRun(status=RunStatus.COMPLETED)

    await CurrentRuntime(execute).run(request(1))
    token = active_context.set(request(1).context)
    try:
        # A legacy caller with no adapter owner still receives a native bound.
        assert 0 < bounded_timeout(30, SimpleNamespace()) <= 1
    finally:
        active_context.reset(token)


async def test_resume_checkpoint_read_is_inside_total_deadline(monkeypatch):
    from dataclasses import replace
    from threading import Event

    from robothor.engine.checkpoint import CheckpointManager
    from robothor.engine.runtime.contracts import StateEnvelope

    released = Event()
    started = Event()

    def read(*args, **kwargs):
        started.set()
        released.wait(1)
        return {"messages": []}

    monkeypatch.setattr(CheckpointManager, "load_latest", read)
    execute = AsyncMock()
    resumed = replace(request(0.02), resume_from="saved-run", checkpoint=StateEnvelope())
    try:
        with pytest.raises(RuntimeDeadlineError):
            await asyncio.wait_for(CurrentRuntime(execute).run(resumed), timeout=0.3)
    finally:
        released.set()
    assert started.is_set()
    execute.assert_not_awaited()
    assert active_context.get() is None


async def test_acceptance_callback_cannot_extend_request_deadline():
    event_started = asyncio.Event()

    async def stalled_status(event):
        event_started.set()
        await asyncio.Event().wait()

    execute = AsyncMock()
    with pytest.raises(RuntimeDeadlineError):
        await asyncio.wait_for(
            CurrentRuntime(execute).run(request(0.02), on_event=stalled_status), timeout=0.3
        )
    assert event_started.is_set()
    execute.assert_not_awaited()
    assert active_context.get() is None


async def test_suppressed_acceptance_cancellation_cannot_admit_execution():
    async def stubborn_status(event):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    execute = AsyncMock(return_value=AgentRun(status=RunStatus.COMPLETED))
    with pytest.raises(RuntimeDeadlineError):
        await CurrentRuntime(execute).run(request(0.02), on_event=stubborn_status)
    execute.assert_not_awaited()


@pytest.mark.parametrize("expire", [False, True])
async def test_native_cancellation_reason_requires_fired_runtime_window(expire):
    from robothor.engine.runtime.deadlines import enclosing_deadline_reason, execute_before_deadline

    seen = []
    entered = asyncio.Event()

    async def execute():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            seen.append(enclosing_deadline_reason(exc))
            raise

    context = ExecutionContext(
        "tenant",
        "owner",
        "request",
        deadline=datetime.now(UTC) + timedelta(seconds=0.02 if expire else 10),
    )
    task = asyncio.create_task(execute_before_deadline(context, execute))
    await entered.wait()
    if not expire:
        task.cancel()
    with pytest.raises(RuntimeDeadlineError if expire else asyncio.CancelledError):
        await task
    assert len(seen) == 1
    assert ("Runtime deadline expired" in seen[0]) is expire
    assert enclosing_deadline_reason(asyncio.CancelledError()) == ""
