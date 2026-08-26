"""The streaming path fed the breaker but never asked it anything.

`_call_llm` skips a model whose breaker is open, because a dead provider
otherwise costs the full per-call timeout on every run — codex/* did exactly
that for a month. `_call_llm_streaming` had no such check, and streaming is
the interactive path: the operator's own chat paid the full timeout against a
provider the engine had already written off.

Consulting it is only safe because success now reaches the breaker from this
path too; otherwise an open breaker could never clear from the one path that
proves a model healthy.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from robothor.engine import llm_client
from robothor.engine.llm_client import LLMClient
from robothor.engine.model_breaker import ModelBreaker


class _Delta:
    content = "hi"
    tool_calls = None
    reasoning_content = None


class _Choice:
    delta = _Delta()
    finish_reason = None


class _Chunk:
    choices = [_Choice()]
    usage = None


class _Stream:
    def __aiter__(self):
        async def gen():
            yield _Chunk()

        return gen()


@pytest.fixture
def breaker(monkeypatch) -> ModelBreaker:
    b = ModelBreaker(on_open=None)
    monkeypatch.setattr(llm_client, "get_model_breaker", lambda: b)
    return b


async def _stream(client, acompletion, models):
    sentinel = object()
    with (
        patch.object(LLMClient, "_prepare_llm_call", new=AsyncMock(return_value=100)),
        patch("robothor.engine.llm_client.litellm.acompletion", acompletion),
        patch("robothor.engine.llm_client.litellm.stream_chunk_builder", return_value=sentinel),
    ):
        result = await client._call_llm_streaming(
            [{"role": "user", "content": "hi"}], models, [], broken_models=set()
        )
    return result, sentinel


@pytest.mark.asyncio
async def test_streaming_skips_a_model_whose_breaker_is_open(breaker):
    """Chat must not pay a full timeout on a provider already written off."""
    for _ in range(10):
        breaker.record_failure("openrouter/dead", reason="test")
    assert breaker.is_open("openrouter/dead"), "fixture failed to open the breaker"

    acompletion = AsyncMock(return_value=_Stream())
    result, sentinel = await _stream(
        LLMClient(), acompletion, ["openrouter/dead", "openrouter/live"]
    )

    assert result is sentinel
    assert [c.kwargs["model"] for c in acompletion.call_args_list] == ["openrouter/live"]


@pytest.mark.asyncio
async def test_streaming_still_uses_a_healthy_model(breaker):
    acompletion = AsyncMock(return_value=_Stream())
    result, sentinel = await _stream(LLMClient(), acompletion, ["openrouter/live"])

    assert result is sentinel
    assert acompletion.await_count == 1


@pytest.mark.asyncio
async def test_a_streamed_success_clears_the_breaker_for_that_model(breaker):
    """The consult is only safe because this path can also close the breaker."""
    acompletion = AsyncMock(return_value=_Stream())
    await _stream(LLMClient(), acompletion, ["openrouter/live"])

    assert not breaker.is_open("openrouter/live")
