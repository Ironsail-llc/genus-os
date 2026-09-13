"""A failed LLM attempt has to leave evidence behind (DIAG 2026-09-13 §4.4).

`llm_client.py:1646-1650` raised `EmptyCompletionError` and discarded the
response object without reading `usage` or `finish_reason`, and
`run_llm_calls.py:186-310` recorded ONE `agent_run_steps` row per
`_do_llm_call` built from the FINAL successful response — with `duration_ms`
covering every attempt plus every backoff.

The measured consequence (DIAG §2.2): `zero_out = 0` on every day of a week in
which the engine retried ~50 empty completions a day. There was no row for a
failed attempt at all, so the failure the fleet hit 50x/day was not
describable from the database, and a night went into a diagnosis the telemetry
should have answered in five minutes.

These tests pin: the exception names what came back, and every attempt gets
its own step row with its own duration.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import llm_client
from robothor.engine.llm_attempts import (
    OUTCOME_EMPTY,
    OUTCOME_SUCCESS,
    describe_completion,
)
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker
from robothor.engine.models import StepType
from robothor.engine.run_llm_calls import LLMCallMixin
from robothor.engine.session import AgentSession


def _response(
    *,
    content: str | None = "an answer",
    tool_calls: Any = None,
    reasoning_content: str | None = None,
    finish_reason: str = "stop",
    prompt_tokens: int = 1200,
    completion_tokens: int = 40,
    reasoning_tokens: int | None = None,
    model: str = "openrouter/primary",
) -> Any:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning_content is not None:
        message.reasoning_content = reasoning_content
    details = SimpleNamespace(reasoning_tokens=reasoning_tokens)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        completion_tokens_details=details,
    )
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage, model=model)


@pytest.fixture(autouse=True)
def _no_jitter_and_isolated_breaker(monkeypatch):
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
    fresh = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: fresh)


class _Runner(LLMCallMixin):
    """The mixin's whole contract: an LLMClient, a config, a watchdog slot."""

    def __init__(self) -> None:
        self._llm = LLMClient()
        self.config = SimpleNamespace()

    @property
    def _active_watchdog(self) -> Any:
        return None

    def _response_cost(self, **_kwargs: Any) -> float:
        return 0.0


# ─── the exception describes what came back ─────────────────────────────


class TestCompletionShape:
    def test_it_reads_finish_reason_and_usage(self) -> None:
        shape = describe_completion(
            _response(finish_reason="length", completion_tokens=9000, reasoning_tokens=8900)
        )
        assert shape.finish_reason == "length"
        assert shape.output_tokens == 9000
        assert shape.reasoning_tokens == 8900
        assert shape.input_tokens == 1200

    def test_an_unparseable_response_is_not_a_failure(self) -> None:
        """Never let a shape we don't understand drive a retry loop."""
        shape = describe_completion(SimpleNamespace(choices=[]))
        assert shape.no_answer is False
        assert shape.reasoning_only is False

    def test_describe_names_every_field_an_operator_greps(self) -> None:
        text = describe_completion(
            _response(content="", finish_reason="length", completion_tokens=7, reasoning_tokens=3)
        ).describe()
        for field in ("finish_reason=length", "output_tokens=7", "reasoning_tokens=3"):
            assert field in text


@pytest.mark.asyncio
async def test_empty_completion_error_names_finish_reason_and_reasoning_tokens(caplog) -> None:
    """The string is what an operator greps at 02:00 (DIAG §4.4)."""
    empty = _response(content="", finish_reason="stop", completion_tokens=0, reasoning_tokens=0)
    client = LLMClient()
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=AsyncMock(return_value=empty)),
        caplog.at_level("WARNING", logger="robothor.engine.llm_client"),
    ):
        result = await client._call_llm(
            [{"role": "user", "content": "hi"}], ["openrouter/only"], [], broken_models=set()
        )
    assert result is None
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "finish_reason=stop" in logged
    assert "output_tokens=0" in logged
    assert "reasoning_present=False" in logged


# ─── one step row per attempt ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_attempt_and_its_retry_are_two_step_rows() -> None:
    """DIAG §2.2: today the failed attempt leaves no row at all."""
    err = Exception("HTTP 502")
    err.status_code = 502
    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(side_effect=[err, _response()]),
        ),
    ):
        await runner._llm_call_and_record(session, ["openrouter/primary"], [], None, set(), 0.3)

    llm_steps = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert len(llm_steps) == 2, "one row per ATTEMPT, not one per _do_llm_call"
    failed, succeeded = llm_steps
    assert failed.error_message and "502" in failed.error_message
    assert failed.model == "openrouter/primary"
    assert succeeded.error_message is None
    assert succeeded.output_tokens == 40
    # The successful row no longer silently covers the failed attempt and its
    # backoff — that conflation is what made `duration_ms` unreadable.
    assert failed.duration_ms is not None


@pytest.mark.asyncio
async def test_an_empty_attempt_is_recorded_with_its_usage() -> None:
    empty = _response(content="", completion_tokens=0, reasoning_tokens=0)
    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(side_effect=[empty, _response()]),
        ),
    ):
        await runner._llm_call_and_record(session, ["openrouter/primary"], [], None, set(), 0.3)
    llm_steps = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert len(llm_steps) == 2
    assert llm_steps[0].error_message and OUTCOME_EMPTY in llm_steps[0].error_message
    assert llm_steps[0].input_tokens == 1200


@pytest.mark.asyncio
async def test_a_clean_call_still_records_exactly_one_row() -> None:
    """The common path must not grow a second row for the attempt that worked."""
    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(return_value=_response()),
        ),
    ):
        await runner._llm_call_and_record(session, ["openrouter/primary"], [], None, set(), 0.3)
    llm_steps = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert len(llm_steps) == 1
    assert llm_steps[0].error_message is None
    assert llm_steps[0].output_tokens == 40


def test_the_success_outcome_is_named() -> None:
    """Both outcome names are public: the failed rows carry them verbatim."""
    assert OUTCOME_SUCCESS == "success"
    assert OUTCOME_EMPTY == "empty"
