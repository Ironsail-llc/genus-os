"""Delivery accounting for interactive Telegram replies.

``delivery.deliver()`` is never called from ``telegram.py`` — the scheduler,
workflow and hook paths own it — so every ``trigger_type='telegram'`` run
used to land in ``agent_runs`` with ``delivery_status IS NULL``. The
operator's primary interface was the one surface with no delivery
accounting at all: a reply that never reached the chat looked exactly like
one that did.

These tests pin the discipline ``robothor/engine/alerts.py`` already
applies (``delivered = bool(sent)``): the status comes from the sender's
actual return value, and a bookkeeping failure never breaks the reply.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.chat import _sessions, get_shared_session
from robothor.engine.models import AgentRun, RunStatus, TriggerType
from robothor.engine.telegram import TelegramBot


@pytest.fixture
def bot(engine_config):
    """A TelegramBot with mocked aiogram Bot/Dispatcher."""
    _sessions.clear()
    with (
        patch("robothor.engine.telegram.Bot") as mock_bot_cls,
        patch("robothor.engine.telegram.Dispatcher"),
    ):
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        mock_bot.edit_message_text = AsyncMock()
        mock_bot.delete_message = AsyncMock()
        mock_bot.send_chat_action = AsyncMock()
        mock_bot_cls.return_value = mock_bot

        tg = TelegramBot(engine_config, MagicMock())
        tg.bot = mock_bot
        yield tg
    _sessions.clear()


@pytest.fixture
def persist_spy():
    """Intercept the delivery-status DB write at its source module.

    ``telegram.py`` imports ``_persist_delivery_status`` lazily inside the
    bookkeeping helper, so patching the source module is what takes effect.
    """
    with patch(
        "robothor.engine.delivery._persist_delivery_status",
        new_callable=AsyncMock,
    ) as spy:
        yield spy


def _completed_run(output_text: str = "Here is your answer") -> AgentRun:
    return AgentRun(
        id="run-interactive-1",
        status=RunStatus.COMPLETED,
        output_text=output_text,
        trigger_type=TriggerType.TELEGRAM,
        trigger_detail="chat:12345",
    )


async def _drive_interactive(bot: TelegramBot, run: AgentRun) -> None:
    """Run one interactive turn to completion."""
    bot.runner.execute = AsyncMock(return_value=run)
    session_key = bot._session_key("12345")
    session = get_shared_session(session_key)
    await bot._run_interactive("12345", session_key, session, "hello")
    task = bot._active_tasks.get("12345")
    if task:
        await task


class TestInteractiveDeliveryTruth:
    @pytest.mark.asyncio
    async def test_successful_send_records_delivered(self, bot, persist_spy):
        """A send that returns messages marks the run delivered + timestamped."""
        run = _completed_run()
        bot.send_message = AsyncMock(return_value=[MagicMock(message_id=777)])

        await _drive_interactive(bot, run)

        assert run.delivery_status == "delivered"
        assert run.delivered_at is not None
        assert run.delivery_channel == "telegram"
        persist_spy.assert_awaited()
        assert persist_spy.await_args.args[0] is run

    @pytest.mark.asyncio
    async def test_empty_send_records_failure(self, bot, persist_spy):
        """A send that returns nothing must NOT be recorded as delivered.

        ``TelegramBot.send_message`` swallows per-chunk exceptions and
        returns ``[]`` on total failure — the same shape that hid 432+
        undelivered alerts behind an assumed success.
        """
        run = _completed_run()
        bot.send_message = AsyncMock(return_value=[])

        await _drive_interactive(bot, run)

        assert run.delivery_status is not None
        assert run.delivery_status.startswith("failed")
        assert run.delivered_at is None
        assert run.delivery_channel == "telegram"
        persist_spy.assert_awaited()

    @pytest.mark.asyncio
    async def test_persistence_failure_does_not_break_the_reply(self, bot):
        """A bookkeeping exception never reaches the user's reply path."""
        run = _completed_run("The answer is 42")
        bot.send_message = AsyncMock(return_value=[MagicMock(message_id=778)])

        with patch(
            "robothor.engine.delivery._persist_delivery_status",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ) as boom:
            await _drive_interactive(bot, run)

        boom.assert_awaited()
        sent_texts = [str(call) for call in bot.send_message.call_args_list]
        assert any("The answer is 42" in text for text in sent_texts), sent_texts
        assert not any("Internal error" in text for text in sent_texts), sent_texts

    @pytest.mark.asyncio
    async def test_error_reply_is_also_accounted(self, bot, persist_spy):
        """A run that failed still reports its reply's delivery outcome."""
        run = AgentRun(
            id="run-interactive-err",
            status=RunStatus.FAILED,
            output_text=None,
            error_message="model timeout",
            trigger_type=TriggerType.TELEGRAM,
        )
        bot.send_message = AsyncMock(return_value=[MagicMock(message_id=779)])

        await _drive_interactive(bot, run)

        assert run.delivery_status == "delivered"
        assert run.delivery_channel == "telegram"

    @pytest.mark.asyncio
    async def test_deep_mode_reply_is_accounted(self, bot, persist_spy):
        """``/deep`` replies get the same accounting as the main run path."""
        run = AgentRun(
            id="run-deep-1",
            status=RunStatus.COMPLETED,
            output_text="Deep analysis result",
            trigger_type=TriggerType.TELEGRAM,
            duration_ms=42500,
            total_cost_usd=0.75,
        )
        bot.runner.execute_deep = AsyncMock(return_value=run)
        bot.send_message = AsyncMock(return_value=[])

        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        message: Any = MagicMock()
        message.chat.id = 12345

        await bot._run_deep_mode("12345", session_key, session, "analyze", message)

        assert run.delivery_status is not None
        assert run.delivery_status.startswith("failed")
        assert run.delivery_channel == "telegram"

    @pytest.mark.asyncio
    async def test_plan_mode_reply_is_accounted(self, bot, persist_spy):
        """``/plan`` exploration replies are accounted too."""
        run = AgentRun(
            id="run-plan-1",
            status=RunStatus.COMPLETED,
            output_text="Step 1. Do the thing.\n[PLAN_READY]",
            trigger_type=TriggerType.TELEGRAM,
        )
        bot.runner.execute = AsyncMock(return_value=run)
        bot.send_message = AsyncMock(return_value=[MagicMock(message_id=780)])

        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        message: Any = MagicMock()
        message.chat.id = 12345
        message.from_user.id = 12345

        with patch(
            "robothor.engine.telegram_plan_mode.save_plan_state_async", new_callable=AsyncMock
        ):
            await bot._run_plan_mode("12345", session_key, session, "do it", message)

        assert run.delivery_status == "delivered"
        assert run.delivery_channel == "telegram"
