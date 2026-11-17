"""One structured log line per tool call.

Every tool call — by an agent, through ``tool_call``, or by a workflow step —
passes through ``ToolRegistry.execute``. Each one writes exactly one
``event=tool_call`` line naming the tool, the agent or workflow, the run, the
outcome, an error type, a short redacted reason, and the duration. A call
refused at admission (before it reaches the registry) writes the same line.

No tool argument and no result body is ever in the line.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import pytest

from robothor.engine.tools.registry import ToolRegistry

LINE_LOGGER = "robothor.engine.tool_calls"


def _fields(line: str) -> dict[str, str]:
    """key=value pairs; a value is bare or double-quoted with \\" escapes."""
    pairs = re.findall(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)', line)
    return {k: (v[1:-1].replace('\\"', '"') if v.startswith('"') else v) for k, v in pairs}


def _lines(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == LINE_LOGGER and "event=tool_call" in r.getMessage()
    ]


def _registry() -> ToolRegistry:
    return ToolRegistry.__new__(ToolRegistry)


def _fake(result: Any):
    async def _execute_tool(name: str, args: dict[str, Any], **kwargs: Any) -> Any:
        if isinstance(result, BaseException):
            raise result
        return result

    return _execute_tool


async def _call(monkeypatch, result: Any, *, tool="example_tool", args=None, **kw) -> Any:
    monkeypatch.setattr("robothor.engine.tools.registry._execute_tool", _fake(result))
    kw.setdefault("agent_id", "example-agent")
    kw.setdefault("run_id", "run-1")
    kw.setdefault("timeout", 0)
    return await ToolRegistry.execute(_registry(), tool, args or {}, **kw)


@pytest.fixture(autouse=True)
def _capture(caplog):
    caplog.set_level(logging.DEBUG)


@pytest.mark.asyncio
async def test_a_successful_call_writes_one_info_line(monkeypatch, caplog) -> None:
    await _call(monkeypatch, {"ok": True})
    lines = _lines(caplog)
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO
    f = _fields(lines[0].getMessage())
    assert f["tool"] == "example_tool"
    assert f["agent"] == "example-agent"
    assert f["run_id"] == "run-1"
    assert f["outcome"] == "ok"
    assert int(f["duration_ms"]) >= 0
    assert "workflow" not in f


@pytest.mark.asyncio
async def test_a_tool_error_writes_one_warning_with_type_and_reason(monkeypatch, caplog) -> None:
    await _call(monkeypatch, {"error": "connection refused by the remote service"})
    (line,) = _lines(caplog)
    assert line.levelno == logging.WARNING
    f = _fields(line.getMessage())
    assert f["outcome"] == "error"
    assert f["error_type"]
    assert "connection refused" in f["reason"]


@pytest.mark.asyncio
async def test_a_raised_exception_names_its_class(monkeypatch, caplog) -> None:
    class ExampleBoomError(RuntimeError):
        pass

    await _call(monkeypatch, ExampleBoomError("it broke"))
    (line,) = _lines(caplog)
    f = _fields(line.getMessage())
    assert f["outcome"] == "error"
    assert f["error_type"] == "ExampleBoomError"


@pytest.mark.asyncio
async def test_a_role_refusal_is_denied(monkeypatch, caplog) -> None:
    from robothor.engine.refusal import refusal

    await _call(
        monkeypatch,
        refusal("role 'viewer' may not use example_tool", gate="role", name="rbac", unlock="x"),
    )
    (line,) = _lines(caplog)
    assert line.levelno == logging.WARNING
    f = _fields(line.getMessage())
    assert f["outcome"] == "denied"
    assert f["error_type"] == "role.rbac"
    assert "may not use" in f["reason"]


@pytest.mark.asyncio
async def test_a_policy_refusal_is_refused(monkeypatch, caplog) -> None:
    from robothor.engine.refusal import refusal

    await _call(
        monkeypatch, refusal("deferred on this run", gate="policy", name="deferred", unlock="x")
    )
    f = _fields(_lines(caplog)[0].getMessage())
    assert f["outcome"] == "refused"
    assert f["error_type"] == "policy.deferred"


@pytest.mark.asyncio
async def test_a_timeout_is_timeout(monkeypatch, caplog) -> None:
    async def _slow(name: str, args: dict[str, Any], **kwargs: Any) -> Any:
        await asyncio.sleep(5)
        return {"ok": True}

    monkeypatch.setattr("robothor.engine.tools.registry._execute_tool", _slow)
    await ToolRegistry.execute(_registry(), "example_tool", {}, agent_id="example-agent", timeout=1)
    lines = _lines(caplog)
    assert len(lines) == 1
    f = _fields(lines[0].getMessage())
    assert f["outcome"] == "timeout"
    assert int(f["duration_ms"]) >= 900


@pytest.mark.asyncio
async def test_no_argument_or_result_value_reaches_the_log(monkeypatch, caplog) -> None:
    await _call(
        monkeypatch,
        {"data": "result-value-456"},
        args={"note": "secret-value-123"},
    )
    await _call(
        monkeypatch,
        {"error": "failed", "detail": "result-value-456"},
        args={"note": "secret-value-123"},
    )
    text = caplog.text
    assert "secret-value-123" not in text
    assert "result-value-456" not in text


@pytest.mark.asyncio
async def test_the_reason_is_redacted(monkeypatch, caplog) -> None:
    await _call(monkeypatch, {"error": "login failed: password=hunter2hunter2hunter2"})
    assert "hunter2hunter2hunter2" not in caplog.text


@pytest.mark.asyncio
async def test_a_huge_error_is_truncated_before_it_is_redacted(monkeypatch, caplog) -> None:
    import robothor.secrets.redaction as redaction

    seen: list[int] = []
    real = redaction.redact

    def _spy(text: str, *a: Any, **k: Any) -> str:
        seen.append(len(text))
        return real(text, *a, **k)

    monkeypatch.setattr(redaction, "redact", _spy)
    await _call(monkeypatch, {"error": "x" * 5_000_000})
    f = _fields(_lines(caplog)[0].getMessage())
    assert len(f["reason"]) <= 300
    assert seen and max(seen) <= 8192


@pytest.mark.asyncio
async def test_a_workflow_call_names_the_workflow(monkeypatch, caplog) -> None:
    await _call(monkeypatch, {"ok": True}, agent_id="workflow:example-flow", run_id="")
    f = _fields(_lines(caplog)[0].getMessage())
    assert f["workflow"] == "example-flow"
    assert "agent" not in f


@pytest.mark.asyncio
async def test_two_attempts_write_two_lines(monkeypatch, caplog) -> None:
    await _call(monkeypatch, {"error": "temporarily unavailable"})
    await _call(monkeypatch, {"ok": True})
    outcomes = [_fields(r.getMessage())["outcome"] for r in _lines(caplog)]
    assert outcomes == ["error", "ok"]


@pytest.mark.asyncio
async def test_a_logging_failure_never_fails_the_call(monkeypatch, caplog) -> None:
    def _broken(*a: Any, **k: Any) -> None:
        raise RuntimeError("log sink is down")

    monkeypatch.setattr(logging.getLogger(LINE_LOGGER), "log", _broken)
    result = await _call(monkeypatch, {"ok": True})
    assert result == {"ok": True}
    assert any(
        r.levelno == logging.ERROR and "tool_call log line" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_a_wrapped_call_writes_one_line_for_the_tool_it_wraps(monkeypatch, caplog) -> None:
    registry = _registry()

    async def _dispatch(name: str, args: dict[str, Any], **kwargs: Any) -> Any:
        if name == "tool_call":
            return await ToolRegistry.execute(
                registry, args["name"], args.get("arguments") or {}, timeout=0, **kwargs
            )
        return {"ok": True}

    monkeypatch.setattr("robothor.engine.tools.registry._execute_tool", _dispatch)
    await ToolRegistry.execute(
        registry,
        "tool_call",
        {"name": "example_tool", "arguments": {}},
        agent_id="example-agent",
        run_id="run-1",
        timeout=0,
    )
    lines = _lines(caplog)
    assert len(lines) == 1
    assert _fields(lines[0].getMessage())["tool"] == "example_tool"


@pytest.mark.asyncio
async def test_a_wrapper_refusal_is_logged_under_the_wrapped_tool(monkeypatch, caplog) -> None:
    from robothor.engine.refusal import refusal

    await _call(
        monkeypatch,
        refusal("not in your allow-list", gate="allowlist", name="tools_allowed", unlock="x"),
        tool="tool_call",
        args={"name": "example_tool", "arguments": {}},
    )
    (line,) = _lines(caplog)
    f = _fields(line.getMessage())
    assert f["tool"] == "example_tool"
    assert f["outcome"] == "denied"


def test_a_call_refused_at_admission_writes_one_line(caplog) -> None:
    from types import SimpleNamespace

    from robothor.engine.tool_admission import ToolAdmissionMixin
    from robothor.engine.tool_verdict import ToolVerdict

    run = SimpleNamespace(id="run-1", agent_id="example-agent")
    session = SimpleNamespace(run=run, record_tool_call=lambda **k: None)
    verdict = ToolVerdict(
        allowed=False,
        message="Blocked by guardrail (example_guard): not allowed now",
        output={
            "guardrail": "example_guard",
            "refused": True,
            "gate": "policy",
            "gate_name": "example_guard",
        },
        gate="policy",
    )
    ToolAdmissionMixin._record_refusal(
        verdict,
        tc=SimpleNamespace(id="c1"),
        tool_name="example_tool",
        session=session,
        scratchpad=None,
        escalation=None,
        iteration_errors=[],
    )
    (line,) = _lines(caplog)
    f = _fields(line.getMessage())
    assert f["tool"] == "example_tool"
    assert f["agent"] == "example-agent"
    assert f["outcome"] == "refused"
    assert f["error_type"] == "policy.example_guard"


def test_the_audit_row_bounds_an_error_before_redacting_it(monkeypatch) -> None:
    """The audit row kept its 500 characters, but redacted the whole text first."""
    import robothor.secrets.redaction as redaction
    from robothor.engine.tools import dispatch

    seen: list[int] = []
    real = redaction.redact

    def _spy(text: str, *a: Any, **k: Any) -> str:
        seen.append(len(text))
        return real(text, *a, **k)

    submitted: list[dict[str, Any]] = []

    class _Writer:
        def submit(self, fn: Any, **kw: Any) -> None:
            submitted.append(kw)

    monkeypatch.setattr(redaction, "redact", _spy)
    monkeypatch.setattr("robothor.engine.event_writer.get_event_writer", lambda: _Writer())
    dispatch._audit_tool_call(
        "example_tool", "example-agent", "test-tenant", status="denied", error="y" * 5_000_000
    )
    assert seen and max(seen) <= 8192
    assert len(submitted[0]["details"]["error"]) <= 500


@pytest.mark.asyncio
async def test_a_classification_failure_never_fails_the_call(monkeypatch, caplog) -> None:
    from robothor.engine import tool_call_log

    def _broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("classifier broke")

    monkeypatch.setattr(tool_call_log, "classify_result", _broken)
    result = await _call(monkeypatch, {"error": "x"})
    assert result == {"error": "x"}
    assert len(_lines(caplog)) == 1
