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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message, PhotoSize

from robothor.constants import DEFAULT_TENANT
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


# File handling — max size for text extraction (5 MB)
MAX_FILE_SIZE = 5 * 1024 * 1024

# Units the Telegram /restart family can queue a restart for, mapped to the
# advisory trigger file that ``robothor-restart.path`` (infra/systemd/) watches.
# The restart TARGET is hardcoded in that path unit's paired .service — this
# file's contents are never read as the unit to restart, only as an audit
# trail (UTC timestamp + sender). Module-level so tests can inject a tmp_path.
# A unit with no entry here has no path-unit watching for it — the handler
# tells the caller to use SSH instead. That list is deliberately short: vision
# and mediamtx are absent because they were disabled by hand after the
# 2026-08-19 GPU thermal event, and re-enabling them unattended would let the
# agent undo a thermal-safety decision on a box nobody is standing next to.
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
# Reverse lookup: model id → display name
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


async def _analyze_photo_bytes(
    raw_bytes: bytes,
    prompt: str = "Describe what you see in this image in detail. Read and transcribe ALL visible text exactly. Note any URLs, names, numbers, UI elements, or content shown.",
) -> str:
    """Send raw image bytes to llama3.2-vision via Ollama for VLM analysis."""
    import base64

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

    # ── File/document/photo messages ──

    async def handle_file(self, message: Message) -> None:
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
                    file_content = f"[Image: {photo.width}x{photo.height}px — could not download]"
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
            parts.append(f"--- File content: {file_name} ---\n{file_content}\n--- End of file ---")

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
        await self._enqueue_message(chat_id, session_key, session, user_text, sender_info=user_info)

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
