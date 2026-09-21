"""The actual dispatcher enforces durable uncertainty; reads stay available."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from robothor.engine.runtime import effects
from robothor.engine.runtime.current import active_context
from robothor.engine.tests.test_runtime_effects import (  # noqa: F401
    context,
    effect_db,
    private_database,
)
from robothor.engine.tools import dispatch


@pytest.fixture
def gateway(effect_db, monkeypatch):  # noqa: F811
    ctx = context()
    writes = []

    async def write(args, tool_context):
        writes.append(args)
        return {"error": "response lost after write", "outcome_unknown": True, "retryable": False}

    async def read(args, tool_context):
        return {"writes": len(writes)}

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"create_note": write, "get_note": read})
    monkeypatch.setattr(
        "robothor.engine.tools.get_registry",
        lambda: SimpleNamespace(get_adapter_route=lambda name: None),
    )

    async def call(name, args=None, *, run="worker", caller=None, runtime_principal=None):
        scope = caller or ctx
        token = active_context.set(
            replace(scope, principal_id=runtime_principal) if runtime_principal else scope
        )
        try:
            return await dispatch._execute_tool(
                name,
                args or {"body": "once"},
                agent_id="main",
                run_id=run,
                tenant_id=scope.tenant_id,
                user_id=scope.principal_id,
                user_role="service",
            )
        finally:
            active_context.reset(token)

    return ctx, writes, call


async def test_uncertain_dispatch_cannot_be_repeated_by_replacement_worker(gateway):
    ctx, writes, call = gateway
    original = await call("create_note")
    assert original["outcome_unknown"] and len(writes) == 1
    repeated = await call("create_note", run="replacement")
    assert repeated["effect_id"] == original["effect_id"] and len(writes) == 1
    assert await call("get_note", run="replacement") == {"writes": 1}
    assert effects.resolve(
        ctx,
        original["effect_id"],
        lambda row: effects.Verification("applied", True, "synthetic-provider:note", {"writes": 1}),
    )
    recovered = await call("create_note", run="replacement")
    assert recovered["recovered"] and recovered["writes"] == 1 and len(writes) == 1
    # Unrelated authorized work is not stopped by another request's uncertainty.
    other = replace(ctx, request_id="other-request")
    await call("create_note", {"body": "different"}, caller=other)
    assert len(writes) == 2


async def test_cancelled_dispatched_write_stays_uncertain(gateway, monkeypatch):
    ctx, writes, call = gateway
    dispatched = asyncio.Event()

    async def handler(args, tool_context):
        writes.append(args)
        dispatched.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"create_note": handler})
    task = asyncio.create_task(call("create_note"))
    await asyncio.wait_for(dispatched.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    repeated = await call("create_note", run="replacement")
    assert repeated["outcome_unknown"] and len(writes) == 1
    assert effects.read(ctx, repeated["effect_id"])["state"] == "uncertain"


async def test_stop_during_reservation_prevents_handler_dispatch(gateway, monkeypatch):
    ctx, writes, call = gateway
    stopped = [False]
    original = effects.begin

    def reserve(*args, **kwargs):
        result = original(*args, **kwargs)
        stopped[0] = True
        return result

    monkeypatch.setattr(effects, "begin", reserve)
    monkeypatch.setattr("robothor.engine.runtime.controls.stopped", lambda *args: stopped[0])
    result = await call("create_note")
    assert "durable stop" in result["error"] and writes == []


async def test_unavailable_journal_refuses_dispatch(gateway, monkeypatch):
    ctx, writes, call = gateway

    def unavailable(*args, **kwargs):
        raise OSError("synthetic database outage")

    monkeypatch.setattr(effects, "begin", unavailable)
    result = await call("create_note")
    assert "no action was dispatched" in result["error"] and writes == []


async def test_adapter_mutation_uses_the_same_journal(gateway, monkeypatch):
    ctx, writes, call = gateway
    remote = AsyncMock(side_effect=TimeoutError("synthetic adapter response lost"))
    session = SimpleNamespace(call_tool=remote)
    pool = SimpleNamespace(get_session=AsyncMock(return_value=session))
    monkeypatch.setattr(
        "robothor.engine.tools.get_registry",
        lambda: SimpleNamespace(get_adapter_route=lambda name: "synthetic"),
    )
    monkeypatch.setattr("robothor.engine.mcp_client.get_mcp_client_pool", lambda: pool)
    first = await call("synthetic_adapter_write")
    second = await call("synthetic_adapter_write", run="replacement")
    assert first["outcome_unknown"] and second["effect_id"] == first["effect_id"]
    assert remote.await_count == 1


async def test_delegated_runtime_uses_authenticated_business_principal(gateway):
    ctx, writes, call = gateway
    original = await call("create_note")
    delegated = await call("create_note", run="child", runtime_principal="service:child")
    assert delegated["effect_id"] == original["effect_id"] and len(writes) == 1
    assert effects.read(ctx, original["effect_id"])["principal_id"] == ctx.principal_id


async def test_cancelled_reservation_cleans_up_without_dispatch(gateway, effect_db, monkeypatch):  # noqa: F811
    import threading

    from robothor.engine.task_registry import get_task_registry

    ctx, writes, call = gateway
    entered, release = threading.Event(), threading.Event()
    original = effects.begin

    def reserve(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(effects, "begin", reserve)
    task = asyncio.create_task(call("create_note"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 0.2)
        assert writes == []
    finally:
        release.set()
        await get_task_registry().drain(timeout=5)
    with effect_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT state FROM agent_runtime_effects WHERE tenant_id=%s", (ctx.tenant_id,))
        assert cur.fetchall() == [("finished",)]
    assert writes == []


async def test_result_persistence_loss_does_not_permit_replay(gateway, monkeypatch):
    ctx, writes, call = gateway

    def fail(*args, **kwargs):
        raise OSError("synthetic storage outage after dispatch")

    monkeypatch.setattr(effects, "finish", fail)
    first = await call("create_note")
    assert first["outcome_unknown"] and len(writes) == 1
    second = await call("create_note", run="replacement")
    assert second["effect_id"] == first["effect_id"] and len(writes) == 1


async def test_late_worker_result_does_not_clear_recovery_state(gateway, monkeypatch):
    ctx, writes, call = gateway

    async def handler(args, tool_context):
        writes.append(args)
        await asyncio.to_thread(effects.abandon_run, ctx, tool_context.run_id)
        return {"ok": True}

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"create_note": handler})
    result = await call("create_note")
    assert result["outcome_unknown"] and len(writes) == 1
    assert effects.read(ctx, result["effect_id"])["state"] == "uncertain"


async def test_failed_enforced_readback_preserves_uncertainty(gateway, monkeypatch):
    ctx, writes, call = gateway

    async def handler(args, tool_context):
        writes.append(args)
        return {"id": "synthetic-note", "success": True}

    async def verify(name, args, result, tool_context):
        from robothor.engine.tools.verification import VerificationOutcome, _enforce_message

        message = _enforce_message(
            name, VerificationOutcome(reference="synthetic-note", verified=False)
        )
        return {
            **result,
            "error": message,
            "verification_failed": True,
            "outcome_unknown": True,
            "retryable": False,
        }

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"create_note": handler})
    monkeypatch.setattr("robothor.engine.tools.verification.verify_tool_result", verify)
    first = await call("create_note")
    second = await call("create_note", run="replacement")
    assert first["outcome_unknown"] and first["verification_failed"]
    assert second["effect_id"] == first["effect_id"] and len(writes) == 1
    assert effects.read(ctx, first["effect_id"])["state"] == "uncertain"


async def test_native_success_without_positive_readback_stays_fenced(gateway, monkeypatch):
    from robothor.engine.runtime.note_recovery import note_options

    ctx, writes, call = gateway

    async def handler(args, tool_context):
        identifier = note_options(tool_context, args)["note_id"]
        writes.append(identifier)
        return {"id": identifier, "title": "Claimed success"}

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"create_note": handler})
    monkeypatch.setattr(
        "robothor.engine.runtime.note_recovery.verify",
        lambda record: effects.Verification("unknown"),
    )
    first = await call("create_note")
    second = await call("create_note", run="replacement")
    assert first["outcome_unknown"] and second["outcome_unknown"]
    assert first["effect_id"] == second["effect_id"] and len(writes) == 1
    assert effects.read(ctx, first["effect_id"])["state"] == "uncertain"


async def test_extension_classification_cannot_bypass_core_write_journal(gateway, monkeypatch):
    _, writes, call = gateway
    monkeypatch.setattr(
        "robothor.engine.tools.read_only.declared_read_only_tools",
        lambda: frozenset({"create_note"}),
    )
    first = await call("create_note")
    second = await call("create_note", run="replacement")
    assert first["outcome_unknown"] and second["effect_id"] == first["effect_id"]
    assert len(writes) == 1


@pytest.mark.parametrize("read_only", [False, True])
async def test_extension_tools_keep_dynamic_classification(gateway, monkeypatch, read_only):
    _, writes, call = gateway
    handlers = dispatch._get_handlers()
    monkeypatch.setattr(
        dispatch, "_get_handlers", lambda: {"extension_action": handlers["create_note"]}
    )
    monkeypatch.setattr(
        "robothor.engine.tools.read_only.declared_read_only_tools",
        lambda: frozenset({"extension_action"}) if read_only else frozenset(),
    )
    first = await call("extension_action")
    second = await call("extension_action", run="replacement")
    assert len(writes) == (2 if read_only else 1)
    if not read_only:
        assert second["effect_id"] == first["effect_id"]
