"""Telegram command / callback / message handlers for TelegramBot.

Extracted from telegram.py 2026-08-24 (phase 3b of the god-object
decomposition). These 27 handlers previously lived as CLOSURES inside a
single 1,015-line _setup_handlers function — unreachable by tests, invisible
to coverage, unpatchable by name. They are ordinary methods now; the
registration table in TelegramBot._setup_handlers is all that remains there.

CONTRACT: handlers may use the composed TelegramBot surface — they ARE
TelegramBot methods at runtime. This mixin exists to give the delivery
layer's largest block a file of its own, not to narrow its surface; the
narrow-contract treatment (typed stubs like PlanModeMixin's) is the follow-up
once the handler bodies stop reaching into bot internals ad hoc.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message

from robothor.constants import DEFAULT_TENANT
from robothor.engine import attachments
from robothor.engine.chat import (
    _plan_is_expired,
    get_shared_session,
)
from robothor.engine.chat_store import (
    clear_plan_state_async,
    clear_session_async,
    update_model_override_async,
)
from robothor.engine.task_registry import get_task_registry

logger = logging.getLogger(__name__)

#: What a `perm:` tap that settled nothing is told, in the alert and appended to
#: the prompt itself. One sentence for every reason a request can be gone
#: (swept, timed out, already decided, lost to a restart): the operator's next
#: action is the same in all of them, and enumerating them tells a forger which
#: ids are live.
STALE_PROMPT = "This request is no longer pending."

#: `perm:` action → (approved, remember_session, confirmation). A table rather
#: than a branch chain so "approve" and "deny" cannot drift apart.
_PERM_DECISIONS: dict[str, tuple[bool, bool, str]] = {
    "approve": (True, False, "Approved"),
    "all": (True, True, "Approved for session"),
    "deny": (False, False, "Denied"),
}


# File handling. The ceiling is Telegram's own (20 MB for `getFile`), not one
# of ours: the 5 MB that used to live here refused files the Bot API would
# happily have handed over, and the operator had no way to tell the difference
# between "too big for Telegram" and "too big for us".
MAX_FILE_SIZE = attachments.MAX_DOWNLOAD_BYTES

#: How long an album's members are collected before the turn is composed.
#: Telegram sends each photo of an album as its own update, typically inside a
#: few hundred milliseconds; 1.5 s covers a slow batch without making the
#: operator wait on an ordinary single file (which never enters this path).
ALBUM_WINDOW_SECONDS = 1.5


@dataclass(frozen=True)
class MediaRef:
    """What one inbound attachment is, before anything has been downloaded.

    Built from the aiogram message so the intake has ONE shape to work with
    rather than a branch per Telegram field. ``size`` is what Telegram claims,
    which is what the ceiling is checked against — checking after the download
    would mean downloading the file we are about to refuse.
    """

    kind: str
    file_id: str
    file_unique_id: str
    name: str
    mime: str = ""
    size: int = 0
    width: int | None = None
    height: int | None = None


def _ref(
    media: Any,
    *,
    kind: str,
    fallback_name: str,
    mime: str = "",
) -> MediaRef | None:
    """One :class:`MediaRef` from an aiogram media object, or None if unusable."""
    file_id = str(getattr(media, "file_id", "") or "")
    unique = str(getattr(media, "file_unique_id", "") or "")
    if not file_id or not unique:
        return None
    resolved_mime = str(getattr(media, "mime_type", "") or mime or "")
    name = str(getattr(media, "file_name", "") or "") or fallback_name
    return MediaRef(
        kind=kind or attachments.kind_for(resolved_mime, name),
        file_id=file_id,
        file_unique_id=unique,
        name=name,
        mime=resolved_mime,
        size=int(getattr(media, "file_size", 0) or 0),
        width=getattr(media, "width", None),
        height=getattr(media, "height", None),
    )


def media_ref(message: Any) -> MediaRef | None:
    """The one attachment on *message*, whatever field Telegram put it in.

    Order matters only in that a message carries exactly one of these. A
    sticker is treated as an image when it is a still one and as a document
    when it is animated or a video, because that is what it is on disk — and
    because the agent asking to look at an animated sticker with ``view_image``
    should be told it is a video file rather than handed a frame.
    """
    if getattr(message, "document", None):
        doc = message.document
        name = str(getattr(doc, "file_name", "") or "") or "document"
        mime = str(getattr(doc, "mime_type", "") or "")
        return _ref(doc, kind=attachments.kind_for(mime, name), fallback_name="document")
    if getattr(message, "photo", None):
        # The last entry is the largest rendition Telegram kept.
        return _ref(message.photo[-1], kind="image", fallback_name="photo.jpg", mime="image/jpeg")
    if getattr(message, "video", None):
        return _ref(message.video, kind="video", fallback_name="video.mp4", mime="video/mp4")
    if getattr(message, "audio", None):
        return _ref(message.audio, kind="audio", fallback_name="audio.mp3", mime="audio/mpeg")
    if getattr(message, "voice", None):
        return _ref(message.voice, kind="voice", fallback_name="voice.ogg", mime="audio/ogg")
    if getattr(message, "video_note", None):
        return _ref(
            message.video_note, kind="video", fallback_name="video_note.mp4", mime="video/mp4"
        )
    if getattr(message, "sticker", None):
        sticker = message.sticker
        animated = bool(
            getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False)
        )
        if animated:
            return _ref(sticker, kind="video", fallback_name="sticker.webm", mime="video/webm")
        return _ref(sticker, kind="image", fallback_name="sticker.webp", mime="image/webp")
    return None


#: Which extensions are worth decoding into the first turn. One list now, in
#: ``robothor.engine.attachments``, because the tool layer needs the same
#: answer and a second copy here is how two surfaces end up disagreeing about
#: what a text file is. Re-exported for instances that patch the name.
#:
#: ``.env`` was on this list and is NOT on the new one: a file named like a
#: credentials file is still SAVED, but quoting it into a prompt is the exact
#: exposure ``robothor.engine.secret_paths`` exists to refuse.
TEXT_EXTENSIONS = attachments.TEXT_EXTENSIONS

# Models available for /model selection (display name → litellm model id).
#
# THIS IS THE ONLY SURFACE ON THIS INSTANCE THAT LISTS MODELS FOR A HUMAN.
# The TUI's /model only prints the current one, the Helm dashboard renders the
# per-agent model as a read-only badge, and `GET /v1/models` is a stub.
#
# Insertion order IS the button order — `_build_model_keyboard` iterates this
# dict with no sort — so `current` entries come first and every `legacy` one
# carries the word in its label. `test_model_status.py` pins both rules, and
# `test_telegram.py` pins that every id here is a registered openrouter/* id.
#
# OpenRouter-only (operator policy 2026-07-07): the OpenAI account was blocked,
# so codex/* auth is dead — no picker entry may route there.
#
# 2026-09-11: Ox Alpha removed (its stealth preview ended; the id no longer
# resolves on OpenRouter at all), and the two modernisation candidates added.
AVAILABLE_MODELS: dict[str, str] = {
    # --- current ---
    "DeepSeek V4.1 Flash": "openrouter/deepseek/deepseek-v4.1-flash",
    "GLM 5.3 Flash": "openrouter/z-ai/glm-5.3-flash",
    "MiMo V2.5": "openrouter/xiaomi/mimo-v2.5",
    "MiMo V2.5 Pro": "openrouter/xiaomi/mimo-v2.5-pro",
    "DeepSeek V4 Pro": "openrouter/deepseek/deepseek-v4-pro",
    "Claude Sonnet 4.6": "openrouter/anthropic/claude-sonnet-4.6",
    "Claude Opus 4.7": "openrouter/anthropic/claude-opus-4.7",
    # --- legacy: still served, no longer the pick ---
    "DeepSeek V4 Flash (legacy)": "openrouter/deepseek/deepseek-v4-flash",
}


MODEL_DISPLAY_NAMES = {v: k for k, v in AVAILABLE_MODELS.items()}


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


async def _analyze_photo_bytes(raw_bytes: bytes, prompt: str = "") -> str:
    """Deprecated shim: the local vision call now lives in the images tool.

    It moved because the channel owned a private copy the tool layer could not
    reach — so ``view_image`` had no way to describe a picture for a model that
    cannot see one, and the channel's copy read a raw ``OLLAMA_URL`` and a
    hardcoded model id, ignoring the ``ROBOTHOR_VISION_MODEL`` the instance had
    declared. Kept as a one-line delegate because instances patch this name.

    Returns the old bracketed error string rather than raising: that is the
    contract this name has always had, and a caller expecting a description is
    handed something it can put in a prompt. New code calls
    :func:`robothor.engine.tools.handlers.images.describe_image_bytes` directly
    and handles the exception, so it can tell a description from a failure.
    """
    from robothor.engine.tools.handlers.images import describe_image_bytes

    try:
        return await describe_image_bytes(raw_bytes, prompt)
    except Exception as e:  # noqa: BLE001 - preserved shim contract
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


class TelegramHandlersMixin:
    """See module docstring."""

    if TYPE_CHECKING:
        # Deliberately WIDE: these handlers are TelegramBot methods that reach
        # across the whole bot surface, and this extraction's goal is a file
        # boundary + testability, not (yet) a narrow contract. The __getattr__
        # escape scopes Any-typing to THIS class only; PlanModeMixin shows the
        # narrow-stub end state once handler bodies stop reaching ad hoc.
        def __getattr__(self, name: str) -> Any: ...

    async def cmd_help(self, message: Message) -> None:
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

    async def cmd_model(self, message: Message) -> None:
        chat_id = str(message.chat.id)
        override = self._model_override.get(chat_id)
        if override:
            current = override
            current_name = MODEL_DISPLAY_NAMES.get(current, current)
            status_line = f"<b>Current model:</b> {html.escape(current_name)} (override)"
        else:
            current = self._get_manifest_primary()
            current_name = MODEL_DISPLAY_NAMES.get(current, current)
            status_line = f"<b>Current model:</b> {html.escape(current_name)} (manifest default)"
        kb = self._build_model_keyboard(current)
        await message.answer(
            f"{status_line}\n\nTap to switch:",
            reply_markup=kb,
        )

    async def cmd_goal(self, message: Message) -> None:
        await self._handle_goal_command(message)

    async def cmd_clear(self, message: Message) -> None:
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

    async def cmd_reset(self, message: Message) -> None:
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

    async def cmd_stop(self, message: Message) -> None:
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

    async def cmd_restart(self, message: Message) -> None:
        await self._handle_restart_command(message, "robothor-engine.service")

    async def cmd_context(self, message: Message) -> None:
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

    async def cmd_status(self, message: Message) -> None:
        """Fleet health snapshot — per-agent last status + 24h metrics."""
        chat_id = str(message.chat.id)
        try:
            # Live schedule state from /health
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://localhost:{self.config.port}/health", timeout=5)
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

    async def cmd_export(self, message: Message) -> None:
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

    async def cmd_plan(self, message: Message) -> None:
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

    async def cmd_deep(self, message: Message) -> None:
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
        await self._run_plan_mode(chat_id, session_key, session, deep_arg, message, deep_plan=True)

    async def cmd_stats(self, message: Message) -> None:
        """Show fleet achievement snapshot — goal satisfaction by agent."""
        try:
            from robothor.engine.buddy import BuddyEngine, format_achievement

            engine = BuddyEngine()
            fleet = engine.compute_daily_stats()
            current_streak, longest_streak = engine.get_streak()

            lines = [
                f"<b>Fleet achievement</b>: {format_achievement(fleet.fleet_achievement_score)}",
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

    async def cmd_buddy(self, message: Message) -> None:
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

    async def cmd_goals(self, message: Message) -> None:
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

    async def cmd_agents(self, message: Message) -> None:
        await self._handle_agents_command(message)

    async def cmd_steer(self, message: Message) -> None:
        await self._handle_steer_command(message)

    # ── Inline keyboard callbacks ──

    async def on_plan_decision(self, callback: CallbackQuery) -> None:
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
            task = asyncio.create_task(self._execute_approved_plan(chat_id, session_key, session))
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

    async def on_model_select(self, callback: CallbackQuery) -> None:
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

    async def on_permission_decision(self, callback: CallbackQuery) -> None:
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

        decision = _PERM_DECISIONS.get(action)
        if decision is None:
            await callback.answer("Unknown action")
            return
        approved, remember, confirmation = decision

        # `resolve` reports whether THIS call settled anything. A prompt can
        # outlive its request — the watchdog sweep reaps orphans, the tool's own
        # wait_for denies on timeout, a restart empties the map — and the
        # keyboard stays live in the chat regardless. Answering "Approved" to a
        # tap that reached nothing is a false confirmation on the approval path.
        settled = mgr.resolve(request_id, approved=approved, remember_session=remember)
        if settled:
            await callback.answer(confirmation)
        else:
            # show_alert, because a toast the operator misses leaves them
            # believing the tap landed.
            await callback.answer(STALE_PROMPT, show_alert=True)

        # Remove inline keyboard after decision
        with contextlib.suppress(Exception):
            if msg and hasattr(msg, "edit_reply_markup"):
                await msg.edit_reply_markup(reply_markup=None)
        if not settled:
            # The alert is transient; the message is what the operator scrolls
            # back to, and an un-annotated prompt reads as a decision that took.
            with contextlib.suppress(Exception):
                if msg and hasattr(msg, "edit_text"):
                    await msg.edit_text(
                        f"{getattr(msg, 'text', '') or ''}\n\n{STALE_PROMPT}".strip()
                    )

    # ── Channel.ask answers ──

    async def on_ask_answer(self, callback: CallbackQuery) -> None:
        """Resolve a pending ``Channel.ask`` from an inline-keyboard tap.

        The body is in ``channels/telegram_ask.py`` with the binding rules it
        has to apply — an answer settles an ask only when it comes from the
        chat AND the sender the ask was minted for.
        """
        from robothor.engine.channels.telegram_ask import handle_ask_callback

        await handle_ask_callback(self, callback)

    # ── Run control callbacks (Steer / Interrupt buttons from /agents) ──

    async def on_runctl_callback(self, callback: CallbackQuery) -> None:
        await self._handle_runctl_callback(callback)

    # ── Delphi proposal approval callbacks ──
    # callback_data shape: ``dp:a:<32-hex>`` (approve) or ``dp:r:<32-hex>``
    # (reject). The Delphi engine runs a separate daemon (port 18801) but
    # uses the same engine codebase, so this handler ships in the shared
    # telegram.py and only fires when a Delphi proposal is in flight.
    async def on_delphi_proposal_decision(self, callback: CallbackQuery) -> None:
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
            await callback.answer(msg_map.get(rc or -1, f"Apply failed (rc={rc})"), show_alert=True)

    # ── Voice notes / video notes ──
    # Previously unhandled, so they were silently dropped. Now acknowledged.
    # Transcription is gated on ROBOTHOR_VOICE_NOTES_ENABLED (no STT provider
    # is wired yet — Claude has no audio endpoint — so this is a placeholder).

    async def handle_voice(self, message: Message) -> None:
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

    # ── File/document/photo/audio/video/sticker messages ──
    #
    # One intake for every kind, and the rule it enforces is that the BYTES
    # SURVIVE. What used to happen: the file was downloaded into memory, turned
    # into text or into the string "[Binary file: name, N bytes]", and dropped.
    # The agent was handed a description of something it could never open,
    # forward, convert, attach to a task or re-read. A photo lost even more —
    # the operator's caption was spent as the vision model's prompt and then
    # cleared, so the question they actually asked never reached the agent.
    #
    # Now: save first (robothor/engine/attachments.py), then describe. Text and
    # PDF extraction still happen, but as a convenience beside the path rather
    # than in place of it, and the extract says how much of the file it is.

    def _attachment_workspace(self) -> Any:
        """Where this instance keeps its inbox. Never a hardcoded home path."""
        return getattr(getattr(self, "config", None), "workspace", None)

    async def _download_media(self, media: MediaRef) -> bytes:
        """Fetch one attachment's bytes through the Bot API.

        Raises on anything that goes wrong; the caller turns that into a
        sentence for the operator rather than a traceback.
        """
        from io import BytesIO

        handle = await self.bot.get_file(media.file_id)
        file_path = getattr(handle, "file_path", None)
        if not file_path:
            raise RuntimeError("Telegram returned no download path for the file")
        buffer = BytesIO()
        await self.bot.download_file(file_path, buffer)
        return buffer.getvalue()

    async def _keep_attachment(
        self, chat_id: str, media: MediaRef, caption: str
    ) -> attachments.NotedAttachment | None:
        """Save one attachment and gather what can cheaply be said about it.

        ``None`` means the download failed — the caller tells the operator, and
        nothing is enqueued, because an agent told "a file arrived" with no file
        behind it is exactly the state this replaces.
        """
        try:
            raw = await self._download_media(media)
        except Exception as exc:  # noqa: BLE001 - reported to the operator
            logger.warning("Telegram attachment download failed (%s): %s", media.name, exc)
            return None

        row = attachments.save_attachment(
            chat_id=chat_id,
            file_id=media.file_id,
            file_unique_id=media.file_unique_id,
            name=media.name,
            data=raw,
            kind=media.kind,
            mime=media.mime,
            width=media.width,
            height=media.height,
            caption=caption,
            workspace=self._attachment_workspace(),
        )
        noted = attachments.NotedAttachment(row=row)
        await self._enrich_attachment(noted, raw, media, chat_id)
        return noted

    async def _enrich_attachment(
        self,
        noted: attachments.NotedAttachment,
        raw: bytes,
        media: MediaRef,
        chat_id: str,
    ) -> None:
        """Extracted text for a document, a local description for a picture.

        Both are best-effort and neither can fail the intake: the file is
        already on disk and the path is already in the note, so the worst case
        is an agent that has to open it itself.
        """
        from pathlib import PurePath

        suffix = PurePath(media.name).suffix.lower()
        extract = attachments.extractable(suffix, media.mime)
        if extract is not None and media.kind != "image":
            whole = ""
            if extract == "pdf":
                whole = await _extract_pdf_text(raw)
            else:
                whole = raw.decode("utf-8", errors="replace")
            noted.text_total_chars = len(whole)
            noted.text = whole[: attachments.TEXT_EXTRACT_CHARS]
            return

        if media.kind != "image":
            return
        if self._model_sees_images(chat_id):
            # The agent's own model can look at the file itself, and its eyes
            # beat a 11B local model's paragraph. The note still names the path
            # and tells it to call view_image.
            return
        from robothor.engine.tools.handlers.images import describe_image_bytes

        try:
            noted.vision = await describe_image_bytes(raw)
        except Exception as exc:  # noqa: BLE001 - a description is never invented
            logger.info("local vision model unavailable for an inbound photo: %s", exc)
            noted.error = (
                "no description available — the local vision model did not answer; "
                "call view_image on the path to look at it yourself"
            )

    def _model_sees_images(self, chat_id: str) -> bool:
        """Can the model this chat runs on be shown a picture?

        Only a model the registry states can — ``unknown`` counts as no, which
        is the safe direction HERE and the opposite of the direction
        ``view_image`` takes. The asymmetry is deliberate: describing an image
        nobody needed described costs one local call, while withholding a
        description from an agent that turns out to be blind costs the turn.
        """
        from robothor.engine.model_registry import image_capability

        return image_capability(self._model_override.get(chat_id, "")) == "accepts"

    async def handle_file(self, message: Message) -> None:
        """Keep whatever arrived, then hand the agent the caption and the paths."""
        if not message.from_user:
            return

        chat_id = str(message.chat.id)

        user_info = self._resolve_user(chat_id, message)
        if user_info is None:
            reply = await self._handle_unregistered_sender(message, str(message.from_user.id))
            await message.answer(reply)
            return

        caption = (message.caption or "").strip()
        media = media_ref(message)
        if media is None:
            await message.answer(
                "I couldn't tell what that attachment was. Send it as a file and I'll keep it."
            )
            return

        if media.size and media.size > attachments.MAX_DOWNLOAD_BYTES:
            await message.answer(attachments.too_large_sentence(media.size, name=media.name))
            return

        noted = await self._keep_attachment(chat_id, media, caption)
        if noted is None:
            await message.answer(
                f"I couldn't download {media.name} from Telegram, so I have nothing to work "
                "with. Try sending it again."
            )
            return

        logger.info(
            "Telegram attachment in chat %s: %s (%s, %s bytes), caption=%s",
            chat_id,
            noted.row.get("name"),
            noted.row.get("kind"),
            noted.row.get("size"),
            "yes" if caption else "none",
        )

        group_id = getattr(message, "media_group_id", None)
        if group_id:
            self._collect_album(chat_id, str(group_id), caption, noted, user_info, message)
            return

        await self._route_attachment_turn(
            chat_id,
            attachments.format_attachment_note(caption, [noted]),
            [noted.row],
            user_info,
            message,
        )

    # ── Albums ──
    #
    # Telegram sends an album as N separate updates sharing a media_group_id,
    # and the caption rides on exactly one of them. Handled one at a time, the
    # operator's three photos became three runs, two of them captionless. They
    # are collected for a short window and delivered as ONE turn with N paths.

    def _collect_album(
        self,
        chat_id: str,
        group_id: str,
        caption: str,
        noted: attachments.NotedAttachment,
        user_info: dict[str, Any],
        message: Message,
    ) -> None:
        """Add one album member to the pending group, starting its timer once."""
        key = (chat_id, group_id)
        pending = self._album_buffers.get(key)
        if pending is None:
            pending = {"items": [], "caption": "", "user_info": user_info, "message": message}
            self._album_buffers[key] = pending
            self._album_tasks[key] = asyncio.create_task(self._flush_album(chat_id, group_id))
        pending["items"].append(noted)
        # First caption wins: Telegram puts it on one member, and a later empty
        # one must not erase it.
        if caption and not pending["caption"]:
            pending["caption"] = caption

    async def _flush_album(self, chat_id: str, group_id: str) -> None:
        """Wait out the window, then enqueue the whole album as one turn."""
        key = (chat_id, group_id)
        try:
            await asyncio.sleep(ALBUM_WINDOW_SECONDS)
            pending = self._album_buffers.pop(key, None)
            if not pending or not pending["items"]:
                return
            items = list(pending["items"])
            await self._route_attachment_turn(
                chat_id,
                attachments.format_attachment_note(pending["caption"], items),
                [item.row for item in items],
                pending["user_info"],
                pending["message"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Telegram album %s could not be delivered", group_id)
        finally:
            self._album_buffers.pop(key, None)
            self._album_tasks.pop(key, None)

    async def _route_attachment_turn(
        self,
        chat_id: str,
        user_text: str,
        rows: list[dict[str, Any]],
        user_info: dict[str, Any],
        message: Message,
    ) -> None:
        """Send the composed turn down the same path a text message takes."""
        session_key = self._session_key(chat_id)
        session = get_shared_session(session_key)

        if session.active_plan and session.active_plan.status == "pending":
            if not _plan_is_expired(session.active_plan):
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

        if getattr(message, "message_id", None):
            self._user_message_id_buffers[chat_id] = str(message.message_id)

        await self._enqueue_message(
            chat_id,
            session_key,
            session,
            user_text,
            sender_info=user_info,
            attachments=rows,
        )

    # ── Interactive text messages ──

    async def handle_text(self, message: Message) -> None:
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

        # ── A pending ask bound to (this chat, this sender) takes this line ──
        # Before the run does: see ``telegram_ask.intercept_ask_answer`` for why
        # reaching `_enqueue_message` would lose the answer entirely.
        from robothor.engine.channels.telegram_ask import intercept_ask_answer

        _quoted = getattr(message.reply_to_message, "message_id", "")
        if await intercept_ask_answer(self, chat_id, telegram_user_id, user_text, str(_quoted)):
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
        await self._enqueue_message(chat_id, session_key, session, user_text, sender_info=user_info)

    async def on_message_reaction(self, event: Any) -> None:
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
                (getattr(user, "username", None) or str(getattr(user, "id", ""))) if user else None
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
