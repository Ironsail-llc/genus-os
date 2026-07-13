"""Tests for the shared LLM call abstraction."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from robothor.engine.llm_client import (
    llm_call,
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
