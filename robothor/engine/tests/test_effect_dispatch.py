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


@pytest.mark.parametrize("tool", ["create_note", "send_email"])
async def test_reported_success_survives_replacement_without_claiming_independent_proof(
    gateway, monkeypatch, tool
):
    ctx, writes, call = gateway

    async def handler(args, tool_context):
        writes.append(args)
        return {"id": "synthetic-provider-id", "status": "accepted"}

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {tool: handler})
    first = await call(tool)
    copies = await asyncio.gather(*(call(tool, run=f"replacement-{i}") for i in range(20)))
    repeated = copies[0]
    assert all(copy == repeated for copy in copies)
    assert len(writes) == 1, "Replacement worker repeated an acknowledged business action"
    assert repeated["id"] == first["id"]
    assert repeated["recovered"] and repeated["verification"] == "reported"
    row = effects.read(ctx, repeated["effect_id"])
    assert row["state"] == "finished"
    assert row["resolution"]["source"] == "tool_response"
    assert not row["resolution"].get("reference"), "Tool acknowledgement is not independent proof"
    await call(tool, caller=replace(ctx, request_id="separate-authorized-request"))
    assert len(writes) == 2


async def test_reported_response_persistence_failure_keeps_replay_fence(gateway, monkeypatch):
    from robothor.engine.runtime import effect_results

    ctx, writes, call = gateway

    async def handler(args, tool_context):
        writes.append(args)
        return {"id": "synthetic"}

    def unavailable(*args):
        raise OSError("Synthetic receipt persistence outage")

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"create_note": handler})
    monkeypatch.setattr(effect_results, "record", unavailable)
    first = await call("create_note")
    assert first["outcome_unknown"] and not first["retryable"]
    repeated = await call("create_note", run="replacement")
    assert repeated["effect_id"] == first["effect_id"] and len(writes) == 1
    assert effects.read(ctx, first["effect_id"])["state"] == "dispatching"


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


# ── Reads that time out are not unresolved writes ─────────────────────────
#
# Observed 2026-10-05: ``claude_code_wait`` (a pure read of a coding job's
# state) timed out, its cancellation was journalled as an ``uncertain`` effect,
# and the guard then refused every later ``claude_code_wait`` for that
# principal — "An earlier action is unresolved; audit/readback is required
# before another write" — wedging the pr-reviewer across runs.


def _effect_rows(connect, tool_name):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT state FROM agent_runtime_effects WHERE tool_name=%s", (tool_name,))
        return [row[0] for row in cur.fetchall()]


def _wait_handlers(waits):
    async def hang(args, tool_context):
        waits.append(args)
        await asyncio.Event().wait()

    async def done(args, tool_context):
        waits.append(args)
        return {"job_id": args.get("job_id"), "status": "done"}

    return hang, done


@pytest.mark.parametrize("tool", ["claude_code_wait", "claude_code_status"])
async def test_timed_out_coding_job_read_leaves_no_uncertain_effect(
    gateway,
    effect_db,  # noqa: F811
    monkeypatch,
    tool,
):
    _, _, call = gateway
    waits: list = []
    hang, done = _wait_handlers(waits)
    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {tool: hang})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(call(tool, {"job_id": "j1"}), 0.3)
    assert _effect_rows(effect_db, tool) == []
    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {tool: done})
    again = await call(tool, {"job_id": "j1"}, run="next-run")
    assert again == {"job_id": "j1", "status": "done"}
    assert len(waits) == 2


async def test_timed_out_read_through_tool_call_leaves_no_uncertain_effect(
    gateway,
    effect_db,  # noqa: F811
    monkeypatch,
):
    """The deferred path: ``tool_call`` wraps the read, and the wrapper's
    timeout cancels the inner dispatch. Neither may journal an effect."""
    from types import SimpleNamespace

    from robothor.engine.tools.handlers import toolsearch

    _, _, call = gateway
    waits: list = []
    hang, done = _wait_handlers(waits)
    inner = {"claude_code_wait": hang}

    async def execute(name, args, **kwargs):
        return await dispatch._execute_tool(
            name,
            args,
            agent_id=kwargs["agent_id"],
            run_id=kwargs["run_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_role=kwargs["user_role"],
        )

    monkeypatch.setattr(
        dispatch, "_get_handlers", lambda: {"tool_call": toolsearch._tool_call, **inner}
    )
    monkeypatch.setattr(toolsearch, "_allowed_names", lambda: ["claude_code_wait"])
    monkeypatch.setattr(
        "robothor.engine.tools.registry.get_registry", lambda: SimpleNamespace(execute=execute)
    )
    wrapped = {"name": "claude_code_wait", "arguments": {"job_id": "j1", "timeout_s": 1200}}
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(call("tool_call", wrapped), 0.3)
    assert _effect_rows(effect_db, "claude_code_wait") == []
    assert _effect_rows(effect_db, "tool_call") == []
    inner["claude_code_wait"] = done
    again = await call("tool_call", wrapped, run="next-run")
    assert again == {"job_id": "j1", "status": "done"}


async def test_pr_review_queue_count_is_a_read(gateway, effect_db, monkeypatch):  # noqa: F811
    """``pr_review_intake(count_only=True)`` only counts the queue; the polling
    form (and the on-demand ``pr=`` form) still write and stay journalled."""
    _, _, call = gateway
    calls: list = []

    async def handler(args, tool_context):
        calls.append(args)
        await asyncio.Event().wait()

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"pr_review_intake": handler})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(call("pr_review_intake", {"count_only": True}), 0.3)
    assert _effect_rows(effect_db, "pr_review_intake") == []
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            call("pr_review_intake", {"count_only": True, "pr": "acme/widgets#1"}), 0.3
        )
    assert _effect_rows(effect_db, "pr_review_intake") == ["uncertain"]


def test_tool_call_inherits_the_classification_of_what_it_wraps():
    from robothor.engine.wrapped_call import is_read_only_call

    assert is_read_only_call("tool_call", {"name": "claude_code_wait", "arguments": {}})
    assert is_read_only_call("tool_call", {"name": "github_pr_diff", "arguments": {}})
    assert not is_read_only_call("tool_call", {"name": "create_note", "arguments": {}})
    assert not is_read_only_call("tool_call", {"name": "tool_call", "arguments": {}})
    assert not is_read_only_call("tool_call", {})
    assert is_read_only_call(
        "tool_call", {"name": "pr_review_intake", "arguments": {"count_only": True}}
    )
    assert not is_read_only_call("tool_call", {"name": "pr_review_intake", "arguments": {}})
    for name in ("jira_get_issue", "github_pr_files", "github_compare", "claude_code_status"):
        assert is_read_only_call(name, {})
    for name in ("claude_code_start", "claude_code_followup", "claude_code_cancel"):
        assert not is_read_only_call(name, {})
