"""One structured log line per tool call, and never its arguments or result.

What an agent did has to be reconstructable from the engine's logs alone: the
tool, who called it (an agent, or a workflow and its run), the outcome, a
short reason when it was not ``ok``, and how long it took. Every path a call
can take ends in exactly one ``event=tool_call`` line: a handler that answers,
one that raises, the RBAC gate, an admission gate (plan mode, human approval),
an unknown tool, a timeout, a workflow ``tool`` step, a proxied call from
``execute_code``, and ``tool_call`` wrapping another tool. The arguments and
the result can hold anything a person typed, so they never reach the line.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.tools import dispatch
from robothor.engine.tools import registry as registry_mod
from robothor.engine.tools.registry import ToolRegistry

LOGGER = "robothor.engine.tools.call_log"
AGENT = "example-agent"
TOOL = "example_tool"
FIELDS = ("event", "tool", "run_id", "outcome", "reason", "duration_ms")


class _NullWriter:
    """The audit event writer, kept off the database."""

    def submit(self, *args: Any, **kwargs: Any) -> None:
        return None


def _registry() -> ToolRegistry:
    reg = ToolRegistry.__new__(ToolRegistry)
    reg._adapter_routes = {}  # type: ignore[attr-defined]
    return reg


@pytest.fixture
def handlers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The real dispatch path, with the handler table and the database stubbed."""
    table: dict[str, Any] = {}
    reg = _registry()
    monkeypatch.setattr(registry_mod, "_registry", reg)
    monkeypatch.setattr("robothor.engine.tools.get_registry", lambda: reg)
    monkeypatch.setattr(dispatch, "_get_handlers", lambda: table)
    monkeypatch.setattr("robothor.engine.permissions.check_tool_permission", lambda *a, **k: None)
    monkeypatch.setattr(dispatch, "_runtime_denial", AsyncMock(return_value=None))
    monkeypatch.setattr("robothor.engine.event_writer.get_event_writer", lambda: _NullWriter())
    monkeypatch.setattr("robothor.engine.repeat_guard.guard_for_run", lambda run: None)
    return table


@pytest.fixture
def lines(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Every ``tool_call`` record, live: read it after the call."""
    caplog.set_level(logging.INFO)

    class _View:
        def _all(self) -> list[Any]:
            return [r for r in caplog.records if getattr(r, "event", None) == "tool_call"]

        def __iter__(self) -> Any:
            return iter(self._all())

        def __len__(self) -> int:
            return len(self._all())

        def __getitem__(self, i: int) -> Any:
            return self._all()[i]

    return _View()  # type: ignore[return-value]


async def _call(
    name: str = TOOL,
    args: dict[str, Any] | None = None,
    *,
    timeout: float = 0,
    agent_id: str = AGENT,
    user_role: str = "member",
) -> dict[str, Any]:
    return await ToolRegistry.execute(
        _registry(),
        name,
        args or {},
        agent_id=agent_id,
        run_id="run-1",
        tenant_id="test-tenant",
        user_id="user-1",
        user_role=user_role,
        timeout=timeout,  # type: ignore[arg-type]
    )


def _ok(result: dict[str, Any] | None = None) -> Any:
    async def handler(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        return result if result is not None else {"ok": True}

    return handler


# ── 1. success ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_successful_call_logs_exactly_one_ok_line(handlers, lines) -> None:
    handlers[TOOL] = _ok()

    assert await _call() == {"ok": True}

    assert len(lines) == 1
    line = lines[0]
    assert line.levelno == logging.INFO
    assert line.name == LOGGER
    assert (line.tool, line.agent, line.run_id, line.outcome) == (TOOL, AGENT, "run-1", "ok")
    assert isinstance(line.duration_ms, int) and line.duration_ms >= 0
    assert line.reason == ""
    assert not hasattr(line, "workflow")


@pytest.mark.asyncio
async def test_the_message_carries_the_fields_as_key_value_pairs(handlers, lines) -> None:
    handlers[TOOL] = _ok()

    await _call()

    message = lines[0].getMessage()
    assert message.startswith("event=tool_call ")
    for part in (f"tool={TOOL}", f"agent={AGENT}", "run_id=run-1", "outcome=ok", "duration_ms="):
        assert part in message


# ── 2. the tool raises ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_tool_that_raises_logs_an_error_line_with_the_error_class(handlers, lines) -> None:
    async def broken(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise ValueError("the backing store said no")

    handlers[TOOL] = broken

    result = await _call()

    assert "error" in result
    assert len(lines) == 1
    assert lines[0].outcome == "error"
    assert "ValueError" in lines[0].reason


@pytest.mark.asyncio
async def test_an_unknown_tool_logs_an_error_line(handlers, lines) -> None:
    await _call("no_such_tool")

    assert len(lines) == 1
    assert (lines[0].tool, lines[0].outcome) == ("no_such_tool", "error")
    assert "Unknown tool" in lines[0].reason


@pytest.mark.asyncio
async def test_a_timeout_logs_an_error_line_naming_the_timeout(handlers, lines) -> None:
    async def slow(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"ok": True}

    handlers[TOOL] = slow

    result = await _call(timeout=0.05)

    assert "timed out" in result["error"]
    assert len(lines) == 1
    assert lines[0].outcome == "error"
    assert "TimeoutError" in lines[0].reason


# ── 3. refused by permissions ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_call_refused_by_permissions_logs_a_refused_line(
    handlers, lines, monkeypatch
) -> None:
    handlers[TOOL] = _ok()
    monkeypatch.setattr(
        "robothor.engine.permissions.check_tool_permission",
        lambda *a, **k: f"Role 'viewer' denied '{TOOL}'",
    )

    result = await _call(user_role="viewer")

    assert result.get("refused") is True
    assert len(lines) == 1
    assert lines[0].outcome == "refused"
    assert f"Role 'viewer' denied '{TOOL}'" in lines[0].reason


@pytest.mark.asyncio
async def test_a_runtime_denial_logs_a_refused_line(handlers, lines, monkeypatch) -> None:
    """A durable stop or goal admission refuses before the handler runs."""
    handlers[TOOL] = _ok()

    async def stopped(name: str, args: dict[str, Any], ctx: Any, **_: Any) -> dict[str, Any]:
        dispatch._audit_tool_call(
            name, ctx.agent_id, ctx.tenant_id, status="denied", error="durable stop"
        )
        return {"error": "durable stop denies further tool dispatch"}

    monkeypatch.setattr(dispatch, "_runtime_denial", stopped)

    await _call()

    assert len(lines) == 1
    assert lines[0].outcome == "refused"


# ── 4. a write recorded as "would have done" ──────────────────────────


@pytest.mark.asyncio
async def test_a_write_with_no_write_connection_logs_ok_would_have_done(handlers, lines) -> None:
    writes: list[dict[str, Any]] = []
    connection = None  # no write connection configured

    async def write_tool(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        if connection is None:
            return {"would_have_done": True, "would": {"tool": TOOL}}
        writes.append(args)
        return {"ok": True}

    handlers[TOOL] = write_tool

    await _call(args={"note": "hello"})

    assert writes == []
    assert len(lines) == 1
    assert lines[0].outcome == "ok"
    assert lines[0].reason == "no write connection: would have done"


# ── 5. arguments and results never reach the log ──────────────────────


@pytest.mark.asyncio
async def test_neither_arguments_nor_results_appear_in_the_log(handlers, lines, caplog) -> None:
    handlers[TOOL] = _ok({"value": "result-value-456"})

    await _call(args={"note": "secret-value-123"})

    assert len(lines) == 1
    assert "secret-value-123" not in caplog.text
    assert "result-value-456" not in caplog.text
    for value in vars(lines[0]).values():
        assert "secret-value-123" not in str(value)
        assert "result-value-456" not in str(value)


@pytest.mark.asyncio
async def test_an_error_that_quotes_an_argument_does_not_carry_it_into_the_line(
    handlers, lines
) -> None:
    async def quoting(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        raise ValueError(f"cannot use {args['note']}")

    handlers[TOOL] = quoting

    await _call(args={"note": "secret-value-123"})

    assert len(lines) == 1
    assert "ValueError" in lines[0].reason
    assert "secret-value-123" not in lines[0].getMessage()
    assert "secret-value-123" not in lines[0].reason


# ── 6. workflow steps ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_workflow_tool_step_logs_the_workflow_and_its_run_id(
    engine_config, lines, monkeypatch
) -> None:
    from robothor.engine.models import (
        WorkflowRun,
        WorkflowStepDef,
        WorkflowStepResult,
        WorkflowStepType,
    )
    from robothor.engine.workflow import WorkflowEngine

    async def fake_execute_tool(name: str, args: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    monkeypatch.setattr(registry_mod, "_execute_tool", fake_execute_tool)
    engine = WorkflowEngine(engine_config, runner=SimpleNamespace(registry=_registry()))
    step = WorkflowStepDef(id="s1", type=WorkflowStepType.TOOL, tool_name=TOOL)
    run = WorkflowRun(workflow_id="example-workflow")

    await engine._run_tool_step(step, run, WorkflowStepResult(step_id="s1"))

    assert len(lines) == 1
    line = lines[0]
    assert (line.tool, line.workflow, line.run_id, line.outcome) == (
        TOOL,
        "example-workflow",
        run.id,
        "ok",
    )
    assert not hasattr(line, "agent")


@pytest.mark.asyncio
async def test_a_workflow_step_blocked_by_a_guardrail_logs_a_refused_line(
    engine_config, lines
) -> None:
    from robothor.engine.guardrails import GuardrailResult
    from robothor.engine.models import (
        WorkflowRun,
        WorkflowStepDef,
        WorkflowStepResult,
        WorkflowStepType,
    )
    from robothor.engine.workflow import WorkflowEngine

    registry = MagicMock()
    registry.execute = AsyncMock(return_value={"ok": True})
    engine = WorkflowEngine(engine_config, runner=SimpleNamespace(registry=registry))
    blocking = MagicMock()
    blocking.check_pre_execution.return_value = GuardrailResult(
        allowed=False, action="blocked", reason="not on this path", guardrail_name="example"
    )
    engine._workflow_guardrails = lambda *a, **k: blocking  # type: ignore[method-assign]
    step = WorkflowStepDef(id="s1", type=WorkflowStepType.TOOL, tool_name=TOOL)
    run = WorkflowRun(workflow_id="example-workflow")

    await engine._run_tool_step(step, run, WorkflowStepResult(step_id="s1"))

    registry.execute.assert_not_awaited()
    assert len(lines) == 1
    assert (lines[0].workflow, lines[0].run_id, lines[0].outcome) == (
        "example-workflow",
        run.id,
        "refused",
    )
    assert "not on this path" in lines[0].reason


# ── 7. retries: one line per attempt ──────────────────────────────────


@pytest.mark.asyncio
async def test_each_attempt_of_a_retried_call_logs_its_own_line(handlers, lines) -> None:
    attempts: list[int] = []

    async def flaky(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("first attempt lost")
        return {"ok": True}

    handlers[TOOL] = flaky

    await _call()
    await _call()

    assert [line.outcome for line in lines] == ["error", "ok"]


@pytest.mark.asyncio
async def test_tool_call_wrapping_a_tool_logs_one_line_for_the_wrapped_tool(
    handlers, lines
) -> None:
    from robothor.engine.tools.dispatch import clear_deferred_allowed, set_deferred_allowed
    from robothor.engine.tools.handlers.toolsearch import _tool_call

    handlers[TOOL] = _ok()
    handlers["tool_call"] = _tool_call
    token = set_deferred_allowed(frozenset({TOOL}))
    try:
        result = await _call("tool_call", {"name": TOOL, "arguments": {}})
    finally:
        clear_deferred_allowed(token)

    assert result == {"ok": True}
    assert [(line.tool, line.outcome) for line in lines] == [(TOOL, "ok")]


@pytest.mark.asyncio
async def test_tool_call_refusing_its_wrapped_tool_logs_one_refused_line(
    handlers, lines, monkeypatch
) -> None:
    from robothor.engine.tools.dispatch import clear_deferred_allowed, set_deferred_allowed
    from robothor.engine.tools.handlers import toolsearch
    from robothor.engine.tools.handlers.toolsearch import _tool_call

    monkeypatch.setattr(toolsearch, "_closest", lambda *a, **k: [])

    handlers[TOOL] = _ok()
    handlers["tool_call"] = _tool_call
    token = set_deferred_allowed(frozenset({"some_other_tool"}))
    try:
        await _call("tool_call", {"name": TOOL, "arguments": {}})
    finally:
        clear_deferred_allowed(token)

    assert [(line.tool, line.outcome) for line in lines] == [(TOOL, "refused")]


# ── 8. a logging failure never breaks the call ────────────────────────


@pytest.mark.asyncio
async def test_a_logger_that_raises_does_not_break_the_call(handlers, caplog) -> None:
    from robothor.engine.tools import call_log

    caplog.set_level(logging.INFO)
    handlers[TOOL] = _ok()

    with patch.object(call_log.logger, "info", side_effect=RuntimeError("log sink down")):
        result = await _call()

    assert result == {"ok": True}
    errors = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.ERROR]
    assert len(errors) == 1


# ── admission gates (agent turns and proxied calls) ───────────────────


class _Run:
    def __init__(self) -> None:
        self.id = "run-1"
        self.steps: list[Any] = []
        self.tenant_id = "test-tenant"
        self.trigger_type = "manual"


class _Session:
    def __init__(self) -> None:
        self.run = _Run()
        self.run_id = "run-1"


class _AgentConfig:
    id = AGENT
    service_role = "service"
    human_approval_timeout = 1.0
    human_approval_fail_open = False


async def _admit(*, readonly_mode: bool = False, guardrail_engine: Any = None) -> Any:
    from robothor.engine.tool_admission import ToolAdmissionMixin

    class _Admitter(ToolAdmissionMixin):
        pass

    return await _Admitter()._admit_tool_call(
        tc=MagicMock(id="call_1"),
        tool_name=TOOL,
        tool_args={"note": "secret-value-123"},
        session=_Session(),
        agent_config=_AgentConfig(),
        guardrail_engine=guardrail_engine,
        hook_registry=None,
        readonly_mode=readonly_mode,
        readonly_tool_set=frozenset(),
        allowed_tool_set=frozenset(),
    )


@pytest.mark.asyncio
async def test_a_plan_mode_refusal_at_admission_logs_a_refused_line(lines) -> None:
    verdict = await _admit(readonly_mode=True)

    assert verdict.allowed is False
    assert len(lines) == 1
    line = lines[0]
    assert (line.tool, line.agent, line.run_id, line.outcome) == (TOOL, AGENT, "run-1", "refused")
    assert "plan mode" in line.reason
    assert "secret-value-123" not in line.getMessage()


@pytest.mark.asyncio
async def test_an_approval_the_operator_refuses_logs_a_refused_line(lines, monkeypatch) -> None:
    from robothor.engine import tool_admission
    from robothor.engine.guardrails import GuardrailEngine

    monkeypatch.setattr(tool_admission, "_log_guardrail_event", lambda **k: None)
    monkeypatch.setattr("robothor.engine.tracking.log_tool_event", lambda **_k: None)

    class _Manager:
        @staticmethod
        async def request_approval(**kwargs: Any) -> bool:
            return False

    monkeypatch.setattr(
        "robothor.engine.permission_escalation.get_permission_manager", lambda: _Manager()
    )
    engine = GuardrailEngine(enabled_policies=["human_approval"])
    engine.set_human_approval_patterns(AGENT, [TOOL])

    verdict = await _admit(guardrail_engine=engine)

    assert verdict.allowed is False
    assert len(lines) == 1
    assert lines[0].outcome == "refused"
    assert "Denied by operator" in lines[0].reason


@pytest.mark.asyncio
async def test_an_admitted_call_logs_nothing_at_admission(lines) -> None:
    verdict = await _admit()

    assert verdict.allowed is True
    assert len(lines) == 0


@pytest.mark.asyncio
async def test_a_proxied_call_over_the_snippet_cap_logs_a_refused_line(
    engine_config, lines
) -> None:
    from robothor.engine.models import AgentConfig, DeliveryMode
    from robothor.engine.runner import AgentRunner
    from robothor.engine.session import AgentSession
    from robothor.engine.tool_proxy import RunToolProxy
    from robothor.engine.tool_turn import ToolTurnRequest

    with patch("robothor.engine.runner.get_registry") as mock_reg:
        registry = MagicMock()
        registry.execute = AsyncMock(return_value={"ok": True})
        mock_reg.return_value = registry
        runner = AgentRunner(engine_config)
        runner.registry = registry
    config = AgentConfig(
        id=AGENT,
        name="Example Agent",
        model_primary="openrouter/test/model",
        model_fallbacks=[],
        delivery_mode=DeliveryMode.NONE,
    )
    req = ToolTurnRequest(
        assistant_msg=MagicMock(tool_calls=[]),
        session=AgentSession(agent_id=AGENT),
        agent_config=config,
        iteration=0,
        allowed_tool_set=frozenset({TOOL}),
    )
    proxy = RunToolProxy(runner=runner, req=req, allowed=frozenset({TOOL}), max_calls=0)

    result = await proxy.call(TOOL, {"note": "secret-value-123"})

    assert "limit reached" in result["error"]
    registry.execute.assert_not_awaited()
    assert len(lines) == 1
    assert (lines[0].tool, lines[0].agent, lines[0].outcome) == (TOOL, AGENT, "refused")


def test_every_line_has_the_documented_fields() -> None:
    """The field names are a contract with whoever queries the logs."""
    from robothor.engine.tools import call_log

    assert set(FIELDS) <= set(call_log.FIELDS)
    assert call_log.OUTCOMES == ("ok", "error", "refused", "deferred")


@pytest.mark.asyncio
async def test_an_action_whose_outcome_is_pending_logs_a_deferred_line(handlers, lines) -> None:
    """An effect still awaiting reconciliation has not failed and has not succeeded."""
    handlers[TOOL] = _ok(
        {"error": "effect pending reconciliation", "outcome_unknown": True, "retryable": False}
    )

    await _call()

    assert len(lines) == 1
    assert lines[0].outcome == "deferred"
    assert "pending" in lines[0].reason
