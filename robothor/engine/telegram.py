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
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
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
    PhotoSize,
)

from robothor.constants import DEFAULT_TENANT
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
from robothor.engine.chunking import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    split_telegram_message,
)
from robothor.engine.delivery import set_telegram_sender
from robothor.engine.models import PlanState, TriggerType
from robothor.engine.task_registry import get_task_registry

if TYPE_CHECKING:
    from robothor.engine.config import EngineConfig
    from robothor.engine.runner import AgentRunner
    from robothor.identity import IdentityContext

logger = logging.getLogger(__name__)

# ── Constants ──

MAX_MESSAGE_LENGTH = TELEGRAM_MAX_MESSAGE_LENGTH
TYPING_INTERVAL = 4  # seconds between typing indicator refreshes
THINKING_TEXT = "\u2728 Thinking..."  # shown instantly while LLM starts up

# Delivery status written onto an interactive run when the Telegram send
# reported nothing sent. ``TelegramBot.send_message`` swallows per-chunk
# exceptions and returns an empty list on total failure, so "no messages"
# is the only signal a lost reply gives us.
INTERACTIVE_SEND_FAILED_STATUS = "failed: telegram send returned no messages"

# Closed-onboarding operator notification rate limit (Task 4, Unified
# Identity Context) -- at most one alert per unregistered sender per hour.
_ONBOARDING_NOTIFY_INTERVAL_SECONDS = 3600.0

# File handling — max size for text extraction (5 MB)
MAX_FILE_SIZE = 5 * 1024 * 1024

# Units the Telegram /restart family can queue a restart for, mapped to the
# advisory trigger file that ``robothor-restart.path`` (infra/systemd/) watches.
# The restart TARGET is hardcoded in that path unit's paired .service — this
# file's contents are never read as the unit to restart, only as an audit
# trail (UTC timestamp + sender). Module-level so tests can inject a tmp_path.
# A unit with no entry here has no path-unit watching for it yet (e.g.
# robothor-delphi-engine) — the handler tells the caller to use SSH instead.
_RESTART_TRIGGERS: dict[str, Path] = {
    "robothor-engine.service": Path("/run/robothor/restart-request"),
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


def _plan_state_to_dict(plan: PlanState) -> dict[str, Any]:
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
        "creator_sender_info": plan.creator_sender_info,
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
    # OpenRouter-only (operator policy 2026-07-07): the OpenAI account was
    # blocked, so codex/* auth is dead — no picker entry may route there.
    # MiMo V2.5 Pro is the fleet-wide primary; the premium entries are
    # operator-selectable escalations billed through the same OpenRouter key.
    "MiMo V2.5": "openrouter/xiaomi/mimo-v2.5",
    "MiMo V2.5 Pro": "openrouter/xiaomi/mimo-v2.5-pro",
    "DeepSeek V4 Pro": "openrouter/deepseek/deepseek-v4-pro",
    "Claude Sonnet 4.6": "openrouter/anthropic/claude-sonnet-4.6",
    "Claude Opus 4.7": "openrouter/anthropic/claude-opus-4.7",
    "Ox Alpha": "openrouter/stealth/ox-alpha",
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
    # Markdown-style links
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


async def _analyze_photo_bytes(
    raw_bytes: bytes,
    prompt: str = "Describe what you see in this image in detail. Read and transcribe ALL visible text exactly. Note any URLs, names, numbers, UI elements, or content shown.",
) -> str:
    """Send raw image bytes to llama3.2-vision via Ollama for VLM analysis."""
    import base64
    import os

    ollama_url = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    img_b64 = base64.b64encode(raw_bytes).decode("utf-8")
    payload = {
        "model": "llama3.2-vision:11b",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Robothor's vision system. Your ONLY job is to describe what is visible in images. "
                    "You MUST always describe image content — never refuse, never say you cannot access websites or URLs. "
                    "If you see a URL in an image, READ and REPORT the text — do not try to visit it. "
                    "Transcribe ALL visible text exactly as shown. Report people, UI elements, screenshots, diagrams, "
                    "app names, domain names, error messages, and any other visible content. Be literal and precise."
                ),
            },
            {"role": "user", "content": prompt, "images": [img_b64]},
        ],
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 1024, "num_gpu": 999},
    }
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{ollama_url}/api/chat", json=payload)
            resp.raise_for_status()
            return resp.json()["message"]["content"]  # type: ignore[no-any-return]
    except Exception as e:
        return f"[Vision analysis failed: {e}]"


def fetch_agent_grades(tenant_id: str, workspace: str | Path) -> list[dict[str, Any]]:
    """Latest benchmark row per agent, scoped to that agent's own suite.

    ``DISTINCT ON (agent_id)`` alone hands the grade to whichever suite wrote
    last under this agent_id. Rows are matched against the agent's on-disk
    suite id where one exists (``canonical_suite_id``); an agent whose suite
    cannot be read keeps its unfiltered latest row rather than vanishing from
    the report.
    """
    from robothor.db.connection import get_connection
    from robothor.engine.tools.handlers.benchmark import canonical_suite_id

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (agent_id, suite_id)
              agent_id, suite_id, run_at, total_cases, passed,
              aggregate_score, judge_errors, failures
            FROM benchmark_results
            WHERE tenant_id = %s
              AND run_at >= NOW() - INTERVAL '48 hours'
            ORDER BY agent_id, suite_id, run_at DESC
            """,
            (tenant_id,),
        )
        rows = cur.fetchall()

    by_agent: dict[str, dict[str, Any]] = {}
    for agent_id, suite_id, run_at, total, passed, aggregate, judge_errors, failures in rows:
        canonical = canonical_suite_id(agent_id, str(workspace))
        if canonical and suite_id != canonical:
            continue
        failures_payload = failures or []
        if isinstance(failures_payload, str):
            import json as _json

            failures_payload = _json.loads(failures_payload)
        current = by_agent.get(agent_id)
        if current is not None and current["run_at"] >= run_at:
            continue
        by_agent[agent_id] = {
            "agent_id": agent_id,
            "suite_id": suite_id,
            "run_at": run_at,
            "total_cases": int(total or 0),
            "passed": int(passed or 0),
            "aggregate_score": float(aggregate) if aggregate is not None else None,
            "judge_errors": int(judge_errors or 0),
            "failing_case_ids": [
                f.get("case_id")
                for f in failures_payload
                if isinstance(f, dict) and f.get("case_id")
            ],
        }
    return list(by_agent.values())


def format_agent_grades(grades: list[dict[str, Any]]) -> str:
    """Render agent grade rows for /goals. Pure — the percentage is derived.

    The fraction and the percentage come from the same two numbers. They used
    to come from different columns: ``{passed}/{total}`` from the counts and
    ``({pct}%)`` from ``pass_rate``, which held the partial-credit aggregate.
    crm-hygiene printed as ``0/4 (18%)`` — zero cases passed, an 18% grade.
    """

    def _rate(g: dict[str, Any]) -> float:
        total = int(g.get("total_cases") or 0)
        return (int(g.get("passed") or 0) / total) if total else 0.0

    lines = ["<b>Agent Performance — job pass rate</b>", ""]
    for g in sorted(grades, key=_rate):
        total = int(g.get("total_cases") or 0)
        passed = int(g.get("passed") or 0)
        pct = int(round(_rate(g) * 100))
        agent_id = str(g.get("agent_id", "?"))
        lines.append(f"  {agent_id:<22s}{passed}/{total} ({pct}%)")

        aggregate = g.get("aggregate_score")
        if aggregate is not None:
            lines.append(f"  ↳ {float(aggregate) * 100:.0f}% partial credit")
        judge_errors = int(g.get("judge_errors") or 0)
        if judge_errors:
            plural = "s" if judge_errors != 1 else ""
            lines.append(f"  ↳ {judge_errors} judge error{plural} — graded as failures")
        failing = [str(c) for c in (g.get("failing_case_ids") or [])][:3]
        if failing:
            lines.append(f"  ↳ failing: {', '.join(failing)}")
    lines.append("")
    lines.append(
        "<i>Each line is the agent's grade on its docs/benchmarks/&lt;agent&gt;/suite.yaml: "
        "cases passed over cases in the suite. Partial credit is the weighted mean of "
        "per-case scores — it moves before the pass rate does, and is never the grade. "
        "Cost is observed, never optimized.</i>"
    )
    return "\n".join(lines)


class TelegramBot:
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
        """Register all message and callback handlers."""

        # ── Slash commands ──

        @self.dp.message(Command("help"))
        async def cmd_help(message: Message) -> None:
            await message.answer(
                "<b>Commands</b>\n\n"
                "/deep — Deep reasoning via RLM ($0.50-$2.00)\n"
                "/plan — Plan before executing (review + approve)\n"
                "/model — Switch AI model\n"
                "/goal — Show or update the active session goal\n"
                "/goals — Show each agent's benchmark grade\n"
                "/clear — Clear conversation history\n"
                "/context — Context window stats\n"
                "/reset — Reset model + history\n"
                "/stop — Cancel current response\n"
                "/agents — List active runs (steer/interrupt)\n"
                "/steer — Steer a live run: /steer [run_id] text\n"
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

        @self.dp.message(Command("goal"))
        async def cmd_goal(message: Message) -> None:
            await self._handle_goal_command(message)

        @self.dp.message(Command("clear"))
        async def cmd_clear(message: Message) -> None:
            chat_id = str(message.chat.id)
            session = get_shared_session(self._session_key(chat_id))
            session.history.clear()
            get_task_registry().spawn(
                clear_session_async(
                    self._session_key(chat_id),
                    tenant_id=self._get_tenant_id(chat_id),
                ),
                name=f"tg-clear-session:{chat_id}",
            )
            await message.answer("Conversation history cleared.")

        @self.dp.message(Command("reset"))
        async def cmd_reset(message: Message) -> None:
            chat_id = str(message.chat.id)
            self._model_override.pop(chat_id, None)
            session = get_shared_session(self._session_key(chat_id))
            session.history.clear()
            session.model_override = None
            get_task_registry().spawn(
                clear_session_async(
                    self._session_key(chat_id),
                    tenant_id=self._get_tenant_id(chat_id),
                ),
                name=f"tg-reset-session:{chat_id}",
            )
            primary = self._get_manifest_primary()
            name = MODEL_DISPLAY_NAMES.get(primary, primary)
            await message.answer(
                f"Session reset. Model reverted to {html.escape(name)} (manifest default)."
            )

        @self.dp.message(Command("stop"))
        async def cmd_stop(message: Message) -> None:
            chat_id = str(message.chat.id)
            # Clear any buffered (not-yet-started) messages
            self._message_buffers.pop(chat_id, None)
            self._drain_scheduled.pop(chat_id, None)
            task = self._active_tasks.get(chat_id)
            if task and not task.done():
                task.cancel()
                self._active_tasks.pop(chat_id, None)
                await message.answer("Stopped.")
            else:
                await message.answer("Nothing running.")

        @self.dp.message(Command("restart"))
        async def cmd_restart(message: Message) -> None:
            await self._handle_restart_command(message, "robothor-engine.service")

        @self.dp.message(Command("restart_delphi"))
        async def cmd_restart_delphi(message: Message) -> None:
            await self._handle_restart_command(message, "robothor-delphi-engine.service")

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
            """Fleet health snapshot — per-agent last status + 24h metrics."""
            chat_id = str(message.chat.id)
            try:
                # Live schedule state from /health
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"http://localhost:{self.config.port}/health", timeout=5
                    )
                    health_data = resp.json()
                agents = health_data.get("agents", {})

                # 24h rollup per agent from analytics
                fleet_health = {}
                try:
                    from robothor.engine.analytics import get_fleet_health

                    fh = await asyncio.to_thread(
                        get_fleet_health,
                        1,
                        self._get_tenant_id(chat_id),
                    )
                    for row in fh.get("per_agent", []):
                        fleet_health[row["agent_id"]] = row
                except Exception as e:
                    logger.debug("/status fleet_health lookup failed: %s", e)

                lines = [
                    f"<b>Engine Status</b> — {health_data.get('status', 'unknown')}",
                    "",
                ]
                merged = set(agents.keys()) | set(fleet_health.keys())
                for aid in sorted(merged):
                    info = agents.get(aid, {})
                    stats = fleet_health.get(aid, {})
                    status = info.get("last_status") or "—"
                    errors = info.get("consecutive_errors", 0)
                    marker = (
                        "\u2705"
                        if status == "completed"
                        else ("\u274c" if status == "failed" else "\u23f3")
                    )
                    line = f"{marker} <b>{html.escape(aid)}</b>: {html.escape(str(status))}"
                    total = stats.get("total_runs") or 0
                    completed = stats.get("completed") or 0
                    if total:
                        success_pct = round(100 * completed / total)
                        line += f" · 24h: {completed}/{total} ({success_pct}%)"
                    cost = stats.get("avg_cost_usd")
                    if cost:
                        line += f" · avg ${cost:.3f}"
                    if errors:
                        line += f" · {errors} consec err"
                    lines.append(line)
                await message.answer("\n".join(lines))
            except Exception as e:
                await message.answer(f"Failed to fetch status: {html.escape(str(e))}")

        @self.dp.message(Command("export"))
        async def cmd_export(message: Message) -> None:
            """Export current session as a markdown file attachment."""

            from aiogram.types import BufferedInputFile

            from robothor.engine.export import chat_session_to_markdown

            chat_id = str(message.chat.id)
            session_key = self._session_key(chat_id)
            session = get_shared_session(session_key)

            if not session.history:
                await message.reply("No messages in current session.")
                return

            md = chat_session_to_markdown(session, session_key=session_key)
            now_str = datetime.now(UTC).strftime("%Y%m%d-%H%M")
            filename = f"session-{now_str}.md"
            file = BufferedInputFile(md.encode(), filename=filename)
            await message.reply_document(
                file, caption=f"Session export ({len(session.history)} messages)"
            )

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

        @self.dp.message(Command("stats"))
        async def cmd_stats(message: Message) -> None:
            """Show fleet achievement snapshot — goal satisfaction by agent."""
            try:
                from robothor.engine.buddy import BuddyEngine, format_achievement

                engine = BuddyEngine()
                fleet = engine.compute_daily_stats()
                current_streak, longest_streak = engine.get_streak()

                lines = [
                    f"<b>Fleet achievement</b>: "
                    f"{format_achievement(fleet.fleet_achievement_score)}",
                    f"\U0001f4d0 Measured: {fleet.agents_measured}/{fleet.agents_total} agents",
                    f"\U0001f525 Streak: {current_streak} days (best: {longest_streak})",
                    f"\U0001f4ca Today: {fleet.tasks_completed} tasks completed",
                    "",
                    "<b>Agents</b> (sat/breached · score):",
                ]
                for s in fleet.per_agent[:15]:
                    lines.append(
                        f"  {s.agent_id:<22s} {s.satisfied_goals}/{s.breached_goals} · "
                        f"{format_achievement(s.achievement_score)}"
                    )
                if len(fleet.per_agent) > 15:
                    lines.append(f"  … and {len(fleet.per_agent) - 15} more")
                await message.answer("\n".join(lines))
            except Exception as e:
                await message.answer(f"Stats unavailable: {html.escape(str(e))}")

        @self.dp.message(Command("buddy"))
        async def cmd_buddy(message: Message) -> None:
            """Show fleet achievement + the three biggest breaches."""
            try:
                from robothor.engine.buddy import BuddyEngine, format_achievement

                engine = BuddyEngine()
                fleet = engine.compute_daily_stats()
                current_streak, longest_streak = engine.get_streak()

                # Biggest breaches = lowest scores among agents with >0 breached
                # goals. Only agents carrying a real score can be ranked \u2014 an
                # unmeasured agent is not "the worst agent", and sorting on a
                # None score raised TypeError.
                breached = [
                    s
                    for s in fleet.per_agent
                    if s.breached_goals > 0 and s.achievement_score is not None
                ]
                breached.sort(key=lambda s: s.achievement_score or 0)

                lines = [
                    "\u26a1 <b>Fleet achievement</b>: "
                    f"{format_achievement(fleet.fleet_achievement_score)}",
                    f"\U0001f4d0 Measured: {fleet.agents_measured}/{fleet.agents_total} agents",
                    f"\U0001f525 Streak: {current_streak} days (best: {longest_streak})",
                    f"\U0001f4ca Tasks today: {fleet.tasks_completed}",
                    "",
                    "<b>Biggest breaches</b>:",
                ]
                if not breached:
                    lines.append("  None — every agent is clear.")
                else:
                    for s in breached[:3]:
                        lines.append(
                            f"  {s.agent_id}: {s.breached_goals} breached · {s.achievement_score}/100"
                        )
                await message.answer("\n".join(lines))
            except Exception as e:
                await message.answer(f"Buddy unavailable: {html.escape(str(e))}")

        @self.dp.message(Command("goals"))
        async def cmd_goals(message: Message) -> None:
            """Show each agent's job grade — pass rate on its benchmark suite."""
            try:
                tenant_id = os.environ.get("ROBOTHOR_TENANT_ID", DEFAULT_TENANT)
                grades = fetch_agent_grades(tenant_id, self.config.workspace)
                if not grades:
                    await message.answer(
                        "<b>Agent Performance</b>\n\nBenchmark cron hasn't run yet — "
                        "check back after 4 AM ET tomorrow, or trigger benchmark-runner manually."
                    )
                    return
                await message.answer(format_agent_grades(grades))
            except Exception as e:
                await message.answer(f"Goals unavailable: {html.escape(str(e))}")

        @self.dp.message(Command("agents"))
        async def cmd_agents(message: Message) -> None:
            await self._handle_agents_command(message)

        @self.dp.message(Command("steer"))
        async def cmd_steer(message: Message) -> None:
            await self._handle_steer_command(message)

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
                        await msg.edit_reply_markup(reply_markup=None)
                except Exception:
                    pass
                # Fire-and-forget — execute in background so Telegram handler is freed
                task = asyncio.create_task(
                    self._execute_approved_plan(chat_id, session_key, session)
                )
                self._active_tasks[chat_id] = task
            elif action == "revise":
                await callback.answer("Send your feedback and I'll revise the plan.")
                await self.send_message(chat_id, "Send your feedback and I'll revise the plan.")
            elif action == "reject":
                session.active_plan.status = "rejected"
                # Persist cleared state
                get_task_registry().spawn(
                    clear_plan_state_async(session_key, tenant_id=self._get_tenant_id(chat_id)),
                    name=f"tg-reject-plan:{chat_id}",
                )
                session.active_plan = None
                # Remove inline keyboard
                try:
                    msg = callback.message
                    if msg and hasattr(msg, "edit_reply_markup"):
                        await msg.edit_reply_markup(reply_markup=None)
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
            get_task_registry().spawn(
                update_model_override_async(
                    self._session_key(chat_id),
                    model_id,
                    tenant_id=self._get_tenant_id(chat_id),
                ),
                name=f"tg-model-override:{chat_id}",
            )
            display = MODEL_DISPLAY_NAMES[model_id]

            # Update the keyboard to reflect selection
            kb = self._build_model_keyboard(model_id)
            try:
                msg = callback.message
                if msg and hasattr(msg, "edit_text"):
                    await msg.edit_text(
                        f"<b>Model switched to:</b> {html.escape(display)}",
                        reply_markup=kb,
                    )
            except Exception:
                pass
            await callback.answer(f"Switched to {display}")

        # ── Permission approval callbacks ──

        @self.dp.callback_query(F.data.startswith("perm:"))
        async def on_permission_decision(callback: CallbackQuery) -> None:
            from robothor.engine.permission_escalation import get_permission_manager

            # Security: only the authorized chat/owner can approve/deny escalations
            msg = callback.message
            if not msg or not hasattr(msg, "chat"):
                await callback.answer("Unauthorized", show_alert=True)
                return
            sender_id = callback.from_user.id if callback.from_user else "unknown"
            if not self._check_owner_gate(
                chat_id=str(msg.chat.id), sender_id=str(sender_id), site="permission_callback"
            ):
                logger.warning(
                    "Unauthorized permission callback from chat_id=%s user_id=%s",
                    msg.chat.id,
                    sender_id,
                )
                await callback.answer("Unauthorized", show_alert=True)
                return

            if not callback.data:
                await callback.answer("Invalid callback data")
                return
            parts = callback.data.split(":", 2)
            if len(parts) < 3:
                await callback.answer("Invalid callback data")
                return

            action = parts[1]  # "approve", "all", or "deny"
            request_id = parts[2]

            mgr = get_permission_manager()
            if not mgr:
                await callback.answer("Permission system not active")
                return

            if action == "approve":
                mgr.resolve(request_id, approved=True)
                await callback.answer("Approved")
            elif action == "all":
                mgr.resolve(request_id, approved=True, remember_session=True)
                await callback.answer("Approved for session")
            elif action == "deny":
                mgr.resolve(request_id, approved=False)
                await callback.answer("Denied")
            else:
                await callback.answer("Unknown action")
                return

            # Remove inline keyboard after decision
            with contextlib.suppress(Exception):
                if msg and hasattr(msg, "edit_reply_markup"):
                    await msg.edit_reply_markup(reply_markup=None)

        # ── Run control callbacks (Steer / Interrupt buttons from /agents) ──

        @self.dp.callback_query(F.data.startswith("runctl:"))
        async def on_runctl_callback(callback: CallbackQuery) -> None:
            await self._handle_runctl_callback(callback)

        # ── Delphi proposal approval callbacks ──
        # callback_data shape: ``dp:a:<32-hex>`` (approve) or ``dp:r:<32-hex>``
        # (reject). The Delphi engine runs a separate daemon (port 18801) but
        # uses the same engine codebase, so this handler ships in the shared
        # telegram.py and only fires when a Delphi proposal is in flight.
        @self.dp.callback_query(F.data.startswith("dp:"))
        async def on_delphi_proposal_decision(callback: CallbackQuery) -> None:
            import asyncio as _asyncio
            import hashlib as _hashlib
            import hmac as _hmac
            import os as _os
            import uuid as _uuid

            msg = callback.message
            if not msg or not hasattr(msg, "chat"):
                await callback.answer("Unauthorized", show_alert=True)
                return
            sender_id = callback.from_user.id if callback.from_user else "unknown"
            if not self._check_owner_gate(
                chat_id=str(msg.chat.id), sender_id=str(sender_id), site="delphi_proposal_callback"
            ):
                logger.warning(
                    "Unauthorized delphi-proposal callback from chat_id=%s user_id=%s",
                    msg.chat.id,
                    sender_id,
                )
                await callback.answer("Unauthorized", show_alert=True)
                return

            if not callback.data:
                await callback.answer("Invalid callback data")
                return
            parts = callback.data.split(":", 2)
            if len(parts) != 3 or parts[0] != "dp":
                await callback.answer("Invalid callback data")
                return
            action_short = parts[1]
            short_hex = parts[2]
            if action_short == "a":
                action = "approve"
            elif action_short == "r":
                action = "reject"
            else:
                await callback.answer("Invalid action")
                return
            try:
                proposal_uuid = str(_uuid.UUID(short_hex))
            except (TypeError, ValueError):
                await callback.answer("Invalid proposal id")
                return

            # Compute the HMAC token here, in the engine process — the secret
            # is not exposed to agents or callback_data.
            secret = _os.environ.get("DELPHI_PROPOSAL_HMAC_SECRET", "")
            if not secret:
                logger.error(
                    "DELPHI_PROPOSAL_HMAC_SECRET unset — cannot dispatch %s",
                    proposal_uuid,
                )
                await callback.answer(
                    "Engine misconfigured (no HMAC secret)",
                    show_alert=True,
                )
                return
            token = _hmac.new(
                secret.encode("utf-8"),
                f"{proposal_uuid}:{action}".encode(),
                _hashlib.sha256,
            ).hexdigest()

            # Run the apply script as a subprocess. Async-safe: we use
            # asyncio.create_subprocess_exec so we don't block the event loop.
            # The script lives in scripts/, ROBOTHOR_TENANT_ID is forced to
            # 'delphi' inside the script's preamble.
            workspace_root = os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))
            script_path = f"{workspace_root}/scripts/delphi_apply_proposal.py"
            python_path = f"{workspace_root}/venv/bin/python"
            try:
                proc = await _asyncio.create_subprocess_exec(
                    python_path,
                    script_path,
                    "--proposal-id",
                    proposal_uuid,
                    "--action",
                    action,
                    "--token",
                    token,
                    stdout=_asyncio.subprocess.PIPE,
                    stderr=_asyncio.subprocess.PIPE,
                    env={**_os.environ, "DELPHI_PROPOSAL_HMAC_SECRET": secret},
                )
                stdout, stderr = await _asyncio.wait_for(proc.communicate(), timeout=15)
                rc = proc.returncode
            except TimeoutError:
                logger.error("delphi_apply_proposal.py timed out for %s", proposal_uuid)
                await callback.answer("Apply timed out", show_alert=True)
                return
            except Exception:
                logger.exception("Failed to invoke delphi_apply_proposal.py")
                await callback.answer("Apply failed (see logs)", show_alert=True)
                return

            if rc == 0:
                verb = "Applied" if action == "approve" else "Rejected"
                await callback.answer(verb)
                with contextlib.suppress(Exception):
                    if msg and hasattr(msg, "edit_reply_markup"):
                        await msg.edit_reply_markup(reply_markup=None)
                with contextlib.suppress(Exception):
                    # Append a small footer to the message so the operator
                    # has a record without scrolling away.
                    if msg and hasattr(msg, "edit_text") and getattr(msg, "text", None):
                        new_text = (getattr(msg, "text", "") or "") + f"\n\n_{verb} ✓_"
                        await msg.edit_text(new_text, parse_mode="Markdown")
            else:
                err = (stderr or b"").decode("utf-8", errors="replace")[:200]
                logger.warning(
                    "delphi_apply_proposal rc=%s stderr=%s",
                    rc,
                    err.strip(),
                )
                # Common rc codes (see delphi_apply_proposal.py): 3=bad token,
                # 4=not found, 5=not pending, 6=expired, 7=dispatch error.
                msg_map = {
                    3: "HMAC mismatch (engine bug?)",
                    4: "Proposal not found",
                    5: "Already decided",
                    6: "Expired",
                    7: "Apply failed",
                }
                await callback.answer(
                    msg_map.get(rc or -1, f"Apply failed (rc={rc})"), show_alert=True
                )

        # ── Voice notes / video notes ──
        # Previously unhandled, so they were silently dropped. Now acknowledged.
        # Transcription is gated on ROBOTHOR_VOICE_NOTES_ENABLED (no STT provider
        # is wired yet — Claude has no audio endpoint — so this is a placeholder).

        @self.dp.message(F.voice | F.video_note)
        async def handle_voice(message: Message) -> None:
            """Acknowledge voice/video notes (transcription pending an STT provider)."""
            if not message.from_user:
                return
            enabled = os.environ.get("ROBOTHOR_VOICE_NOTES_ENABLED", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if not enabled:
                await message.answer(
                    "🎤 I can't process voice notes yet — please send text. "
                    "(Voice transcription will arrive once an STT provider is configured.)"
                )
                return
            media = message.voice or message.video_note
            try:
                if media is not None:
                    await self.bot.get_file(media.file_id)  # verify reachability
                await message.answer(
                    "🎤 Voice received — transcription isn't wired yet (placeholder). "
                    "Send text for now."
                )
            except Exception as e:
                await message.answer(f"Couldn't fetch the voice note: {e}")

        # ── File/document/photo messages ──

        @self.dp.message(F.document | F.photo)
        async def handle_file(message: Message) -> None:
            """Handle file/document/photo attachments — extract content and process."""
            if not message.from_user:
                return

            chat_id = str(message.chat.id)

            # ── Resolve user identity ──
            user_info = self._resolve_user(chat_id, message)
            if user_info is None:
                reply = await self._handle_unregistered_sender(message, str(message.from_user.id))
                await message.answer(reply)
                return

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
                file_name = f"photo_{photo.file_unique_id}.jpg"
                try:
                    file = await self.bot.get_file(photo.file_id)
                    if file.file_path:
                        from io import BytesIO

                        buf = BytesIO()
                        await self.bot.download_file(file.file_path, buf)
                        raw_bytes = buf.getvalue()
                        # Use VLM to analyze the image
                        vlm_prompt = (
                            caption
                            or "Describe what you see in this image in detail. Note any text, people, objects, URLs, or notable details."
                        )
                        vision_desc = await _analyze_photo_bytes(raw_bytes, vlm_prompt)
                        file_content = f"[Image: {photo.width}x{photo.height}px]\n\nVision analysis:\n{vision_desc}"
                        # Caption already consumed as prompt — clear it to avoid duplication
                        if caption:
                            caption = ""
                    else:
                        file_content = (
                            f"[Image: {photo.width}x{photo.height}px — could not download]"
                        )
                except Exception as e:
                    logger.warning("Failed to process photo: %s", e)
                    file_content = f"[Failed to process photo: {e}]"

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
                    await self._run_plan_mode(
                        chat_id, session_key, session, user_text, message, sender_info=user_info
                    )
                    return
                session.active_plan.status = "expired"
                session.active_plan = None

            if session.plan_mode:
                session.plan_mode = False
                await self._run_plan_mode(
                    chat_id, session_key, session, user_text, message, sender_info=user_info
                )
                return

            # Execute via coalescing buffer (shared with handle_text)
            await self._enqueue_message(
                chat_id, session_key, session, user_text, sender_info=user_info
            )

        # ── Interactive text messages ──

        @self.dp.message(F.text)
        async def handle_text(message: Message) -> None:
            """Handle incoming text messages — streaming response."""
            if not message.text or not message.from_user:
                return

            chat_id = str(message.chat.id)
            user_text = message.text.strip()

            # ── Skill bundles: "/bundle-name" composes a multi-skill prompt ──
            if user_text.startswith("/"):
                from robothor.engine.skill_bundles import resolve_slash_command

                _kind, _bundle = resolve_slash_command(user_text.split()[0])
                if _kind == "bundle" and _bundle is not None:
                    _parts = user_text.split(maxsplit=1)
                    _extra = _parts[1] if len(_parts) > 1 else ""
                    user_text = (
                        f"{_bundle.instruction}\n\n"
                        f"Run these skills in order: {', '.join(_bundle.skills)}."
                        + (f"\n\nAdditional context: {_extra}" if _extra else "")
                    )

            logger.info(
                "Telegram message from %s (chat %s): %s",
                message.from_user.first_name,
                chat_id,
                user_text[:100],
            )

            # ── Resolve user identity ──
            from robothor.engine.onboarding import is_onboarding, process_onboarding

            telegram_user_id = str(message.from_user.id)

            # Handle in-progress onboarding first (only reachable when
            # ROBOTHOR_OPEN_ONBOARDING started a session for this sender —
            # the closed-onboarding refusal path below never calls
            # start_onboarding, so this dict stays empty by default).
            if is_onboarding(telegram_user_id):
                reply = process_onboarding(telegram_user_id, user_text)
                if reply:
                    from robothor.engine.users import clear_cache

                    clear_cache()
                    await message.answer(reply)
                return

            user_info = self._resolve_user(chat_id, message)
            if user_info is None:
                # Unregistered private chat user — closed-onboarding refusal
                # (or legacy self-service onboarding under the escape flag).
                reply = await self._handle_unregistered_sender(message, telegram_user_id)
                await message.answer(reply)
                return

            session_key = self._session_key(chat_id)
            session = get_shared_session(session_key)

            # ── Channel-bus reply resolution ──
            # If this message is a Telegram "Reply to" quote, look up the
            # original in channel_message_map. When it was a fleet surface
            # (e.g. a DevOps report), prepend a compact quote so main sees
            # the thread context inline — and remember the linkage so the
            # user turn's JSONB records the reference.
            reply_ctx: dict[str, Any] | None = None
            if message.reply_to_message and message.reply_to_message.message_id:
                from robothor.engine.channel_bus import (
                    format_reply_prefix,
                    resolve_reply_context_async,
                )

                tenant_id = self._get_tenant_id(chat_id)
                reply_ctx = await resolve_reply_context_async(
                    chat_id=chat_id,
                    platform_message_id=str(message.reply_to_message.message_id),
                    tenant_id=tenant_id,
                )
                if reply_ctx:
                    prefix = format_reply_prefix(reply_ctx)
                    user_text = f"{prefix}\n\n{user_text}"
                    self._reply_context_buffers[chat_id] = reply_ctx

            # Remember this Telegram message id so the drain can record an
            # inbound map row after persistence.
            if message.message_id:
                self._user_message_id_buffers[chat_id] = str(message.message_id)

            # ── Check for pending plan — ANY text = feedback for revision ──
            # Approval/rejection only via inline keyboard buttons.
            if session.active_plan and session.active_plan.status == "pending":
                if not _plan_is_expired(session.active_plan):
                    await self._iterate_plan(
                        chat_id, session_key, session, user_text, sender_info=user_info
                    )
                    return
                session.active_plan.status = "expired"
                session.active_plan = None

            # ── Check plan_mode toggle — route through plan pipeline ──
            if session.plan_mode:
                session.plan_mode = False  # One-shot: auto-disable after use
                await self._run_plan_mode(
                    chat_id, session_key, session, user_text, message, sender_info=user_info
                )
                return

            # Execute via coalescing buffer (shared with handle_file)
            await self._enqueue_message(
                chat_id, session_key, session, user_text, sender_info=user_info
            )

        @self.dp.message_reaction()
        async def on_message_reaction(event: Any) -> None:
            """Record operator 👍/👎/😡 reactions as goal-judge signals (Phase 2).

            A reaction is a real operator verdict that anchors (clamps) the
            judge's inferred satisfaction. Fails soft — a telemetry write must
            never disturb the bot.
            """
            try:
                from robothor.engine.operator_signals import (
                    clear_reaction,
                    record_reaction,
                    resolve_reacted_message,
                )

                chat_id = str(event.chat.id)
                message_id = int(event.message_id)
                tenant_id = self._get_tenant_id(chat_id)
                user = getattr(event, "user", None)
                reactor = (
                    (getattr(user, "username", None) or str(getattr(user, "id", "")))
                    if user
                    else None
                )
                added = [getattr(rt, "emoji", None) for rt in (event.new_reaction or [])]
                emojis = [e for e in added if e]
                if not emojis:
                    # Reaction retracted — clear the prior verdict so it stops
                    # counting (BUG-4). Only when an old reaction existed.
                    if event.old_reaction:
                        clear_reaction(
                            chat_id=chat_id,
                            message_id=message_id,
                            reactor=reactor,
                            tenant_id=tenant_id,
                        )
                    return
                agent_id, run_id = resolve_reacted_message(message_id, chat_id, tenant_id)
                for emoji in emojis:
                    record_reaction(
                        chat_id=chat_id,
                        message_id=message_id,
                        emoji=emoji,
                        reactor=reactor,
                        agent_id=agent_id,
                        run_id=run_id,
                        tenant_id=tenant_id,
                    )
            except Exception as exc:
                logger.debug("on_message_reaction failed: %s", exc)

    # ── Message coalescing ──────────────────────────────────────────
    # Telegram splits long messages into ~4096-char chunks, each arriving
    # as a separate update.  We buffer them and drain once per batch so
    # the agent sees one combined message instead of N orphaned runs.

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
                        checklist_text = _format_checklist_html(todos)
                        if checklist_msg_id:
                            await self._retry_on_flood(
                                self.bot.edit_message_text(
                                    chat_id=int(chat_id),
                                    message_id=checklist_msg_id,
                                    text=checklist_text,
                                    parse_mode="HTML",
                                )
                            )
                        else:
                            msg = await self._retry_on_flood(
                                self.bot.send_message(
                                    chat_id=int(chat_id),
                                    text=checklist_text,
                                    parse_mode="HTML",
                                )
                            )
                            if msg:
                                checklist_msg_id = msg.message_id
                    elif checklist_msg_id:
                        with contextlib.suppress(Exception):
                            await self.bot.delete_message(
                                chat_id=int(chat_id),
                                message_id=checklist_msg_id,
                            )
                        checklist_msg_id = None
                except Exception:
                    logger.debug("Checklist update failed", exc_info=True)

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

    async def _run_plan_mode(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        user_text: str,
        message: Message,
        deep_plan: bool = False,
        sender_info: dict[str, Any] | None = None,
    ) -> None:
        """Execute agent in plan mode with read-only tools, display plan with approval keyboard.

        ``sender_info`` (Task 4 Finding 1 fix) is the per-message resolved
        sender identity, threaded exactly like ``_run_interactive``'s
        ``sender_info`` parameter. When the caller (``handle_text``/
        ``handle_file``) already resolved it, it's passed straight through.
        When omitted (``cmd_plan``/``cmd_deep`` invoke this directly with no
        prior resolution), this resolves fresh from ``message`` here — still
        a per-message resolution, keyed to THIS message's actual sender via
        ``message.from_user.id`` — never the shared, chat_id-keyed
        ``_chat_user_info`` cache, which reflects whichever sender happened
        to message last and can be stale or, in a group chat with concurrent
        senders, outright wrong for the message actually driving this run.
        The resolved identity is frozen onto the created ``PlanState`` as
        ``creator_sender_info`` so a later approval/execution reads the
        plan's author, not whoever is cached (or merely clicked Approve) at
        that later point.
        """
        import uuid
        from datetime import datetime

        user = (
            sender_info if sender_info is not None else (self._resolve_user(chat_id, message) or {})
        )
        tenant_id = user.get("tenant_id") or self.config.tenant_id
        _identity = self._build_identity(user, chat_id, tenant_id)

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

        run_for_delivery: Any = None
        try:
            model = self._model_override.get(chat_id)
            history = list(session.history)

            run = await self.runner.execute(
                agent_id=self.config.default_chat_agent,
                message=user_text,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail=f"plan:{chat_id}",
                model_override=model,
                conversation_history=history or None,
                readonly_mode=True,
                deep_plan=deep_plan,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

            plan_text = _extract_plan_text(run.output_text or "")

            # Accumulate history so revisions have full context
            session.history.append({"role": "user", "content": user_text})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Delete thinking message, deliver as new message
            if stream_msg_id is not None:
                with contextlib.suppress(Exception):
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=stream_msg_id)

            if plan_text:
                plan = PlanState(
                    plan_id=str(uuid.uuid4()),
                    plan_text=plan_text,
                    original_message=user_text,
                    status="pending",
                    created_at=datetime.now(UTC).isoformat(),
                    exploration_run_id=run.id,
                    deep_plan=deep_plan,
                    creator_sender_info=user or None,
                )
                session.active_plan = plan

                # Persist plan state to DB
                get_task_registry().spawn(
                    save_plan_state_async(
                        session_key,
                        _plan_state_to_dict(plan),
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-plan:{chat_id}",
                )

                sent_plan = await self.send_message(chat_id, plan_text)
                await self._record_interactive_delivery(run, sent_plan)

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
                sent_fallback = await self.send_message(
                    chat_id, run.output_text or "No plan produced."
                )
                await self._record_interactive_delivery(run, sent_fallback)
        except Exception as e:
            logger.error("Plan mode failed: %s", e, exc_info=True)
            sent_error = await self.send_message(chat_id, f"Plan mode error: {html.escape(str(e))}")
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
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
        sender_info: dict[str, Any] | None = None,
    ) -> None:
        """Execute deep reasoning via RLM, show progress edits and result.

        ``sender_info`` (Task 4 Finding 1 fix): same per-message-resolved
        threading pattern as ``_run_plan_mode`` — captured up front (before
        any await) and never re-read from the shared, chat_id-keyed
        ``_chat_user_info`` cache.
        """
        user = (
            sender_info if sender_info is not None else (self._resolve_user(chat_id, message) or {})
        )
        tenant_id = user.get("tenant_id") or self.config.tenant_id
        _identity = self._build_identity(user, chat_id, tenant_id)

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

        async def on_progress(progress: dict[str, Any]) -> None:
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

        run_for_delivery: Any = None
        try:
            history = list(session.history)

            run = await self.runner.execute_deep(
                query=query,
                on_progress=on_progress,
                conversation_history=history or None,
                trigger_type=TriggerType.TELEGRAM,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

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
                get_task_registry().spawn(
                    save_exchange_async(
                        session_key,
                        f"/deep {query}",
                        run.output_text,
                        channel="telegram",
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-deep:{chat_id}",
                )

            # Delete progress message, deliver result as new message
            if progress_msg_id is not None:
                with contextlib.suppress(Exception):
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=progress_msg_id)

            if run.output_text:
                duration_s = (run.duration_ms or 0) / 1000
                cost_str = f"${run.total_cost_usd:.2f}" if run.total_cost_usd else "$?.??"
                footer = f"\n\n<i>RLM: {duration_s:.1f}s / {cost_str}</i>"
                sent_result = await self.send_message(chat_id, run.output_text)
                await self._record_interactive_delivery(run, sent_result)
                await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=footer,
                )
            elif run.error_message:
                sent_failure = await self.send_message(
                    chat_id,
                    f"\u274c Deep reasoning failed: {html.escape(run.error_message)}",
                )
                await self._record_interactive_delivery(run, sent_failure)
        except Exception as e:
            logger.error("Deep mode failed: %s", e, exc_info=True)
            sent_error = await self.send_message(
                chat_id, f"Deep reasoning error: {html.escape(str(e))}"
            )
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
        finally:
            typing_active = False
            typing_task.cancel()

    async def _execute_approved_plan(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
    ) -> None:
        """Execute an approved plan in background mode.

        Runs as a fire-and-forget asyncio task (caller wraps in create_task
        -- the ``asyncio.create_task`` scheduling gap between the approval
        click and this coroutine's body actually running is exactly the
        window in which a group chat's shared ``_chat_user_info[chat_id]``
        cache can be overwritten by an unrelated sender's message (Task 4
        Finding 1). Attribution here instead comes from
        ``plan.creator_sender_info`` -- the identity frozen at plan-creation
        time in ``_run_plan_mode`` -- not whoever is cached for the chat, and
        not whoever happened to click "Approve" (any group member can click
        approve; the plan's author owns what it does end to end).

        Sends immediate acknowledgement, executes with continuous-mode overrides
        for long-running tasks, and delivers the final result as a new Telegram
        message (triggering a push notification).
        """
        plan = session.active_plan
        if not plan:
            await self.send_message(chat_id, "No pending plan to execute.")
            return

        # Check expiration before executing
        if _plan_is_expired(plan):
            plan.status = "expired"
            session.active_plan = None
            await self.send_message(
                chat_id, "Plan has expired. Please start a new plan with /plan."
            )
            return

        plan.status = "approved"

        user = plan.creator_sender_info or {}
        tenant_id = user.get("tenant_id") or self.config.tenant_id
        _identity = self._build_identity(user, chat_id, tenant_id)

        # Deep plan: route to RLM with rich context instead of agent execution
        if plan.deep_plan:
            await self._execute_deep_plan(chat_id, session_key, session)
            return

        # ── Immediate acknowledgement ──
        await self.send_message(
            chat_id,
            "\u2705 On it \u2014 executing in the background. "
            "I'll send progress updates and notify you when done. "
            "Send /stop to cancel.",
        )

        run_for_delivery: Any = None
        try:
            model = self._model_override.get(chat_id)

            # Build agent config with continuous-mode overrides for background execution
            bg_config = self._build_background_config()

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
                agent_config=bg_config,
                trigger_type=TriggerType.TELEGRAM,
                trigger_detail=f"plan-exec:{chat_id}",
                model_override=model,
                conversation_history=None,  # CLEAN CONTEXT
                execution_mode=True,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

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
                get_task_registry().spawn(
                    save_exchange_async(
                        session_key,
                        plan.original_message,
                        run.output_text,
                        channel="telegram",
                        model_override=model,
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-plan-exec:{chat_id}",
                )

            # Clear plan + persist
            session.active_plan = None
            get_task_registry().spawn(
                clear_plan_state_async(session_key, tenant_id=tenant_id),
                name=f"tg-clear-plan-exec:{chat_id}",
            )

            # Send final result as NEW message (not edit) so user gets push notification
            duration_s = (run.duration_ms or 0) / 1000
            cost_str = f"${run.total_cost_usd:.4f}" if run.total_cost_usd else "$0"
            footer = f"\n\n\u2014 {duration_s:.0f}s / {cost_str}"

            if run.output_text:
                sent_result = await self.send_message(chat_id, run.output_text + footer)
            elif run.error_message:
                sent_result = await self.send_message(
                    chat_id, f"Plan failed: {run.error_message}{footer}"
                )
            else:
                sent_result = await self.send_message(chat_id, f"Plan complete. No output.{footer}")
            await self._record_interactive_delivery(run, sent_result)
        except asyncio.CancelledError:
            logger.info("Background plan execution cancelled for chat %s", chat_id)
            sent_cancel = await self.send_message(chat_id, "Plan execution cancelled.")
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_cancel)
        except Exception as e:
            logger.error("Background plan execution failed: %s", e, exc_info=True)
            sent_error = await self.send_message(chat_id, f"Execution error: {html.escape(str(e))}")
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
        finally:
            self._active_tasks.pop(chat_id, None)

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

    async def _execute_deep_plan(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
    ) -> None:
        """Execute approved deep plan — route to RLM with rich context.

        Attribution comes from ``plan.creator_sender_info`` (Task 4 Finding
        1), same reasoning as ``_execute_approved_plan``: this runs off a
        create_task fire-and-forget schedule and must not read whichever
        sender happens to be cached for the chat by the time it executes.
        """
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

        async def on_progress(progress: dict[str, Any]) -> None:
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

        run_for_delivery: Any = None
        try:
            # Build rich context from plan + exploration output
            user = plan.creator_sender_info or {}
            tenant_id = user.get("tenant_id") or self.config.tenant_id
            _identity = self._build_identity(user, chat_id, tenant_id)
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
                trigger_type=TriggerType.TELEGRAM,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

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
                get_task_registry().spawn(
                    save_exchange_async(
                        session_key,
                        plan.original_message,
                        run.output_text,
                        channel="telegram",
                        model_override=self._model_override.get(chat_id),
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-deep-exec:{chat_id}",
                )

            # Clear plan + persist
            session.active_plan = None
            get_task_registry().spawn(
                clear_plan_state_async(session_key, tenant_id=tenant_id),
                name=f"tg-clear-deep-exec:{chat_id}",
            )

            # Delete progress message, deliver result as new message
            if progress_msg_id is not None:
                with contextlib.suppress(Exception):
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=progress_msg_id)

            if run.output_text:
                duration_s = (run.duration_ms or 0) / 1000
                cost_str = f"${run.total_cost_usd:.2f}" if run.total_cost_usd else "$?.??"
                footer = f"\n\n<i>RLM: {duration_s:.1f}s / {cost_str}</i>"
                sent_result = await self.send_message(chat_id, run.output_text)
                await self._record_interactive_delivery(run, sent_result)
                await self.bot.send_message(chat_id=int(chat_id), text=footer)
            elif run.error_message:
                sent_failure = await self.send_message(
                    chat_id,
                    f"\u274c Deep reasoning failed: {html.escape(run.error_message)}",
                )
                await self._record_interactive_delivery(run, sent_failure)
        except Exception as e:
            logger.error("Deep plan execution failed: %s", e, exc_info=True)
            sent_error = await self.send_message(
                chat_id, f"Deep reasoning error: {html.escape(str(e))}"
            )
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
        finally:
            typing_active = False
            typing_task.cancel()

    async def _iterate_plan(
        self,
        chat_id: str,
        session_key: str,
        session: Any,
        feedback: str,
        sender_info: dict[str, Any] | None = None,
    ) -> None:
        """Revise the active plan based on user feedback (keeps same plan_id).

        ``sender_info`` (Task 4 Finding 1 fix): the per-message resolved
        identity of whoever sent this feedback text — threaded straight
        through from ``handle_text``, exactly like ``_run_interactive``'s
        ``sender_info``. Unlike the approval paths, revision feedback is
        attributed to whoever is actively typing right now (same as plan
        creation itself), not frozen to the plan's original creator — this
        is still a read-only exploration step, not the privileged execution
        phase. Falls back to the legacy ``_chat_user_info[chat_id]`` cache
        read only when the caller doesn't thread anything through (keeps any
        direct caller byte-identical).
        """
        from datetime import datetime

        user = sender_info if sender_info is not None else (self._chat_user_info.get(chat_id) or {})
        tenant_id = user.get("tenant_id") or self._get_tenant_id(chat_id)
        _identity = self._build_identity(user, chat_id, tenant_id)

        plan = session.active_plan
        if not plan:
            await self.send_message(chat_id, "No pending plan to revise.")
            return

        # Check expiration before iterating
        if _plan_is_expired(plan):
            plan.status = "expired"
            session.active_plan = None
            await self.send_message(
                chat_id, "Plan has expired. Please start a new plan with /plan."
            )
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

        run_for_delivery: Any = None
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
                model_override=model,
                conversation_history=history or None,
                readonly_mode=True,
                tenant_id=tenant_id,
                user_id=str(user.get("user_id") or f"telegram:{chat_id}"),
                user_role=str(user.get("role") or "user"),
                identity=_identity,
            )
            run_for_delivery = run

            revised_plan_text = _extract_plan_text(run.output_text or "")

            # Update history
            session.history.append({"role": "user", "content": feedback})
            if run.output_text:
                session.history.append({"role": "assistant", "content": run.output_text})
            if len(session.history) > self._max_history:
                session.history[:] = session.history[-self._max_history :]

            # Delete thinking message, deliver as new message
            if stream_msg_id is not None:
                with contextlib.suppress(Exception):
                    await self.bot.delete_message(chat_id=int(chat_id), message_id=stream_msg_id)

            if revised_plan_text:
                # Update plan in-place (same plan_id)
                plan.plan_text = revised_plan_text

                revision_label = f"<b>Plan v{plan.revision_count + 1}</b>"
                sent_revision = await self.send_message(chat_id, revised_plan_text)
                await self._record_interactive_delivery(run, sent_revision)

                kb = self._build_plan_keyboard(plan.plan_id, plan.revision_count)
                await self.bot.send_message(
                    chat_id=int(chat_id),
                    text=f"{revision_label} — Approve this plan?",
                    reply_markup=kb,
                )

                # Persist updated plan state
                get_task_registry().spawn(
                    save_plan_state_async(
                        session_key,
                        _plan_state_to_dict(plan),
                        tenant_id=tenant_id,
                    ),
                    name=f"tg-save-plan-revision:{chat_id}",
                )
            else:
                sent_fallback = await self.send_message(
                    chat_id, run.output_text or "No revised plan produced."
                )
                await self._record_interactive_delivery(run, sent_fallback)
        except Exception as e:
            logger.error("Plan iteration failed: %s", e, exc_info=True)
            sent_error = await self.send_message(chat_id, f"Revision error: {html.escape(str(e))}")
            if getattr(run_for_delivery, "delivery_status", None) is None:
                await self._record_interactive_delivery(run_for_delivery, sent_error)
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
