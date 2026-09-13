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

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import llm_client
from robothor.engine.llm_attempts import (
    OUTCOME_CANCELLED,
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
    """Zero the backoff: a hand-advanced clock must not sleep for real."""
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


# ─── the durations mean what they say (review I4) ───────────────────────


class _Clock:
    """A monotonic clock the provider stub advances by hand."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


@pytest.mark.asyncio
async def test_the_success_row_does_not_cover_the_failed_attempt() -> None:
    """§2.2's headline: `duration_ms` stops standing for the whole retry loop.

    Pinned with a hand-advanced clock, because with mocks every real duration
    is 0ms and the claim was true of nothing measurable — the first cut of
    this fix could be reverted to `elapsed_ms` with the suite still green.
    """
    clock = _Clock()
    err = Exception("HTTP 502")
    err.status_code = 502

    async def _acompletion(**_kwargs: Any) -> Any:
        if not _acompletion.called:  # type: ignore[attr-defined]
            _acompletion.called = True  # type: ignore[attr-defined]
            clock.t += 5.0
            raise err
        clock.t += 0.5
        return _response()

    _acompletion.called = False  # type: ignore[attr-defined]

    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch("time.monotonic", clock),
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=_acompletion),
    ):
        _resp, _model, elapsed_ms, _msg = await runner._llm_call_and_record(
            session, ["openrouter/primary"], [], None, set(), 0.3
        )

    failed, succeeded = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert failed.duration_ms == 5_000
    assert succeeded.duration_ms == 500, "the success row carries ITS attempt, not the loop"
    assert elapsed_ms == 5_500, "the caller still sees the whole dispatch"


# ─── the rows survive the failures they describe (review I2) ────────────


@pytest.mark.asyncio
async def test_attempt_rows_survive_a_spent_credential_raise() -> None:
    """`_call_llm` re-raises on spent credit — the one failure with no row."""
    spent = Exception("Insufficient credits: your account has no remaining balance")
    spent.status_code = 402
    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=AsyncMock(side_effect=spent)),
        pytest.raises(Exception, match="Insufficient credits"),
    ):
        await runner._llm_call_and_record(session, ["openrouter/only"], [], None, set(), 0.3)

    rows = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert rows, "the raise must not take the evidence with it"
    assert "error_402" in (rows[0].error_message or "")


# ─── one meaning for duration_ms on both paths (review I5) ──────────────


class _Delta:
    content = "hi"
    tool_calls = None
    reasoning_content = None


class _Chunk:
    choices = [SimpleNamespace(delta=_Delta(), finish_reason=None)]
    usage = None


class _Stream:
    def __aiter__(self):
        async def gen():
            yield _Chunk()

        return gen()


@pytest.mark.asyncio
async def test_the_streaming_path_times_the_attempt_too() -> None:
    """`duration_ms` must not mean one thing streamed and another not.

    Non-streaming times the provider attempt; streaming used to fall back to
    the whole dispatch including `_prepare_llm_call`, so one column carried two
    definitions depending on whether the run streamed.
    """
    clock = _Clock()

    async def _prepare(*_args: Any, **_kwargs: Any) -> int:
        clock.t += 30.0  # token counting / compaction check, before any call
        return 100

    async def _acompletion(**_kwargs: Any) -> Any:
        clock.t += 2.0
        return _Stream()

    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch("time.monotonic", clock),
        patch.object(LLMClient, "_prepare_llm_call", new=_prepare),
        patch("robothor.engine.llm_client.litellm.acompletion", new=_acompletion),
        patch(
            "robothor.engine.llm_client.litellm.stream_chunk_builder",
            return_value=_response(),
        ),
    ):
        await runner._llm_call_and_record(
            session, ["openrouter/primary"], [], AsyncMock(), set(), 0.3
        )

    (row,) = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert row.duration_ms == 2_000, "the provider attempt, not the prep before it"


@pytest.mark.asyncio
async def test_a_streamed_answerless_reply_writes_exactly_one_row() -> None:
    """One provider call is one row (review N1).

    The streamed success path recorded the attempt AND returned the response,
    so an answerless stream produced a failed-attempt row and a success row for
    the same call — two rows, one of them claiming an answer that was never
    there. The streaming path now refuses an answerless reply the way the
    non-streaming one does: nothing was emitted to `on_content` (there was no
    content), so advancing cannot duplicate text.
    """
    calls: list[str] = []

    async def _acompletion(**kwargs: Any) -> Any:
        calls.append(kwargs["model"])
        return _Stream()

    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=_acompletion),
        patch(
            "robothor.engine.llm_client.litellm.stream_chunk_builder",
            return_value=_response(content="", completion_tokens=0),
        ),
    ):
        await runner._llm_call_and_record(
            session, ["openrouter/primary"], [], AsyncMock(), set(), 0.3
        )

    rows = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert len(calls) == 1
    assert len(rows) == 1, "one provider call must leave one row"
    assert rows[0].error_message and OUTCOME_EMPTY in rows[0].error_message


@pytest.mark.asyncio
async def test_a_streamed_answer_still_writes_exactly_one_row() -> None:
    """…and the common path does not grow a second row either."""
    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion", new=AsyncMock(return_value=_Stream())
        ),
        patch(
            "robothor.engine.llm_client.litellm.stream_chunk_builder",
            return_value=_response(),
        ),
    ):
        await runner._llm_call_and_record(
            session, ["openrouter/primary"], [], AsyncMock(), set(), 0.3
        )
    rows = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert len(rows) == 1
    assert rows[0].error_message is None


@pytest.mark.asyncio
async def test_a_cancelled_attempt_records_its_row_then_re_raises() -> None:
    """A run-deadline cancel is not an Exception, so nothing caught it (N4).

    The attempt still happened and still cost wall clock; the cancellation
    still stands.
    """
    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner._llm_call_and_record(session, ["openrouter/only"], [], None, set(), 0.3)

    rows = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert len(rows) == 1
    assert rows[0].error_message and OUTCOME_CANCELLED in rows[0].error_message


@pytest.mark.asyncio
async def test_attempt_rows_survive_a_cancellation() -> None:
    """A run-deadline cancel cuts straight through the recording line."""
    err = Exception("HTTP 502")
    err.status_code = 502
    runner = _Runner()
    session = AgentSession(agent_id="test-agent")
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch(
            "robothor.engine.llm_client.litellm.acompletion",
            new=AsyncMock(side_effect=[err, asyncio.CancelledError()]),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await runner._llm_call_and_record(session, ["openrouter/only"], [], None, set(), 0.3)

    rows = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    assert len(rows) == 2, "the 502 AND the attempt the cancel cut through"
    assert "error_502" in (rows[0].error_message or "")
    assert OUTCOME_CANCELLED in (rows[1].error_message or "")
