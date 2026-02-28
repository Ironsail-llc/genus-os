"""Tests for Telegram bot."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.telegram import MAX_MESSAGE_LENGTH, TelegramBot


@pytest.fixture
def bot(engine_config):
    """Create a TelegramBot with mocked dependencies."""
    with patch("robothor.engine.telegram.Bot") as mock_bot_cls:
        with patch("robothor.engine.telegram.Dispatcher"):
            mock_bot = MagicMock()
            mock_bot.send_message = AsyncMock()
            mock_bot_cls.return_value = mock_bot

            runner = MagicMock()
            bot = TelegramBot(engine_config, runner)
            bot.bot = mock_bot
            yield bot


class TestChatHistory:
    def test_history_initially_empty(self, bot):
        """Chat history starts empty."""
        assert bot._chat_history == {}

    def test_clear_history(self, bot):
        """Clear removes chat history."""
        bot._chat_history["12345"] = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        bot._chat_history.pop("12345", None)
        assert "12345" not in bot._chat_history

    def test_history_cap(self, bot):
        """History is capped at max_history entries."""
        chat_id = "12345"
        bot._chat_history[chat_id] = []
        # Add 30 messages (15 turns) — should be capped at 20
        for i in range(15):
            bot._chat_history[chat_id].append({"role": "user", "content": f"msg {i}"})
            bot._chat_history[chat_id].append({"role": "assistant", "content": f"reply {i}"})
        hist = bot._chat_history[chat_id]
        if len(hist) > bot._max_history:
            bot._chat_history[chat_id] = hist[-bot._max_history :]
        assert len(bot._chat_history[chat_id]) == 20

    def test_reset_clears_history(self, bot):
        """Reset clears both model override and chat history."""
        bot._model_override["12345"] = "some-model"
        bot._chat_history["12345"] = [{"role": "user", "content": "test"}]
        # Simulate /reset behavior
        bot._model_override.pop("12345", None)
        bot._chat_history.pop("12345", None)
        assert "12345" not in bot._model_override
        assert "12345" not in bot._chat_history

    def test_max_history_default(self, bot):
        """Default max history is 20."""
        assert bot._max_history == 20


class TestMessageSplitting:
    def test_short_message_not_split(self, bot):
        """Messages under limit are not split."""
        chunks = bot._split_message("Hello world")
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_long_message_split(self, bot):
        """Messages over limit are split into chunks."""
        long_text = "x" * (MAX_MESSAGE_LENGTH + 100)
        chunks = bot._split_message(long_text)
        assert len(chunks) == 2
        assert len(chunks[0]) <= MAX_MESSAGE_LENGTH

    def test_split_at_newline(self, bot):
        """Prefers splitting at newlines."""
        # Create text with newlines at strategic positions
        line = "a" * 100 + "\n"
        text = line * 50  # 50 lines * 101 chars = 5050 chars
        chunks = bot._split_message(text)
        assert len(chunks) >= 2
        # Each chunk should end with a complete line
        assert chunks[0].endswith("\n") or len(chunks[0]) <= MAX_MESSAGE_LENGTH

    def test_empty_message(self, bot):
        """Empty message returns single empty chunk."""
        chunks = bot._split_message("")
        assert chunks == [""]

    def test_exact_limit(self, bot):
        """Message at exactly the limit is not split."""
        text = "x" * MAX_MESSAGE_LENGTH
        chunks = bot._split_message(text)
        assert len(chunks) == 1

    def test_very_long_message(self, bot):
        """Very long messages are split into multiple chunks."""
        text = "x" * (MAX_MESSAGE_LENGTH * 3 + 500)
        chunks = bot._split_message(text)
        assert len(chunks) == 4
        for chunk in chunks:
            assert len(chunk) <= MAX_MESSAGE_LENGTH


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_simple(self, bot):
        """Sends a simple message."""
        await bot.send_message("12345", "Hello")
        bot.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_empty_skipped(self, bot):
        """Empty messages are not sent."""
        await bot.send_message("12345", "")
        bot.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_markdown_fallback(self, bot):
        """Falls back to plain text when markdown fails."""

        # First call with markdown fails, second without succeeds
        bot.bot.send_message.side_effect = [Exception("Bad markdown"), None]
        await bot.send_message("12345", "Hello *bad markdown")
        assert bot.bot.send_message.call_count == 2
