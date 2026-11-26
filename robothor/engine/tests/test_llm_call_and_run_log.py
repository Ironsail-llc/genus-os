"""One structured log line per LLM call, and one per finished run.

"It is slow" could not be broken down from the logs: no line said how long a
model call took, how many tokens it moved, or how a run ended. Each provider
attempt now writes ``event=llm_call`` and every run, of any trigger, writes
``event=run_end`` from ``_finish_run``, the one door every exit goes through.
Neither line carries prompt text, tool arguments or personal data.
"""

from __future__ import annotations

import logging
import re
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import llm_client
from robothor.engine.config import EngineConfig
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker
from robothor.engine.models import AgentRun, RunStatus, RunStep, StepType, TriggerType
from robothor.engine.run_llm_calls import LLMCallMixin
from robothor.engine.runner import AgentRunner
from robothor.engine.session import AgentSession

PROMPT = "please summarize the note for Alice at agent@example.com"


def _fields(line: str) -> dict[str, str]:
    pairs = re.findall(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)', line)
    return {k: (v[1:-1] if v.startswith('"') else v) for k, v in pairs}


def _events(caplog, event: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if f"event={event} " in r.getMessage()]


def _response(*, cached: int | None = 300) -> Any:
    message = SimpleNamespace(content="an answer", tool_calls=None)
    usage = SimpleNamespace(
        prompt_tokens=1200,
        completion_tokens=40,
        completion_tokens_details=SimpleNamespace(reasoning_tokens=None),
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], usage=usage, model="openrouter/primary")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, caplog):
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
    fresh = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: fresh)
    caplog.set_level(logging.DEBUG)


class _Runner(LLMCallMixin):
    def __init__(self) -> None:
        self._llm = LLMClient()
        self.config = SimpleNamespace()

    @property
    def _active_watchdog(self) -> Any:
        return None

    def _response_cost(self, **_kwargs: Any) -> float:
        return 0.0


def _session() -> AgentSession:
    session = AgentSession(agent_id="example-agent")
    session.run.agent_id = "example-agent"
    session.messages = [{"role": "user", "content": PROMPT}]
    return session


async def _call(models: list[str], side_effect: list[Any], **kw: Any) -> AgentSession:
    session = _session()
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(side_effect=side_effect),
        ),
    ):
        await _Runner()._llm_call_and_record(session, models, [], None, set(), 0.3, **kw)
    return session


class TestLLMCallLine:
    @pytest.mark.asyncio
    async def test_a_clean_call_writes_one_info_line(self, caplog) -> None:
        session = await _call(["openrouter/primary"], [_response()])
        (line,) = _events(caplog, "llm_call")
        assert line.levelno == logging.INFO
        f = _fields(line.getMessage())
        assert f["agent"] == "example-agent"
        assert f["run_id"] == session.run.id
        assert f["model"] == "openrouter/primary"
        assert f["outcome"] == "success"
        assert f["input_tokens"] == "1200"
        assert f["output_tokens"] == "40"
        assert f["cached_tokens"] == "300"
        assert int(f["latency_ms"]) >= 0
        assert f["streamed"] == "false"
        assert f["attempt"] == "1"
        assert f["fallback"] == "0"

    @pytest.mark.asyncio
    async def test_a_retry_writes_one_line_per_attempt(self, caplog) -> None:
        err = Exception("HTTP 502")
        err.status_code = 502  # type: ignore[attr-defined]
        await _call(["openrouter/primary"], [err, _response()])
        first, second = (_fields(r.getMessage()) for r in _events(caplog, "llm_call"))
        assert first["outcome"] == "error_502"
        assert first["error_type"] == "Exception"
        assert (first["attempt"], second["attempt"]) == ("1", "2")
        assert second["outcome"] == "success"

    @pytest.mark.asyncio
    async def test_a_fallback_names_its_position(self, caplog) -> None:
        err = Exception("forbidden")
        err.status_code = 403  # type: ignore[attr-defined]
        await _call(["openrouter/primary", "openrouter/secondary"], [err, _response()])
        lines = [_fields(r.getMessage()) for r in _events(caplog, "llm_call")]
        assert lines[0]["model"] == "openrouter/primary"
        assert lines[0]["fallback"] == "0"
        assert lines[-1]["model"] == "openrouter/secondary"
        assert lines[-1]["fallback"] == "1"
        assert lines[-1]["outcome"] == "success"
        assert [r.levelno for r in _events(caplog, "llm_call")][0] == logging.WARNING

    @pytest.mark.asyncio
    async def test_a_streamed_call_says_so(self, caplog) -> None:
        from robothor.engine.llm_attempts import describe_completion, note_outcome

        async def _streaming(self: Any, *a: Any, **k: Any) -> Any:
            response = _response()
            note_outcome(
                "openrouter/primary", time.monotonic(), shape=describe_completion(response)
            )
            return response

        async def _on_content(_text: str) -> None:
            return None

        session = _session()
        with (
            patch.object(LLMClient, "_call_llm_streaming", new=_streaming),
        ):
            await _Runner()._llm_call_and_record(
                session, ["openrouter/primary"], [], _on_content, set(), 0.3
            )
        (line,) = _events(caplog, "llm_call")
        assert _fields(line.getMessage())["streamed"] == "true"

    @pytest.mark.asyncio
    async def test_no_prompt_text_reaches_the_line(self, caplog) -> None:
        await _call(["openrouter/primary"], [_response()])
        text = " ".join(r.getMessage() for r in _events(caplog, "llm_call"))
        assert "Alice" not in text
        assert "agent@example.com" not in text


def _finished(status: RunStatus, **kw: Any) -> AgentRun:
    run = AgentRun(
        id="00000000-0000-0000-0000-0000000000r1",
        agent_id="example-agent",
        trigger_type=TriggerType.TELEGRAM,
        status=status,
        duration_ms=4321,
        input_tokens=1000,
        output_tokens=200,
        **kw,
    )
    run.steps = [
        RunStep(step_type=StepType.LLM_CALL),
        RunStep(step_type=StepType.TOOL_CALL),
        RunStep(step_type=StepType.LLM_CALL),
        RunStep(step_type=StepType.LLM_CALL, error_message="error_502: HTTP 502"),
    ]
    return run


def _finish(run: AgentRun) -> None:
    runner = AgentRunner(EngineConfig())
    with (
        patch.object(AgentRunner, "_persist_run_sync"),
        patch.object(AgentRunner, "_persist_run"),
    ):
        runner._finish_run(run)


class TestRunEndLine:
    @pytest.mark.parametrize(
        ("status", "extra", "outcome", "level"),
        [
            (RunStatus.COMPLETED, {}, "completed", logging.INFO),
            (RunStatus.FAILED, {"error_message": "boom"}, "error", logging.WARNING),
            (RunStatus.TIMEOUT, {}, "timeout", logging.WARNING),
            (RunStatus.CANCELLED, {}, "cancelled", logging.WARNING),
            (RunStatus.COMPLETED, {"budget_exhausted": True}, "cap", logging.WARNING),
        ],
    )
    def test_every_exit_writes_one_line(self, caplog, status, extra, outcome, level) -> None:
        _finish(_finished(status, **extra))
        (line,) = _events(caplog, "run_end")
        assert line.levelno == level
        f = _fields(line.getMessage())
        assert f["agent"] == "example-agent"
        assert f["run_id"] == "00000000-0000-0000-0000-0000000000r1"
        assert f["trigger"] == "telegram"
        assert f["duration_ms"] == "4321"
        assert f["iterations"] == "2"
        assert f["total_tokens"] == "1200"
        assert f["outcome"] == outcome

    def test_no_output_or_error_text_reaches_the_line(self, caplog) -> None:
        _finish(_finished(RunStatus.FAILED, error_message="failed for Alice", output_text=PROMPT))
        (line,) = _events(caplog, "run_end")
        assert "Alice" not in line.getMessage()


TENANT = "acme-child-a"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"


async def _call_in(tenant: str, trace_id: str) -> None:
    session = _session()
    session.run.tenant_id = tenant
    session.trace_id = trace_id
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(return_value=_response()),
        ),
    ):
        await _Runner()._llm_call_and_record(session, ["openrouter/primary"], [], None, set(), 0.3)


class TestTenantAndTrace:
    @pytest.mark.asyncio
    async def test_the_llm_call_line_names_the_tenant_and_trace_last(self, caplog) -> None:
        await _call_in(TENANT, TRACE_ID)
        (line,) = _events(caplog, "llm_call")
        f = _fields(line.getMessage())
        assert f["tenant_id"] == TENANT
        assert f["trace_id"] == TRACE_ID
        assert list(f)[-2:] == ["tenant_id", "trace_id"]

    @pytest.mark.asyncio
    async def test_an_llm_call_with_no_trace_omits_the_key(self, caplog) -> None:
        await _call_in(TENANT, "")
        f = _fields(_events(caplog, "llm_call")[0].getMessage())
        assert f["tenant_id"] == TENANT
        assert "trace_id" not in f

    def test_the_run_end_line_names_the_tenant_and_trace_last(self, caplog) -> None:
        from robothor.engine.telemetry import TraceContext

        run = _finished(RunStatus.COMPLETED, tenant_id=TENANT)
        trace = TraceContext(trace_id=TRACE_ID, run_id=run.id, agent_id=run.agent_id)
        runner = AgentRunner(EngineConfig())
        with (
            patch.object(AgentRunner, "_persist_run_sync"),
            patch.object(AgentRunner, "_persist_run"),
            patch.object(TraceContext, "publish_metrics"),
        ):
            runner._finish_run(run, trace)
        f = _fields(_events(caplog, "run_end")[0].getMessage())
        assert f["tenant_id"] == TENANT
        assert f["trace_id"] == TRACE_ID
        assert list(f)[-2:] == ["tenant_id", "trace_id"]

    def test_a_run_end_with_no_trace_omits_the_key(self, caplog) -> None:
        _finish(_finished(RunStatus.COMPLETED, tenant_id=TENANT))
        f = _fields(_events(caplog, "run_end")[0].getMessage())
        assert f["tenant_id"] == TENANT
        assert "trace_id" not in f
