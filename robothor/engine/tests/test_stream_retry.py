"""A transient stream failure retries once on the same model (channel streaming).

Before: a streaming attempt that died with a connection reset, a 5xx or a 529
was blamed and the chain advanced to the next model, so a provider blip handed
a Telegram turn to a weaker fallback. With ``ROBOTHOR_CHANNEL_STREAMING`` on and
the caller inside a channel-streaming scope, the same model gets ONE more try:
the partial text is discarded and the client is told through the run's status
sink. Stalls still fall back (a second stall would double the wait), and a
second failure still falls back. Outside the scope, or with the flag off,
nothing changes — the webchat client appends deltas and would show the partial
text twice.

Also here: inside the scope, text the model writes in a tool turn keeps
reaching ``on_content`` (shown as interim progress), where today it is cut off
the moment the first tool-call delta arrives.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import llm_client
from robothor.engine.channel_streaming import channel_stream_scope
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker, _current_run_id_var
from robothor.engine.run_status import register_status_sink, unregister_status_sink

RUN_ID = "run-stream-retry"


def _chunk(content: str | None = None, tool_calls: list[Any] | None = None) -> Any:
    delta = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)


def _tool_call_chunk() -> Any:
    tc = SimpleNamespace(
        id="call-1",
        index=0,
        function=SimpleNamespace(name="web_search", arguments='{"q": "tides"}'),
    )
    return _chunk(tool_calls=[tc])


class _ProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"provider said {status_code}")
        self.status_code = status_code


class _Stream:
    """Yields ``chunks``; raises ``fail_with`` after them when given."""

    def __init__(self, chunks: list[Any], fail_with: BaseException | None = None) -> None:
        self._chunks = chunks
        self._fail_with = fail_with

    def __aiter__(self):
        async def gen():
            for c in self._chunks:
                yield c
            if self._fail_with is not None:
                raise self._fail_with

        return gen()


@pytest.fixture
def breaker(monkeypatch) -> ModelBreaker:
    b = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: b)
    return b


@pytest.fixture
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(llm_client, "STREAM_RETRY_DELAY_SECONDS", 0.0, raising=False)


@pytest.fixture
def status_events():
    events: list[dict[str, Any]] = []

    async def sink(event: dict[str, Any]) -> None:
        events.append(event)

    register_status_sink(RUN_ID, sink)
    token = _current_run_id_var.set(RUN_ID)
    yield events
    _current_run_id_var.reset(token)
    unregister_status_sink(RUN_ID)


async def _call(acompletion: Any, models: list[str], on_content: Any = None) -> tuple[Any, list]:
    built: list[list[Any]] = []

    def builder(chunks: list[Any]) -> Any:
        built.append(list(chunks))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="x", tool_calls=None))]
        )

    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
        patch("robothor.engine.llm_client.litellm.stream_chunk_builder", side_effect=builder),
    ):
        result = await LLMClient()._call_llm_streaming(
            [{"role": "user", "content": "hi"}],
            models,
            [],
            on_content=on_content,
            broken_models=set(),
        )
    return result, built


def _models_called(acompletion: AsyncMock) -> list[str]:
    return [c.kwargs["model"] for c in acompletion.call_args_list]


TRANSIENT = [
    pytest.param(_ProviderError(503), id="503"),
    pytest.param(_ProviderError(529), id="529-overloaded"),
    pytest.param(ConnectionResetError("Connection reset by peer"), id="connection-reset"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", TRANSIENT)
async def test_a_transient_failure_retries_once_on_the_same_model(
    breaker, no_retry_pause, channel_streaming, status_events, error
):
    """AC4: completes on the SAME model; one stream_retry status; partial discarded."""
    channel_streaming(True)
    seen: list[str] = []

    async def on_content(text: str) -> None:
        seen.append(text)

    acompletion = AsyncMock(
        side_effect=[
            _Stream([_chunk("partial ans")], fail_with=error),
            _Stream([_chunk("Full "), _chunk("answer.")]),
        ]
    )
    with channel_stream_scope():
        result, built = await _call(acompletion, ["openrouter/a", "openrouter/b"], on_content)

    assert result is not None
    assert _models_called(acompletion) == ["openrouter/a", "openrouter/a"]
    retries = [e for e in status_events if e.get("event") == "stream_retry"]
    assert len(retries) == 1
    assert retries[0].get("model") == "openrouter/a"
    # The response is rebuilt from the second attempt's chunks only.
    assert len(built) == 1
    assert [c.choices[0].delta.content for c in built[0]] == ["Full ", "answer."]
    assert seen[-1] == "Full answer."


@pytest.mark.asyncio
async def test_a_second_failure_falls_back(
    breaker, no_retry_pause, channel_streaming, status_events
):
    """AC5: one in-place retry, not two."""
    channel_streaming(True)
    acompletion = AsyncMock(
        side_effect=[
            _Stream([_chunk("p")], fail_with=_ProviderError(502)),
            _Stream([_chunk("p")], fail_with=_ProviderError(502)),
            _Stream([_chunk("From b.")]),
        ]
    )
    with channel_stream_scope():
        result, _ = await _call(acompletion, ["openrouter/a", "openrouter/b"])

    assert result is not None
    assert _models_called(acompletion) == ["openrouter/a", "openrouter/a", "openrouter/b"]


@pytest.mark.asyncio
async def test_a_stall_still_falls_back(breaker, no_retry_pause, channel_streaming, status_events):
    """AC6: a stalled stream is not retried in place."""
    channel_streaming(True)
    acompletion = AsyncMock(
        side_effect=[
            _Stream([_chunk("p")], fail_with=TimeoutError("stalled")),
            _Stream([_chunk("From b.")]),
        ]
    )
    with channel_stream_scope():
        await _call(acompletion, ["openrouter/a", "openrouter/b"])

    assert _models_called(acompletion) == ["openrouter/a", "openrouter/b"]
    assert not [e for e in status_events if e.get("event") == "stream_retry"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("on", "scoped"), [(False, True), (True, False)])
async def test_no_retry_without_both_flag_and_scope(
    breaker, no_retry_pause, channel_streaming, status_events, on, scoped
):
    """Today's behaviour: flag off, or a caller (webchat) outside the scope."""
    channel_streaming(on)
    acompletion = AsyncMock(
        side_effect=[
            _Stream([_chunk("p")], fail_with=_ProviderError(503)),
            _Stream([_chunk("From b.")]),
        ]
    )
    if scoped:
        with channel_stream_scope():
            await _call(acompletion, ["openrouter/a", "openrouter/b"])
    else:
        await _call(acompletion, ["openrouter/a", "openrouter/b"])

    assert _models_called(acompletion) == ["openrouter/a", "openrouter/b"]
    assert not status_events


def _tool_turn_stream() -> _Stream:
    return _Stream([_chunk("Let me check."), _tool_call_chunk(), _chunk(" Still checking.")])


@pytest.mark.asyncio
async def test_tool_turn_text_reaches_a_streaming_channel(breaker, channel_streaming):
    """AC7: inside the scope, text after a tool-call delta is not suppressed."""
    channel_streaming(True)
    seen: list[str] = []

    async def on_content(text: str) -> None:
        seen.append(text)

    with channel_stream_scope():
        await _call(AsyncMock(return_value=_tool_turn_stream()), ["openrouter/a"], on_content)

    assert seen[-1] == "Let me check. Still checking."


@pytest.mark.asyncio
@pytest.mark.parametrize(("on", "scoped"), [(False, True), (True, False)])
async def test_tool_turn_text_suppressed_as_today_otherwise(breaker, channel_streaming, on, scoped):
    channel_streaming(on)
    seen: list[str] = []

    async def on_content(text: str) -> None:
        seen.append(text)

    acompletion = AsyncMock(return_value=_tool_turn_stream())
    if scoped:
        with channel_stream_scope():
            await _call(acompletion, ["openrouter/a"], on_content)
    else:
        await _call(acompletion, ["openrouter/a"], on_content)

    assert seen == ["Let me check."]
