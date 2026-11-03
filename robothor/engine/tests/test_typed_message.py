"""What the person typed travels beside the assembled turn.

The turn a run starts from is assembled: Telegram prepends a quote of the
fleet message being replied to and inlines the extracted text of an uploaded
file, and webchat does the same with its attachments. Credential intake must
not mistake any of that for the person handing a credential over, so each
channel passes the raw text the person typed as ``typed_message``, and the
runner keeps it on the session.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robothor.engine.chat import _sessions, get_shared_session
from robothor.engine.models import AgentRun, RunStatus, TriggerType
from robothor.engine.telegram import TelegramBot


@pytest.fixture
def bot(engine_config):
    _sessions.clear()
    with (
        patch("robothor.engine.telegram.Bot") as mock_bot_cls,
        patch("robothor.engine.telegram.Dispatcher"),
    ):
        raw = MagicMock()
        raw.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        raw.edit_message_text = AsyncMock()
        raw.delete_message = AsyncMock()
        raw.send_chat_action = AsyncMock()
        mock_bot_cls.return_value = raw
        instance = TelegramBot(engine_config, MagicMock())
        instance.bot = raw
        yield instance
    _sessions.clear()


def _text_message(text: str, *, reply_to: int | None = None):
    message = MagicMock()
    message.text = text
    message.chat.id = "12345"
    message.chat.type = "private"
    message.from_user.id = "4242"
    message.from_user.username = None
    message.from_user.first_name = "Alice"
    message.message_id = 7
    message.forward_origin = None
    message.forward_from = None
    message.forward_date = None
    if reply_to is None:
        message.reply_to_message = None
    else:
        message.reply_to_message.message_id = reply_to
    message.answer = AsyncMock()
    return message


class TestTelegram:
    @pytest.mark.asyncio
    async def test_a_quoted_reply_is_not_typed_text(self, bot):
        bot._enqueue_message = AsyncMock()
        bot._resolve_user = MagicMock(
            return_value={
                "tenant_id": "t",
                "display_name": "Alice",
                "role": "owner",
                "user_id": "u",
            }
        )
        with (
            patch(
                "robothor.engine.channel_bus.resolve_reply_context_async",
                new=AsyncMock(return_value={"platform_message_id": "5"}),
            ),
            patch(
                "robothor.engine.channel_bus.format_reply_prefix",
                return_value="> [a fleet report] OPENWEATHER_API_KEY=c0ffee42c0ffee42",
            ),
        ):
            await bot.handle_text(_text_message("store it", reply_to=5))
        args, kwargs = bot._enqueue_message.call_args
        assert "OPENWEATHER_API_KEY" in args[3], "the turn should still carry the quote"
        assert kwargs.get("typed_text") == "store it"

    @pytest.mark.asyncio
    async def test_the_drain_threads_the_typed_text_to_the_run(self, bot):
        captured: dict = {}

        async def fake_run_interactive(*a, **kw):
            captured.update(kw)

        bot._run_interactive = fake_run_interactive
        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        await bot._enqueue_message("12345", session_key, session, "> quote\n\nhi", typed_text="hi")
        await asyncio.sleep(0.5)
        assert captured.get("typed_text") == "hi"

    @pytest.mark.asyncio
    async def test_the_interactive_run_passes_it_to_the_runner(self, bot):
        run = AgentRun(
            id="r1",
            status=RunStatus.COMPLETED,
            output_text="ok",
            trigger_type=TriggerType.TELEGRAM,
            trigger_detail="chat:12345",
        )
        bot.runner.execute = AsyncMock(return_value=run)
        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        with patch("robothor.engine.delivery._persist_delivery_status", new=AsyncMock()):
            await bot._run_interactive(
                "12345", session_key, session, "> quote\n\nhi", typed_text="hi"
            )
            task = bot._active_tasks.get("12345")
            if task:
                await task
        assert bot.runner.execute.await_args.kwargs.get("typed_message") == "hi"


class TestRunner:
    @pytest.mark.asyncio
    async def test_the_session_keeps_the_typed_text(
        self, engine_config, sample_agent_config, mock_litellm_response
    ):
        from robothor.engine import session_registry
        from robothor.engine.runner import AgentRunner

        with patch("robothor.engine.runner.get_registry") as mock_reg:
            registry = MagicMock()
            registry.build_for_agent.return_value = [
                {"type": "function", "function": {"name": "list_tasks"}}
            ]
            registry.get_tool_names.return_value = ["list_tasks"]
            mock_reg.return_value = registry
            runner = AgentRunner(engine_config)
            runner.registry = registry

        seen: list = []

        async def execute(name, args, **kwargs):
            live = session_registry.lookup(kwargs.get("run_id", ""))
            seen.append(getattr(live, "typed_message", None))
            return {"ok": True}

        registry.execute = AsyncMock(side_effect=execute)
        tc = MagicMock()
        tc.id = "c1"
        tc.function.name = "list_tasks"
        tc.function.arguments = json.dumps({})
        first = mock_litellm_response(content=None, tool_calls=[tc])
        first.choices[0].message.content = None
        replies = iter([first, mock_litellm_response(content="done")])

        async def completion(**kwargs):
            return next(replies)

        with (
            patch("robothor.engine.runner.create_run"),
            patch("robothor.engine.runner.update_run"),
            patch("robothor.engine.run_finalizer.create_step"),
            patch("litellm.acompletion", side_effect=completion),
        ):
            await runner.execute(
                "test-agent",
                "> quote\n\nhi",
                trigger_type=TriggerType.MANUAL,
                agent_config=sample_agent_config,
                user_id="u",
                user_role="owner",
                typed_message="hi",
            )
        assert seen == ["hi"]


class TestForwarded:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("marker", ["forward_origin", "forward_from", "forward_date"])
    async def test_a_forwarded_message_is_not_typed_text(self, bot, marker):
        """A forwarded message is somebody else's words; the person typed none."""
        bot._enqueue_message = AsyncMock()
        bot._resolve_user = MagicMock(
            return_value={
                "tenant_id": "t",
                "display_name": "Alice",
                "role": "owner",
                "user_id": "u",
            }
        )
        # Built at run time: a literal key-shaped string trips secret scanning.
        message = _text_message("OPENWEATHER_API_KEY=" + "c0ffee42" * 4)
        setattr(message, marker, MagicMock())
        await bot.handle_text(message)
        _args, kwargs = bot._enqueue_message.call_args
        assert kwargs.get("typed_text") == ""


class TestBuffersAreForgotten:
    @pytest.mark.asyncio
    async def test_stop_drops_the_pending_typed_text(self, bot):
        bot._message_buffers["12345"] = ["queued"]
        bot._typed_buffers["12345"] = ["queued"]
        message = _text_message("/stop")
        await bot.cmd_stop(message)
        assert "12345" not in bot._typed_buffers
        assert "12345" not in bot._message_buffers

    @pytest.mark.asyncio
    async def test_shutdown_drops_every_pending_typed_text(self, bot):
        bot._typed_buffers["12345"] = ["queued"]
        bot.dp.stop_polling = AsyncMock()
        bot.bot.session.close = AsyncMock()
        await bot.stop(polling_stop_timeout=0.01)
        assert bot._typed_buffers == {}
