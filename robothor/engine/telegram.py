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
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from robothor.engine.chat import (
    get_main_session_key,
    get_shared_session,
)
from robothor.engine.chat_store import (
    save_exchange_async,
)
from robothor.engine.chunking import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    split_telegram_message,
)
from robothor.engine.delivery import set_telegram_sender
from robothor.engine.models import TriggerType
from robothor.engine.task_registry import get_task_registry

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner
    from robothor.identity import IdentityContext

from robothor.engine.telegram_handlers import (  # noqa: E402
    AVAILABLE_MODELS,
    TelegramHandlersMixin,
)
from robothor.engine.telegram_plan_mode import (  # noqa: E402
    TYPING_INTERVAL,
    PlanModeMixin,
)

logger = logging.getLogger(__name__)

# ── Constants ──

MAX_MESSAGE_LENGTH = TELEGRAM_MAX_MESSAGE_LENGTH
THINKING_TEXT = "\u2728 Thinking..."  # shown instantly while LLM starts up

# Delivery status written onto an interactive run when the Telegram send
# reported nothing sent. ``TelegramBot.send_message`` swallows per-chunk
# exceptions and returns an empty list on total failure, so "no messages"
# is the only signal a lost reply gives us.
INTERACTIVE_SEND_FAILED_STATUS = "failed: telegram send returned no messages"

# Closed-onboarding operator notification rate limit (Task 4, Unified
# Identity Context) -- at most one alert per unregistered sender per hour.
_ONBOARDING_NOTIFY_INTERVAL_SECONDS = 3600.0

_RESTART_TRIGGERS: dict[str, Path] = {
    # The engine keeps the original single-file trigger from #205 for
    # compatibility; the rest use the per-unit request directory, where the
    # FILENAME is the request and the handler matches it against its own
    # hardcoded allowlist. Adding a key here does NOT grant anything on its
    # own — infra/bin/robothor-restart-handler.sh is the authority, and a name
    # it does not recognise is discarded and logged.
    "robothor-engine.service": Path("/run/robothor/restart-request"),
    "robothor-delphi-engine.service": Path("/run/robothor/restart-requests/robothor-delphi-engine"),
    "robothor-bridge.service": Path("/run/robothor/restart-requests/robothor-bridge"),
    "robothor-app.service": Path("/run/robothor/restart-requests/robothor-app"),
}

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
    "todo_write": "Updating checklist",
}


def _friendly_tool_name(tool: str) -> str:
    """Map tool name to a human-readable label for streaming indicators."""
    return _TOOL_LABELS.get(tool, tool.replace("_", " ").title())


def _format_checklist_html(todos: list[dict[str, str]]) -> str:
    """Render a todo list as Telegram HTML checklist."""
    from robothor.engine.todolist import TodoList

    return TodoList.format_for_telegram(todos)


def _sanitize_preview(text: str, max_len: int = 100) -> str:
    """Collapse newlines/control characters to spaces and cap length.

    Used to embed untrusted, attacker-controlled message text (an
    unregistered sender's raw message) inside an operator-facing
    notification (review Finding 2). Without this, a crafted multi-line
    message could plant its own fake "To register them:" line followed by a
    bogus CLI command, made to look identical to the genuine registration
    hint that follows — tricking the operator into copy-pasting an
    attacker-supplied command. Stripping every line break and control
    character confines the preview to a single line no matter what the
    sender sent, so it can never spawn a second line that impersonates the
    notification's own structure.
    """
    collapsed = re.sub(r"[\r\n\t\x00-\x1f\x7f]+", " ", text)
    return collapsed.strip()[:max_len]


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
    # Markdown-style links
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


class TelegramBot(TelegramHandlersMixin, PlanModeMixin):
    """Aiogram v3 Telegram bot for Genus OS."""

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
        self._session_locks: dict[str, asyncio.Lock] = {}  # chat_id → per-chat lock

        # Message coalescing — Telegram splits long messages into chunks.
        # Buffer them and drain before acting so fragments become one run.
        self._message_buffers: dict[str, list[str]] = {}  # chat_id → queued texts
        self._drain_scheduled: dict[str, bool] = {}  # chat_id → drain task pending

        # Channel-bus reply context: when the user taps "Reply" on a surfaced
        # message, we capture the resolved quote here so the drain can prefix
        # the prompt and persist the linkage in the user turn's JSONB.
        self._reply_context_buffers: dict[str, dict[str, Any]] = {}
        self._user_message_id_buffers: dict[str, str] = {}

        # Per-message resolved sender identity, captured once in the handler
        # (where message.from_user.id is available) and popped alongside the
        # buffer in _drain_and_run — threaded through as an explicit
        # parameter rather than re-read from _chat_user_info at run_agent()
        # execution time. Fixes the group-chat attribution race: a second
        # sender's message landing in the async gap between message receipt
        # and run execution used to be able to silently reattribute an
        # in-flight run (Task 4, Unified Identity Context).
        self._pending_sender_info: dict[str, dict[str, Any]] = {}

        # Max conversation history entries (user + assistant pairs)
        self._max_history = 40  # match chat.py MAX_HISTORY

        # Per-chat resolved user identity (from tenant_users table). Kept for
        # _get_tenant_id/_session_key and as the fallback source for
        # run_agent() when no sender_info was threaded through explicitly.
        self._chat_user_info: dict[str, dict[str, Any]] = {}

        # Rate limit for the closed-onboarding operator notification — at
        # most once per Telegram sender id per hour, so a spammer retrying
        # the refusal can't flood the operator's chat.
        self._onboarding_notify_last: dict[str, float] = {}

        self._setup_handlers()

        # Register send function for delivery module
        set_telegram_sender(self.send_message)

    def _get_session_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create a per-chat asyncio.Lock for session mutation safety."""
        if chat_id not in self._session_locks:
            self._session_locks[chat_id] = asyncio.Lock()
        return self._session_locks[chat_id]

    def _setup_handlers(self) -> None:
        """Register all message and callback handlers.

        The handler BODIES live on TelegramHandlersMixin
        (telegram_handlers.py) as ordinary methods — testable, patchable,
        and visible to the size ratchet — after 1,015 lines of closures
        lived invisibly inside this one function. Registration order is
        preserved exactly (aiogram matches in order; stacked decorators
        registered bottom-up, so the table applies them reversed).
        """
        self.dp.message(Command("help"))(self.cmd_help)
        self.dp.message(Command("model"))(self.cmd_model)
        self.dp.message(Command("goal"))(self.cmd_goal)
        self.dp.message(Command("clear"))(self.cmd_clear)
        self.dp.message(Command("reset"))(self.cmd_reset)
        self.dp.message(Command("stop"))(self.cmd_stop)
        self.dp.message(Command("restart"))(self.cmd_restart)
        self.dp.message(Command("restart_delphi"))(self.cmd_restart_delphi)
        self.dp.message(Command("context"))(self.cmd_context)
        self.dp.message(Command("status"))(self.cmd_status)
        self.dp.message(Command("export"))(self.cmd_export)
        self.dp.message(Command("plan"))(self.cmd_plan)
        self.dp.message(Command("deep"))(self.cmd_deep)
        self.dp.message(Command("stats"))(self.cmd_stats)
        self.dp.message(Command("buddy"))(self.cmd_buddy)
        self.dp.message(Command("goals"))(self.cmd_goals)
        self.dp.message(Command("agents"))(self.cmd_agents)
        self.dp.message(Command("steer"))(self.cmd_steer)
        self.dp.callback_query(F.data.startswith("plan:"))(self.on_plan_decision)
        self.dp.callback_query(F.data.startswith("model:"))(self.on_model_select)
        self.dp.callback_query(F.data.startswith("perm:"))(self.on_permission_decision)
        self.dp.callback_query(F.data.startswith("runctl:"))(self.on_runctl_callback)
        self.dp.callback_query(F.data.startswith("dp:"))(self.on_delphi_proposal_decision)
        self.dp.message(F.voice | F.video_note)(self.handle_voice)
        self.dp.message(F.document | F.photo)(self.handle_file)
        self.dp.message(F.text)(self.handle_text)
        self.dp.message_reaction()(self.on_message_reaction)

    async def _enqueue_message(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        user_text: str,
        *,
        sender_info: dict[str, Any] | None = None,
    ) -> None:
        """Buffer a message and schedule a drain if none is pending.

        ``sender_info`` is the dict ``_resolve_user`` returned for THIS
        specific incoming message, captured synchronously in the handler
        (where ``message.from_user.id`` is available). It's stashed here —
        same last-write-wins pattern as ``_reply_context_buffers`` /
        ``_user_message_id_buffers`` — and popped in ``_drain_and_run``
        atomically alongside the text buffer, then threaded through to
        ``_run_interactive`` as an explicit parameter. This is what fixes
        the group-chat attribution race: run_agent() no longer re-reads the
        shared, chat_id-keyed ``_chat_user_info`` cache at execution time,
        where a second sender's message arriving in the async gap could
        silently overwrite it before this run gets to read it.
        """
        self._message_buffers.setdefault(chat_id, []).append(user_text)
        if sender_info is not None:
            self._pending_sender_info[chat_id] = sender_info

        # If a run is already active, the message waits — the run's finally
        # block will kick off a new drain when it finishes.
        task = self._active_tasks.get(chat_id)
        if task and not task.done():
            return

        # If a drain is already scheduled, it will pick this up too.
        if self._drain_scheduled.get(chat_id):
            return

        self._drain_scheduled[chat_id] = True
        asyncio.create_task(self._drain_and_run(chat_id, session_key, session))

    async def _drain_and_run(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
    ) -> None:
        """Yield briefly to let sibling chunks buffer, then run once."""
        # aiogram dispatches all updates from one getUpdates batch as
        # concurrent asyncio tasks.  sleep(0) lets them all enqueue.
        # 0.3s safety margin covers cross-batch edge cases.
        await asyncio.sleep(0.3)

        buf = self._message_buffers.pop(chat_id, [])
        self._drain_scheduled[chat_id] = False

        if not buf:
            return

        combined_text = "\n".join(buf)
        reply_ctx = self._reply_context_buffers.pop(chat_id, None)
        user_message_id = self._user_message_id_buffers.pop(chat_id, None)
        # Popped in the same synchronous stretch as `buf` above — no await
        # in between — so this is exactly the sender info pending at the
        # moment this batch was claimed, immune to a later message's
        # overwrite of _pending_sender_info/_chat_user_info.
        sender_info = self._pending_sender_info.pop(chat_id, None)
        await self._run_interactive(
            chat_id,
            session_key,
            session,
            combined_text,
            reply_ctx=reply_ctx,
            user_message_id=user_message_id,
            sender_info=sender_info,
        )

    async def _record_interactive_delivery(self, run: Any, sent: Any) -> None:
        """Record whether an interactive reply actually reached Telegram.

        ``delivery.deliver()`` is only wired into the hook, workflow and
        scheduler paths — it never runs for an interactive chat turn — so
        without this every ``trigger_type='telegram'`` run carried
        ``delivery_status IS NULL`` and a reply that never landed looked
        exactly like one that did.

        Truth comes from the sender's return value, not from reaching the
        line after the send: ``TelegramBot.send_message`` swallows per-chunk
        exceptions and returns one ``Message`` per delivered chunk (``[]``
        when every chunk failed). Same discipline as
        ``robothor.engine.alerts._send_telegram``'s ``delivered = bool(sent)``.

        Bookkeeping only — never raises, so a DB hiccup cannot turn a
        successful reply into an error for the operator.

        Args:
            run: The ``AgentRun`` this reply belongs to. ``None`` is a no-op
                (the run may not exist yet when an early failure replies).
            sent: Whatever ``TelegramBot.send_message`` returned.
        """
        if run is None:
            return
        try:
            delivered = bool(sent)
            run.delivery_status = "delivered" if delivered else INTERACTIVE_SEND_FAILED_STATUS
            run.delivered_at = datetime.now(UTC) if delivered else None
            run.delivery_channel = "telegram"
            if not delivered:
                logger.warning(
                    "Interactive Telegram reply reported no sent messages (run=%s)",
                    getattr(run, "id", None),
                )
            # Imported here (not at module scope) so the DB write stays a
            # lazy dependency and tests can patch it at its source module.
            from robothor.engine.delivery import _persist_delivery_status

            await _persist_delivery_status(run)
        except Exception as e:
            logger.warning("Interactive delivery bookkeeping failed: %s", e)

    async def _run_interactive(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        user_text: str,
        *,
        reply_ctx: dict[str, Any] | None = None,
        user_message_id: str | None = None,
        sender_info: dict[str, Any] | None = None,
    ) -> None:
        """Execute an interactive agent run with streaming, typing indicator, and history management.

        Shared by handle_text and handle_file — the single execution path for
        interactive Telegram messages. ``reply_ctx`` carries the resolved
        channel-bus reply (when the user tapped "Reply to" on a surfaced
        message) so we can annotate the persisted user turn.

        ``sender_info`` is the per-message identity dict threaded through
        from ``_drain_and_run`` (Task 4) — when given, ``run_agent()`` uses
        it verbatim instead of re-reading the shared ``_chat_user_info``
        cache. When omitted (e.g. a caller invoking this directly, bypassing
        the coalescing buffer), the legacy chat_id-keyed cache lookup is
        used — this keeps flag-off/no-threading callers byte-identical.
        """
        # ── Idle timeout: compress stale sessions ──
        now = time.monotonic()
        last = self._last_message_at.get(chat_id, 0.0)
        if last > 0 and (now - last) > self._idle_timeout:
            try:
                from robothor.engine.context import maybe_compress

                async with self._get_session_lock(chat_id):
                    if len(session.history) > 5:
                        history_snapshot = list(session.history)
                        original_count = len(session.history)
                        compressed = await maybe_compress(history_snapshot, threshold=20_000)
                        if (
                            len(compressed) < original_count
                            and len(session.history) == original_count
                        ):
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

        # ── Status-only updates (no content streaming) ──
        # Telegram rate-limits editMessageText to ~20/min.  Instead of streaming
        # content token-by-token, we show brief tool-status edits on the
        # "Thinking..." message and deliver the final output as a new message.
        tool_status_interval = 10.0  # max one status edit per 10 seconds
        last_status_time: float = 0.0
        checklist_msg_id: int | None = None

        async def on_tool(event: dict[str, Any]) -> None:
            nonlocal last_status_time, checklist_msg_id
            if event.get("event") == "tool_start":
                now = time.monotonic()
                if (now - last_status_time) < tool_status_interval:
                    return
                label = _friendly_tool_name(event.get("tool", ""))
                try:
                    if stream_msg_id is not None:
                        await self.bot.edit_message_text(
                            chat_id=int(chat_id),
                            message_id=stream_msg_id,
                            text=f"\U0001f527 {label}...",
                            parse_mode=None,
                        )
                        last_status_time = now
                except Exception:
                    pass
            elif event.get("event") == "todo_updated":
                todos = event.get("todos", [])
                try:
                    if todos:
                        checklist_msg_id = await self._send_or_edit_checklist(
                            chat_id=chat_id, todos=todos, message_id=checklist_msg_id
                        )
                    elif checklist_msg_id:
                        with contextlib.suppress(Exception):
                            await self.bot.delete_message(
                                chat_id=int(chat_id),
                                message_id=checklist_msg_id,
                            )
                        checklist_msg_id = None
                except Exception:
                    # WARNING, not DEBUG: this block was raising TypeError on
                    # every update for as long as the call sites were wrong,
                    # and a DEBUG line is why nobody noticed the live
                    # checklist had simply stopped existing.
                    logger.warning("Checklist update failed", exc_info=True)

        # ── Execute agent ──
        model = self._model_override.get(chat_id)

        async def run_agent() -> None:
            nonlocal stream_msg_id
            _lock = self._get_session_lock(chat_id)
            # Held outside the try so the error path can still account for
            # the delivery of its own reply when the run itself exists.
            run_for_delivery: Any = None
            try:
                async with _lock:
                    history = list(session.history)

                # Build trigger_detail with sender identity for warmup
                # Task 4: prefer the identity threaded through explicitly for
                # THIS message (captured in the handler, before any async
                # gap) over the shared, chat_id-keyed cache — a caller that
                # bypasses the coalescing buffer (direct _run_interactive
                # call, no sender_info) falls back to the cache unchanged.
                _user = (
                    sender_info if sender_info is not None else self._chat_user_info.get(chat_id)
                )
                _sender = _user["display_name"] if _user else ""
                _detail = f"chat:{chat_id}"
                if _sender:
                    _safe = _sender.replace("|", "")
                    _detail += f"|sender:{_safe}"
                _tenant = (_user.get("tenant_id") if _user else None) or self._get_tenant_id(
                    chat_id
                )

                # Unified identity context (robothor.identity) — built from the
                # same cached lookup_user()/fallback dict as _sender/user_id
                # above. `verified` reflects whether _user came from a real
                # tenant_users row (carries `user_id`) vs. a fabricated
                # fallback (unregistered sender, primary-chat/group default).
                # The legacy `|sender:` trigger_detail suffix stays for now
                # (removed in a later phase) — execute() prefers this explicit
                # identity over that parse.
                #
                # `identifier` is the sender's Telegram user id when known
                # (threaded via `_user["telegram_user_id"]`, set by
                # `_resolve_user`) — not chat_id, which conflates every
                # sender in a group chat into one identity (Task 4 carry-note
                # from Task 2). Falls back to chat_id only when the
                # telegram_user_id wasn't captured (e.g. a manually-seeded
                # _chat_user_info entry, or no from_user on the message).
                _identity = self._build_identity(_user, chat_id, _tenant)

                run = await self.runner.execute(
                    agent_id=self.config.default_chat_agent,
                    message=user_text,
                    trigger_type=TriggerType.TELEGRAM,
                    trigger_detail=_detail,
                    on_tool=on_tool,
                    model_override=model,
                    conversation_history=history or None,
                    tenant_id=_tenant,
                    user_id=str((_user or {}).get("user_id") or f"telegram:{chat_id}"),
                    user_role=str((_user or {}).get("role") or "user"),
                    identity=_identity,
                )
                run_for_delivery = run

                async with _lock:
                    # Always record user message in session history
                    session.history.append({"role": "user", "content": user_text})

                    if run.output_text:
                        session.history.append({"role": "assistant", "content": run.output_text})
                    elif run.error_message:
                        session.history.append(
                            {
                                "role": "assistant",
                                "content": f"[Run failed: {run.error_message}]",
                            }
                        )

                    # Trim from front
                    if len(session.history) > self._max_history:
                        session.history[:] = session.history[-self._max_history :]

                # Build user JSONB extras. When the user replied to a
                # surfaced fleet message, include the linkage in the user
                # turn's JSONB so history and audits can see the thread.
                user_extras: dict[str, Any] | None = None
                if user_message_id or reply_ctx:
                    user_extras = {}
                    if user_message_id:
                        user_extras["telegram_message_id"] = user_message_id
                    if reply_ctx:
                        user_extras["replies_to"] = {
                            "platform_message_id": str(reply_ctx.get("platform_message_id", "")),
                            "author_agent_id": reply_ctx.get("author_agent_id", ""),
                            "author_display_name": reply_ctx.get("author_display_name", ""),
                            "chat_message_id": reply_ctx.get("chat_message_id"),
                        }

                # Delete status message, deliver final output as new message.
                # Capture the Telegram Message objects returned by send_message
                # so the channel bus can map platform_message_ids to the
                # assistant chat_message row we'll persist below.
                if stream_msg_id is not None:
                    with contextlib.suppress(Exception):
                        await self.bot.delete_message(
                            chat_id=int(chat_id), message_id=stream_msg_id
                        )

                sent_messages: list[Any] = []
                if run.output_text:
                    sent_messages = await self.send_message(chat_id, run.output_text)
                elif run.error_message:
                    sent_messages = await self.send_message(chat_id, f"Error: {run.error_message}")
                else:
                    sent_messages = await self.send_message(chat_id, "Done. No output produced.")

                await self._record_interactive_delivery(run, sent_messages)

                assistant_platform_ids = [
                    str(m.message_id)
                    for m in (sent_messages or [])
                    if getattr(m, "message_id", None) is not None
                ]

                # Sequenced persistence: save the exchange, then write map rows
                # linked to the real chat_message ids (so downstream audits and
                # reply-to lookups resolve against real rows). One background
                # task keeps the response path snappy but the writes ordered.
                if run.output_text:

                    async def _persist_and_map(
                        session_key: str = session_key,
                        user_text: str = user_text,
                        output_text: str | None = run.output_text,
                        model: Any = model,
                        tenant_id: str = _tenant,
                        user_extras: dict[str, Any] | None = user_extras,
                        user_message_id: str | None = user_message_id,
                        chat_id: str = chat_id,
                        assistant_platform_ids: list[str] = assistant_platform_ids,
                        author_run_id: str | None = run.id,
                    ) -> None:
                        from robothor.engine.channel_bus import (
                            record_inbound,
                            record_outbound,
                        )

                        inserted = await save_exchange_async(
                            session_key,
                            user_text,
                            output_text or "",
                            channel="telegram",
                            model_override=model,
                            tenant_id=tenant_id,
                            user_extras=user_extras,
                        )
                        if not inserted or len(inserted) < 2:
                            return
                        user_chat_message_id, assistant_chat_message_id = inserted

                        loop = asyncio.get_running_loop()

                        if user_message_id:
                            try:
                                await loop.run_in_executor(
                                    None,
                                    lambda: record_inbound(
                                        tenant_id=tenant_id,
                                        channel="telegram",
                                        chat_id=chat_id,
                                        platform_message_id=user_message_id,
                                        session_key=session_key,
                                        chat_message_id=user_chat_message_id,
                                        author_agent_id="user",
                                    ),
                                )
                            except Exception as e:
                                logger.debug("tg-map-inbound failed for %s: %s", chat_id, e)

                        if assistant_platform_ids:
                            try:
                                await loop.run_in_executor(
                                    None,
                                    lambda: record_outbound(
                                        tenant_id=tenant_id,
                                        channel="telegram",
                                        chat_id=chat_id,
                                        platform_message_ids=assistant_platform_ids,
                                        session_key=session_key,
                                        chat_message_id=assistant_chat_message_id,
                                        author_agent_id="main",
                                        author_run_id=author_run_id,
                                    ),
                                )
                            except Exception as e:
                                logger.debug("tg-map-outbound failed for %s: %s", chat_id, e)

                    get_task_registry().spawn(
                        _persist_and_map(),
                        name=f"tg-persist-and-map:{chat_id}",
                    )

                # Ingest conversation to memory (fire-and-forget)
                if len(session.history) >= 4:
                    from robothor.memory.conversation_ingest import (
                        ingest_conversation_session,
                    )

                    get_task_registry().spawn(
                        ingest_conversation_session(
                            session_key=session_key,
                            history=list(session.history),
                            agent_id=self.config.default_chat_agent,
                            trigger_type="telegram",
                            run_id=run.id,
                            tenant_id=self.config.tenant_id,
                        ),
                        name=f"conv-ingest:{chat_id}",
                    )

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
                async with _lock:
                    session.history.append({"role": "user", "content": user_text})
                    session.history.append(
                        {
                            "role": "assistant",
                            "content": f"[Internal error — run failed: {e}]",
                        }
                    )
                    if len(session.history) > self._max_history:
                        session.history[:] = session.history[-self._max_history :]
                sent_error = await self.send_message(
                    chat_id, f"Internal error: {html.escape(str(e))}"
                )
                # Only when nothing was recorded yet — a failure in the
                # post-reply bookkeeping must not overwrite the real
                # reply's already-verified outcome.
                if getattr(run_for_delivery, "delivery_status", None) is None:
                    await self._record_interactive_delivery(run_for_delivery, sent_error)
            finally:
                nonlocal typing_active
                typing_active = False
                typing_task.cancel()
                self._active_tasks.pop(chat_id, None)

                # Drain any messages that arrived while this run was active
                if self._message_buffers.get(chat_id) and not self._drain_scheduled.get(chat_id):
                    self._drain_scheduled[chat_id] = True
                    asyncio.create_task(self._drain_and_run(chat_id, session_key, session))

        task = asyncio.create_task(run_agent())
        self._active_tasks[chat_id] = task

    def _build_background_config(self) -> Any:
        """Build agent config with continuous-mode overrides for background plan execution."""
        from robothor.engine.config import load_agent_config

        config = load_agent_config(self.config.default_chat_agent, self.config.manifest_dir)
        if config is None:
            raise RuntimeError(f"Agent config not found: {self.config.default_chat_agent}")

        # Enable continuous mode — raises caps for sustained multi-hour runs
        config.continuous = True
        config.safety_cap = max(config.safety_cap, 2000)
        config.timeout_seconds = max(config.timeout_seconds, 86400)  # 24h
        config.max_iterations = max(config.max_iterations, 100)
        config.stall_timeout_seconds = max(config.stall_timeout_seconds, 600)
        config.checkpoint_enabled = True
        config.progress_report_interval = 20  # frequent Telegram updates
        return config

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

    def _build_identity(
        self, user: dict[str, Any] | None, chat_id: str, tenant_id: str
    ) -> IdentityContext | None:
        """Build the unified IdentityContext for a resolved Telegram sender.

        Shared by every execution path — interactive ``run_agent``, ``/plan``,
        ``/deep``, plan iteration, and post-approval execution — so this
        construction logic lives in exactly one place (Task 4 Finding 1
        follow-up). ``user`` must be a per-message-resolved sender dict (or,
        for the plan-approval paths, the identity frozen on
        ``PlanState.creator_sender_info`` at plan-creation time) — never a
        fresh read of the shared, chat_id-keyed ``_chat_user_info`` cache,
        which a different sender in a group chat can overwrite between
        capture and execution.

        ``identifier`` is the sender's Telegram user id when known (not
        chat_id, which would conflate every sender in a group chat into one
        identity). Falls back to chat_id only when telegram_user_id wasn't
        captured (no ``from_user`` on the message, or a manually-seeded
        legacy cache entry).
        """
        if not user:
            return None
        from robothor.identity import IdentityContext

        return IdentityContext(
            tenant_id=user.get("tenant_id") or tenant_id,
            channel="telegram",
            identifier=str(user.get("telegram_user_id") or chat_id),
            verified="user_id" in user,
            display_name=user.get("display_name") or "",
            role=user.get("role") or "",
            tenant_user_id=user.get("user_id"),
            person_id=user.get("person_id"),
        )

    def _resolve_user(self, chat_id: str, message: Message) -> dict[str, Any] | None:
        """Resolve the Telegram sender to a tenant user.

        Returns a dict with tenant_id, display_name, role — or None if the
        user is unregistered and should be routed to the closed-onboarding
        refusal (or, when ``ROBOTHOR_OPEN_ONBOARDING`` is set, the legacy
        self-service onboarding flow).

        For the primary operator chat (default_chat_id), falls back to
        operator_name from config if tenant_users has no entry — this
        fabrication is gated by ``ROBOTHOR_TELEGRAM_ROLE_GATES`` (Task 4,
        Unified Identity Context):

        - "off" (default): fabricates unconditionally, byte-identical to
          pre-Task-4 behavior.
        - "observe": still fabricates, but logs loudly (warning level) each
          time, so the operator can see how often an unregistered sender is
          riding the primary-chat fallback before enforce closes it.
        - "enforce": no fabrication — an unregistered default-chat sender is
          treated like any other unknown sender (group fallback below, or
          the closed-onboarding path for a private chat) — UNLESS
          ``ROBOTHOR_ALLOW_UNREGISTERED_OWNER_FALLBACK=1`` (escape hatch for
          a fresh install with no owner row yet).

        Similarly, an unregistered group-chat sender's fabricated role is
        "user" only under "off" (byte-identical); "observe"/"enforce" use
        "guest" instead — role_permissions is fail-closed (no guest rows
        exist yet), so this is a safe default-off tightening on its own.
        """
        from robothor.engine.feature_flags import (
            allow_unregistered_owner_fallback,
            telegram_role_gates_mode,
        )
        from robothor.engine.users import lookup_user

        if not message.from_user:
            return {
                "tenant_id": self.config.tenant_id,
                "display_name": "",
                "role": "user",
            }

        telegram_user_id = str(message.from_user.id)
        user_info = lookup_user(telegram_user_id, tenant_id=self.config.tenant_id)

        if user_info is not None:
            user_info = {**user_info, "telegram_user_id": telegram_user_id}
            self._chat_user_info[chat_id] = user_info
            return user_info

        mode = telegram_role_gates_mode()

        # Unregistered user — primary operator gets a fallback
        if chat_id == self.config.default_chat_id and (
            mode != "enforce" or allow_unregistered_owner_fallback()
        ):
            if mode == "observe":
                logger.warning(
                    "telegram_role_gates: fabricating owner identity for unregistered "
                    "default-chat sender telegram_id=%s tenant=%s — register them with "
                    "`robothor user add` to stop seeing this",
                    telegram_user_id,
                    self.config.tenant_id,
                )
            fallback = {
                "tenant_id": self.config.tenant_id,
                "display_name": self.config.operator_name or message.from_user.first_name or "",
                "role": "owner",
                "telegram_user_id": telegram_user_id,
            }
            self._chat_user_info[chat_id] = fallback
            return fallback

        # Unregistered user in a group chat — use default tenant. Role is
        # "guest" once the flag leaves "off" (Phase 5 seeds actual
        # role_permissions rows for it; fail-closed until then means zero
        # tool grants, not a privilege increase).
        if message.chat.type != "private":
            fallback = {
                "tenant_id": self.config.tenant_id,
                "display_name": message.from_user.first_name or "",
                "role": "user" if mode == "off" else "guest",
                "telegram_user_id": telegram_user_id,
            }
            self._chat_user_info[chat_id] = fallback
            return fallback

        # Unregistered user in private chat — route to closed-onboarding
        # refusal (or legacy self-service onboarding under the escape flag).
        return None

    def _sender_is_owner(self, telegram_user_id: str) -> bool:
        """Resolve ``telegram_user_id`` directly (independent of the chat it
        posted from) and report whether that sender's registered role is
        "owner".

        Used by ``_check_owner_gate`` under
        ``ROBOTHOR_TELEGRAM_ROLE_GATES=observe|enforce`` so a non-owner
        posting from the operator's own chat_id — or the owner posting from
        a different chat — is judged by their own registered role, not by
        which chat_id the message arrived on.
        """
        from robothor.engine.users import lookup_user

        info = lookup_user(telegram_user_id, tenant_id=self.config.tenant_id)
        return bool(info and info.get("role") == "owner")

    def _check_owner_gate(self, *, chat_id: str, sender_id: str, site: str) -> bool:
        """Authorize an owner-only Telegram surface, per the
        ``ROBOTHOR_TELEGRAM_ROLE_GATES`` rollout ladder (Task 4, Unified
        Identity Context).

        Every owner-only command/callback (``/restart``, ``/agents``,
        ``/steer``, the ``perm:``/``dp:``/``runctl:`` callbacks) used to gate
        solely on ``chat_id == default_chat_id`` — which authorizes anyone
        posting in the operator's chat, not just the operator. This is the
        fix:

        - "off" (default): the legacy chat_id-equality check only —
          byte-identical to pre-flag behavior.
        - "observe": evaluates BOTH checks but still enforces the OLD
          (chat_id) check; logs a structured
          ``telegram_role_gates: divergence`` line whenever they disagree,
          so an operator can audit what enforce would decide before
          flipping it.
        - "enforce": the role check only — chat_id is irrelevant to
          authorization from here on.
        """
        from robothor.engine.feature_flags import telegram_role_gates_mode

        chat_ok = str(chat_id) == str(self.config.default_chat_id)
        mode = telegram_role_gates_mode()
        if mode == "off":
            return chat_ok

        role_ok = self._sender_is_owner(str(sender_id))
        if mode == "observe":
            if chat_ok != role_ok:
                logger.warning(
                    "telegram_role_gates: divergence site=%s chat_ok=%s role_ok=%s sender=%s",
                    site,
                    chat_ok,
                    role_ok,
                    sender_id,
                )
            return chat_ok

        return role_ok

    def _get_tenant_id(self, chat_id: str) -> str:
        """Get the resolved tenant_id for a chat, falling back to config."""
        info = self._chat_user_info.get(chat_id)
        return info["tenant_id"] if info else self.config.tenant_id

    async def _handle_unregistered_sender(self, message: Message, telegram_user_id: str) -> str:
        """Route an unregistered private-chat sender to onboarding or refusal.

        Gated by ``feature_flags.open_onboarding_enabled()`` — default OFF
        (Task 4, Unified Identity Context: the operator decision to close
        self-provisioning). When open, delegates to the legacy
        ``onboarding.start_onboarding`` flow (any unknown private sender can
        create their own tenant). When closed (default), returns a generic
        refusal — no operator name, safe to ship as platform code — and
        notifies the operator (rate-limited to once per sender per hour)
        with a registration hint.
        """
        from robothor.engine.feature_flags import open_onboarding_enabled

        if open_onboarding_enabled():
            from robothor.engine.onboarding import start_onboarding

            return start_onboarding(telegram_user_id)

        await self._notify_operator_of_unregistered_sender(message, telegram_user_id)
        return (
            "This bot is not open for self-registration. "
            "If you believe you should have access, please contact the workspace operator directly."
        )

    async def _notify_operator_of_unregistered_sender(
        self, message: Message, telegram_user_id: str
    ) -> None:
        """Best-effort, rate-limited operator alert for a refused unregistered sender.

        At most once per Telegram sender id per hour (in-process dict) — a
        spammer retrying the closed-onboarding refusal must not be able to
        flood the operator's chat with one notification per message. Sends
        via ``self.send_message`` — the same wrapper ``delivery.py``
        registers as the platform's Telegram sender — targeting
        ``config.default_chat_id`` (the operator's primary chat).

        The sender's raw message text is untrusted and attacker-controlled
        (review Finding 2) — it is sanitized (newlines/control characters
        collapsed to spaces via ``_sanitize_preview``) and rendered quoted on
        a single line, clearly delimited from the "To register them:" hint
        that follows. Without this, a crafted multi-line message could
        embed its own fake hint line + bogus CLI command, formatted to look
        identical to the genuine one, to trick the operator into running it.
        """
        now = time.monotonic()
        # ``last`` must default to "never notified", not to 0.0. time.monotonic()'s
        # epoch is arbitrary (time since boot on Linux) — comparing against a
        # literal 0.0 silently drops the very first notification for any sender
        # whenever process/system uptime is under the rate-limit window (e.g. a
        # freshly booted CI runner), while never manifesting on a long-lived host
        # where monotonic() is always far past the window.
        last = self._onboarding_notify_last.get(telegram_user_id)
        if last is not None and now - last < _ONBOARDING_NOTIFY_INTERVAL_SECONDS:
            return
        self._onboarding_notify_last[telegram_user_id] = now

        try:
            sender = message.from_user
            username = getattr(sender, "username", None) if sender else None
            raw_preview = getattr(message, "text", None) or getattr(message, "caption", None) or ""
            preview = _sanitize_preview(raw_preview)
            hint = (
                f'robothor user add --tenant {self.config.tenant_id} --name "<name>" '
                f"--role member --telegram-id {telegram_user_id}"
            )
            lines = [
                "\U0001f6ab Unregistered sender messaged the bot (onboarding closed):",
                f"Telegram id: {telegram_user_id}",
                f"Username: @{username}" if username else "Username: (none)",
                f'Message (quoted, untrusted): "{preview}"' if preview else "Message: (none)",
                "",
                "To register them:",
                hint,
            ]
            await self.send_message(self.config.default_chat_id, "\n".join(lines))
        except Exception:
            logger.warning(
                "Failed to notify operator of unregistered sender %s",
                telegram_user_id,
                exc_info=True,
            )

    async def _handle_restart_command(self, message: Message, unit_name: str) -> None:
        """Queue a restart by writing the advisory trigger file that
        ``robothor-restart.path`` (infra/systemd/robothor-restart.path) watches.

        Owner-only (gate on default_chat_id). systemd — not this process —
        holds the privilege to restart the unit: the path unit's paired
        service hardcodes the restart target, so this handler's write is
        advisory only (a UTC timestamp + sender, for audit) and can never
        choose which unit gets restarted.

        Only ``robothor-engine.service`` has a path unit today (see
        ``_RESTART_TRIGGERS``); any other unit — e.g.
        robothor-delphi-engine.service — gets told to use SSH instead of
        silently doing nothing.
        """
        chat_id = str(message.chat.id)
        sender_id = message.from_user.id if message.from_user else "unknown"
        if not self._check_owner_gate(
            chat_id=chat_id, sender_id=str(sender_id), site="restart_command"
        ):
            logger.warning(
                "Unauthorized /restart attempt from chat_id=%s user_id=%s",
                chat_id,
                sender_id,
            )
            await message.answer("Unauthorized.")
            return

        trigger_path = _RESTART_TRIGGERS.get(unit_name)
        if trigger_path is None:
            await message.answer(
                f"No restart path-unit exists for {unit_name} — use SSH to restart it."
            )
            return

        line = f"{datetime.now(UTC).isoformat()} telegram:{sender_id}\n"
        try:
            trigger_path.write_text(line)
        except OSError as e:
            logger.exception("Failed to write restart trigger for %s", unit_name)
            await message.answer(f"Failed to queue restart of {unit_name}: {e}")
            return

        await message.answer(f"Restart of {unit_name} queued.")

    async def _handle_goal_command(self, message: Message) -> None:
        """Manage the operator's long-running session goal from Telegram.

        Backed by a crm_task with the session_goal tag. Evidence is typed:
          - test_run  reference must match pytest:passed:N or be a UUID
          - commit    reference must be a git SHA (validated via git cat-file)
          - ci_run    reference must be an https:// URL
          - note      free-form (does not satisfy completion guard)

        Shortcuts:
          /goal evidence-commit HEAD          — resolves HEAD via `git rev-parse`
          /goal evidence-test pytest:passed:N — short alias for `evidence test_run …`

        Owner-only (gated via ``_check_owner_gate``, Task 4/5 follow-up,
        Unified Identity Context). Previously this handler had NO
        authorization check of any kind — any sender in any chat could
        mutate the operator's session goal.
        """
        chat_id = str(message.chat.id)
        sender_id = message.from_user.id if message.from_user else "unknown"
        if not self._check_owner_gate(
            chat_id=chat_id, sender_id=str(sender_id), site="goal_command"
        ):
            logger.warning(
                "Unauthorized /goal attempt from chat_id=%s user_id=%s",
                chat_id,
                sender_id,
            )
            await message.answer("Unauthorized.")
            return

        from robothor.engine.session_goal import (
            add_criterion,
            add_evidence,
            complete_goal,
            create_active_goal,
            edit_objective,
            get_active_goal,
            regenerate_goal_md_cache,
            set_metric_target,
        )

        text = (message.text or "").strip()
        arg = text.removeprefix("/goal").strip()
        agent_id = self.config.default_chat_agent
        tenant_id = self.config.tenant_id
        workspace = self.config.workspace

        try:
            if not arg or arg == "status":
                goal = get_active_goal(tenant_id=tenant_id, agent_id=agent_id)
                await message.answer(self._format_goal_status(goal, agent_id=agent_id))
                return

            command, _, rest = arg.partition(" ")
            command = command.lower()
            rest = rest.strip()

            if command == "set":
                if not rest:
                    await message.answer(
                        "Usage: <code>/goal set &lt;objective&gt;</code>\n"
                        "An active goal must be completed (<code>/goal done &lt;note&gt;</code>) "
                        "before a new one can be created."
                    )
                    return
                try:
                    goal = create_active_goal(
                        tenant_id=tenant_id, objective=rest, agent_id=agent_id
                    )
                except ValueError as exc:
                    if "active goal already exists" not in str(exc):
                        raise
                    await message.answer(
                        "An active goal already exists. Complete it first with "
                        "<code>/goal done &lt;note&gt;</code>."
                    )
                    return
                regenerate_goal_md_cache(
                    tenant_id=tenant_id, workspace=workspace, agent_id=agent_id
                )
                await message.answer(self._format_goal_status(goal, agent_id=agent_id))
                return

            if command == "evidence":
                kind, _, summary = rest.partition(" ")
                kind = kind.strip().lower()
                summary, _, reference = summary.strip().partition("--ref=")
                summary = summary.strip()
                reference = reference.strip()
                if kind not in {"test_run", "commit", "ci_run", "note"}:
                    await message.answer(
                        "Usage: <code>/goal evidence &lt;kind&gt; &lt;summary&gt; "
                        "[--ref=&lt;reference&gt;]</code>\n"
                        "kind must be one of: test_run, commit, ci_run, note"
                    )
                    return
                if not summary:
                    await message.answer("Evidence summary is required.")
                    return
                goal = add_evidence(
                    tenant_id=tenant_id,
                    kind=kind,
                    summary=summary,
                    reference=reference,
                    agent_id=agent_id,
                    workspace=workspace,
                )
                regenerate_goal_md_cache(
                    tenant_id=tenant_id, workspace=workspace, agent_id=agent_id
                )
                await message.answer(self._format_goal_status(goal, agent_id=agent_id))
                return

            if command == "evidence-commit":
                # `/goal evidence-commit HEAD` or `/goal evidence-commit <sha>`
                ref = (rest or "HEAD").strip()
                if ref.upper() == "HEAD":
                    try:
                        result = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=str(workspace),
                            capture_output=True,
                            text=True,
                            timeout=5,
                        )
                        if result.returncode != 0:
                            await message.answer(
                                f"git rev-parse HEAD failed: {html.escape(result.stderr.strip())}"
                            )
                            return
                        ref = result.stdout.strip()
                    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                        await message.answer(f"git unavailable: {exc}")
                        return
                goal = add_evidence(
                    tenant_id=tenant_id,
                    kind="commit",
                    summary=f"committed {ref[:12]}",
                    reference=ref,
                    agent_id=agent_id,
                    workspace=workspace,
                )
                regenerate_goal_md_cache(
                    tenant_id=tenant_id, workspace=workspace, agent_id=agent_id
                )
                await message.answer(self._format_goal_status(goal, agent_id=agent_id))
                return

            if command == "evidence-test":
                ref = rest.strip() or "pytest:passed:0"
                summary = f"recorded {ref}"
                goal = add_evidence(
                    tenant_id=tenant_id,
                    kind="test_run",
                    summary=summary,
                    reference=ref,
                    agent_id=agent_id,
                    workspace=workspace,
                )
                regenerate_goal_md_cache(
                    tenant_id=tenant_id, workspace=workspace, agent_id=agent_id
                )
                await message.answer(self._format_goal_status(goal, agent_id=agent_id))
                return

            if command in {"done", "complete"}:
                if not rest:
                    await message.answer("Usage: <code>/goal done &lt;completion note&gt;</code>")
                    return
                goal = complete_goal(
                    tenant_id=tenant_id,
                    note=rest,
                    agent_id=agent_id,
                    workspace=workspace,
                )
                regenerate_goal_md_cache(
                    tenant_id=tenant_id, workspace=workspace, agent_id=agent_id
                )
                await message.answer(self._format_goal_status(goal, agent_id=agent_id))
                return

            if command in {"edit-objective", "edit"}:
                if not rest:
                    await message.answer("Usage: <code>/goal edit-objective &lt;text&gt;</code>")
                    return
                goal = edit_objective(tenant_id=tenant_id, agent_id=agent_id, objective=rest)
                regenerate_goal_md_cache(
                    tenant_id=tenant_id, workspace=workspace, agent_id=agent_id
                )
                await message.answer(self._format_goal_status(goal, agent_id=agent_id))
                return

            if command in {"add-criterion", "add-crit"}:
                if not rest:
                    await message.answer("Usage: <code>/goal add-criterion &lt;text&gt;</code>")
                    return
                goal = add_criterion(tenant_id=tenant_id, agent_id=agent_id, text=rest)
                regenerate_goal_md_cache(
                    tenant_id=tenant_id, workspace=workspace, agent_id=agent_id
                )
                await message.answer(self._format_goal_status(goal, agent_id=agent_id))
                return

            if command == "set-target":
                # Format: /goal set-target <metric> <op><value>
                # e.g.    /goal set-target error_rate <0.02
                parts = rest.split(maxsplit=1)
                if len(parts) != 2:
                    await message.answer(
                        "Usage: <code>/goal set-target &lt;metric&gt; "
                        "&lt;op&gt;&lt;value&gt;</code>"
                    )
                    return
                metric, target = parts[0].strip(), parts[1].strip()
                goal = set_metric_target(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    metric=metric,
                    target=target,
                )
                regenerate_goal_md_cache(
                    tenant_id=tenant_id, workspace=workspace, agent_id=agent_id
                )
                await message.answer(self._format_goal_status(goal, agent_id=agent_id))
                return

            await message.answer(
                "<b>Goal commands</b>\n\n"
                "/goal — show active goal\n"
                "/goal set &lt;objective&gt; — create if none exists\n"
                "/goal edit-objective &lt;text&gt; — replace objective in place\n"
                "/goal add-criterion &lt;text&gt; — append a success criterion\n"
                "/goal set-target &lt;metric&gt; &lt;op&gt;&lt;value&gt; — add/replace metric target\n"
                "/goal evidence &lt;kind&gt; &lt;summary&gt; [--ref=&lt;ref&gt;] — record evidence\n"
                "  kinds: test_run, commit, ci_run, note\n"
                "/goal evidence-commit &lt;sha|HEAD&gt; — shortcut, validates SHA\n"
                "/goal evidence-test pytest:passed:N — shortcut for test_run\n"
                "/goal done &lt;note&gt; — complete after one valid test_run AND one valid commit"
            )
        except Exception as e:
            logger.exception("Telegram /goal failed")
            await message.answer(f"Goal unavailable: {html.escape(str(e))}")

    def _format_goal_status(self, goal: Any, *, agent_id: str) -> str:
        """Format a session goal for Telegram HTML."""
        from robothor.engine.session_goal import summarize_goal

        if goal is None:
            return (
                f"<b>Active goal</b> — {html.escape(agent_id)}\n\n"
                "No active goal.\n"
                "Set one with <code>/goal set &lt;objective&gt;</code>."
            )

        data = summarize_goal(goal, workspace=self.config.workspace)
        lines = [
            f"<b>Active goal</b> — {html.escape(agent_id)}",
            f"Status: <code>{html.escape(str(data['status']))}</code>",
            f"Objective: {html.escape(str(data['objective']))}",
            f"Evidence: {data['valid_evidence_count']}/{data['evidence_count']} validated",
        ]

        missing = data.get("missing_completion_requirements") or []
        if missing:
            lines.append("")
            lines.append("<b>Missing before completion</b>:")
            lines.extend(f"- {html.escape(str(item))}" for item in missing)
        else:
            lines.append("")
            lines.append("Ready for completion.")
        return "\n".join(lines)

    def _session_key(self, chat_id: str) -> str:
        """Return a DB session key for a Telegram chat.

        The primary operator's chat (matches default_chat_id) maps to the canonical
        shared session key so Telegram and Helm share one conversation.
        Other chats keep the telegram: prefix.
        """
        if chat_id == self.config.default_chat_id:
            return get_main_session_key()
        return f"telegram:{chat_id}"

    def _load_persisted_history(self) -> None:
        """Restore model overrides for non-primary Telegram chats.

        The canonical shared session (the primary chat) is restored by
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

    async def _handle_agents_command(self, message: Message) -> None:
        """List currently-active runs (this engine process) with Steer/Interrupt buttons.

        Reads the in-process session_registry directly — the bot and the
        runner share a process, so no HTTP round-trip through health.py is
        needed here (unlike an external dashboard, which would hit
        GET /api/runs/active).
        """
        chat_id = str(message.chat.id)
        sender_id = message.from_user.id if message.from_user else "unknown"
        if not self._check_owner_gate(
            chat_id=chat_id, sender_id=str(sender_id), site="agents_command"
        ):
            logger.warning(
                "Unauthorized /agents attempt from chat_id=%s user_id=%s",
                chat_id,
                sender_id,
            )
            await message.answer("Unauthorized.")
            return

        from robothor.engine import session_registry

        runs: list[tuple[str, str]] = []
        for run_id in session_registry.active_run_ids():
            session = session_registry.lookup(run_id)
            if session is None:
                continue
            runs.append((run_id, session.run.agent_id))

        if not runs:
            await message.answer("No active runs.")
            return

        lines = ["<b>Active runs</b>", ""]
        for run_id, agent_id in runs:
            lines.append(f"<code>{html.escape(run_id[:8])}</code> — {html.escape(agent_id)}")

        await message.answer(
            "\n".join(lines),
            reply_markup=self._build_agents_keyboard(runs),
        )

    async def _handle_runctl_callback(self, callback: CallbackQuery) -> None:
        """Handle the Steer/Interrupt inline buttons from /agents.

        callback_data shape: ``runctl:i:<run_id>`` (interrupt) or
        ``runctl:s:<run_id>`` (steer). Steer can't collect free text from a
        button tap, so it just replies with the /steer command to send.
        """
        msg = callback.message
        if not msg or not hasattr(msg, "chat"):
            await callback.answer("Unauthorized", show_alert=True)
            return
        sender_id = callback.from_user.id if callback.from_user else "unknown"
        if not self._check_owner_gate(
            chat_id=str(msg.chat.id), sender_id=str(sender_id), site="runctl_callback"
        ):
            logger.warning(
                "Unauthorized runctl callback from chat_id=%s user_id=%s",
                msg.chat.id,
                sender_id,
            )
            await callback.answer("Unauthorized", show_alert=True)
            return

        if not callback.data:
            await callback.answer("Invalid callback data")
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3 or parts[0] != "runctl":
            await callback.answer("Invalid callback data")
            return

        action, run_id = parts[1], parts[2]

        if action == "i":
            from robothor.engine.interrupt_api import interrupt_session

            ok = interrupt_session(run_id)
            if ok:
                await callback.answer(f"Interrupt requested for {run_id[:8]}")
            else:
                await callback.answer("Run no longer active")
            with contextlib.suppress(Exception):
                if hasattr(msg, "edit_reply_markup"):
                    await msg.edit_reply_markup(reply_markup=None)
        elif action == "s":
            await callback.answer(
                f"Send: /steer {run_id[:8]} <your message>",
                show_alert=True,
            )
        else:
            await callback.answer("Unknown action")

    async def _handle_steer_command(self, message: Message) -> None:
        """Inject a steering message into a live run.

        ``/steer [run_id_prefix] <text>`` — resolves the run by a unique
        run_id prefix among active runs. If the first token doesn't match
        any active run, the whole argument string is treated as steer text
        targeting the single active run (only when exactly one is active).
        """
        chat_id = str(message.chat.id)
        sender_id = message.from_user.id if message.from_user else "unknown"
        if not self._check_owner_gate(
            chat_id=chat_id, sender_id=str(sender_id), site="steer_command"
        ):
            logger.warning(
                "Unauthorized /steer attempt from chat_id=%s user_id=%s",
                chat_id,
                sender_id,
            )
            await message.answer("Unauthorized.")
            return

        from robothor.engine import session_registry
        from robothor.engine.interrupt_api import steer_session

        text = (message.text or "").removeprefix("/steer").strip()
        if not text:
            await message.answer("Usage: /steer [run_id_prefix] <text>")
            return

        run_ids = session_registry.active_run_ids()
        if not run_ids:
            await message.answer("No active runs to steer.")
            return

        first_token, _, rest = text.partition(" ")
        matches = [rid for rid in run_ids if rid.startswith(first_token)]

        if len(matches) == 1:
            run_id = matches[0]
            steer_text = rest.strip()
            if not steer_text:
                await message.answer("Usage: /steer <run_id_prefix> <text>")
                return
        elif len(matches) > 1:
            await message.answer(
                f"Ambiguous run_id prefix {first_token!r} matches {len(matches)} active runs "
                "— use more characters."
            )
            return
        elif len(run_ids) == 1:
            run_id = run_ids[0]
            steer_text = text
        else:
            await message.answer(
                "Multiple active runs — specify a run_id prefix: /steer <run_id_prefix> <text>"
            )
            return

        ok = steer_session(run_id, steer_text)
        if ok:
            await message.answer(f"Steered {run_id[:8]}: {html.escape(steer_text)}")
        else:
            await message.answer(f"Run {run_id[:8]} is no longer active.")

    def _build_agents_keyboard(self, runs: list[tuple[str, str]]) -> InlineKeyboardMarkup:
        """Build the Steer/Interrupt inline keyboard for /agents.

        callback_data carries the full run_id (needed for lookup); only the
        display text is truncated. UUIDs comfortably fit Telegram's 64-byte
        callback_data limit (``runctl:i:`` + 36-char uuid4 = 45 bytes).
        """
        buttons: list[list[InlineKeyboardButton]] = []
        for run_id, _agent_id in runs:
            buttons.append(
                [
                    InlineKeyboardButton(text="Steer", callback_data=f"runctl:s:{run_id}"),
                    InlineKeyboardButton(text="Interrupt", callback_data=f"runctl:i:{run_id}"),
                ]
            )
        return InlineKeyboardMarkup(inline_keyboard=buttons)

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

    async def _send_or_edit_checklist(
        self,
        chat_id: str,
        todos: list[dict[str, str]],
        message_id: int | None,
    ) -> int | None:
        """Render the live todo checklist, editing in place after the first send.

        Extracted from the ``todo_updated`` handler so it can be tested at all.
        Inline, both calls handed ``_retry_on_flood`` a pre-built coroutine
        instead of the zero-arg factory it documents, so every update raised
        TypeError before reaching the network — swallowed by the handler's
        ``except Exception``.
        """
        text = _format_checklist_html(todos)
        if message_id:
            await self._retry_on_flood(
                lambda: self.bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=message_id,
                    text=text,
                    parse_mode="HTML",
                )
            )
            return message_id
        msg = await self._retry_on_flood(
            lambda: self.bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode="HTML",
            )
        )
        return msg.message_id if msg else None

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
        if not callable(coro_factory):
            raise TypeError(
                "_retry_on_flood needs a zero-arg factory, not a pre-built "
                "coroutine — a coroutine cannot be awaited twice, so retrying "
                "it is impossible. Wrap the call in a lambda."
            )
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

    async def send_message(self, chat_id: str, text: str, **_ignored: Any) -> list[Any]:
        """Send a message to a Telegram chat, splitting if needed.

        Never raises: a chunk that fails as HTML is retried as plain text,
        and a chunk that fails both is logged and **omitted from the
        result**. The returned list therefore holds one Message per chunk
        that actually reached Telegram and is SHORTER than the chunk count
        when sends failed (empty when they all did) — it is the only
        evidence of delivery, which is why ``delivery._deliver_telegram``
        compares its length against ``split_telegram_message`` rather than
        assuming a send succeeded.

        Args:
            chat_id: Target Telegram chat id.
            text: Message body; split on ``MAX_MESSAGE_LENGTH``.

        Returns:
            The Message objects Telegram acknowledged, in send order, so
            callers (the channel bus) can record platform message_ids for
            reply-to resolution.
        """
        if not text:
            return []

        sent: list[Any] = []
        chunks = self._split_message(text)
        for chunk in chunks:
            html_chunk = _md_to_html(chunk)
            result: Any = None
            try:
                result = await self._retry_on_flood(
                    lambda c=html_chunk: self.bot.send_message(
                        chat_id=int(chat_id),
                        text=c,
                        parse_mode=ParseMode.HTML,
                    )
                )
            except Exception:
                try:
                    result = await self._retry_on_flood(
                        lambda c=chunk: self.bot.send_message(
                            chat_id=int(chat_id),
                            text=c,
                            parse_mode=None,
                        )
                    )
                except Exception as e:
                    logger.error("Failed to send Telegram message: %s", e)
            if result is not None:
                sent.append(result)
        return sent

    def _split_message(self, text: str) -> list[str]:
        """Split text into chunks that fit Telegram's limit.

        Delegates to ``robothor.engine.chunking.split_telegram_message`` so
        the delivery layer can predict the chunk count with the exact same
        algorithm (see that module's docstring).
        """
        return split_telegram_message(text, MAX_MESSAGE_LENGTH)

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
            commands = [
                BotCommand(command="deep", description="Deep reasoning via RLM"),
                BotCommand(command="plan", description="Plan before executing"),
                BotCommand(command="model", description="Switch AI model"),
                BotCommand(command="goal", description="Show or update the active session goal"),
                BotCommand(command="goals", description="Agent benchmark grades"),
                BotCommand(command="clear", description="Clear conversation history"),
                BotCommand(command="context", description="Context window stats"),
                BotCommand(command="status", description="Engine health"),
                BotCommand(command="stats", description="RPG stats and XP"),
                BotCommand(command="buddy", description="Full buddy profile"),
                BotCommand(command="reset", description="Reset model + history"),
                BotCommand(command="stop", description="Cancel current response"),
                BotCommand(command="agents", description="List active runs (steer/interrupt)"),
                BotCommand(command="steer", description="Steer a live run: /steer [run_id] text"),
                BotCommand(command="restart", description="Restart the engine (owner only)"),
                BotCommand(
                    command="restart_delphi", description="Restart the Delphi engine (owner only)"
                ),
                BotCommand(command="help", description="Show commands"),
            ]
            # Set for both default and private-chat scopes so DMs see the full menu
            await self.bot.set_my_commands(commands)
            await self.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        except Exception as e:
            logger.warning("Failed to set bot commands: %s", e)

        logger.info("Starting Telegram bot polling...")
        # Bounded-backoff retry on network errors (5s doubling to a 60s cap,
        # indefinitely). A boot-time DNS failure used to propagate out of this
        # task, which the daemon's FIRST_COMPLETED wait treats as a shutdown
        # trigger — one transient getUpdates failure restarted the whole
        # engine (with exit 0, so OnFailure never paged). A Telegram outage
        # must not take down the scheduler/health/watchdog subsystems.
        backoff = 5.0
        while True:
            attempt_started = time.monotonic()
            try:
                # Explicitly resolve allowed_updates from registered handlers so the
                # message_reaction handler (Phase 2 operator signals) actually receives
                # reaction updates — Telegram omits them from getUpdates by default.
                await self.dp.start_polling(
                    self.bot, allowed_updates=self.dp.resolve_used_update_types()
                )
                return  # clean stop (aiogram handles SIGTERM/SIGINT) — shutdown
            except TelegramNetworkError as e:
                # After a healthy long-lived session, restart the backoff ladder.
                if time.monotonic() - attempt_started > 300:
                    backoff = 5.0
                logger.warning("Telegram polling network error (retrying in %.0fs): %s", backoff, e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
            except Exception as e:
                logger.error("Telegram polling failed: %s", e, exc_info=True)
                raise

    async def stop(self) -> None:
        """Stop the bot gracefully."""
        # Clear message buffers and cancel all active tasks
        self._message_buffers.clear()
        self._drain_scheduled.clear()
        for task in self._active_tasks.values():
            task.cancel()
        self._active_tasks.clear()

        with contextlib.suppress(Exception):
            await self.bot.session.close()
