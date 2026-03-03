"""Tests for Telegram bot."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramRetryAfter

from robothor.engine.chat import _sessions, get_shared_session
from robothor.engine.telegram import MAX_MESSAGE_LENGTH, TelegramBot


@pytest.fixture
def bot(engine_config):
    """Create a TelegramBot with mocked dependencies."""
    _sessions.clear()
    with patch("robothor.engine.telegram.Bot") as mock_bot_cls:
        with patch("robothor.engine.telegram.Dispatcher"):
            mock_bot = MagicMock()
            mock_bot.send_message = AsyncMock()
            mock_bot_cls.return_value = mock_bot

            runner = MagicMock()
            bot = TelegramBot(engine_config, runner)
            bot.bot = mock_bot
            yield bot
    _sessions.clear()


class TestChatHistory:
    def test_shared_session_initially_empty(self, bot):
        """Shared session history starts empty."""
        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        assert session.history == []

    def test_clear_history(self, bot):
        """Clear removes chat history from shared session."""
        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        session.history.extend(
            [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ]
        )
        session.history.clear()
        assert session.history == []

    def test_history_cap(self, bot):
        """History is capped at max_history entries."""
        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        # Add 50 messages (25 turns) — should be capped at 40
        for i in range(25):
            session.history.append({"role": "user", "content": f"msg {i}"})
            session.history.append({"role": "assistant", "content": f"reply {i}"})
        if len(session.history) > bot._max_history:
            session.history[:] = session.history[-bot._max_history :]
        assert len(session.history) == 40

    def test_reset_clears_history(self, bot):
        """Reset clears both model override and shared session history."""
        bot._model_override["12345"] = "some-model"
        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        session.history.append({"role": "user", "content": "test"})
        # Simulate /reset behavior
        bot._model_override.pop("12345", None)
        session.history.clear()
        session.model_override = None
        assert "12345" not in bot._model_override
        assert session.history == []

    def test_max_history_default(self, bot):
        """Default max history is 40 (matching chat.py MAX_HISTORY)."""
        assert bot._max_history == 40


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


class TestConcurrentHistory:
    """Tests for concurrent execution without locks — both channels' messages appear."""

    @pytest.mark.asyncio
    async def test_both_channels_messages_in_history(self, bot):
        """After concurrent execution, all 4 messages appear in history."""
        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)

        async def channel_work(channel: str, msg: str) -> None:
            _ = list(session.history)
            await asyncio.sleep(0.01)  # simulate LLM call
            session.history.append({"role": "user", "content": f"{channel}: {msg}"})
            session.history.append({"role": "assistant", "content": f"re: {channel}: {msg}"})

        await asyncio.gather(
            channel_work("telegram", "hello"),
            channel_work("helm", "world"),
        )

        assert len(session.history) == 4, "Both exchanges must be present"
        # All 4 messages present (order-agnostic)
        contents = {m["content"] for m in session.history}
        assert "telegram: hello" in contents
        assert "re: telegram: hello" in contents
        assert "helm: world" in contents
        assert "re: helm: world" in contents


def _make_flood_error(retry_after: int = 0) -> TelegramRetryAfter:
    """Create a TelegramRetryAfter exception for testing."""
    method = MagicMock()
    type(method).__name__ = "sendMessage"
    return TelegramRetryAfter(method=method, message="Flood control", retry_after=retry_after)


class TestFloodControl:
    """Tests for Telegram flood control (rate limit) retry logic."""

    @pytest.mark.asyncio
    async def test_retry_on_flood_succeeds_after_retry(self, bot):
        """Retries on TelegramRetryAfter and succeeds."""
        call_count = 0

        async def flaky():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _make_flood_error(retry_after=0)
            return "ok"

        result = await bot._retry_on_flood(flaky)
        assert result == "ok"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_retry_on_flood_raises_after_max_retries(self, bot):
        """Raises TelegramRetryAfter when all retries exhausted."""

        async def always_flood():
            raise _make_flood_error(retry_after=0)

        with pytest.raises(TelegramRetryAfter):
            await bot._retry_on_flood(always_flood, max_retries=2)

    @pytest.mark.asyncio
    async def test_retry_on_flood_passes_non_flood_exceptions(self, bot):
        """Non-flood exceptions are not caught."""

        async def bad():
            raise ValueError("not a flood")

        with pytest.raises(ValueError, match="not a flood"):
            await bot._retry_on_flood(bad)

    @pytest.mark.asyncio
    async def test_send_message_retries_on_flood(self, bot):
        """send_message retries on flood control and succeeds."""
        flood = _make_flood_error(retry_after=0)
        bot.bot.send_message = AsyncMock(side_effect=[flood, None])

        await bot.send_message("12345", "Hello")
        assert bot.bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_send_message_flood_exhausted_falls_to_plain(self, bot):
        """When HTML send exhausts retries, falls back to plain text."""
        flood = _make_flood_error(retry_after=0)
        # 3 flood errors for HTML (exhausts retries) → then plain succeeds
        bot.bot.send_message = AsyncMock(side_effect=[flood, flood, flood, None])

        await bot.send_message("12345", "Hello")
        assert bot.bot.send_message.call_count == 4
        # Last call should be plain text (parse_mode=None)
        last_call = bot.bot.send_message.call_args
        assert last_call.kwargs.get("parse_mode") is None

    @pytest.mark.asyncio
    async def test_edit_final_retries_on_flood(self, bot):
        """_edit_final retries on flood control and succeeds."""
        flood = _make_flood_error(retry_after=0)
        bot.bot.edit_message_text = AsyncMock(side_effect=[flood, None])

        await bot._edit_final("12345", 42, "Final text")
        assert bot.bot.edit_message_text.call_count == 2

    @pytest.mark.asyncio
    async def test_edit_final_flood_exhausted_falls_to_plain(self, bot):
        """When HTML edit exhausts retries, falls back to plain text."""
        flood = _make_flood_error(retry_after=0)
        # 3 flood errors for HTML (exhausts retries) → then plain succeeds
        bot.bot.edit_message_text = AsyncMock(side_effect=[flood, flood, flood, None])

        await bot._edit_final("12345", 42, "Final text")
        assert bot.bot.edit_message_text.call_count == 4
        # Last call should be plain text (parse_mode=None)
        last_call = bot.bot.edit_message_text.call_args
        assert last_call.kwargs.get("parse_mode") is None
