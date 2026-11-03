"""A credential the person hands over does not stay at rest anywhere.

The person's own turn has to reach the model once, or ``vault_set`` cannot
store the value, so ``session.messages`` keeps it raw for that turn. Two writers
copied that raw turn out of the process:

* the checkpoint writer serialised ``session.messages`` as it stood, so any
  chat that handed over a key and then made more tool calls left the value in
  ``agent_run_checkpoints``, and resume read it back;
* the Telegram handler logged the first 100 characters of every message at
  INFO, before any intake code ran.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.checkpoint import CheckpointManager
from robothor.secrets.redaction import forget_known_values, register_known_value

SHAPED = "ghp_FAKE0000_must_not_reach_a_stored_row_0000"
BARE = "3f9a1c7e5b2d4a6f8e0c9b1d3a5f7e2c"


@pytest.fixture(autouse=True)
def _no_known_values():
    forget_known_values()
    yield
    forget_known_values()


def _saved(messages):
    mgr = CheckpointManager(run_id="test")
    mgr.record_success()
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    cursor = MagicMock()
    conn.cursor.return_value = cursor
    with patch("robothor.db.connection.get_connection", return_value=conn):
        assert mgr.save(3, messages) is True
    return cursor.execute.call_args.args[1][2]


class TestCheckpoints:
    def test_a_credential_in_the_persons_turn_is_not_checkpointed(self):
        turn = {"role": "user", "content": f"here is my github token {SHAPED}"}

        stored = _saved([{"role": "system", "content": "sys"}, turn])

        assert SHAPED not in stored
        assert "here is my github token" in json.loads(stored)[1]["content"]

    def test_a_stored_value_is_not_checkpointed_even_without_a_known_shape(self):
        register_known_value(BARE, fingerprint="test")
        turn = {"role": "user", "content": f"my openweather key is {BARE}"}

        assert BARE not in _saved([turn])

    def test_a_credential_in_a_multimodal_turn_is_not_checkpointed(self):
        turn = {
            "role": "user",
            "content": [{"type": "text", "text": f"token: {SHAPED}"}],
        }

        assert SHAPED not in _saved([turn])

    def test_the_live_session_keeps_the_turn_it_has_to_read(self):
        turn = {"role": "user", "content": f"here is my github token {SHAPED}"}
        messages = [turn]

        _saved(messages)

        assert messages[0]["content"].endswith(SHAPED)


OPERATOR = "12345"  # engine_config.default_chat_id


@pytest.fixture
def bot(engine_config):
    from robothor.engine.telegram import TelegramBot

    with (
        patch("robothor.engine.telegram.Bot") as mock_bot_cls,
        patch("robothor.engine.telegram.Dispatcher"),
    ):
        raw = MagicMock()
        raw.send_message = AsyncMock()
        mock_bot_cls.return_value = raw
        instance = TelegramBot(engine_config, MagicMock())
        instance.bot = raw
        instance._enqueue_message = AsyncMock()
        instance._resolve_user = MagicMock(
            return_value={
                "tenant_id": "test-tenant",
                "display_name": "Alice",
                "role": "owner",
                "user_id": "tu-1",
            }
        )
        yield instance


def _message(text: str):
    message = MagicMock()
    message.text = text
    message.chat.id = OPERATOR
    message.chat.type = "private"
    message.from_user.id = "4242"
    message.from_user.username = None
    message.from_user.first_name = "Alice"
    message.message_id = 7
    message.reply_to_message = None
    return message


class TestTelegramLog:
    @pytest.mark.asyncio
    async def test_the_message_text_is_not_logged(self, bot, caplog):
        caplog.set_level(logging.DEBUG, logger="robothor.engine.telegram_handlers")

        await bot.handle_text(_message(f"my openweather key is {BARE}"))

        bot._enqueue_message.assert_called_once()
        assert BARE not in caplog.text
        assert "openweather" not in caplog.text
