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
from datetime import UTC
from typing import TYPE_CHECKING, Any

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
)

from robothor.engine.chat import (
    _extract_plan_text,
    _plan_is_expired,
    get_main_session_key,
    get_shared_session,
)
from robothor.engine.chat_store import (
    clear_plan_state_async,
    clear_session_async,
    save_exchange_async,
    save_plan_state_async,
    update_model_override_async,
)
from robothor.engine.delivery import set_telegram_sender
from robothor.engine.models import PlanState, TriggerType

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

# File handling — max size for text extraction (5 MB)
MAX_FILE_SIZE = 5 * 1024 * 1024

# Friendly tool names for streaming indicators
_TOOL_LABELS = {
    "search_memory": "Searching memory",
    "read_file": "Reading file",
    "write_file": "Writing file",
    "web_search": "Searching the web",
    "web_fetch": "Fetching page",
    "exec": "Running command",
    "list_tasks": "Checking tasks",
    "create_task": "Creating task",
    "store_memory": "Saving to memory",
    "get_entity": "Looking up contact",
    "search_records": "Searching records",
}


def _friendly_tool_name(tool: str) -> str:
    """Map tool name to a human-readable label for streaming indicators."""
    return _TOOL_LABELS.get(tool, tool.replace("_", " ").title())


def _plan_state_to_dict(plan: PlanState) -> dict:
    """Serialize PlanState to dict for DB persistence."""
    return {
        "plan_id": plan.plan_id,
        "plan_text": plan.plan_text,
        "original_message": plan.original_message,
        "status": plan.status,
        "created_at": plan.created_at,
        "exploration_run_id": plan.exploration_run_id,
        "rejection_feedback": plan.rejection_feedback,
        "revision_count": plan.revision_count,
        "revision_history": plan.revision_history,
        "execution_run_id": plan.execution_run_id,
        "deep_plan": plan.deep_plan,
    }


# Extensions we'll try to read as text
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".csv",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".py",
    ".js",
    ".ts",
    ".sh",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
    ".eml",
    ".tex",
    ".rst",
    ".sql",
    ".env",
}

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


async def _extract_pdf_text(raw_bytes: bytes) -> str:
    """Best-effort text extraction from a PDF."""
    try:
        import io

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"[Page {i + 1}]\n{text}")
        if pages:
            return "\n\n".join(pages)
        return "[PDF: no extractable text (may be image-based)]"
    except ImportError:
        return "[PDF file — install pypdf for text extraction]"
    except Exception as e:
        return f"[PDF text extraction failed: {e}]"


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
        self._last_message_at: dict[str, float] = {}  # chat_id → monotonic timestamp
        self._idle_timeout: float = 900.0  # 15 minutes

        # Max conversation history entries (user + assistant pairs)
        self._max_history = 40  # match chat.py MAX_HISTORY

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
                "/deep — Deep reasoning via RLM ($0.50-$2.00)\n"
                "/plan — Plan before executing (review + approve)\n"
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
            session = get_shared_session(self._session_key(chat_id))
            session.history.clear()
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
            session = get_shared_session(self._session_key(chat_id))
            session.history.clear()
            session.model_override = None
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
            session = get_shared_session(self._session_key(chat_id))
            history = list(session.history)

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

        @self.dp.message(Command("plan"))
        async def cmd_plan(message: Message) -> None:
            """Start plan mode for the given message, or toggle plan_mode flag."""
            chat_id = str(message.chat.id)
            session_key = self._session_key(chat_id)
            session = get_shared_session(session_key)

            # Parse: /plan <message> runs plan immediately, /plan alone toggles
            user_text = (message.text or "").strip()
            plan_arg = user_text.removeprefix("/plan").strip()

            if not plan_arg:
                session.plan_mode = not session.plan_mode
                state = "ON" if session.plan_mode else "OFF"
                await message.answer(
                    f"Plan mode: <b>{state}</b>\nNext message will be planned before execution."
                    if session.plan_mode
                    else f"Plan mode: <b>{state}</b>"
                )
                return

            # Execute plan mode immediately with the argument
            await self._run_plan_mode(chat_id, session_key, session, plan_arg, message)

        @self.dp.message(Command("deep"))
        async def cmd_deep(message: Message) -> None:
            """Start deep reasoning via RLM — plans first, then routes to RLM."""
            chat_id = str(message.chat.id)
            session_key = self._session_key(chat_id)
            session = get_shared_session(session_key)

            user_text = (message.text or "").strip()
            deep_arg = user_text.removeprefix("/deep").strip()

            if not deep_arg:
                await message.answer(
                    "<b>/deep — Deep Reasoning (RLM)</b>\n\n"
                    "Usage: <code>/deep &lt;question&gt;</code>\n\n"
                    "Plans first (gathers context), then invokes the Recursive "
                    "Language Model with rich context for complex reasoning.\n"
                    "Typical cost: $0.50–$2.00.\n\n"
                    "Example:\n"
                    "<code>/deep What calendar conflicts do I have this week?</code>"
                )
                return

            # Route through plan mode with deep_plan=True
            await self._run_plan_mode(
                chat_id, session_key, session, deep_arg, message, deep_plan=True
            )

        # ── Inline keyboard callbacks ──

        @self.dp.callback_query(F.data.startswith("plan:"))
        async def on_plan_decision(callback: CallbackQuery) -> None:
            """Handle plan approve/reject from inline keyboard."""
            if not callback.data or not callback.message:
                return
            chat_id = str(callback.message.chat.id)
            session_key = self._session_key(chat_id)
            session = get_shared_session(session_key)

            parts = callback.data.split(":", 2)
            if len(parts) < 3:
                await callback.answer("Invalid callback")
                return

            action = parts[1]  # approve or reject
            plan_id = parts[2]

            if not session.active_plan or session.active_plan.plan_id != plan_id:
                await callback.answer("Plan no longer active")
                return

            if _plan_is_expired(session.active_plan):
                session.active_plan.status = "expired"
                session.active_plan = None
                await callback.answer("Plan expired")
                return

            if action == "approve":
                await callback.answer("Executing plan...")
                # Remove inline keyboard
                try:
                    msg = callback.message
                    if msg and hasattr(msg, "edit_reply_markup"):
                        await msg.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
                except Exception:
                    pass
                await self._execute_approved_plan(chat_id, session_key, session)
            elif action == "revise":
                await callback.answer("Send your feedback and I'll revise the plan.")
                await self.send_message(chat_id, "Send your feedback and I'll revise the plan.")
            elif action == "reject":
                session.active_plan.status = "rejected"
                # Persist cleared state
                asyncio.create_task(
                    clear_plan_state_async(session_key, tenant_id=self.config.tenant_id)
                )
                session.active_plan = None
                # Remove inline keyboard
                try:
                    msg = callback.message
                    if msg and hasattr(msg, "edit_reply_markup"):
                        await msg.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
                except Exception:
                    pass
                await callback.answer("Plan rejected")
                await self.send_message(chat_id, "Plan rejected. Send a new message.")

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
            # Sync to shared session so webchat also picks up the override
            session = get_shared_session(self._session_key(chat_id))
            session.model_override = model_id
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

        # ── File/document/photo messages ──

        @self.dp.message(F.document | F.photo)
        async def handle_file(message: Message) -> None:
            """Handle file/document/photo attachments — extract content and process."""
            if not message.from_user:
                return

            chat_id = str(message.chat.id)
            caption = (message.caption or "").strip()

            # Determine what was sent
            file_desc = ""
            file_content = ""
            file_name = ""

            if message.document:
                doc = message.document
                file_name = doc.file_name or "unnamed_file"
                file_size = doc.file_size or 0

                if file_size > MAX_FILE_SIZE:
                    await message.answer(
                        f"File too large ({file_size // 1024}KB). Max {MAX_FILE_SIZE // 1024 // 1024}MB."
                    )
                    return

                # Download and try to extract text
                try:
                    file = await self.bot.get_file(doc.file_id)
                    if file.file_path:
                        from io import BytesIO

                        buf = BytesIO()
                        await self.bot.download_file(file.file_path, buf)
                        raw_bytes = buf.getvalue()

                        # Check extension for text extraction
                        ext = "." + file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""

                        if ext in TEXT_EXTENSIONS:
                            try:
                                file_content = raw_bytes.decode("utf-8", errors="replace")
                            except Exception:
                                file_content = "[Binary content — could not decode as text]"
                        elif ext == ".pdf":
                            file_content = await _extract_pdf_text(raw_bytes)
                        else:
                            file_content = f"[Binary file: {file_name}, {len(raw_bytes)} bytes]"
                except Exception as e:
                    logger.warning("Failed to download file %s: %s", file_name, e)
                    file_content = f"[Failed to download file: {e}]"

                file_desc = f"[File: {file_name}]"

            elif message.photo:
                # Get highest resolution photo
                photo: PhotoSize = message.photo[-1]
                file_desc = "[Photo attached]"
                try:
                    file = await self.bot.get_file(photo.file_id)
                    if file.file_path:
                        file_name = f"photo_{photo.file_unique_id}.jpg"
                        file_content = f"[Image: {photo.width}x{photo.height}px — text extraction not available for photos]"
                except Exception as e:
                    logger.warning("Failed to process photo: %s", e)
                    file_content = "[Failed to process photo]"

            # Build the user message with file context
            parts = []
            if caption:
                parts.append(caption)
            if file_desc:
                parts.append(file_desc)
            if file_content and file_content.startswith("["):
                # Just a descriptor, include it
                parts.append(file_content)
            elif file_content:
                # Actual text content — wrap it
                # Truncate very long files to avoid blowing context
                max_chars = 50_000
                if len(file_content) > max_chars:
                    file_content = (
                        file_content[:max_chars]
                        + f"\n\n[... truncated, {len(file_content)} total chars]"
                    )
                parts.append(
                    f"--- File content: {file_name} ---\n{file_content}\n--- End of file ---"
                )

            user_text = "\n\n".join(parts) if parts else file_desc

            logger.info(
                "Telegram file from %s (chat %s): %s, caption=%s",
                message.from_user.first_name,
                chat_id,
                file_name or "photo",
                caption[:50] if caption else "(none)",
            )

            # Route through the same execution path as text messages
            session_key = self._session_key(chat_id)
            session = get_shared_session(session_key)

            # Check for pending plan
            if session.active_plan and session.active_plan.status == "pending":
                if not _plan_is_expired(session.active_plan):
                    # File message = feedback on plan, re-plan
                    session.active_plan.rejection_feedback = user_text
                    session.active_plan.status = "superseded"
                    session.active_plan = None
                    await self._run_plan_mode(chat_id, session_key, session, user_text, message)
                    return
                else:
                    session.active_plan.status = "expired"
                    session.active_plan = None

            if session.plan_mode:
                session.plan_mode = False
                await self._run_plan_mode(chat_id, session_key, session, user_text, message)
                return

            # Execute via _run_interactive (shared with handle_text)
            await self._run_interactive(chat_id, session_key, session, user_text)

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

            session_key = self._session_key(chat_id)
            session = get_shared_session(session_key)

            # ── Check for pending plan — ANY text = feedback for revision ──
            # Approval/rejection only via inline keyboard buttons.
            if session.active_plan and session.active_plan.status == "pending":
                if not _plan_is_expired(session.active_plan):
                    await self._iterate_plan(chat_id, session_key, session, user_text)
                    return
                else:
                    session.active_plan.status = "expired"
                    session.active_plan = None

            # ── Check plan_mode toggle — route through plan pipeline ──
            if session.plan_mode:
                session.plan_mode = False  # One-shot: auto-disable after use
                await self._run_plan_mode(chat_id, session_key, session, user_text, message)
                return

            # Execute via _run_interactive (shared with handle_file)
            await self._run_interactive(chat_id, session_key, session, user_text)

    async def _run_interactive(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        user_text: str,
    ) -> None:
        """Execute an interactive agent run with streaming, typing indicator, and history management.

        Shared by handle_text and handle_file — the single execution path for
        interactive Telegram messages.
        """
        # ── Idle timeout: compress stale sessions ──
        now = time.monotonic()
        last = self._last_message_at.get(chat_id, 0.0)
        if last > 0 and (now - last) > self._idle_timeout:
            try:
                from robothor.engine.context import maybe_compress

                if session.history and len(session.history) > 5:
                    original_count = len(session.history)
                    compressed = await maybe_compress(session.history, threshold=20_000)
                    if len(compressed) < original_count:
                        session.history[:] = compressed
                        logger.info(
                            "Idle timeout compression for chat %s (%d→%d messages)",
                            chat_id,
                            original_count,
                            len(compressed),
                        )
            except Exception as e:
                logger.debug("Idle compression failed: %s", e)
        self._last_message_at[chat_id] = now

        # ── Typing indicator ──
        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
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
        stream_edit_interval: float = STREAM_EDIT_INTERVAL
        current_text: str = ""

        async def _edit_status(suffix: str) -> None:
            """Edit streaming message to show status indicator below current text."""
            nonlocal stream_msg_id, last_edit_time, stream_edit_interval
            now = time.monotonic()
            if (now - last_edit_time) < stream_edit_interval:
                return  # Rate-limited
            display = (current_text + suffix)[: MAX_MESSAGE_LENGTH - 5]
            try:
                if stream_msg_id is not None:
                    await self.bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=stream_msg_id,
                        text=display,
                        parse_mode=None,
                    )
                    last_edit_time = now
            except TelegramRetryAfter as e:
                stream_edit_interval = max(stream_edit_interval, e.retry_after + 1.0)
            except Exception:
                pass

        async def on_content(accumulated_text: str) -> None:
            nonlocal stream_msg_id, last_edit_time, last_edit_len, first_content
            nonlocal stream_edit_interval, current_text

            current_text = accumulated_text
            now = time.monotonic()
            text_len = len(accumulated_text)

            if first_content:
                first_content = False
            else:
                time_ok = (now - last_edit_time) >= stream_edit_interval
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
            except TelegramRetryAfter as e:
                stream_edit_interval = max(stream_edit_interval, e.retry_after + 1.0)
            except Exception:
                pass

            last_edit_time = now
            last_edit_len = text_len

        async def on_tool(event: dict) -> None:
            if event.get("event") == "tool_start":
                label = _friendly_tool_name(event.get("tool", ""))
                await _edit_status(f"\n\n\U0001f527 {label}...")

        async def on_status(event: dict) -> None:
            if event.get("event") == "tools_done":
                await _edit_status("\n\n\U0001f4ad Thinking...")

        # ── Execute agent ──
        model = self._model_override.get(chat_id)

        async def run_agent() -> None:
            nonlocal stream_msg_id
            try:
                history = list(session.history)
                run = await self.runner.execute(
                    agent_id=self.config.default_chat_agent,
                    message=user_text,
                    trigger_type=TriggerType.TELEGRAM,
                    trigger_detail=f"chat:{chat_id}",
                    on_content=on_content,
                    on_tool=on_tool,
                    on_status=on_status,
                    model_override=model,
                    conversation_history=history or None,
                )

                # Always record user message in session history
                session.history.append({"role": "user", "content": user_text})

                if run.output_text:
                    session.history.append({"role": "assistant", "content": run.output_text})
                elif run.error_message:
                    # Record error so the next run knows what failed
                    session.history.append(
                        {
                            "role": "assistant",
                            "content": f"[Run failed: {run.error_message}]",
                        }
                    )

                # Trim from front
                if len(session.history) > self._max_history:
                    session.history[:] = session.history[-self._max_history :]

                # Persist to DB (fire-and-forget)
                if run.output_text:
                    asyncio.create_task(
                        save_exchange_async(
                            session_key,
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
                            chat_id,
                            stream_msg_id,
                            "Done. No output produced.",
                        )
                    else:
                        await self.send_message(chat_id, "Done. No output produced.")

            except asyncio.CancelledError:
                # /stop was called during execution
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
                # Record the failed attempt so next run has context
                session.history.append({"role": "user", "content": user_text})
                session.history.append(
                    {
                        "role": "assistant",
                        "content": f"[Internal error — run failed: {e}]",
                    }
                )
                if len(session.history) > self._max_history:
                    session.history[:] = session.history[-self._max_history :]
                await self.send_message(chat_id, f"Internal error: {html.escape(str(e))}")
            finally:
                nonlocal typing_active
                typing_active = False
                typing_task.cancel()
                self._active_tasks.pop(chat_id, None)

        task = asyncio.create_task(run_agent())
        self._active_tasks[chat_id] = task

    async def _run_plan_mode(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        user_text: str,
        message: Message,
        deep_plan: bool = False,
    ) -> None:
        """Execute agent in plan mode with read-only tools, display plan with approval keyboard."""
        import uuid
        from datetime import datetime

        # Typing indicator
        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)

        typing_task = asyncio.create_task(typing_loop())

        thinking_emoji = "\U0001f9e0" if deep_plan else "\U0001f4cb"
        thinking_label = "Gathering context for deep reasoning..." if deep_plan else "Planning..."
        try:
            thinking_msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text=f"{thinking_emoji} {thinking_label}",
                parse_mode=None,
            )
            stream_msg_id: int | None = thinking_msg.message_id
        except Exception:
            stream_msg_id = None

        last_edit_time: float = 0.0
        last_edit_len: int = 0
        first_content = True
        stream_edit_interval: float = STREAM_EDIT_INTERVAL

        async def on_content(accumulated_text: str) -> None:
            nonlocal stream_msg_id, last_edit_time, last_edit_len, first_content
            nonlocal stream_edit_interval
            now = time.monotonic()
            text_len = len(accumulated_text)
            if first_content:
                first_content = False
            else:
                time_ok = (now - last_edit_time) >= stream_edit_interval
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
            except TelegramRetryAfter as e:
                stream_edit_interval = max(stream_edit_interval, e.retry_after + 1.0)
            except Exception:
                pass
            last_edit_time = now
            last_edit_len = text_len

        try:
            model = self._model_override.get(chat_id)
            history = list(session.history)

            run = await self.runner.execute(
                agent_id=self.config.default_chat_agent,
                message=user_text,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail=f"plan:{chat_id}",
                on_content=on_content,
                model_override=model,
                conversation_history=history or None,
                readonly_mode=True,
                deep_plan=deep_plan,
            )

            plan_text = _extract_plan_text(run.output_text or "")

            # Accumulate history so revisions have full context
            session.history.append({"role": "user", "content": user_text})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            if plan_text:
                plan = PlanState(
                    plan_id=str(uuid.uuid4()),
                    plan_text=plan_text,
                    original_message=user_text,
                    status="pending",
                    created_at=datetime.now(UTC).isoformat(),
                    exploration_run_id=run.id,
                    deep_plan=deep_plan,
                )
                session.active_plan = plan

                # Persist plan state to DB
                asyncio.create_task(
                    save_plan_state_async(
                        session_key,
                        _plan_state_to_dict(plan),
                        tenant_id=self.config.tenant_id,
                    )
                )

                # Display plan with approval keyboard
                if stream_msg_id is not None:
                    await self._edit_final(chat_id, stream_msg_id, plan_text)
                else:
                    await self.send_message(chat_id, plan_text)

                label = (
                    "<b>Approve this deep plan?</b>" if deep_plan else "<b>Approve this plan?</b>"
                )
                kb = self._build_plan_keyboard(plan.plan_id)
                await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=label,
                    reply_markup=kb,
                )
            else:
                if stream_msg_id is not None:
                    await self._edit_final(
                        chat_id,
                        stream_msg_id,
                        run.output_text or "No plan produced.",
                    )
                else:
                    await self.send_message(chat_id, run.output_text or "No plan produced.")
        except Exception as e:
            logger.error("Plan mode failed: %s", e, exc_info=True)
            await self.send_message(chat_id, f"Plan mode error: {html.escape(str(e))}")
        finally:
            typing_active = False
            typing_task.cancel()

    async def _run_deep_mode(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        query: str,
        message: Message,
    ) -> None:
        """Execute deep reasoning via RLM, show progress edits and result."""
        # Typing indicator
        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)

        typing_task = asyncio.create_task(typing_loop())

        try:
            thinking_msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text="\U0001f9e0 Deep reasoning...",
                parse_mode=None,
            )
            progress_msg_id: int | None = thinking_msg.message_id
        except Exception:
            progress_msg_id = None

        async def on_progress(progress: dict) -> None:
            nonlocal progress_msg_id
            elapsed = progress.get("elapsed_s", 0)
            text = f"\U0001f9e0 Deep reasoning... {elapsed}s elapsed"
            try:
                if progress_msg_id is not None:
                    await self.bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=progress_msg_id,
                        text=text,
                        parse_mode=None,
                    )
            except Exception:
                pass

        try:
            history = list(session.history)

            run = await self.runner.execute_deep(
                query=query,
                on_progress=on_progress,
                conversation_history=history or None,
            )

            # Record in session history
            session.history.append({"role": "user", "content": f"/deep {query}"})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            elif run.error_message:
                session.history.append(
                    {
                        "role": "assistant",
                        "content": f"[Deep reasoning failed: {run.error_message}]",
                    }
                )
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Persist exchange to DB
            if run.output_text:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        f"/deep {query}",
                        run.output_text,
                        channel="telegram",
                        tenant_id=self.config.tenant_id,
                    )
                )

            # Display result
            if run.output_text:
                # Cost/time footer
                duration_s = (run.duration_ms or 0) / 1000
                cost_str = f"${run.total_cost_usd:.2f}" if run.total_cost_usd else "$?.??"
                footer = f"\n\n<i>RLM: {duration_s:.1f}s / {cost_str}</i>"

                result_text = run.output_text
                if progress_msg_id is not None:
                    # Edit the progress message with the full result
                    await self._edit_final(chat_id, progress_msg_id, result_text)
                    # Send cost footer as separate message (final text may be at Telegram limit)
                    await self.bot.send_message(
                        chat_id=int(chat_id),
                        text=footer,
                    )
                else:
                    await self.send_message(chat_id, result_text + footer)
            elif run.error_message:
                error_text = f"\u274c Deep reasoning failed: {html.escape(run.error_message)}"
                if progress_msg_id is not None:
                    try:
                        await self.bot.edit_message_text(
                            chat_id=int(chat_id),
                            message_id=progress_msg_id,
                            text=error_text,
                        )
                    except Exception:
                        await self.send_message(chat_id, error_text)
                else:
                    await self.send_message(chat_id, error_text)
        except Exception as e:
            logger.error("Deep mode failed: %s", e, exc_info=True)
            await self.send_message(chat_id, f"Deep reasoning error: {html.escape(str(e))}")
        finally:
            typing_active = False
            typing_task.cancel()

    async def _execute_approved_plan(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
    ) -> None:
        """Execute an approved plan — full tools or deep reasoning."""
        plan = session.active_plan
        if not plan:
            await self.send_message(chat_id, "No pending plan to execute.")
            return

        plan.status = "approved"

        # Deep plan: route to RLM with rich context instead of agent execution
        if plan.deep_plan:
            await self._execute_deep_plan(chat_id, session_key, session)
            return

        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)

        typing_task = asyncio.create_task(typing_loop())

        try:
            thinking_msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text="\u2705 Executing plan...",
                parse_mode=None,
            )
            stream_msg_id: int | None = thinking_msg.message_id
        except Exception:
            stream_msg_id = None

        last_edit_time: float = 0.0
        last_edit_len: int = 0
        first_content = True
        stream_edit_interval: float = STREAM_EDIT_INTERVAL

        async def on_content(accumulated_text: str) -> None:
            nonlocal stream_msg_id, last_edit_time, last_edit_len, first_content
            nonlocal stream_edit_interval
            now = time.monotonic()
            text_len = len(accumulated_text)
            if first_content:
                first_content = False
            else:
                time_ok = (now - last_edit_time) >= stream_edit_interval
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
            except TelegramRetryAfter as e:
                stream_edit_interval = max(stream_edit_interval, e.retry_after + 1.0)
            except Exception:
                pass
            last_edit_time = now
            last_edit_len = text_len

        try:
            model = self._model_override.get(chat_id)

            # CONTEXT RESET — clean execution context, no planning history.
            # The LLM only sees the plan + original request. This structurally
            # prevents re-planning (the agent never sees its own plan output
            # as part of a conversation it needs to continue).
            execution_message = (
                "Execute the following approved plan. "
                "Use your tools to carry out each step.\n"
                "Do NOT re-plan, re-draft, or produce another version. ACT.\n\n"
                f"Original request: {plan.original_message}\n\n"
                f"Approved plan:\n{plan.plan_text}"
            )

            run = await self.runner.execute(
                agent_id=self.config.default_chat_agent,
                message=execution_message,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail=f"plan-exec:{chat_id}",
                on_content=on_content,
                model_override=model,
                conversation_history=None,  # CLEAN CONTEXT
                execution_mode=True,
            )

            # Track execution run ID on plan
            plan.execution_run_id = run.id

            # Merge execution result back into session history for follow-up continuity
            session.history.append(
                {"role": "user", "content": f"[Plan executed] {plan.original_message}"}
            )
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            elif run.error_message:
                session.history.append(
                    {"role": "assistant", "content": f"[Execution failed: {run.error_message}]"}
                )
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Persist to DB
            if run.output_text:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        plan.original_message,
                        run.output_text,
                        channel="telegram",
                        model_override=model,
                        tenant_id=self.config.tenant_id,
                    )
                )

            # Clear plan + persist
            session.active_plan = None
            asyncio.create_task(
                clear_plan_state_async(session_key, tenant_id=self.config.tenant_id)
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
                    await self._edit_final(chat_id, stream_msg_id, "Done. No output.")
                else:
                    await self.send_message(chat_id, "Done. No output.")
        except Exception as e:
            logger.error("Plan execution failed: %s", e, exc_info=True)
            await self.send_message(chat_id, f"Execution error: {html.escape(str(e))}")
        finally:
            typing_active = False
            typing_task.cancel()

    async def _execute_deep_plan(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
    ) -> None:
        """Execute approved deep plan — route to RLM with rich context."""
        plan = session.active_plan
        if not plan:
            await self.send_message(chat_id, "No pending plan.")
            return

        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)

        typing_task = asyncio.create_task(typing_loop())

        try:
            thinking_msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text="\U0001f9e0 Deep reasoning...",
                parse_mode=None,
            )
            progress_msg_id: int | None = thinking_msg.message_id
        except Exception:
            progress_msg_id = None

        async def on_progress(progress: dict) -> None:
            nonlocal progress_msg_id
            elapsed = progress.get("elapsed_s", 0)
            text = f"\U0001f9e0 Deep reasoning... {elapsed}s elapsed"
            try:
                if progress_msg_id is not None:
                    await self.bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=progress_msg_id,
                        text=text,
                        parse_mode=None,
                    )
            except Exception:
                pass

        try:
            # Build rich context from plan + exploration output
            exploration_output = ""
            for msg in reversed(session.history):
                if msg.get("role") == "assistant" and msg.get("content"):
                    exploration_output = msg["content"]
                    break

            context = (
                f"Original request: {plan.original_message}\n\n"
                f"Research plan:\n{plan.plan_text}\n\n"
                f"Exploration output:\n{exploration_output}"
            )

            run = await self.runner.execute_deep(
                query=plan.original_message,
                on_progress=on_progress,
                context_override=context,
            )

            # Track execution run ID
            plan.execution_run_id = run.id

            # Record in session history
            session.history.append(
                {"role": "user", "content": f"[Deep plan executed] {plan.original_message}"}
            )
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            elif run.error_message:
                session.history.append(
                    {
                        "role": "assistant",
                        "content": f"[Deep reasoning failed: {run.error_message}]",
                    }
                )
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Persist exchange to DB
            if run.output_text:
                asyncio.create_task(
                    save_exchange_async(
                        session_key,
                        plan.original_message,
                        run.output_text,
                        channel="telegram",
                        model_override=self._model_override.get(chat_id),
                        tenant_id=self.config.tenant_id,
                    )
                )

            # Clear plan + persist
            session.active_plan = None
            asyncio.create_task(
                clear_plan_state_async(session_key, tenant_id=self.config.tenant_id)
            )

            # Display result
            if run.output_text:
                duration_s = (run.duration_ms or 0) / 1000
                cost_str = f"${run.total_cost_usd:.2f}" if run.total_cost_usd else "$?.??"
                footer = f"\n\n<i>RLM: {duration_s:.1f}s / {cost_str}</i>"

                if progress_msg_id is not None:
                    await self._edit_final(chat_id, progress_msg_id, run.output_text)
                    await self.bot.send_message(chat_id=int(chat_id), text=footer)
                else:
                    await self.send_message(chat_id, run.output_text + footer)
            elif run.error_message:
                error_text = f"\u274c Deep reasoning failed: {html.escape(run.error_message)}"
                if progress_msg_id is not None:
                    try:
                        await self.bot.edit_message_text(
                            chat_id=int(chat_id),
                            message_id=progress_msg_id,
                            text=error_text,
                        )
                    except Exception:
                        await self.send_message(chat_id, error_text)
                else:
                    await self.send_message(chat_id, error_text)
        except Exception as e:
            logger.error("Deep plan execution failed: %s", e, exc_info=True)
            await self.send_message(chat_id, f"Deep reasoning error: {html.escape(str(e))}")
        finally:
            typing_active = False
            typing_task.cancel()

    async def _iterate_plan(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        feedback: str,
    ) -> None:
        """Revise the active plan based on user feedback (keeps same plan_id)."""
        from datetime import datetime

        plan = session.active_plan
        if not plan:
            await self.send_message(chat_id, "No pending plan to revise.")
            return

        # Save current plan text to revision history
        plan.revision_history.append(
            {
                "plan_text": plan.plan_text,
                "feedback": feedback,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        plan.revision_count += 1

        # Typing indicator
        typing_active = True

        async def typing_loop() -> None:
            while typing_active:
                with contextlib.suppress(Exception):
                    await self.bot.send_chat_action(chat_id=int(chat_id), action=ChatAction.TYPING)
                await asyncio.sleep(TYPING_INTERVAL)

        typing_task = asyncio.create_task(typing_loop())

        try:
            thinking_msg = await self.bot.send_message(
                chat_id=int(chat_id),
                text=f"\u270f\ufe0f Revising plan (v{plan.revision_count + 1})...",
                parse_mode=None,
            )
            stream_msg_id: int | None = thinking_msg.message_id
        except Exception:
            stream_msg_id = None

        last_edit_time: float = 0.0
        last_edit_len: int = 0
        first_content = True
        stream_edit_interval: float = STREAM_EDIT_INTERVAL

        async def on_content(accumulated_text: str) -> None:
            nonlocal stream_msg_id, last_edit_time, last_edit_len, first_content
            nonlocal stream_edit_interval
            now = time.monotonic()
            text_len = len(accumulated_text)
            if first_content:
                first_content = False
            else:
                time_ok = (now - last_edit_time) >= stream_edit_interval
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
            except TelegramRetryAfter as e:
                stream_edit_interval = max(stream_edit_interval, e.retry_after + 1.0)
            except Exception:
                pass
            last_edit_time = now
            last_edit_len = text_len

        try:
            model = self._model_override.get(chat_id)

            # Build iteration prompt with current plan + feedback
            iteration_message = (
                "[PLAN REVISION]\n"
                "The user reviewed your plan and gave this feedback:\n"
                f'"{feedback}"\n\n'
                f"Current plan:\n{plan.plan_text}\n\n"
                "Revise the plan to address their feedback. "
                "Keep everything they didn't object to.\n"
                'Start with "Changes:" summarizing what you changed.\n'
                "End with [PLAN_READY]."
            )

            history = list(session.history)

            run = await self.runner.execute(
                agent_id=self.config.default_chat_agent,
                message=iteration_message,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail=f"plan-revise:{chat_id}",
                on_content=on_content,
                model_override=model,
                conversation_history=history or None,
                readonly_mode=True,
            )

            revised_plan_text = _extract_plan_text(run.output_text or "")

            # Update history
            session.history.append({"role": "user", "content": feedback})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            if revised_plan_text:
                # Update plan in-place (same plan_id)
                plan.plan_text = revised_plan_text

                # Display revised plan with approval keyboard
                revision_label = f"<b>Plan v{plan.revision_count + 1}</b>"
                if stream_msg_id is not None:
                    await self._edit_final(chat_id, stream_msg_id, revised_plan_text)
                else:
                    await self.send_message(chat_id, revised_plan_text)

                kb = self._build_plan_keyboard(plan.plan_id, plan.revision_count)
                await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"{revision_label} — Approve this plan?",
                    reply_markup=kb,
                )

                # Persist updated plan state
                asyncio.create_task(
                    save_plan_state_async(
                        session_key,
                        _plan_state_to_dict(plan),
                        tenant_id=self.config.tenant_id,
                    )
                )
            else:
                # Agent didn't produce a revised plan
                if stream_msg_id is not None:
                    await self._edit_final(
                        chat_id,
                        stream_msg_id,
                        run.output_text or "No revised plan produced.",
                    )
                else:
                    await self.send_message(chat_id, run.output_text or "No revised plan produced.")
        except Exception as e:
            logger.error("Plan iteration failed: %s", e, exc_info=True)
            await self.send_message(chat_id, f"Revision error: {html.escape(str(e))}")
        finally:
            typing_active = False
            typing_task.cancel()

    def _build_plan_keyboard(self, plan_id: str, revision_count: int = 0) -> InlineKeyboardMarkup:
        """Build inline keyboard for plan approval (3-button: Approve / Revise / Reject)."""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="\u2705 Approve",
                        callback_data=f"plan:approve:{plan_id}",
                    ),
                    InlineKeyboardButton(
                        text="\u270f\ufe0f Revise",
                        callback_data=f"plan:revise:{plan_id}",
                    ),
                    InlineKeyboardButton(
                        text="\u274c Reject",
                        callback_data=f"plan:reject:{plan_id}",
                    ),
                ]
            ]
        )

    def _session_key(self, chat_id: str) -> str:
        """Return a DB session key for a Telegram chat.

        Philip's chat (matches default_chat_id) maps to the canonical
        shared session key so Telegram and Helm share one conversation.
        Other chats keep the telegram: prefix.
        """
        if chat_id == self.config.default_chat_id:
            return get_main_session_key()
        return f"telegram:{chat_id}"

    def _load_persisted_history(self) -> None:
        """Restore model overrides for non-primary Telegram chats.

        The canonical shared session (Philip's chat) is restored by
        chat.py's _restore_sessions() at startup — no duplicate load needed.
        Only non-primary telegram: chats need their own restore here.
        """
        from robothor.engine.chat_store import load_all_sessions

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
                # Load into shared session store
                session = get_shared_session(key)
                history = data.get("history", [])
                if history:
                    session.history = history
                model = data.get("model_override")
                if model:
                    self._model_override[chat_id] = model
                    session.model_override = model
                restored += 1
            if restored:
                logger.info("Restored %d non-primary Telegram sessions from DB", restored)
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

    async def _retry_on_flood(
        self,
        coro_factory: Any,
        max_retries: int = 3,
    ) -> Any:
        """Retry a Telegram API call on flood control (rate limit).

        Args:
            coro_factory: Zero-arg callable returning an awaitable. Must be a
                factory (not a pre-built coroutine) since you can't await twice.
            max_retries: Maximum retry attempts.

        Returns:
            The result of the awaitable on success.

        Raises:
            TelegramRetryAfter: If all retries are exhausted.
        """
        last_exc: TelegramRetryAfter | None = None
        for attempt in range(1, max_retries + 1):
            try:
                return await coro_factory()
            except TelegramRetryAfter as e:
                last_exc = e
                wait = e.retry_after + 0.5  # buffer — retry_after is an int
                logger.warning(
                    "Telegram flood control: retry %d/%d, waiting %.1fs",
                    attempt,
                    max_retries,
                    wait,
                )
                await asyncio.sleep(wait)
        logger.error("Telegram flood control: all %d retries exhausted", max_retries)
        raise last_exc  # type: ignore[misc]

    async def _edit_final(self, chat_id: str, message_id: int, text: str) -> None:
        """Edit a streamed message with the final text. Tries HTML, falls back to plain."""
        if len(text) > MAX_MESSAGE_LENGTH:
            with contextlib.suppress(Exception):
                await self.bot.delete_message(chat_id=int(chat_id), message_id=message_id)
            await self.send_message(chat_id, text)
            return

        # Try HTML (converted from markdown) — with flood-control retry
        try:
            await self._retry_on_flood(
                lambda: self.bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=message_id,
                    text=_md_to_html(text),
                    parse_mode=ParseMode.HTML,
                )
            )
            return
        except Exception:
            pass

        # Fallback to plain text — with flood-control retry
        try:
            await self._retry_on_flood(
                lambda: self.bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=message_id,
                    text=text,
                    parse_mode=None,
                )
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
                await self._retry_on_flood(
                    lambda c=html_chunk: self.bot.send_message(
                        chat_id=int(chat_id),
                        text=c,
                        parse_mode=ParseMode.HTML,
                    )
                )
            except Exception:
                try:
                    await self._retry_on_flood(
                        lambda c=chunk: self.bot.send_message(
                            chat_id=int(chat_id),
                            text=c,
                            parse_mode=None,
                        )
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
                    BotCommand(command="deep", description="Deep reasoning via RLM"),
                    BotCommand(command="plan", description="Plan before executing"),
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
