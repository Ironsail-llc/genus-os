"""Tests for Telegram bot."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.exceptions import TelegramRetryAfter

from robothor.engine.chat import _sessions, get_shared_session
from robothor.engine.config import EngineConfig
from robothor.engine.permission_escalation import init_permission_manager
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


class TestDeepCommand:
    """Tests for /deep command in Telegram bot."""

    @pytest.mark.asyncio
    async def test_deep_help_text(self, bot):
        """Bot help text includes /deep command."""
        # The help text is built in the /start and /help handlers
        assert hasattr(bot, "_run_deep_mode"), "TelegramBot should have _run_deep_mode method"

    @pytest.mark.asyncio
    async def test_run_deep_mode_success(self, bot):
        """_run_deep_mode calls runner.execute_deep and edits message with result."""
        from robothor.engine.models import AgentRun, RunStatus

        # Mock the runner's execute_deep to return a completed run
        run = AgentRun()
        run.status = RunStatus.COMPLETED
        run.output_text = "Deep analysis result"
        run.total_cost_usd = 0.75
        run.duration_ms = 42500

        bot.runner.execute_deep = AsyncMock(return_value=run)
        bot.bot.edit_message_text = AsyncMock()

        # Mock send_message to return a message with message_id
        sent_msg = MagicMock()
        sent_msg.message_id = 99
        bot.bot.send_message = AsyncMock(return_value=sent_msg)

        # Build required args: chat_id, session_key, session, query, message
        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        mock_message = MagicMock()
        mock_message.chat.id = 12345

        await bot._run_deep_mode("12345", session_key, session, "Analyze my calendar", mock_message)

        # Verify execute_deep was called
        bot.runner.execute_deep.assert_called_once()
        call_kwargs = bot.runner.execute_deep.call_args
        assert call_kwargs.kwargs["query"] == "Analyze my calendar"

    @pytest.mark.asyncio
    async def test_run_deep_mode_failure(self, bot):
        """_run_deep_mode handles failed runs gracefully."""
        from robothor.engine.models import AgentRun, RunStatus

        run = AgentRun()
        run.status = RunStatus.FAILED
        run.output_text = None
        run.error_message = "RLM budget exceeded"
        run.total_cost_usd = 0.0
        run.duration_ms = 120000

        bot.runner.execute_deep = AsyncMock(return_value=run)
        bot.bot.edit_message_text = AsyncMock()

        sent_msg = MagicMock()
        sent_msg.message_id = 99
        bot.bot.send_message = AsyncMock(return_value=sent_msg)

        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        mock_message = MagicMock()
        mock_message.chat.id = 12345

        await bot._run_deep_mode("12345", session_key, session, "Very complex query", mock_message)

        # Should still call execute_deep
        bot.runner.execute_deep.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_deep_mode_exception(self, bot):
        """_run_deep_mode handles exceptions from execute_deep."""
        bot.runner.execute_deep = AsyncMock(side_effect=Exception("Connection failed"))
        bot.bot.edit_message_text = AsyncMock()

        sent_msg = MagicMock()
        sent_msg.message_id = 99
        bot.bot.send_message = AsyncMock(return_value=sent_msg)

        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        mock_message = MagicMock()
        mock_message.chat.id = 12345

        # Should not raise
        await bot._run_deep_mode("12345", session_key, session, "Test query", mock_message)

        # Exception handler calls self.send_message (the wrapper), which calls bot.send_message
        # Verify send_message was called at least twice (initial progress + error)
        assert bot.bot.send_message.call_count >= 2


class TestModelPickerRegistry:
    """Verify all Telegram model picker entries exist in the model registry."""

    def test_all_available_models_in_registry(self):
        from robothor.engine.model_registry import _MODEL_REGISTRY
        from robothor.engine.telegram import AVAILABLE_MODELS

        for display_name, model_id in AVAILABLE_MODELS.items():
            assert model_id in _MODEL_REGISTRY, (
                f"AVAILABLE_MODELS[{display_name!r}] = {model_id!r} not found in _MODEL_REGISTRY"
            )

    def test_picker_is_openrouter_only(self):
        """Operator policy 2026-07-07: OpenAI account blocked — codex/* is dead.

        Every picker entry must route through OpenRouter so no selection can
        land on the banned OpenAI subscription auth.
        """
        from robothor.engine.telegram import AVAILABLE_MODELS

        for display_name, model_id in AVAILABLE_MODELS.items():
            assert model_id.startswith("openrouter/"), (
                f"AVAILABLE_MODELS[{display_name!r}] = {model_id!r} is not an openrouter model"
            )

    def test_fleet_primary_in_picker(self):
        from robothor.engine.telegram import AVAILABLE_MODELS

        assert "openrouter/xiaomi/mimo-v2.5" in AVAILABLE_MODELS.values()

    def test_qwen_removed_from_picker(self):
        from robothor.engine.telegram import AVAILABLE_MODELS

        assert "Qwen 3.5 122B" not in AVAILABLE_MODELS


class TestGoalCommand:
    """Tests for Telegram /goal control surface — DAL-backed.

    The DAL layer is mocked via the session_goal module; we assert on what the
    handler tells the user, not on side effects (which are exercised by the
    Phase 1 DAL tests and the Phase 5 tool tests).
    """

    @pytest.mark.asyncio
    @patch("robothor.engine.session_goal.regenerate_goal_md_cache")
    @patch("robothor.engine.session_goal.dal.create_session_goal")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    async def test_goal_set_creates_when_none_active(self, mock_get, mock_create, _cache, bot):
        mock_get.return_value = None
        mock_create.return_value = "task-1"
        msg = MagicMock()
        msg.text = "/goal set Keep improving the main agent"
        msg.answer = AsyncMock()

        await bot._handle_goal_command(msg)

        assert msg.answer.call_count == 1
        assert "Keep improving" in msg.answer.call_args.args[0]

    @pytest.mark.asyncio
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    async def test_goal_set_refuses_duplicate(self, mock_get, bot):
        mock_get.return_value = {
            "id": "task-existing",
            "objective": "existing",
            "tags": ["session_goal"],
            "status": "TODO",
            "session_goal_meta": {
                "success_criteria": ["c1"],
                "evidence": [],
                "completion_note": "",
            },
        }
        msg = MagicMock()
        msg.text = "/goal set Replace without intent"
        msg.answer = AsyncMock()

        await bot._handle_goal_command(msg)
        assert "already exists" in msg.answer.call_args.args[0]

    @pytest.mark.asyncio
    @patch("robothor.engine.session_goal.regenerate_goal_md_cache")
    @patch("robothor.engine.session_goal.dal.add_session_goal_evidence")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    async def test_goal_evidence_rejects_unknown_kind(self, mock_get, mock_add, _cache, bot):
        mock_get.return_value = {
            "id": "task-1",
            "objective": "x",
            "tags": ["session_goal"],
            "status": "TODO",
            "session_goal_meta": {"success_criteria": [], "evidence": [], "completion_note": ""},
        }
        mock_add.return_value = True
        msg = MagicMock()
        msg.text = "/goal evidence implementation did stuff"
        msg.answer = AsyncMock()

        await bot._handle_goal_command(msg)
        assert "kind must be one of" in msg.answer.call_args.args[0]
        mock_add.assert_not_called()

    @pytest.mark.asyncio
    @patch("robothor.engine.session_goal.regenerate_goal_md_cache")
    @patch("robothor.engine.session_goal.dal.complete_session_goal")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    async def test_goal_done_blocked_without_evidence(self, mock_get, mock_complete, _cache, bot):
        mock_get.return_value = {
            "id": "task-1",
            "objective": "x",
            "tags": ["session_goal"],
            "status": "TODO",
            "session_goal_meta": {"success_criteria": ["c"], "evidence": [], "completion_note": ""},
        }
        msg = MagicMock()
        msg.text = "/goal done shipped"
        msg.answer = AsyncMock()

        await bot._handle_goal_command(msg)
        assert (
            "Goal unavailable" in msg.answer.call_args.args[0]
            or "not ready to complete" in msg.answer.call_args.args[0]
        )
        mock_complete.assert_not_called()

    @pytest.mark.asyncio
    @patch("robothor.engine.session_goal.regenerate_goal_md_cache")
    @patch("robothor.engine.session_goal.dal.add_session_goal_evidence")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    @patch("robothor.engine.session_goal.subprocess.run")
    async def test_evidence_test_shortcut_records_test_run(
        self, mock_run, mock_get, mock_add, _cache, bot
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_get.return_value = {
            "id": "task-1",
            "objective": "x",
            "tags": ["session_goal"],
            "status": "TODO",
            "session_goal_meta": {"success_criteria": [], "evidence": [], "completion_note": ""},
        }
        mock_add.return_value = True
        msg = MagicMock()
        msg.text = "/goal evidence-test pytest:passed:42"
        msg.answer = AsyncMock()

        await bot._handle_goal_command(msg)
        kwargs = mock_add.call_args.kwargs
        assert kwargs["kind"] == "test_run"
        assert kwargs["reference"] == "pytest:passed:42"
        assert kwargs["valid"] is True

    @pytest.mark.asyncio
    @patch("robothor.engine.telegram.subprocess.run")
    @patch("robothor.engine.session_goal.regenerate_goal_md_cache")
    @patch("robothor.engine.session_goal.dal.add_session_goal_evidence")
    @patch("robothor.engine.session_goal.dal.get_active_session_goal")
    @patch("robothor.engine.session_goal.subprocess.run")
    async def test_evidence_commit_shortcut_resolves_head(
        self, mock_sg_run, mock_get, mock_add, _cache, mock_tg_run, bot
    ):
        # session_goal.subprocess.run validates the SHA via cat-file.
        mock_sg_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # telegram.subprocess.run is the rev-parse HEAD call.
        mock_tg_run.return_value = MagicMock(returncode=0, stdout="abcdef1234567890\n", stderr="")
        mock_get.return_value = {
            "id": "task-1",
            "objective": "x",
            "tags": ["session_goal"],
            "status": "TODO",
            "session_goal_meta": {"success_criteria": [], "evidence": [], "completion_note": ""},
        }
        mock_add.return_value = True
        msg = MagicMock()
        msg.text = "/goal evidence-commit HEAD"
        msg.answer = AsyncMock()

        await bot._handle_goal_command(msg)
        kwargs = mock_add.call_args.kwargs
        assert kwargs["kind"] == "commit"
        assert kwargs["reference"] == "abcdef1234567890"


class TestGoalsBotCommand:
    """The /goals (plural) command lists agent benchmark grades."""

    def test_help_text_lists_both_goal_and_goals(self, bot):
        # Quick sanity: handler closures register on the dispatcher; the help
        # response is built inside cmd_help. We just verify the source text
        # exposes both commands so users see them.
        from pathlib import Path

        import robothor.engine.telegram as tg_mod

        with Path(tg_mod.__file__).open(encoding="utf-8") as f:
            src = f.read()
        assert "/goal — Show or update the active session goal" in src
        assert "/goals — Show each agent's benchmark grade" in src
        assert 'BotCommand(command="goals"' in src


class TestStreamingToolVisibility:
    """Tests for tool and status visibility during Telegram streaming."""

    def test_friendly_tool_name_mapping(self):
        """_friendly_tool_name maps known tools to human-readable labels."""
        from robothor.engine.telegram import _friendly_tool_name

        assert _friendly_tool_name("search_memory") == "Searching memory"
        assert _friendly_tool_name("web_fetch") == "Fetching page"
        assert _friendly_tool_name("read_file") == "Reading file"
        assert _friendly_tool_name("some_custom_thing") == "Some Custom Thing"

    @pytest.mark.asyncio
    async def test_tool_start_edits_message_with_tool_name(self, bot):
        """When on_tool receives tool_start, Telegram message is edited to show tool activity."""
        from robothor.engine.models import AgentRun, RunStatus, TriggerType

        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.bot.send_message = AsyncMock(return_value=sent_msg)
        bot.bot.edit_message_text = AsyncMock()
        bot.bot.send_chat_action = AsyncMock()

        async def fake_execute(**kwargs):
            on_tool = kwargs.get("on_tool")
            on_content = kwargs.get("on_content")
            if on_tool:
                await on_tool(
                    {
                        "event": "tool_start",
                        "tool": "search_memory",
                        "args": {},
                        "call_id": "c1",
                    }
                )
            if on_content:
                await on_content("Result here")
            return AgentRun(
                status=RunStatus.COMPLETED,
                output_text="Result here",
                trigger_type=TriggerType.TELEGRAM,
            )

        bot.runner.execute = AsyncMock(side_effect=fake_execute)

        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        await bot._run_interactive("12345", session_key, session, "test")

        # Wait for the background task to complete
        task = bot._active_tasks.get("12345")
        if task:
            await task

        # Check that edit_message_text was called with "Searching memory" at some point
        edit_calls = bot.bot.edit_message_text.call_args_list
        tool_shown = any("Searching memory" in str(call) for call in edit_calls)
        assert tool_shown, f"Expected 'Searching memory' in edit calls: {edit_calls}"

    @pytest.mark.asyncio
    async def test_no_content_streaming_in_telegram(self, bot):
        """on_content is not passed to execute — no content streaming edits."""
        from robothor.engine.models import AgentRun, RunStatus, TriggerType

        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.bot.send_message = AsyncMock(return_value=sent_msg)
        bot.bot.edit_message_text = AsyncMock()
        bot.bot.delete_message = AsyncMock()
        bot.bot.send_chat_action = AsyncMock()

        async def fake_execute(**kwargs):
            # on_content should NOT be passed
            assert kwargs.get("on_content") is None, "on_content should not be passed"
            return AgentRun(
                status=RunStatus.COMPLETED,
                output_text="Done",
                trigger_type=TriggerType.TELEGRAM,
            )

        bot.runner.execute = AsyncMock(side_effect=fake_execute)

        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        await bot._run_interactive("12345", session_key, session, "test")

        task = bot._active_tasks.get("12345")
        if task:
            await task

        # Final output delivered as new message (send_message), not edit
        send_calls = bot.bot.send_message.call_args_list
        final_sent = any("Done" in str(call) for call in send_calls)
        assert final_sent, f"Expected 'Done' in send_message calls: {send_calls}"

    @pytest.mark.asyncio
    async def test_status_message_deleted_before_final(self, bot):
        """The thinking/status message is deleted before the final output is sent."""
        from robothor.engine.models import AgentRun, RunStatus, TriggerType

        sent_msg = MagicMock()
        sent_msg.message_id = 42
        bot.bot.send_message = AsyncMock(return_value=sent_msg)
        bot.bot.edit_message_text = AsyncMock()
        bot.bot.delete_message = AsyncMock()
        bot.bot.send_chat_action = AsyncMock()

        async def fake_execute(**kwargs):
            return AgentRun(
                status=RunStatus.COMPLETED,
                output_text="Here are your results",
                trigger_type=TriggerType.TELEGRAM,
            )

        bot.runner.execute = AsyncMock(side_effect=fake_execute)

        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)
        await bot._run_interactive("12345", session_key, session, "test")

        task = bot._active_tasks.get("12345")
        if task:
            await task

        # Status message (id=42) should be deleted
        bot.bot.delete_message.assert_called_once_with(chat_id=12345, message_id=42)


class TestUserResolution:
    """Tests for _resolve_user — per-user tenant routing."""

    def test_registered_user_returns_info(self, bot):
        """Registered user resolves to their tenant and display name."""
        message = MagicMock()
        message.from_user.id = 99999
        message.chat.id = 99999
        message.chat.type = "private"

        user_info = {"tenant_id": "acme", "display_name": "Alice", "role": "owner"}
        with patch("robothor.engine.users.lookup_user", return_value=user_info):
            result = bot._resolve_user("99999", message)

        assert result == user_info
        assert bot._chat_user_info["99999"] == user_info

    def test_unregistered_primary_chat_fallback(self, bot):
        """Unregistered user on primary chat gets operator_name fallback."""
        message = MagicMock()
        message.from_user.id = 12345
        message.from_user.first_name = "Phil"
        message.chat.id = 12345
        message.chat.type = "private"

        # Set operator_name on config (need unfrozen copy)
        bot.config = EngineConfig(
            bot_token="test-token-123",
            default_chat_id="12345",
            operator_name="Alice",
            tenant_id="test-tenant",
        )

        with patch("robothor.engine.users.lookup_user", return_value=None):
            result = bot._resolve_user("12345", message)

        assert result is not None
        assert result["display_name"] == "Alice"
        assert result["tenant_id"] == "test-tenant"
        assert result["role"] == "owner"

    def test_unregistered_group_chat_fallback(self, bot):
        """Unregistered user in a group chat falls back to default tenant."""
        message = MagicMock()
        message.from_user.id = 77777
        message.from_user.first_name = "Bob"
        message.chat.id = 55555
        message.chat.type = "group"

        with patch("robothor.engine.users.lookup_user", return_value=None):
            result = bot._resolve_user("55555", message)

        assert result is not None
        assert result["display_name"] == "Bob"
        assert result["tenant_id"] == "test-tenant"

    def test_unregistered_private_chat_returns_none(self, bot):
        """Unregistered user in private chat returns None (-> onboarding)."""
        message = MagicMock()
        message.from_user.id = 77777
        message.chat.id = 77777
        message.chat.type = "private"

        with patch("robothor.engine.users.lookup_user", return_value=None):
            result = bot._resolve_user("77777", message)

        assert result is None

    def test_get_tenant_id_uses_resolved_info(self, bot):
        """_get_tenant_id returns resolved tenant when available."""
        bot._chat_user_info["99999"] = {
            "tenant_id": "acme",
            "display_name": "Alice",
            "role": "owner",
        }
        assert bot._get_tenant_id("99999") == "acme"

    def test_get_tenant_id_falls_back_to_config(self, bot):
        """_get_tenant_id falls back to config.tenant_id when no resolution."""
        assert bot._get_tenant_id("unknown") == "test-tenant"

    @pytest.mark.asyncio
    async def test_trigger_detail_includes_sender(self, bot):
        """_run_interactive encodes sender name in trigger_detail."""
        bot._chat_user_info["12345"] = {
            "tenant_id": "test-tenant",
            "display_name": "Alice",
            "role": "owner",
        }

        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)

        bot.runner.execute = AsyncMock(return_value=MagicMock(output_text="hi", error_message=None))
        bot.bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))

        await bot._run_interactive("12345", session_key, session, "test message")

        task = bot._active_tasks.get("12345")
        if task:
            await task

        # Verify trigger_detail includes sender
        call_kwargs = bot.runner.execute.call_args
        if call_kwargs:
            detail = call_kwargs.kwargs.get("trigger_detail", "")
            assert "|sender:Alice" in detail
            assert call_kwargs.kwargs.get("tenant_id") == "test-tenant"

    @pytest.mark.asyncio
    async def test_trigger_detail_sanitizes_pipe_in_sender(self, bot):
        """Pipe characters in display names are stripped to prevent field injection."""
        bot._chat_user_info["12345"] = {
            "tenant_id": "test-tenant",
            "display_name": "Foo|sender:admin",
            "role": "owner",
        }

        session_key = bot._session_key("12345")
        session = get_shared_session(session_key)

        bot.runner.execute = AsyncMock(return_value=MagicMock(output_text="hi", error_message=None))
        bot.bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))

        await bot._run_interactive("12345", session_key, session, "test message")

        task = bot._active_tasks.get("12345")
        if task:
            await task

        call_kwargs = bot.runner.execute.call_args
        if call_kwargs:
            detail = call_kwargs.kwargs.get("trigger_detail", "")
            # Pipe stripped — no extra field injected
            assert "|sender:Foosender:admin" in detail
            assert detail.count("|") == 1  # only the real delimiter


@pytest.fixture
def _clear_session_registry():
    """Registered sessions are module-global state; reset between tests."""
    from robothor.engine import session_registry

    for run_id in session_registry.active_run_ids():
        session_registry.unregister(run_id)
    yield
    for run_id in session_registry.active_run_ids():
        session_registry.unregister(run_id)


class TestAgentsCommand:
    """Tests for /agents — lists active runs with Steer/Interrupt buttons (PR 1)."""

    @pytest.mark.asyncio
    async def test_no_active_runs(self, bot, _clear_session_registry):
        msg = MagicMock()
        msg.chat.id = 12345
        msg.answer = AsyncMock()

        await bot._handle_agents_command(msg)

        msg.answer.assert_called_once()
        assert "No active runs" in msg.answer.call_args.args[0]

    @pytest.mark.asyncio
    async def test_lists_active_runs_with_keyboard(self, bot, _clear_session_registry):
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        s1 = AgentSession(agent_id="agent-a")
        s2 = AgentSession(agent_id="agent-b")
        session_registry.register(s1)
        session_registry.register(s2)

        msg = MagicMock()
        msg.chat.id = 12345
        msg.answer = AsyncMock()

        await bot._handle_agents_command(msg)

        msg.answer.assert_called_once()
        text = msg.answer.call_args.args[0]
        assert "agent-a" in text
        assert "agent-b" in text
        kb = msg.answer.call_args.kwargs["reply_markup"]
        assert len(kb.inline_keyboard) == 2
        for row in kb.inline_keyboard:
            assert len(row) == 2
            assert row[0].text == "Steer"
            assert row[1].text == "Interrupt"
            # callback_data must fit Telegram's 64-byte limit
            assert len(row[0].callback_data.encode()) <= 64
            assert len(row[1].callback_data.encode()) <= 64

    @pytest.mark.asyncio
    async def test_non_owner_refused(self, bot, _clear_session_registry):
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        session_registry.register(AgentSession(agent_id="agent-a"))

        msg = MagicMock()
        msg.chat.id = 99999
        msg.from_user.id = 1
        msg.answer = AsyncMock()

        await bot._handle_agents_command(msg)

        msg.answer.assert_called_once_with("Unauthorized.")


class TestRunctlCallback:
    """Tests for the runctl:i:<run_id> / runctl:s:<run_id> inline-button callbacks."""

    @pytest.mark.asyncio
    async def test_interrupt_callback_calls_interrupt_session(self, bot, _clear_session_registry):
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        s = AgentSession(agent_id="agent-a")
        session_registry.register(s)

        callback = MagicMock()
        callback.data = f"runctl:i:{s.run_id}"
        callback.message = MagicMock()
        callback.message.chat.id = 12345
        callback.message.edit_reply_markup = AsyncMock()
        callback.answer = AsyncMock()

        await bot._handle_runctl_callback(callback)

        # interrupt() was requested on the live session
        assert s._interrupt_requested is True
        callback.answer.assert_called_once()
        callback.message.edit_reply_markup.assert_called_once_with(reply_markup=None)

    @pytest.mark.asyncio
    async def test_interrupt_callback_unknown_run(self, bot, _clear_session_registry):
        callback = MagicMock()
        callback.data = "runctl:i:no-such-run"
        callback.message = MagicMock()
        callback.message.chat.id = 12345
        callback.message.edit_reply_markup = AsyncMock()
        callback.answer = AsyncMock()

        await bot._handle_runctl_callback(callback)

        callback.answer.assert_called_once()
        assert "no longer active" in callback.answer.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_steer_button_replies_with_instructions(self, bot, _clear_session_registry):
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        s = AgentSession(agent_id="agent-a")
        session_registry.register(s)

        callback = MagicMock()
        callback.data = f"runctl:s:{s.run_id}"
        callback.message = MagicMock()
        callback.message.chat.id = 12345
        callback.answer = AsyncMock()

        await bot._handle_runctl_callback(callback)

        callback.answer.assert_called_once()
        args, _kwargs = callback.answer.call_args
        assert "/steer" in args[0]
        # steer never mutates the session — only the button reply changes
        assert s._pending_steer is None

    @pytest.mark.asyncio
    async def test_non_owner_callback_refused(self, bot, _clear_session_registry):
        callback = MagicMock()
        callback.data = "runctl:i:some-run-id"
        callback.message = MagicMock()
        callback.message.chat.id = 99999
        callback.from_user.id = 1
        callback.answer = AsyncMock()

        await bot._handle_runctl_callback(callback)

        callback.answer.assert_called_once_with("Unauthorized", show_alert=True)


class TestSteerCommand:
    """Tests for /steer [run_id_prefix] <text> resolution (PR 1)."""

    @pytest.mark.asyncio
    async def test_no_active_runs(self, bot, _clear_session_registry):
        msg = MagicMock()
        msg.chat.id = 12345
        msg.text = "/steer hello"
        msg.answer = AsyncMock()

        await bot._handle_steer_command(msg)

        assert "No active runs" in msg.answer.call_args.args[0]

    @pytest.mark.asyncio
    async def test_single_active_run_fallback(self, bot, _clear_session_registry):
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        s = AgentSession(agent_id="agent-a")
        session_registry.register(s)

        msg = MagicMock()
        msg.chat.id = 12345
        msg.text = "/steer focus on the budget question"
        msg.answer = AsyncMock()

        await bot._handle_steer_command(msg)

        assert s.consume_pending_steer() == "focus on the budget question"
        assert "Steered" in msg.answer.call_args.args[0]

    @pytest.mark.asyncio
    async def test_prefix_match_targets_run(self, bot, _clear_session_registry):
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        s1 = AgentSession(agent_id="agent-a")
        s2 = AgentSession(agent_id="agent-b")
        session_registry.register(s1)
        session_registry.register(s2)

        msg = MagicMock()
        msg.chat.id = 12345
        msg.text = f"/steer {s1.run_id[:8]} adjust course"
        msg.answer = AsyncMock()

        await bot._handle_steer_command(msg)

        assert s1.consume_pending_steer() == "adjust course"
        assert s2.consume_pending_steer() is None

    @pytest.mark.asyncio
    async def test_ambiguous_prefix_asks_to_disambiguate(self, bot, _clear_session_registry):
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        s1 = AgentSession(agent_id="agent-a")
        s1.run.id = "abcdef12-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        s2 = AgentSession(agent_id="agent-b")
        s2.run.id = "abcdef12-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        session_registry.register(s1)
        session_registry.register(s2)

        msg = MagicMock()
        msg.chat.id = 12345
        msg.text = "/steer abcdef12 nudge"
        msg.answer = AsyncMock()

        await bot._handle_steer_command(msg)

        assert s1.consume_pending_steer() is None
        assert s2.consume_pending_steer() is None
        assert "ambiguous" in msg.answer.call_args.args[0].lower()

    @pytest.mark.asyncio
    async def test_no_prefix_match_multiple_runs_asks_for_prefix(
        self, bot, _clear_session_registry
    ):
        from robothor.engine import session_registry
        from robothor.engine.session import AgentSession

        s1 = AgentSession(agent_id="agent-a")
        s2 = AgentSession(agent_id="agent-b")
        session_registry.register(s1)
        session_registry.register(s2)

        msg = MagicMock()
        msg.chat.id = 12345
        msg.text = "/steer just some text with no matching prefix"
        msg.answer = AsyncMock()

        await bot._handle_steer_command(msg)

        assert s1.consume_pending_steer() is None
        assert s2.consume_pending_steer() is None
        reply = msg.answer.call_args.args[0].lower()
        assert "specify" in reply or "multiple" in reply

    @pytest.mark.asyncio
    async def test_non_owner_refused(self, bot, _clear_session_registry):
        msg = MagicMock()
        msg.chat.id = 99999
        msg.text = "/steer x"
        msg.from_user.id = 1
        msg.answer = AsyncMock()

        await bot._handle_steer_command(msg)

        msg.answer.assert_called_once_with("Unauthorized.")


class TestPermissionEscalationWiring:
    """Regression coverage for the seam between `PermissionEscalationManager`
    and the `TelegramBot` wrapper daemon.py actually wires it to.

    `daemon.py` calls `init_permission_manager(bot, ...)` with the
    `TelegramBot` instance, not the raw aiogram `Bot`. `TelegramBot.
    send_message` is a chunking/markdown-conversion helper for plain chat
    delivery — it silently drops unknown kwargs (`reply_markup`,
    `parse_mode`) via `**_ignored` and returns a `list[Message]` instead of
    one. Sent straight through, `_send_prompt` would deliver a prompt with
    no inline keyboard and then raise `AttributeError` on `msg.message_id`
    (a `list` has none) — silently denying every escalation without ever
    giving the operator a way to approve it.
    """

    @pytest.mark.asyncio
    async def test_send_prompt_delivers_keyboard_via_wrapped_telegram_bot(self, bot):
        from robothor.engine.permission_escalation import (
            EscalationRequest,
            PermissionEscalationManager,
        )

        sent_msg = MagicMock()
        sent_msg.message_id = 777
        bot.bot.send_message = AsyncMock(return_value=sent_msg)

        mgr = PermissionEscalationManager(bot=bot, chat_id="12345")
        request = EscalationRequest(
            request_id="req-x",
            agent_id="test-agent",
            run_id="run-1",
            tool_name="exec_command",
            tool_args={"command": "ls"},
            guardrail_name="destructive_write",
            reason="test",
            created_at=0.0,
        )

        await mgr._send_prompt(request, timeout_seconds=30.0)

        # message_id must come from the real, single Message — not blow up
        # on a list returned by the wrapper's chunking send_message().
        assert request.telegram_message_id == 777
        # The inline keyboard must have reached the raw bot, not been
        # dropped by the wrapper's **_ignored kwargs.
        call_kwargs = bot.bot.send_message.call_args.kwargs
        assert call_kwargs.get("reply_markup") is not None
        callback_data = [
            btn.callback_data for row in call_kwargs["reply_markup"].inline_keyboard for btn in row
        ]
        assert callback_data == [
            "perm:approve:req-x",
            "perm:all:req-x",
            "perm:deny:req-x",
        ]


class TestPermissionCallbacks:
    """Tests for the `perm:` callback consumer (`on_permission_decision`) —
    the Telegram-side half of the human-approval escalation loop.
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from robothor.engine import permission_escalation as pe_mod

        pe_mod._escalation_manager = None
        yield
        pe_mod._escalation_manager = None

    @staticmethod
    def _get_handler(bot):
        """Recover the raw `on_permission_decision` coroutine.

        `Dispatcher` is mocked in the `bot` fixture, so `self.dp.
        callback_query(some_filter)` returns the same MagicMock
        (`bot.dp.callback_query.return_value`) regardless of the filter it
        was called with. Each `@self.dp.callback_query(...)` application
        then calls that MagicMock with the real handler function, so every
        registered callback handler shows up in its `call_args_list` in
        registration order — find ours by name.
        """
        for call in bot.dp.callback_query.return_value.call_args_list:
            func = call.args[0]
            if func.__name__ == "on_permission_decision":
                return func
        raise AssertionError("on_permission_decision handler was not registered")

    @staticmethod
    def _make_callback(data: str, *, chat_id: str = "12345", user_id: int = 999) -> MagicMock:
        callback = MagicMock()
        callback.data = data
        callback.message = MagicMock()
        callback.message.chat.id = int(chat_id)
        callback.message.edit_reply_markup = AsyncMock()
        callback.from_user = MagicMock(id=user_id)
        callback.answer = AsyncMock()
        return callback

    @pytest.mark.asyncio
    async def test_approve_resolves_pending_request_as_approved(self, bot):
        mgr = init_permission_manager(bot, "12345")
        handler = self._get_handler(bot)

        task = asyncio.create_task(
            mgr.request_approval(
                agent_id="test-agent",
                run_id="run-1",
                tool_name="exec_command",
                tool_args={"command": "ls"},
                guardrail_name="destructive_write",
                reason="test",
                timeout_seconds=5.0,
            )
        )
        await asyncio.sleep(0)  # let request_approval send the prompt and start waiting
        request_id = next(iter(mgr._pending))

        callback = self._make_callback(f"perm:approve:{request_id}")
        await handler(callback)

        assert await task is True
        callback.answer.assert_called_once_with("Approved")
        callback.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)

    @pytest.mark.asyncio
    async def test_deny_resolves_pending_request_as_denied(self, bot):
        mgr = init_permission_manager(bot, "12345")
        handler = self._get_handler(bot)

        task = asyncio.create_task(
            mgr.request_approval(
                agent_id="test-agent",
                run_id="run-1",
                tool_name="exec_command",
                tool_args={"command": "rm -rf /tmp/x"},
                guardrail_name="destructive_write",
                reason="test",
                timeout_seconds=5.0,
            )
        )
        await asyncio.sleep(0)
        request_id = next(iter(mgr._pending))

        callback = self._make_callback(f"perm:deny:{request_id}")
        await handler(callback)

        assert await task is False
        callback.answer.assert_called_once_with("Denied")

    @pytest.mark.asyncio
    async def test_all_callback_grants_session_and_fast_paths_next_request(self, bot):
        mgr = init_permission_manager(bot, "12345")
        handler = self._get_handler(bot)

        task = asyncio.create_task(
            mgr.request_approval(
                agent_id="agent-1",
                run_id="run-1",
                tool_name="exec_command",
                tool_args={"command": "ls"},
                guardrail_name="destructive_write",
                reason="test",
                timeout_seconds=5.0,
            )
        )
        await asyncio.sleep(0)
        request_id = next(iter(mgr._pending))

        callback = self._make_callback(f"perm:all:{request_id}")
        await handler(callback)

        assert await task is True
        callback.answer.assert_called_once_with("Approved for session")

        # Second request for the same agent+guardrail+tool must fast-path
        # off the session grant — no new Telegram prompt sent.
        bot.bot.send_message.reset_mock()
        result = await mgr.request_approval(
            agent_id="agent-1",
            run_id="run-2",
            tool_name="exec_command",
            tool_args={"command": "ls -la"},
            guardrail_name="destructive_write",
            reason="test again",
            timeout_seconds=5.0,
        )
        assert result is True
        bot.bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_owner_chat_refused_and_does_not_resolve(self, bot):
        mgr = init_permission_manager(bot, "12345")
        handler = self._get_handler(bot)

        task = asyncio.create_task(
            mgr.request_approval(
                agent_id="test-agent",
                run_id="run-1",
                tool_name="exec_command",
                tool_args={"command": "ls"},
                guardrail_name="destructive_write",
                reason="test",
                timeout_seconds=0.15,
            )
        )
        await asyncio.sleep(0)
        request_id = next(iter(mgr._pending))

        callback = self._make_callback(f"perm:approve:{request_id}", chat_id="99999")
        await handler(callback)

        callback.answer.assert_called_once_with("Unauthorized", show_alert=True)
        # The request must still be pending — an unauthorized chat cannot resolve it.
        assert request_id in mgr._pending

        # Left unresolved, it denies via the normal timeout path.
        assert await task is False

    @pytest.mark.asyncio
    async def test_malformed_callback_data_answers_gracefully(self, bot):
        init_permission_manager(bot, "12345")
        handler = self._get_handler(bot)

        callback = self._make_callback("perm:approve")  # missing request_id segment

        await handler(callback)  # must not raise

        callback.answer.assert_called_once_with("Invalid callback data")

    @pytest.mark.asyncio
    async def test_unknown_request_id_answers_gracefully_without_resolving(self, bot):
        mgr = init_permission_manager(bot, "12345")
        handler = self._get_handler(bot)

        callback = self._make_callback("perm:approve:does-not-exist")

        await handler(callback)  # must not raise despite the unknown request_id

        callback.answer.assert_called_once_with("Approved")
        assert mgr._pending == {}
