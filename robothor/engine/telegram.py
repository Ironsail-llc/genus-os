"""
Telegram Bot — aiogram v3 bot for interactive chat and delivery.

Features:
- Streaming text delivery with "Thinking..." indicator and block cursor
- Typing indicator while the agent is processing
- /model command with inline keyboard for model switching
- /reset, /stop, /status, /help commands
- HTML parse mode (more reliable than Markdown for Telegram)
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from robothor.engine.chat_store import (
    clear_session_async,
    load_all_sessions,
    save_exchange_async,
    update_model_override_async,
)
from robothor.engine.delivery import set_telegram_sender
from robothor.engine.models import TriggerType

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner

logger = logging.getLogger(__name__)

# ── Constants ──

MAX_MESSAGE_LENGTH = 4096
STREAM_CURSOR = " \u258d"  # ▍ block cursor
STREAM_EDIT_INTERVAL = 0.5  # seconds between message edits
STREAM_MIN_NEW_CHARS = 20  # min new chars before editing
TYPING_INTERVAL = 4  # seconds between typing indicator refreshes
THINKING_TEXT = "\u2728 Thinking..."  # shown instantly while LLM starts up

# Models available for /model selection (display name → litellm model id)
AVAILABLE_MODELS: dict[str, str] = {
    "Claude Sonnet 4.6": "anthropic/claude-sonnet-4-6",
    "Kimi K2.5": "openrouter/moonshotai/kimi-k2.5",
    "Gemini 2.5 Pro": "gemini/gemini-2.5-pro",
    "Gemini 2.5 Flash": "gemini/gemini-2.5-flash",
}

# Reverse lookup: model id → display name
MODEL_DISPLAY_NAMES = {v: k for k, v in AVAILABLE_MODELS.items()}


def _md_to_html(text: str) -> str:
    """Best-effort Markdown → Telegram HTML conversion.

    Handles: **bold**, *italic*, `code`, ```code blocks```, [links](url).
    Escapes raw HTML first so user content is safe.
    """
    # Escape any existing HTML entities in the source text
    text = html.escape(text)
    # Code blocks (``` ... ```)
    text = re.sub(r"```(\w*)\n(.*?)```", r"<pre>\2</pre>", text, flags=re.DOTALL)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Bold (**text** or __text__)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    # Italic (*text* or _text_) — careful not to match inside URLs or words with underscores
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", text)
    # Links [text](url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


class TelegramBot:
    """Aiogram v3 Telegram bot for Robothor."""

    def __init__(self, config: EngineConfig, runner: AgentRunner) -> None:
        self.config = config
        self.runner = runner
        self.bot = Bot(
            token=config.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()

        # Per-chat state (in-memory, resets on restart)
        self._model_override: dict[str, str] = {}  # chat_id → model_id
        self._active_tasks: dict[str, asyncio.Task[Any]] = {}  # chat_id → running task
        self._chat_history: dict[str, list[dict[str, str]]] = {}  # chat_id → messages

        # Max conversation history entries (user + assistant pairs)
        self._max_history = 20

        self._setup_handlers()

        # Register send function for delivery module
        set_telegram_sender(self.send_message)

    def _setup_handlers(self) -> None:
        """Register all message and callback handlers."""

        # ── Slash commands ──

        @self.dp.message(Command("help"))
        async def cmd_help(message: Message) -> None:
            await message.answer(
                "<b>Robothor Commands</b>\n\n"
                "/model — Switch AI model\n"
                "/clear — Clear conversation history\n"
                "/context — Context window stats\n"
                "/reset — Reset model + history\n"
                "/stop — Cancel current response\n"
                "/status — Engine health\n"
                "/help — This message",
            )

        @self.dp.message(Command("model"))
        async def cmd_model(message: Message) -> None:
            chat_id = str(message.chat.id)
            override = self._model_override.get(chat_id)
            if override:
                current = override
                current_name = MODEL_DISPLAY_NAMES.get(current, current)
                status_line = f"<b>Current model:</b> {html.escape(current_name)} (override)"
            else:
                current = self._get_manifest_primary()
                current_name = MODEL_DISPLAY_NAMES.get(current, current)
                status_line = (
                    f"<b>Current model:</b> {html.escape(current_name)} (manifest default)"
                )
            kb = self._build_model_keyboard(current)
            await message.answer(
                f"{status_line}\n\nTap to switch:",
                reply_markup=kb,
            )

        @self.dp.message(Command("clear"))
        async def cmd_clear(message: Message) -> None:
            chat_id = str(message.chat.id)
            self._chat_history.pop(chat_id, None)
            asyncio.create_task(
                clear_session_async(
                    self._session_key(chat_id),
                    tenant_id=self.config.tenant_id,
                )
            )
            await message.answer("Conversation history cleared.")

        @self.dp.message(Command("reset"))
        async def cmd_reset(message: Message) -> None:
            chat_id = str(message.chat.id)
            self._model_override.pop(chat_id, None)
            self._chat_history.pop(chat_id, None)
            asyncio.create_task(
                clear_session_async(
                    self._session_key(chat_id),
                    tenant_id=self.config.tenant_id,
                )
            )
            primary = self._get_manifest_primary()
            name = MODEL_DISPLAY_NAMES.get(primary, primary)
            await message.answer(
                f"Session reset. Model reverted to {html.escape(name)} (manifest default)."
            )

        @self.dp.message(Command("stop"))
        async def cmd_stop(message: Message) -> None:
            chat_id = str(message.chat.id)
            task = self._active_tasks.get(chat_id)
            if task and not task.done():
                task.cancel()
                self._active_tasks.pop(chat_id, None)
                await message.answer("Stopped.")
            else:
                await message.answer("Nothing running.")

        @self.dp.message(Command("context"))
        async def cmd_context(message: Message) -> None:
            chat_id = str(message.chat.id)
            history = self._chat_history.get(chat_id, [])

            from robothor.engine.context import get_context_stats

            stats = get_context_stats(history)

            lines = [
                "<b>Context Window</b>\n",
                f"Messages: {stats['message_count']}",
                f"Estimated tokens: {stats['estimated_tokens']:,}",
                f"Usage: {stats['usage_pct']}% of threshold",
                f"Compress threshold: {stats['compress_threshold']:,}",
                f"Would compress: {'yes' if stats['would_compress'] else 'no'}",
            ]
            roles = stats.get("role_counts", {})
            if roles:
                parts = [f"{r}: {c}" for r, c in sorted(roles.items())]
                lines.append(f"By role: {', '.join(parts)}")
            await message.answer("\n".join(lines))

        @self.dp.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"http://localhost:{self.config.port}/health", timeout=5
                    )
                    data = resp.json()
                lines = [f"<b>Engine Status</b> — {data.get('status', 'unknown')}\n"]
                agents = data.get("agents", {})
                for aid, info in sorted(agents.items()):
                    status = info.get("last_status") or "—"
                    errors = info.get("consecutive_errors", 0)
                    marker = (
                        "\u2705"
                        if status == "completed"
                        else ("\u274c" if status == "failed" else "\u23f3")
                    )
                    line = f"{marker} <b>{html.escape(aid)}</b>: {html.escape(str(status))}"
                    if errors:
                        line += f" ({errors} errors)"
                    lines.append(line)
                await message.answer("\n".join(lines))
            except Exception as e:
                await message.answer(f"Failed to fetch status: {html.escape(str(e))}")

        # ── Inline keyboard callbacks ──

        @self.dp.callback_query(F.data.startswith("model:"))
        async def on_model_select(callback: CallbackQuery) -> None:
            if not callback.data or not callback.message:
                return
            model_id = callback.data.removeprefix("model:")
            chat_id = str(callback.message.chat.id)

            if model_id not in MODEL_DISPLAY_NAMES:
                await callback.answer("Unknown model")
                return

            self._model_override[chat_id] = model_id
            asyncio.create_task(
                update_model_override_async(
                    self._session_key(chat_id),
                    model_id,
                    tenant_id=self.config.tenant_id,
                )
            )
            display = MODEL_DISPLAY_NAMES[model_id]

            # Update the keyboard to reflect selection
            kb = self._build_model_keyboard(model_id)
            try:
                msg = callback.message
                if msg and hasattr(msg, "edit_text"):
                    await msg.edit_text(  # type: ignore[union-attr]
                        f"<b>Model switched to:</b> {html.escape(display)}",
                        reply_markup=kb,
                    )
            except Exception:
                pass
            await callback.answer(f"Switched to {display}")

        # ── Interactive text messages ──

        @self.dp.message(F.text)
        async def handle_text(message: Message) -> None:
            """Handle incoming text messages — streaming response."""
            if not message.text or not message.from_user:
                return

            chat_id = str(message.chat.id)
            user_text = message.text.strip()

            logger.info(
                "Telegram message from %s (chat %s): %s",
                message.from_user.first_name,
                chat_id,
                user_text[:100],
            )

            # ── Typing indicator ──
            typing_active = True

            async def typing_loop() -> None:
                while typing_active:
                    with contextlib.suppress(Exception):
                        await self.bot.send_chat_action(
                            chat_id=int(chat_id), action=ChatAction.TYPING
                        )
                    await asyncio.sleep(TYPING_INTERVAL)

            typing_task = asyncio.create_task(typing_loop())

            # ── Send "Thinking..." immediately ──
            try:
                thinking_msg = await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=THINKING_TEXT,
                    parse_mode=None,
                )
                stream_msg_id: int | None = thinking_msg.message_id
            except Exception:
                stream_msg_id = None

            # ── Streaming state ──
            last_edit_time: float = 0.0
            last_edit_len: int = 0
            first_content = True

            async def on_content(accumulated_text: str) -> None:
                nonlocal stream_msg_id, last_edit_time, last_edit_len, first_content

                now = time.monotonic()
                text_len = len(accumulated_text)

                # First real content replaces "Thinking..." immediately
                if first_content:
                    first_content = False
                    # Force immediate edit on first content
                else:
                    # Throttle subsequent edits
                    time_ok = (now - last_edit_time) >= STREAM_EDIT_INTERVAL
                    chars_ok = (text_len - last_edit_len) >= STREAM_MIN_NEW_CHARS
                    if not time_ok and not chars_ok:
                        return

                display = accumulated_text[: MAX_MESSAGE_LENGTH - 5] + STREAM_CURSOR

                try:
                    if stream_msg_id is not None:
                        await self.bot.edit_message_text(
                            chat_id=int(chat_id),
                            message_id=stream_msg_id,
                            text=display,
                            parse_mode=None,
                        )
                    else:
                        sent = await self.bot.send_message(
                            chat_id=int(chat_id),
                            text=display,
                            parse_mode=None,
                        )
                        stream_msg_id = sent.message_id
                except Exception:
                    pass

                last_edit_time = now
                last_edit_len = text_len

            # ── Execute agent ──
            model = self._model_override.get(chat_id)
            history = list(self._chat_history.get(chat_id, []))

            async def run_agent() -> None:
                nonlocal stream_msg_id
                try:
                    run = await self.runner.execute(
                        agent_id=self.config.default_chat_agent,
                        message=user_text,
                        trigger_type=TriggerType.TELEGRAM,
                        trigger_detail=f"chat:{chat_id}",
                        on_content=on_content,
                        model_override=model,
                        conversation_history=history or None,
                    )

                    # Save conversation history
                    if run.output_text:
                        chat_hist = self._chat_history.setdefault(chat_id, [])
                        chat_hist.append({"role": "user", "content": user_text})
                        chat_hist.append({"role": "assistant", "content": run.output_text})
                        # Cap at max_history entries (trim from front)
                        if len(chat_hist) > self._max_history:
                            self._chat_history[chat_id] = chat_hist[-self._max_history :]

                        # Persist to DB (fire-and-forget)
                        asyncio.create_task(
                            save_exchange_async(
                                self._session_key(chat_id),
                                user_text,
                                run.output_text,
                                channel="telegram",
                                model_override=model,
                                tenant_id=self.config.tenant_id,
                            )
                        )

                    if run.output_text:
                        if stream_msg_id is not None:
                            await self._edit_final(chat_id, stream_msg_id, run.output_text)
                        else:
                            await self.send_message(chat_id, run.output_text)
                    elif run.error_message:
                        err = f"Error: {run.error_message}"
                        if stream_msg_id is not None:
                            await self._edit_final(chat_id, stream_msg_id, err)
                        else:
                            await self.send_message(chat_id, err)
                    else:
                        if stream_msg_id is not None:
                            await self._edit_final(
                                chat_id, stream_msg_id, "Done. No output produced."
                            )
                        else:
                            await self.send_message(chat_id, "Done. No output produced.")

                except asyncio.CancelledError:
                    # /stop was called
                    if stream_msg_id is not None:
                        with contextlib.suppress(Exception):
                            await self.bot.edit_message_text(
                                chat_id=int(chat_id),
                                message_id=stream_msg_id,
                                text="Stopped.",
                                parse_mode=None,
                            )
                except Exception as e:
                    logger.error("Failed to process message: %s", e, exc_info=True)
                    await self.send_message(chat_id, f"Internal error: {html.escape(str(e))}")
                finally:
                    nonlocal typing_active
                    typing_active = False
                    typing_task.cancel()
                    self._active_tasks.pop(chat_id, None)

            task = asyncio.create_task(run_agent())
            self._active_tasks[chat_id] = task

    def _session_key(self, chat_id: str) -> str:
        """Return a DB session key for a Telegram chat."""
        return f"telegram:{chat_id}"

    def _load_persisted_history(self) -> None:
        """Restore chat history and model overrides from PostgreSQL."""
        try:
            sessions = load_all_sessions(
                limit_per_session=self._max_history,
                tenant_id=self.config.tenant_id,
            )
            restored = 0
            for key, data in sessions.items():
                if not key.startswith("telegram:"):
                    continue
                chat_id = key.removeprefix("telegram:")
                history = data.get("history", [])
                if history:
                    self._chat_history[chat_id] = history
                model = data.get("model_override")
                if model:
                    self._model_override[chat_id] = model
                restored += 1
            if restored:
                logger.info("Restored %d Telegram chat sessions from DB", restored)
        except Exception as e:
            logger.warning("Failed to load persisted chat history: %s", e)

    def _get_manifest_primary(self) -> str:
        """Get the main agent's manifest primary model."""
        from robothor.engine.config import load_agent_config

        cfg = load_agent_config("main", self.config.manifest_dir)
        return cfg.model_primary if cfg else ""

    def _build_model_keyboard(self, current_model: str) -> InlineKeyboardMarkup:
        """Build inline keyboard for model selection."""
        buttons: list[list[InlineKeyboardButton]] = []
        row: list[InlineKeyboardButton] = []

        for display_name, model_id in AVAILABLE_MODELS.items():
            label = f"\u2705 {display_name}" if model_id == current_model else display_name
            row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"model:{model_id}",
                )
            )
            if len(row) == 2:
                buttons.append(row)
                row = []

        if row:
            buttons.append(row)

        return InlineKeyboardMarkup(inline_keyboard=buttons)

    async def _edit_final(self, chat_id: str, message_id: int, text: str) -> None:
        """Edit a streamed message with the final text. Tries HTML, falls back to plain."""
        if len(text) > MAX_MESSAGE_LENGTH:
            with contextlib.suppress(Exception):
                await self.bot.delete_message(chat_id=int(chat_id), message_id=message_id)
            await self.send_message(chat_id, text)
            return

        # Try HTML (converted from markdown)
        try:
            await self.bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=message_id,
                text=_md_to_html(text),
                parse_mode=ParseMode.HTML,
            )
            return
        except Exception:
            pass

        # Fallback to plain text
        try:
            await self.bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=message_id,
                text=text,
                parse_mode=None,
            )
        except Exception as e:
            logger.error("Failed to edit final message: %s", e)

    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a message to a Telegram chat, splitting if needed."""
        if not text:
            return

        chunks = self._split_message(text)
        for chunk in chunks:
            html_chunk = _md_to_html(chunk)
            try:
                await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=html_chunk,
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                try:
                    await self.bot.send_message(
                        chat_id=int(chat_id),
                        text=chunk,
                        parse_mode=None,
                    )
                except Exception as e:
                    logger.error("Failed to send Telegram message: %s", e)

    def _split_message(self, text: str) -> list[str]:
        """Split text into chunks that fit Telegram's limit."""
        if len(text) <= MAX_MESSAGE_LENGTH:
            return [text]

        chunks = []
        while text:
            if len(text) <= MAX_MESSAGE_LENGTH:
                chunks.append(text)
                break

            split_pos = text.rfind("\n", 0, MAX_MESSAGE_LENGTH)
            if split_pos == -1 or split_pos < MAX_MESSAGE_LENGTH // 2:
                split_pos = MAX_MESSAGE_LENGTH

            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip("\n")

        return chunks

    async def start_polling(self) -> None:
        """Start the bot in long-polling mode."""
        if not self.config.bot_token:
            logger.warning("No bot token configured, Telegram bot disabled")
            while True:
                await asyncio.sleep(3600)

        # Restore persisted chat history from DB
        self._load_persisted_history()

        # Register command menu with Telegram
        try:
            await self.bot.set_my_commands(
                [
                    BotCommand(command="model", description="Switch AI model"),
                    BotCommand(command="clear", description="Clear conversation history"),
                    BotCommand(command="context", description="Context window stats"),
                    BotCommand(command="status", description="Engine health"),
                    BotCommand(command="reset", description="Reset model + history"),
                    BotCommand(command="stop", description="Cancel current response"),
                    BotCommand(command="help", description="Show commands"),
                ]
            )
        except Exception as e:
            logger.warning("Failed to set bot commands: %s", e)

        logger.info("Starting Telegram bot polling...")
        try:
            await self.dp.start_polling(self.bot)
        except Exception as e:
            logger.error("Telegram polling failed: %s", e, exc_info=True)
            raise

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        # Cancel all active tasks
        for task in self._active_tasks.values():
            task.cancel()
        self._active_tasks.clear()

        with contextlib.suppress(Exception):
            await self.bot.session.close()
