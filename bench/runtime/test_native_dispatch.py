"""Installed candidate frameworks, native dispatch and private durable records."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("deepagents")
pytest.importorskip("litellm")

from bench.runtime import test_store_host
from bench.runtime.adapter import CandidateRuntime
from bench.runtime.candidates import FixtureGateway
from bench.runtime.native_gateway import NativeGateway
from bench.runtime.store_host import StoreHost
from bench.runtime.test_candidate_boundaries import candidate
from robothor.engine import tracking
from robothor.engine.models import AgentConfig
from robothor.engine.runtime import controls
from robothor.engine.runtime.contracts import ExecutionContext, RunRequest
from robothor.engine.session import AgentSession
from robothor.engine.tool_admission import ToolAdmissionMixin
from robothor.engine.tool_turn import ToolTurnRequest
from robothor.engine.tools import dispatch
from robothor.engine.tools.registry import ToolRegistry

database = test_store_host.database
private_database = test_store_host.private_database
rows = test_store_host.rows


@pytest.fixture
def native(database, monkeypatch, tmp_path):
    tenant, connect = database
    root = Path(__file__).resolve().parents[2] / "crm/migrations"
    with connect() as conn, conn.cursor() as cur:
        for name in ("029_cache_token_tracking.sql", "125_agent_run_step_batch.sql"):
            cur.execute((root / name).read_text())
        cur.execute("""CREATE TABLE IF NOT EXISTS synthetic_native_receipts (
            tenant_id TEXT, key TEXT, value TEXT, PRIMARY KEY (tenant_id,key))""")
    monkeypatch.setattr(tracking, "get_connection", connect)
    audits, effects, permissions, calls = [], [], [], []
    monkeypatch.setattr("robothor.audit.logger.log_event", lambda **event: audits.append(event))
    monkeypatch.setattr(tracking, "log_guardrail_event", lambda **event: None)
    monkeypatch.setattr(tracking, "log_tool_event", lambda **event: None)
    policy = {"deny": False, "lose_reply": False, "receipt_visible": True}

    def authorize(role, actual_tenant, name, *, user_id):
        permissions.append((role, actual_tenant, name, user_id))
        assert (role, actual_tenant, name, user_id) == ("user", tenant, "record", "operator")
        return "synthetic permission denied" if policy["deny"] else None

    monkeypatch.setattr("robothor.engine.permissions.check_tool_permission", authorize)
    schema = FixtureGateway(tenant).schemas

    def register(registry):
        registry._schemas = {"record": schema[0]}

    monkeypatch.setattr(ToolRegistry, "_register_all", register)
    registry = ToolRegistry()
    monkeypatch.setattr("robothor.engine.tools.get_registry", lambda: registry)

    async def record(arguments, context):
        assert context.tenant_id == tenant and context.user_id == "operator"
        assert arguments == {"key": "report", "value": "delivered"}
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO synthetic_native_receipts VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (tenant, arguments["key"], arguments["value"]),
            )
            effects.append(cur.rowcount)
        if policy["lose_reply"]:
            import httpx

            raise httpx.ReadTimeout("synthetic provider reply lost after commit")
        return {"ok": True}

    monkeypatch.setattr(dispatch, "_get_handlers", lambda: {"record": record})

    def verified():
        if not policy["receipt_visible"]:
            return False
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT key,value FROM synthetic_native_receipts WHERE tenant_id=%s", (tenant,)
            )
            return cur.fetchall() == [("report", "delivered")]

    session = AgentSession(agent_id="synthetic")
    session.run.tenant_id, session.run.user_id, session.run.user_role = tenant, "operator", "user"
    runner = ToolAdmissionMixin()
    runner.registry, runner.config = registry, SimpleNamespace(workspace=tmp_path)
    runner._active_watchdog = None
    guardrails = MagicMock()
    guardrails.check_pre_execution.return_value = SimpleNamespace(allowed=True, action="allow")
    guardrails.check_post_execution.return_value = SimpleNamespace(action="allow")
    turn = ToolTurnRequest(
        assistant_msg=None,
        session=session,
        agent_config=AgentConfig(id="synthetic", name="Synthetic", tools_allowed=["record"]),
        iteration=0,
        guardrail_engine=guardrails,
        allowed_tool_set=frozenset({"record"}),
    )
    gateway = NativeGateway(runner=runner, turn=turn, schemas=schema, verify=verified)

    async def prepare(request):
        assert request.context.principal_id == "operator" and not request.options
        return gateway

    return SimpleNamespace(
        tenant=tenant,
        connect=connect,
        turn=turn,
        gateway=gateway,
        host=StoreHost(prepare),
        effects=effects,
        audits=audits,
        permissions=permissions,
        calls=calls,
        policy=policy,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["pydantic-ai", "deepagents"])
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "duplicate-proposal",
        "plan",
        "permission",
        "guardrail",
        "stop",
        "lost-step-ack",
        "lost-step-ack-queued",
        "lost-provider-reply-verified",
        "lost-provider-reply-unresolved",
    ],
)
async def test_framework_native_dispatch_and_durable_outcome(native, name, outcome, monkeypatch):
    p = native
    if outcome == "plan":
        p.turn.readonly_mode = True
    elif outcome == "permission":
        p.policy["deny"] = True
    elif outcome == "guardrail":
        p.turn.guardrail_engine.check_pre_execution.return_value = SimpleNamespace(
            allowed=False, action="block", guardrail_name="fixture", reason="denied"
        )
    elif outcome.startswith("lost-step-ack"):
        original = tracking.create_steps_batch

        def lost_ack(steps):
            original(steps)
            raise ConnectionError("synthetic lost step acknowledgement")

        monkeypatch.setattr(tracking, "create_steps_batch", lost_ack)
    elif outcome.startswith("lost-provider-reply"):
        p.policy["lose_reply"] = True
        p.policy["receipt_visible"] = outcome.endswith("verified")

    def during_provider():
        if outcome == "stop":
            controls.issue(p.tenant, p.turn.session.run.id, "cancel")

    proposals = [("record", {"key": "report", "value": "delivered"})]
    if outcome in {"duplicate-proposal", "lost-step-ack-queued", "lost-provider-reply-unresolved"}:
        proposals *= 2
    adapter = candidate(name, proposals, p.calls, during_provider)
    runtime = CandidateRuntime(adapter, p.host)
    request = RunRequest(
        ExecutionContext(p.tenant, "operator", str(uuid4())), "synthetic", "Store report"
    )
    result = await runtime.run(request)
    (saved,) = rows(p.connect, p.tenant)
    with p.connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tool_name,error_message FROM agent_run_steps WHERE run_id=%s", (saved["id"],)
        )
        steps = cur.fetchall()
    assert saved["user_role"] == "user"
    assert saved["runtime_context"]["runtime_id"] == name
    if outcome in {"success", "duplicate-proposal"}:
        assert result.verified and saved["status"] == "completed"
        assert p.effects == [1] and len(p.calls) == 1
        assert steps == [("record", None)]
        assert len(p.audits) == 1 and p.audits[0]["status"] == "ok"
    elif outcome.startswith("lost-step-ack"):
        assert not result.verified and result.unresolved and saved["status"] == "failed"
        assert p.effects == [1] and len(p.calls) == 1
        assert steps == [("record", None)]  # committed despite the uncertain acknowledgement
    elif outcome.startswith("lost-provider-reply"):
        assert p.effects == [1] and len(p.calls) == 1
        assert len(steps) == 1 and steps[0][1]
        assert result.verified is p.policy["receipt_visible"]
        assert result.unresolved is not p.policy["receipt_visible"]
        assert saved["status"] == ("completed" if p.policy["receipt_visible"] else "failed")
    else:
        assert not result.verified and result.unresolved and not p.effects
        assert saved["status"] == ("cancelled" if outcome == "stop" else "failed")
        if outcome == "stop":
            assert not steps and len(p.calls) == 1
        else:
            assert 1 <= len(steps) <= 4 and all(error for _, error in steps)
        if outcome == "permission":
            assert p.permissions and all(a["status"] == "denied" for a in p.audits)
        else:
            assert not p.permissions
