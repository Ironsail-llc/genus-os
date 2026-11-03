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


def _loaded(messages):
    """What ``load_latest`` hands back for a row holding *messages*."""
    from robothor.engine.checkpoint import CHECKPOINT_SCHEMA_VERSION

    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    cursor = MagicMock()
    cursor.fetchone.return_value = {
        "step_number": 3,
        "messages": messages,
        "scratchpad": None,
        "plan": None,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
    }
    conn.cursor.return_value = cursor
    with patch("robothor.db.connection.get_connection", return_value=conn):
        return CheckpointManager.load_latest("test")


class TestRestore:
    def _with_request(self, request: str):
        from robothor.engine.task_context import install_context, make_context

        messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": request}]
        install_context(messages, make_context(request, [], run_id="test"))
        return messages

    def test_a_checkpoint_written_before_the_fix_is_redacted_on_restore(self):
        register_known_value(BARE, fingerprint="test")
        loaded = _loaded(self._with_request(f"my openweather key is {BARE}"))
        assert loaded is not None
        assert BARE not in json.dumps(loaded["messages"])

    def test_a_resumed_runs_originating_message_has_the_value_redacted(self):
        from robothor.engine.task_context import read_context

        register_known_value(BARE, fingerprint="test")
        loaded = _loaded(self._with_request(f"my openweather key is {BARE}"))
        record = read_context(loaded["messages"])
        assert record is not None, "redaction broke the task-context record"
        assert BARE not in record["request"]
        assert "my openweather key is" in record["request"]


class TestWrappedCall:
    def test_a_tool_call_wrapped_vault_set_is_not_checkpointed(self):
        wrapped = {"name": "vault_set", "arguments": {"key": "providers/x/api_key", "value": BARE}}
        turn = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "tool_call", "arguments": json.dumps(wrapped)},
                }
            ],
        }
        assert BARE not in _saved([turn])


#: A stored key below the opaque-run threshold (32), so only registration of
#: the vault's values can catch it on restore.
STORED_24 = "Ab3dE6gH9jK2mN5pQ8sT1vW4"
GUID = "3f8e2a1c-9b7d-4e6f-a5c3-1d2e3f4a5b6c"
CHAT_ID = "-1001234567890"


class TestRestoreAfterARestart:
    """The registry of handed-over values lives in one process and dies with
    it; a checkpoint lives thirty days. A row written before redaction, read
    back by a fresh engine, must still lose the value."""

    def _with_request(self, request: str):
        return TestRestore()._with_request(request)

    def test_a_value_the_vault_holds_is_redacted_by_a_fresh_process(self):
        forget_known_values()  # a fresh process: nothing registered yet
        messages = self._with_request(f"my openweather key is {STORED_24}")
        messages.append({"role": "assistant", "content": f"Stored {STORED_24} for you."})
        with patch("robothor.vault.export_env", return_value={"OPENWEATHER_API_KEY": STORED_24}):
            loaded = _loaded(messages)
        leaked = STORED_24 in json.dumps(loaded["messages"])
        assert not leaked, "a stored value came back from an old checkpoint"

    def test_identifiers_the_vault_holds_are_not_redacted_afterwards(self):
        """The vault also holds a tenant GUID and a chat id; after a restore
        they must not become placeholders in every later tool result."""
        from robothor.secrets.redaction import redact_tool_result

        forget_known_values()
        vault = {"M365_TENANT_ID": GUID, "TELEGRAM_CHAT_ID": CHAT_ID, "X_API_KEY": STORED_24}
        with patch("robothor.vault.export_env", return_value=vault):
            _loaded(self._with_request("hello"))
        config = f"tenant: {GUID}\nchat: {CHAT_ID}\n"
        assert redact_tool_result({"content": config}) == {"content": config}
        later = redact_tool_result({"content": f"key {STORED_24}"})["content"]
        leaked = STORED_24 in later
        assert not leaked, "a stored key was not registered for later redaction"

    def test_a_bare_hex_run_the_person_typed_is_redacted_even_if_never_stored(self):
        never_stored = "9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a"
        with patch("robothor.vault.export_env", return_value={}):
            loaded = _loaded(self._with_request(f"my openweather key is {never_stored}"))
        leaked = never_stored in json.dumps(loaded["messages"])
        assert not leaked

    def test_a_git_sha_in_a_tool_result_survives_restore(self):
        sha = "3f2a9c1e7b4d6a8f0c2e4b6d8a0c2e4f6b8d0a2c"
        messages = self._with_request("what changed?")
        messages.append({"role": "tool", "tool_call_id": "c1", "content": f"commit {sha}"})
        with patch("robothor.vault.export_env", return_value={}):
            loaded = _loaded(messages)
        assert sha in json.dumps(loaded["messages"])

    def test_an_unreadable_vault_still_restores(self):
        with patch("robothor.vault.export_env", side_effect=RuntimeError("no master key")):
            loaded = _loaded(self._with_request("hello"))
        assert loaded is not None
