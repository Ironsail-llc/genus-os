"""Candidate tools reuse native admission; all dispatch effects are synthetic."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from bench.runtime.native_gateway import NativeGateway

from robothor.engine.models import AgentConfig, AgentRun
from robothor.engine.runtime.contracts import RuntimeStoppedError
from robothor.engine.session import AgentSession
from robothor.engine.tool_admission import ToolAdmissionMixin
from robothor.engine.tool_turn import ToolTurnRequest


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr("robothor.engine.tracking.create_steps_batch", lambda steps: len(steps))
    runner = ToolAdmissionMixin()
    runner.registry = MagicMock()
    runner.registry.execute = AsyncMock(return_value={"ok": True})
    runner._active_watchdog = None
    runner.config = SimpleNamespace(workspace=tmp_path)
    session = AgentSession(agent_id="candidate")
    session.run.user_id = "principal"
    session.run.user_role = "operator"
    session.run.is_benchmark = True
    session.run.accessible_tenant_ids = (session.run.tenant_id,)
    guardrails = MagicMock()
    guardrails.check_pre_execution.return_value = SimpleNamespace(allowed=True, action="allow")
    guardrails.check_post_execution.return_value = SimpleNamespace(action="allow")
    turn = ToolTurnRequest(
        assistant_msg=MagicMock(),
        session=session,
        agent_config=AgentConfig(id="candidate", name="Candidate"),
        iteration=0,
        guardrail_engine=guardrails,
        allowed_tool_set=frozenset({"read_file", "write_file"}),
        readonly_tool_set=frozenset({"read_file"}),
    )
    schemas = [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
        for name in ("read_file", "write_file")
    ]
    evidence = {"verified": False}
    gateway = NativeGateway(
        runner=runner, turn=turn, schemas=schemas, verify=lambda: evidence["verified"]
    )
    run = AgentRun(agent_id="candidate", user_id="principal", tenant_id=session.run.tenant_id)
    return SimpleNamespace(
        runner=runner, turn=turn, gateway=gateway, run=run, evidence=evidence, schemas=schemas
    )


@pytest.mark.asyncio
async def test_native_dispatch_identity_and_evidence(prepared):
    p = prepared
    p.gateway.bind_run(p.run)
    result = await p.gateway.invoke(p.run.tenant_id, "write_file", {"path": "fixture"})
    assert result == {"ok": True}
    dispatched = p.runner.registry.execute.await_args.kwargs
    assert dispatched["run_id"] == p.run.id
    assert dispatched["user_id"] == "principal"
    assert dispatched["user_role"] == "operator"
    assert dispatched["accessible_tenant_ids"] == (p.run.tenant_id,)
    assert dispatched["is_benchmark"] is True
    assert len(p.run.steps) == 1
    assert p.run.steps[0].tool_name == "write_file"
    assert not any(m.get("role") == "tool" for m in p.turn.session.messages)
    assert p.gateway.verified is False
    p.evidence["verified"] = True
    assert p.gateway.verified is True


@pytest.mark.parametrize("field", ["tenant_id", "user_id", "agent_id"])
def test_untrusted_run_identity_refused(prepared, field):
    setattr(prepared.run, field, "different")
    with pytest.raises(ValueError, match="identity"):
        prepared.gateway.bind_run(prepared.run)


def test_session_cannot_be_rebound(prepared):
    prepared.gateway.bind_run(prepared.run)
    with pytest.raises(ValueError, match="bound once"):
        prepared.gateway.bind_run(prepared.run)


@pytest.mark.parametrize("missing", ["role", "guardrails", "schema", "empty"])
def test_incomplete_preparation_refused(prepared, missing):
    p = prepared
    if missing == "role":
        p.turn.session.run.user_role = ""
    elif missing == "guardrails":
        p.turn.guardrail_engine = None
    elif missing == "schema":
        p.schemas[0]["function"]["name"] = "execute_code"
    else:
        p.schemas.clear()
    with pytest.raises(ValueError):
        NativeGateway(runner=p.runner, turn=p.turn, schemas=p.schemas, verify=lambda: True)


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["plan", "allowlist", "guardrail", "hook", "approval"])
async def test_native_gates_prevent_effects_and_record_refusal(prepared, gate):
    p = prepared
    name = "write_file"
    if gate == "plan":
        p.turn.readonly_mode = True
    elif gate == "allowlist":
        name = "exec"
    elif gate == "guardrail":
        p.turn.guardrail_engine.check_pre_execution.return_value = SimpleNamespace(
            allowed=False, action="block", guardrail_name="policy", reason="denied"
        )
    elif gate == "hook":
        from robothor.engine.hook_registry import HookAction

        p.turn.hook_registry = SimpleNamespace(
            dispatch=AsyncMock(return_value=SimpleNamespace(action=HookAction.BLOCK, reason="no"))
        )
    else:
        p.turn.agent_config.human_approval_tools = ["write_*"]
    p.gateway.bind_run(p.run)
    result = await p.gateway.invoke(p.run.tenant_id, name, {})
    assert "error" in result
    p.runner.registry.execute.assert_not_awaited()
    if gate != "approval":
        assert len(p.run.steps) == 1 and p.run.steps[0].error_message


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["unbound", "tenant", "interrupt"])
async def test_admission_refuses_unbound_foreign_or_stopped(prepared, gate):
    p = prepared
    tenant = p.run.tenant_id
    if gate != "unbound":
        p.gateway.bind_run(p.run)
    if gate == "tenant":
        tenant = "other"
    if gate == "interrupt":
        p.turn.session.interrupt()
    with pytest.raises((ValueError, RuntimeStoppedError)):
        await p.gateway.invoke(tenant, "write_file", {})
    p.runner.registry.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cap_and_schema_snapshot(prepared):
    p = prepared
    p.schemas[0]["function"]["name"] = "exec"
    p.gateway.schemas.clear()
    assert p.gateway.schemas[0]["function"]["name"] == "read_file"
    p.gateway.bind_run(p.run)
    for _ in range(4):
        await p.gateway.invoke(p.run.tenant_id, "read_file", {})
    result = await p.gateway.invoke(p.run.tenant_id, "read_file", {})
    assert result["guard"] == "execute_code_call_cap"
    assert p.runner.registry.execute.await_count == 4


@pytest.mark.asyncio
async def test_store_host_binds_before_persisting(prepared, monkeypatch):
    from bench.runtime.store_host import StoreHost

    from robothor.engine.runtime.contracts import ExecutionContext, RunRequest, StateEnvelope

    p = prepared
    monkeypatch.setattr("robothor.engine.runtime.controls.stopped", lambda *args: False)
    host = StoreHost(AsyncMock(return_value=p.gateway))
    persisted = []

    def insert(run, metadata):
        assert p.turn.session.run is run
        assert run.user_role == "operator"
        persisted.append(run.id)

    monkeypatch.setattr(host, "_insert_and_attach", insert)
    request = RunRequest(
        ExecutionContext(p.run.tenant_id, "principal", "fixture-request"),
        "candidate",
        "read the fixture",
    )
    run, gateway = await host.prepare(request, StateEnvelope("fixture", "1"))
    assert persisted == [run.id]
    await gateway.invoke(run.tenant_id, "read_file", {})
    assert p.runner.registry.execute.await_args.kwargs["run_id"] == run.id


@pytest.mark.asyncio
async def test_read_failure_does_not_poison_later_reads(prepared):
    p = prepared
    p.runner.registry.execute.side_effect = [{"error": "temporary read failure"}, {"ok": True}]
    p.gateway.bind_run(p.run)
    first = await p.gateway.invoke(p.run.tenant_id, "read_file", {})
    assert first == {"error": "temporary read failure"}
    second = await p.gateway.invoke(p.run.tenant_id, "read_file", {})
    assert second == {"ok": True}
    assert len(p.run.steps) == 2
