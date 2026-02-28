"""Tests for the context module — token estimation and compression."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.context import (
    COMPRESS_THRESHOLD,
    KEEP_RECENT,
    estimate_tokens,
    get_context_stats,
    maybe_compress,
)


class TestEstimateTokens:
    def test_empty_messages(self):
        assert estimate_tokens([]) == 0

    def test_text_only(self):
        messages = [
            {"role": "user", "content": "Hello world"},  # 11 chars
            {"role": "assistant", "content": "Hi there"},  # 8 chars
        ]
        # (11 + 8) / 4 = 4
        assert estimate_tokens(messages) == 4

    def test_tool_calls_add_overhead(self):
        messages = [
            {"role": "user", "content": "test"},  # 4 chars / 4 = 1
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "search_memory",
                            "arguments": '{"query": "test"}',
                        },
                    }
                ],
            },
        ]
        result = estimate_tokens(messages)
        # 4 chars from "test" + 17 chars from arguments + 13 from name = 34 / 4 = 8
        # Plus 400 for 1 tool call = 408
        assert result > 400  # Tool call overhead dominates

    def test_none_content_safe(self):
        messages = [{"role": "assistant", "content": None}]
        assert estimate_tokens(messages) == 0


class TestMaybeCompress:
    @pytest.mark.asyncio
    async def test_no_compression_below_threshold(self):
        messages = [
            {"role": "system", "content": "You are a bot."},
            {"role": "user", "content": "Hello"},
        ]
        result = await maybe_compress(messages)
        assert result is messages  # Same object, not modified

    @pytest.mark.asyncio
    async def test_no_compression_too_few_messages(self):
        # Even if we could compress, not enough messages to split
        messages = [{"role": "system", "content": "x" * 400_000}]
        result = await maybe_compress(messages)
        assert result is messages

    @pytest.mark.asyncio
    async def test_compression_triggered(self):
        # Build a large conversation
        messages = [{"role": "system", "content": "System prompt."}]
        for i in range(100):
            messages.append({"role": "user", "content": f"Message {i} " + "x" * 3000})
            messages.append({"role": "assistant", "content": f"Response {i} " + "y" * 3000})

        est = estimate_tokens(messages)
        assert est >= COMPRESS_THRESHOLD

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Summary of conversation."

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            result = await maybe_compress(messages)

        # Should be compressed
        assert len(result) < len(messages)
        # System prompt preserved
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "System prompt."
        # Last KEEP_RECENT messages preserved
        assert result[-1] == messages[-1]

    @pytest.mark.asyncio
    async def test_system_prompt_always_preserved(self):
        messages = [{"role": "system", "content": "IMPORTANT SYSTEM PROMPT"}]
        for i in range(100):
            messages.append({"role": "user", "content": "x" * 4000})
            messages.append({"role": "assistant", "content": "y" * 4000})

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Summary."

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            result = await maybe_compress(messages)

        assert result[0]["content"] == "IMPORTANT SYSTEM PROMPT"

    @pytest.mark.asyncio
    async def test_recent_messages_preserved(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(100):
            messages.append({"role": "user", "content": f"user_{i} " + "x" * 3000})
            messages.append({"role": "assistant", "content": f"asst_{i} " + "y" * 3000})

        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = "Summary."

        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            result = await maybe_compress(messages)

        # Last KEEP_RECENT messages should be exactly preserved
        recent_in_result = result[-(KEEP_RECENT):]
        recent_original = messages[-(KEEP_RECENT):]
        assert recent_in_result == recent_original

    @pytest.mark.asyncio
    async def test_llm_failure_fallback(self):
        messages = [{"role": "system", "content": "sys"}]
        for i in range(100):
            messages.append({"role": "user", "content": "x" * 4000})
            messages.append({"role": "assistant", "content": "y" * 4000})

        with patch(
            "litellm.acompletion", new_callable=AsyncMock, side_effect=RuntimeError("API down")
        ):
            result = await maybe_compress(messages)

        # Should still compress with fallback message
        assert len(result) < len(messages)
        # Check for the fallback placeholder
        summary_msg = result[1]
        assert "Previous conversation" in summary_msg["content"]
        assert "compressed" in summary_msg["content"]


class TestGetContextStats:
    def test_basic_stats(self):
        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        stats = get_context_stats(messages)
        assert stats["message_count"] == 3
        assert stats["estimated_tokens"] > 0
        assert stats["compress_threshold"] == COMPRESS_THRESHOLD
        assert isinstance(stats["usage_pct"], float)
        assert stats["would_compress"] is False
        assert stats["role_counts"]["system"] == 1
        assert stats["role_counts"]["user"] == 1
        assert stats["role_counts"]["assistant"] == 1

    def test_empty_messages(self):
        stats = get_context_stats([])
        assert stats["message_count"] == 0
        assert stats["estimated_tokens"] == 0
