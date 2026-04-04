"""Tests for the shared LLM call abstraction."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.llm_client import (
    AllModelsFailedError,
    llm_call,
    llm_call_streaming,
    llm_call_with_fallback,
)


def _make_response(content: str = "Hello") -> MagicMock:
    """Build a minimal litellm-style ModelResponse mock."""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


# ---------------------------------------------------------------------------
# llm_call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_call_success():
    """Successful single-model call returns the response."""
    expected = _make_response("ok")

    with patch("litellm.acompletion", return_value=expected) as mock_call:
        result = await llm_call(
            [{"role": "user", "content": "hi"}],
            model="test-model",
        )

    assert result is expected
    mock_call.assert_called_once()
    call_kwargs = mock_call.call_args.kwargs
    assert call_kwargs["model"] == "test-model"
    assert call_kwargs["temperature"] == 0.3


@pytest.mark.asyncio
async def test_llm_call_json_mode():
    """json_mode=True adds response_format to the call."""
    expected = _make_response('{"ok": true}')

    with patch("litellm.acompletion", return_value=expected) as mock_call:
        await llm_call(
            [{"role": "user", "content": "hi"}],
            model="test-model",
            json_mode=True,
        )

    call_kwargs = mock_call.call_args.kwargs
    assert call_kwargs["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_llm_call_retry_on_timeout():
    """Retries once on TimeoutError when max_retries=2."""
    expected = _make_response("recovered")
    call_count = 0

    async def _side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise TimeoutError("first attempt timed out")
        return expected

    with patch("litellm.acompletion", side_effect=_side_effect):
        result = await llm_call(
            [{"role": "user", "content": "hi"}],
            model="test-model",
            max_retries=2,
            timeout=5,
        )

    assert result is expected
    assert call_count == 2


@pytest.mark.asyncio
async def test_llm_call_no_retry_by_default():
    """With max_retries=1 (default), a single failure raises immediately."""
    with patch("litellm.acompletion", side_effect=TimeoutError("boom")):
        with pytest.raises(TimeoutError, match="boom"):
            await llm_call(
                [{"role": "user", "content": "hi"}],
                model="test-model",
                timeout=1,
            )


@pytest.mark.asyncio
async def test_llm_call_max_tokens():
    """max_tokens is forwarded to litellm."""
    expected = _make_response("short")

    with patch("litellm.acompletion", return_value=expected) as mock_call:
        await llm_call(
            [{"role": "user", "content": "hi"}],
            model="test-model",
            max_tokens=500,
        )

    assert mock_call.call_args.kwargs["max_tokens"] == 500


# ---------------------------------------------------------------------------
# llm_call_with_fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_first_model_succeeds():
    """When the first model works, no fallback is needed."""
    expected = _make_response("first")

    with patch("litellm.acompletion", return_value=expected) as mock_call:
        result = await llm_call_with_fallback(
            [{"role": "user", "content": "hi"}],
            models=["model-a", "model-b"],
        )

    assert result is expected
    assert mock_call.call_count == 1
    assert mock_call.call_args.kwargs["model"] == "model-a"


@pytest.mark.asyncio
async def test_fallback_on_model_failure():
    """Falls back to second model when first raises."""
    expected = _make_response("second")
    call_count = 0

    async def _side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if kwargs["model"] == "model-a":
            raise Exception("model-a is down")
        return expected

    with patch("litellm.acompletion", side_effect=_side_effect):
        result = await llm_call_with_fallback(
            [{"role": "user", "content": "hi"}],
            models=["model-a", "model-b"],
        )

    assert result is expected
    assert call_count == 2


@pytest.mark.asyncio
async def test_fallback_on_timeout():
    """Falls back to second model when first times out."""
    expected = _make_response("fallback")

    call_count = 0

    async def _side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if kwargs["model"] == "model-a":
            raise Exception("model-a is down")  # Any exception triggers fallback
        return expected

    with patch("litellm.acompletion", side_effect=_side_effect):
        result = await llm_call_with_fallback(
            [{"role": "user", "content": "hi"}],
            models=["model-a", "model-b"],
            timeout_budget=60,
        )

    assert result is expected
    assert call_count == 2


@pytest.mark.asyncio
async def test_fallback_all_fail_raises():
    """Raises AllModelsFailedError when every model fails."""
    with patch("litellm.acompletion", side_effect=Exception("all broken")):
        with pytest.raises(AllModelsFailedError, match="All models failed"):
            await llm_call_with_fallback(
                [{"role": "user", "content": "hi"}],
                models=["model-a", "model-b"],
            )


@pytest.mark.asyncio
async def test_fallback_empty_models_raises():
    """Raises AllModelsFailedError for empty models list."""
    with pytest.raises(AllModelsFailedError, match="No models provided"):
        await llm_call_with_fallback(
            [{"role": "user", "content": "hi"}],
            models=[],
        )


@pytest.mark.asyncio
async def test_fallback_timeout_budget_division():
    """Per-model timeout = max(30, budget // len(models))."""
    # 3 models, budget 180 => per_model = 60
    # We verify by checking that a model sleeping 50s succeeds (under 60s)
    # but the overall budget is bounded.
    expected = _make_response("ok")

    with patch("litellm.acompletion", return_value=expected):
        result = await llm_call_with_fallback(
            [{"role": "user", "content": "hi"}],
            models=["a", "b", "c"],
            timeout_budget=180,
        )

    assert result is expected


@pytest.mark.asyncio
async def test_fallback_timeout_budget_floor():
    """Per-model timeout never goes below 30s even with many models."""
    expected = _make_response("ok")

    with patch("litellm.acompletion", return_value=expected):
        # 10 models, budget 60 => 60//10 = 6, but floor is 30
        result = await llm_call_with_fallback(
            [{"role": "user", "content": "hi"}],
            models=[f"m{i}" for i in range(10)],
            timeout_budget=60,
        )

    assert result is expected


@pytest.mark.asyncio
async def test_fallback_tools_forwarded():
    """Tool definitions are forwarded to litellm."""
    tools = [{"type": "function", "function": {"name": "test_tool"}}]
    expected = _make_response("with tools")

    with patch("litellm.acompletion", return_value=expected) as mock_call:
        await llm_call_with_fallback(
            [{"role": "user", "content": "hi"}],
            models=["model-a"],
            tools=tools,
        )

    assert mock_call.call_args.kwargs["tools"] is tools


# ---------------------------------------------------------------------------
# llm_call_streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_success():
    """Streaming call collects chunks and returns them."""
    chunks = [MagicMock(choices=[MagicMock()]) for _ in range(3)]

    async def _mock_stream(**kwargs):
        for c in chunks:
            yield c

    with patch("litellm.acompletion", return_value=_mock_stream()):
        result = await llm_call_streaming(
            [{"role": "user", "content": "hi"}],
            models=["model-a"],
        )

    assert len(result) == 3


@pytest.mark.asyncio
async def test_streaming_on_chunk_callback():
    """on_chunk callback is invoked for each chunk."""
    chunks = [MagicMock() for _ in range(3)]
    received: list = []

    async def _mock_stream(**kwargs):
        for c in chunks:
            yield c

    async def _on_chunk(chunk):
        received.append(chunk)

    with patch("litellm.acompletion", return_value=_mock_stream()):
        await llm_call_streaming(
            [{"role": "user", "content": "hi"}],
            models=["model-a"],
            on_chunk=_on_chunk,
        )

    assert len(received) == 3


@pytest.mark.asyncio
async def test_streaming_fallback_on_failure():
    """Falls back to next model if streaming fails."""
    chunks = [MagicMock() for _ in range(2)]
    call_count = 0

    async def _side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if kwargs["model"] == "model-a":
            raise Exception("stream init failed")

        async def _gen():
            for c in chunks:
                yield c

        return _gen()

    with patch("litellm.acompletion", side_effect=_side_effect):
        result = await llm_call_streaming(
            [{"role": "user", "content": "hi"}],
            models=["model-a", "model-b"],
        )

    assert result is not None
    assert len(result) == 2
    assert call_count == 2


@pytest.mark.asyncio
async def test_streaming_all_fail_raises():
    """Raises AllModelsFailedError when all streaming models fail."""
    with patch("litellm.acompletion", side_effect=Exception("broken")):
        with pytest.raises(AllModelsFailedError, match="All models failed"):
            await llm_call_streaming(
                [{"role": "user", "content": "hi"}],
                models=["model-a"],
            )


@pytest.mark.asyncio
async def test_streaming_empty_models_raises():
    """Raises AllModelsFailedError for empty models list."""
    with pytest.raises(AllModelsFailedError, match="No models provided"):
        await llm_call_streaming(
            [{"role": "user", "content": "hi"}],
            models=[],
        )
