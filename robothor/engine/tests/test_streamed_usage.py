"""A streamed model call records the tokens a non-streamed one does.

Every streamed call recorded ``input_tokens=0``, for every provider family:
litellm's stream wrapper holds the provider's usage back from the caller
unless the request asks for it (``stream_options.include_usage``), and the
engine never asked. The rebuilt response then counted output from the text
and input as zero, so run totals undercounted and the runaway token cap saw
only output on interactive runs.

The request now asks for usage on every streamed call. A stream that still
carries none (a provider that does not report it) is given the engine's own
input estimate, and its ``event=llm_call`` line says ``estimated=true``.
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from litellm.types.utils import (
    Delta,
    ModelResponseStream,
    PromptTokensDetailsWrapper,
    StreamingChoices,
    Usage,
)

from robothor.engine import llm_client
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker
from robothor.engine.models import StepType
from robothor.engine.run_llm_calls import LLMCallMixin
from robothor.engine.session import AgentSession

MESSAGES = [
    {"role": "system", "content": "You are example-agent. " * 40},
    {"role": "user", "content": "Summarize the open items, please."},
]


def _chunk(text: str | None = None, *, finish: str | None = None, usage: Any = None, choices=True):
    kw: dict[str, Any] = {"model": "m", "id": "c1"}
    if usage is not None:
        kw["usage"] = usage
    found = [StreamingChoices(delta=Delta(content=text), finish_reason=finish)] if choices else []
    return ModelResponseStream(choices=found, **kw)


def _openai_usage() -> Usage:
    return Usage(
        prompt_tokens=1200,
        completion_tokens=40,
        total_tokens=1240,
        prompt_tokens_details=PromptTokensDetailsWrapper(cached_tokens=300),
    )


def _anthropic_usage() -> Usage:
    return Usage(
        prompt_tokens=1200, completion_tokens=40, total_tokens=1240, cache_read_input_tokens=300
    )


#: How each family's usage reaches the caller once it is asked for.
FAMILIES = {
    # OpenAI / OpenRouter / DeepSeek: a usage-only chunk after the finish.
    "openai/gpt-6-luna": [
        _chunk("hel"),
        _chunk("lo", finish="stop"),
        _chunk(choices=False, usage=_openai_usage()),
    ],
    # Gemini on Vertex: usage on the final content chunk.
    "vertex_ai/gemini-3.8-flash": [
        _chunk("hel"),
        _chunk("lo", finish="stop", usage=_openai_usage()),
    ],
    # Anthropic: a usage-only chunk carrying the cache read.
    "anthropic/claude-sonnet-4-6": [
        _chunk("hello", finish="stop"),
        _chunk(choices=False, usage=_anthropic_usage()),
    ],
}


class _Stream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, caplog):
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MIN", 0.0)
    monkeypatch.setattr(llm_client, "TRANSIENT_RETRY_JITTER_MAX", 0.0)
    fresh = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: fresh)
    caplog.set_level(logging.INFO)


class _Runner(LLMCallMixin):
    def __init__(self) -> None:
        self._llm = LLMClient()
        self.config = SimpleNamespace()

    @property
    def _active_watchdog(self) -> Any:
        return None

    def _response_cost(self, **_kwargs: Any) -> float:
        return 0.0


async def _streamed(model: str, chunks: list[Any]) -> tuple[AgentSession, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    async def _acompletion(**kwargs: Any) -> Any:
        sent.append(kwargs)
        return _Stream(chunks)

    session = AgentSession(agent_id="example-agent")
    session.messages = [dict(m) for m in MESSAGES]
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", new=_acompletion),
    ):
        await _Runner()._llm_call_and_record(session, [model], [], AsyncMock(), set(), 0.3)
    return session, sent


def _llm_step(session: AgentSession) -> Any:
    (step,) = [s for s in session.run.steps if s.step_type == StepType.LLM_CALL]
    return step


def _line(caplog) -> dict[str, str]:
    (record,) = [r for r in caplog.records if "event=llm_call " in r.getMessage()]
    return dict(re.findall(r"(\w+)=(\S+)", record.getMessage()))


@pytest.mark.asyncio
@pytest.mark.parametrize("model", sorted(FAMILIES))
async def test_a_streamed_call_asks_for_usage(model: str) -> None:
    _session, sent = await _streamed(model, FAMILIES[model])
    assert sent[0]["stream"] is True
    assert sent[0]["stream_options"] == {"include_usage": True}


def test_a_non_streamed_call_is_not_sent_stream_options() -> None:
    kwargs = LLMClient()._build_llm_kwargs("openai/gpt-6-luna", MESSAGES, [], 100, 0.3)
    assert "stream_options" not in kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("model", sorted(FAMILIES))
async def test_a_streamed_call_records_the_providers_tokens(model: str, caplog) -> None:
    session, _sent = await _streamed(model, FAMILIES[model])
    step = _llm_step(session)
    assert step.input_tokens == 1200
    assert step.output_tokens == 40
    line = _line(caplog)
    assert line["input_tokens"] == "1200"
    assert line["cached_tokens"] == "300"
    assert line["streamed"] == "true"
    assert "estimated" not in line


@pytest.mark.asyncio
async def test_a_stream_with_no_usage_is_estimated_and_says_so(caplog) -> None:
    from robothor.engine.context import estimate_tokens

    session, _sent = await _streamed(
        "openai/gpt-6-luna", [_chunk("hel"), _chunk("lo", finish="stop")]
    )
    step = _llm_step(session)
    assert step.input_tokens == estimate_tokens(MESSAGES)
    assert step.input_tokens > 0
    line = _line(caplog)
    assert line["estimated"] == "true"
    assert int(line["input_tokens"]) == step.input_tokens
