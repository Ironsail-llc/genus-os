"""Unparseable tool-call arguments are bad output, not a provider outage.

The local fallback model sometimes emits tool-call JSON it never closes.
litellm parses `tool_call["function"]["arguments"]` inside its ollama request
transformation and re-raises the `json.JSONDecodeError` as

    litellm.APIConnectionError: Unterminated string starting at: line 1
    column 3175 (char 3174)

Twelve of those in 24 hours. An `APIConnectionError` carries status 500, so
the engine read it as the provider being down: it burned all five local
attempts on a failure that is identical every time, then recorded a breaker
failure. Enough of those open the circuit on the on-device tier — the tier
every agent's chain ends in, and the only one still answering during a cloud
credential outage. email-classifier and devops-analyst runs ended "All models
failed to respond" with a healthy model sitting behind an open breaker.

Two properties, and the second is the one that mattered:

* one immediate retry against the SAME model with the same messages (a
  truncated generation is worth re-rolling once), then fall through the chain
  as before — not five attempts at a deterministic parse failure; and
* the breaker records nothing. Bad output is a fact about one completion.
  Marking the model broken for it takes the fleet's last tier away.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import litellm
import pytest

from robothor.engine import llm_client
from robothor.engine.llm_client import LLMClient, is_malformed_tool_arguments
from robothor.engine.model_breaker import ModelBreaker

LOCAL_MODEL = "ollama_chat/qwen3.8:27b"
FALLBACK_MODEL = "openrouter/vendor/fallback"


def _malformed() -> Exception:
    """The exact exception observed, verbatim from the logs."""
    return litellm.exceptions.APIConnectionError(
        message="Unterminated string starting at: line 1 column 3175 (char 3174)",
        llm_provider="ollama_chat",
        model=LOCAL_MODEL,
    )


@pytest.fixture
def client() -> LLMClient:
    return LLMClient()


@pytest.fixture
def recorded_failures(monkeypatch) -> list[tuple[str, str]]:
    """A fresh breaker per test that also reports what it was told."""
    fresh = ModelBreaker(on_open=None)
    seen: list[tuple[str, str]] = []
    original = fresh.record_failure

    def _spy(model: str, reason: str = "") -> None:
        seen.append((model, reason))
        original(model, reason)

    fresh.record_failure = _spy  # type: ignore[method-assign]
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: fresh)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
    return seen


class TestClassification:
    def test_the_observed_exception_is_recognised(self) -> None:
        assert is_malformed_tool_arguments(_malformed()) is True

    def test_a_raw_json_decode_error_is_recognised(self) -> None:
        try:
            json.loads('{"a": "b')
        except json.JSONDecodeError as e:
            assert is_malformed_tool_arguments(e) is True

    def test_a_wrapped_json_decode_error_is_recognised(self) -> None:
        """litellm versions differ on whether the cause survives."""
        try:
            try:
                json.loads('{"a": "b')
            except json.JSONDecodeError as cause:
                raise RuntimeError("tool call parse failed") from cause
        except RuntimeError as e:
            assert is_malformed_tool_arguments(e) is True

    def test_a_real_connection_failure_is_not(self) -> None:
        """The whole point is to keep telling these two apart."""
        e = litellm.exceptions.APIConnectionError(
            message="Connection refused", llm_provider="ollama_chat", model=LOCAL_MODEL
        )
        assert is_malformed_tool_arguments(e) is False

    def test_an_ordinary_5xx_is_not(self) -> None:
        e = Exception("HTTP 502")
        e.status_code = 502  # type: ignore[attr-defined]
        assert is_malformed_tool_arguments(e) is False


class TestItIsRetriedNotSurfacedAsAnOutage:
    @pytest.mark.asyncio
    async def test_one_retry_on_the_same_model_succeeds(self, client, recorded_failures) -> None:
        """The brief's headline case: raised once, then the model answers."""
        ok = object()
        acompletion = AsyncMock(side_effect=[_malformed(), ok])
        with (
            patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
            patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
        ):
            result = await client._call_llm(
                [{"role": "user", "content": "classify this"}],
                [LOCAL_MODEL, FALLBACK_MODEL],
                [],
                broken_models=set(),
            )

        assert result is ok
        assert [c.kwargs["model"] for c in acompletion.call_args_list] == [
            LOCAL_MODEL,
            LOCAL_MODEL,
        ]
        assert recorded_failures == []

    @pytest.mark.asyncio
    async def test_the_retry_carries_the_same_messages(self, client, recorded_failures) -> None:
        ok = object()
        acompletion = AsyncMock(side_effect=[_malformed(), ok])
        messages = [{"role": "user", "content": "classify this"}]
        with (
            patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
            patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
        ):
            await client._call_llm(messages, [LOCAL_MODEL], [], broken_models=set())

        sent = [c.kwargs["messages"] for c in acompletion.call_args_list]
        assert len(sent) == 2
        assert sent[0] == sent[1]

    @pytest.mark.asyncio
    async def test_it_retries_once_not_five_times(self, client, recorded_failures) -> None:
        """A deterministic parse failure must not eat the local retry budget."""
        ok = object()
        acompletion = AsyncMock(side_effect=[_malformed(), _malformed(), ok])
        with (
            patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
            patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
        ):
            result = await client._call_llm(
                [{"role": "user", "content": "classify this"}],
                [LOCAL_MODEL, FALLBACK_MODEL],
                [],
                broken_models=set(),
            )

        assert result is ok
        assert [c.kwargs["model"] for c in acompletion.call_args_list] == [
            LOCAL_MODEL,
            LOCAL_MODEL,
            FALLBACK_MODEL,
        ]

    @pytest.mark.asyncio
    async def test_the_breaker_is_never_told_about_it(self, client, recorded_failures) -> None:
        """The regression that opened the circuit on the fleet's last tier."""
        acompletion = AsyncMock(side_effect=[_malformed()] * 8)
        with (
            patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
            patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
        ):
            result = await client._call_llm(
                [{"role": "user", "content": "classify this"}],
                [LOCAL_MODEL],
                [],
                broken_models=set(),
            )

        assert result is None  # the chain is exhausted, as before
        assert recorded_failures == [], "bad output was counted as a provider outage"

    @pytest.mark.asyncio
    async def test_a_real_connection_failure_still_opens_the_breaker(
        self, client, recorded_failures
    ) -> None:
        """The narrowing must not blind the breaker to an actual outage."""
        down = litellm.exceptions.APIConnectionError(
            message="Connection refused", llm_provider="ollama_chat", model=LOCAL_MODEL
        )
        acompletion = AsyncMock(side_effect=[down] * 8)
        with (
            patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
            patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
        ):
            await client._call_llm(
                [{"role": "user", "content": "hi"}], [LOCAL_MODEL], [], broken_models=set()
            )

        assert [m for m, _ in recorded_failures] == [LOCAL_MODEL]
