import asyncio
from unittest.mock import AsyncMock

import pytest

from bench.runtime.stream_allowance_experiment import LimitedStream, install
from robothor.engine import llm_client


class Stream:
    def __init__(self):
        self.aclose = AsyncMock()
        self.calls = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.calls += 1
        if self.calls == 1:
            return "partial tool arguments"
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


async def test_total_allowance_closes_stalled_stream_after_partial_chunk():
    source = Stream()
    stream = LimitedStream(source, asyncio.get_running_loop().time() + 0.03)
    assert await anext(stream) == "partial tool arguments"
    with pytest.raises(TimeoutError):
        await anext(stream)
    source.aclose.assert_awaited_once()
    with pytest.raises(TimeoutError):
        await anext(stream)
    source.aclose.assert_awaited_once()
    assert source.calls == 2


async def test_outer_cancellation_closes_the_provider_stream():
    source = Stream()
    stream = LimitedStream(source, asyncio.get_running_loop().time() + 60)
    await anext(stream)
    task = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    source.aclose.assert_awaited_once()


async def test_connection_time_spends_same_allowance_and_local_is_unchanged(monkeypatch):
    source = Stream()

    async def open_stream(model, kwargs):
        await asyncio.sleep(0.02)
        return source

    monkeypatch.setattr(llm_client, "_gated_acompletion", open_stream)
    install(monkeypatch, 0.01, cloud_only=True)
    stream = await llm_client._gated_acompletion("openrouter/test/model", {"timeout": 120})
    with pytest.raises(TimeoutError):
        await anext(stream)
    assert source.calls == 0
    source.aclose.assert_awaited_once()
    assert await llm_client._gated_acompletion("ollama_chat/model", {"timeout": 600}) is source


async def test_native_stream_fallback_discards_incomplete_tool_arguments(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import litellm

    partial = litellm.ModelResponse(
        stream=True,
        choices=[
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "partial",
                            "type": "function",
                            "function": {"name": "create_task", "arguments": '{"title":'},
                        }
                    ]
                },
            }
        ],
    )
    final = litellm.ModelResponse(
        stream=True,
        choices=[{"index": 0, "delta": {"content": "fallback answer"}, "finish_reason": "stop"}],
    )
    source = Stream()

    async def primary_next():
        source.calls += 1
        if source.calls == 1:
            return partial
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    class Primary:
        aclose = source.aclose

        def __aiter__(self):
            return self

        __anext__ = staticmethod(primary_next)

    async def fallback():
        yield final

    opened = []

    async def open_stream(model, kwargs):
        opened.append(model)
        return Primary() if model == "openrouter/primary" else fallback()

    client = llm_client.LLMClient()
    monkeypatch.setattr(client, "_prepare_llm_call", AsyncMock(return_value=1))
    monkeypatch.setattr(client, "_key_pool", lambda model: None)
    monkeypatch.setattr(llm_client, "_streaming_skip_reason", lambda *args: None)
    monkeypatch.setattr(llm_client, "_gated_acompletion", open_stream)
    monkeypatch.setattr(
        llm_client, "get_model_breaker", lambda: SimpleNamespace(record_success=lambda *a: None)
    )
    builder = Mock(
        return_value=litellm.ModelResponse(
            choices=[{"message": {"role": "assistant", "content": "fallback answer"}}]
        )
    )
    monkeypatch.setattr(litellm, "stream_chunk_builder", builder)
    install(monkeypatch, 0.03, cloud_only=True)
    result = await client._call_llm_streaming(
        [{"role": "user", "content": "synthetic only"}],
        ["openrouter/primary", "openrouter/fallback"],
        [],
        AsyncMock(),
        broken_models=set(),
    )
    assert result.choices[0].message.content == "fallback answer"
    assert opened == ["openrouter/primary", "openrouter/fallback"]
    builder.assert_called_once_with([final])
    source.aclose.assert_awaited_once()
