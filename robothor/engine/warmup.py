"""
Session Warmth — pre-loads context so agents start warm, not cold.

Builds a preamble string from:
1. Session history (last run status, duration, errors)
2. Memory blocks (operational_findings, contacts_summary, etc.)
3. Context files (status files agents would otherwise waste tool calls reading)
4. Peer agent status (what related agents did recently)

Every section wrapped in try/except — never crashes, silently degrades.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)

MAX_WARMTH_CHARS = 4000
MAX_BLOCK_CHARS = 800
MAX_FILE_CHARS = 600

# ── Warmup kind (cron | interactive) ──────────────────────────────
# Runner sets this around the warmup call so hooks can discriminate
# between scheduled heartbeat runs and interactive chat turns. ContextVars
# do not auto-propagate to executors, so runner sets it *inside* the
# executor closure via `set_warmup_kind`.

_CURRENT_WARMUP_KIND: ContextVar[str | None] = ContextVar("robothor_warmup_kind", default=None)


@contextmanager
def set_warmup_kind(kind: str | None) -> Iterator[None]:
    token = _CURRENT_WARMUP_KIND.set(kind)
    try:
        yield
    finally:
        _CURRENT_WARMUP_KIND.reset(token)


def current_warmup_kind() -> str | None:
    return _CURRENT_WARMUP_KIND.get()


# ── Dynamic context hooks ─────────────────────────────────────────
# Callables that return optional context strings. Called during warmup
# preamble construction. Each hook has a 100ms timeout.

_CONTEXT_HOOKS: list[Callable[[], str | None]] = []
_AGENT_CONTEXT_HOOKS: list[Callable[[AgentConfig], str | None]] = []


def register_context_hook(fn: Callable[[], str | None]) -> None:
    """Register a dynamic context hook for warmup preambles."""
    _CONTEXT_HOOKS.append(fn)


def register_agent_context_hook(fn: Callable[[AgentConfig], str | None]) -> None:
    """Register an agent-aware context hook (receives AgentConfig)."""
    _AGENT_CONTEXT_HOOKS.append(fn)


def _run_context_hooks() -> str:
    """Run all context hooks, collecting results within 100ms timeout each."""
    import time

    results: list[str] = []
    for hook in _CONTEXT_HOOKS:
        try:
            start = time.monotonic()
            result = hook()
            elapsed = time.monotonic() - start
            if elapsed > 0.1:
                logger.debug("Context hook %s took %.0fms (>100ms)", hook.__name__, elapsed * 1000)
            if result:
                results.append(result)
        except Exception as e:
            logger.debug("Context hook %s failed: %s", hook.__name__, e)

    if not results:
        return ""
    return "--- SITUATIONAL CONTEXT ---\n" + "\n".join(results)


def build_warmth_preamble(
    config: AgentConfig,
    workspace: Path,
    tenant_id: str = DEFAULT_TENANT,
) -> str:
    """Build a warmth preamble string for an agent run.

    Returns up to MAX_WARMTH_CHARS of pre-loaded context. Empty string
    if no warmup config or all sections fail.
    """
    sections: list[str] = []
    total_start = time.monotonic()
    # Section timings for stall diagnosis — heartbeat runs have been timing
    # out before the first LLM call, and warmup is the biggest blocking
    # slab in init. Log anything > 500ms per section, and total > 5s.
    _section_timings: dict[str, float] = {}

    def _run_section(name: str, fn: Callable[[], str | None]) -> None:
        start = time.monotonic()
        try:
            result = fn()
        except Exception as e:
            logger.debug("Warmup %s failed for %s: %s", name, config.id, e)
            result = None
        elapsed = time.monotonic() - start
        _section_timings[name] = elapsed
        if elapsed > 0.5:
            logger.info("warmup %s: section=%s ms=%d", config.id, name, int(elapsed * 1000))
        if result:
            sections.append(result)

    _run_section("history", lambda: _build_history_section(config.id))
    _run_section(
        "memory_blocks",
        lambda: _build_memory_blocks_section(config.warmup_memory_blocks, tenant_id=tenant_id),
    )
    _run_section(
        "context_files",
        lambda: _build_context_files_section(config.warmup_context_files, workspace),
    )
    _run_section("peers", lambda: _build_peer_section(config.warmup_peer_agents))
    _run_section("context_hooks", _run_context_hooks)

    def _breadcrumbs() -> str | None:
        from robothor.memory.breadcrumbs import (
            format_breadcrumbs_for_warmup,
            load_recent_breadcrumbs,
        )

        breadcrumbs = load_recent_breadcrumbs(config.id, limit=5, tenant_id=tenant_id)
        return format_breadcrumbs_for_warmup(breadcrumbs)

    _run_section("breadcrumbs", _breadcrumbs)

    def _preferences() -> str | None:
        from robothor.memory.preferences import get_stale_preferences

        stale = get_stale_preferences(tenant_id=tenant_id)
        if not stale:
            return None
        lines = ["# Preferences flagged as possibly stale (verify with operator)"]
        lines.extend(f"- {p.get('preference', '?')}" for p in stale[:5])
        return "\n".join(lines)

    _run_section("preferences", _preferences)
    _run_section("agent_hooks", lambda: _run_agent_context_hooks(config))

    total_elapsed = time.monotonic() - total_start
    if total_elapsed > 5.0:
        breakdown = " ".join(
            f"{k}={int(v * 1000)}" for k, v in _section_timings.items() if v > 0.05
        )
        logger.warning(
            "warmup %s: total_ms=%d breakdown=%s",
            config.id,
            int(total_elapsed * 1000),
            breakdown,
        )

    if not sections:
        return ""

    preamble = "\n\n".join(sections)
    if len(preamble) > MAX_WARMTH_CHARS:
        preamble = preamble[:MAX_WARMTH_CHARS] + "\n[warmup truncated]"

    return preamble


def _build_history_section(agent_id: str) -> str:
    """Build session history from agent_schedules."""
    from robothor.engine.tracking import get_schedule

    schedule = get_schedule(agent_id)
    if not schedule:
        return ""

    lines = ["--- SESSION HISTORY ---"]

    last_status = schedule.get("last_status")
    if last_status:
        lines.append(f"Last run: {last_status}")

    last_duration = schedule.get("last_duration_ms")
    if last_duration is not None:
        lines.append(f"Duration: {last_duration}ms")

    last_run_at = schedule.get("last_run_at")
    if last_run_at:
        if isinstance(last_run_at, datetime):
            now = datetime.now(UTC)
            delta = (
                now - last_run_at.replace(tzinfo=UTC)
                if last_run_at.tzinfo is None
                else now - last_run_at
            )
            hours = delta.total_seconds() / 3600
            lines.append(f"Hours since last run: {hours:.1f}")
        else:
            lines.append(f"Last run at: {last_run_at}")

    consecutive_errors = schedule.get("consecutive_errors", 0)
    if consecutive_errors and consecutive_errors > 0:
        lines.append(f"WARNING: {consecutive_errors} consecutive errors")

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_memory_blocks_section(block_names: list[str], tenant_id: str = DEFAULT_TENANT) -> str:
    """Read memory blocks and format them, flagging stale ones."""
    if not block_names:
        return ""

    from robothor.memory.blocks import read_block

    lines = ["--- MEMORY BLOCKS ---"]
    for name in block_names:
        try:
            result = read_block(name, tenant_id=tenant_id)
            content = (
                result.get("content", "")
                if isinstance(result, dict)
                else str(result)
                if result
                else ""
            )
            if content:
                # Check staleness — flag blocks older than 24h
                stale_tag = ""
                last_written = result.get("last_written_at") if isinstance(result, dict) else None
                if last_written:
                    try:
                        from datetime import datetime as _dt

                        written_dt = _dt.fromisoformat(last_written)
                        if written_dt.tzinfo is None:
                            written_dt = written_dt.replace(tzinfo=UTC)
                        age_hours = (datetime.now(UTC) - written_dt).total_seconds() / 3600
                        if age_hours > 24:
                            stale_tag = f" [STALE — {age_hours:.0f}h old]"
                    except (ValueError, TypeError):
                        pass

                truncated = content[:MAX_BLOCK_CHARS]
                if len(content) > MAX_BLOCK_CHARS:
                    truncated += "..."
                lines.append(f"[{name}]{stale_tag}\n{truncated}")
        except Exception as e:
            logger.debug("Failed to read memory block %s: %s", name, e)

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_context_files_section(file_paths: list[str], workspace: Path) -> str:
    """Read context files (status files etc.) and format them."""
    if not file_paths:
        return ""

    lines = ["--- CONTEXT FILES ---"]
    for rel_path in file_paths:
        try:
            full_path = workspace / rel_path
            if not full_path.exists():
                continue
            content = full_path.read_text()
            if not content.strip():
                continue
            truncated = content[:MAX_FILE_CHARS]
            if len(content) > MAX_FILE_CHARS:
                truncated += "..."
            age_hours = (time.time() - full_path.stat().st_mtime) / 3600
            age_label = f" (stale — {age_hours:.0f}h ago)" if age_hours > 4 else ""
            lines.append(f"[{rel_path}]{age_label}\n{truncated}")
        except Exception as e:
            logger.debug("Failed to read context file %s: %s", rel_path, e)

    return "\n".join(lines) if len(lines) > 1 else ""


def _open_tasks_section(tenant_id: str, limit: int = 10) -> str:
    """Render the top open tasks grouped by assigned agent.

    For main's Telegram warmup — lets the supervisor answer 'what's open?'
    without spinning tool calls.
    """
    try:
        from robothor.crm.dal import list_tasks

        rows = list_tasks(
            tenant_id=tenant_id,
            exclude_resolved=True,
            limit=limit,
        )
        if not rows:
            return "--- OPEN TASKS ---\nNothing open."
        grouped: dict[str, list[dict]] = {}
        for t in rows:
            key = t.get("assigned_to_agent") or "unassigned"
            grouped.setdefault(key, []).append(t)
        lines = ["--- OPEN TASKS ---"]
        for agent, tasks in sorted(grouped.items()):
            lines.append(f"[{agent}]")
            for t in tasks[:5]:
                obj = t.get("objective") or ""
                obj_part = f" — {obj}" if obj else ""
                short_id = str(t.get("id") or "")[:8]
                lines.append(
                    f"  • {t.get('title', '(no title)')} "
                    f"({t.get('status', '?')}{obj_part}) [{short_id}]"
                )
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Open-tasks section failed: %s", e)
        return ""


def _recent_fleet_surfaces(tenant_id: str, hours: int = 6, limit: int = 6) -> str:
    """Pull recent fleet agent deliveries from the channel bus (dual-writes
    into main's session with origin='channel_bus'). Gives main awareness of
    what other agents posted to the operator's Telegram channel in the last
    few hours.
    """
    try:
        from robothor.db import get_connection

        sql = """
            SELECT
                cm.created_at,
                cm.message->>'author_agent_id' AS author,
                COALESCE(cm.message->>'content', '') AS content
            FROM chat_messages cm
            JOIN chat_sessions cs ON cs.id = cm.session_id
            WHERE cs.tenant_id = %s
              AND cm.message->>'origin' = 'channel_bus'
              AND cm.message->>'author_agent_id' IS NOT NULL
              AND cm.message->>'author_agent_id' != 'main'
              AND cm.created_at > NOW() - (%s || ' hours')::interval
            ORDER BY cm.created_at DESC
            LIMIT %s
        """
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (tenant_id, str(hours), limit))
                rows = cur.fetchall()
        if not rows:
            return ""
        lines = [f"--- RECENT FLEET SURFACES (last {hours}h) ---"]
        for created_at, author, content in rows:
            ts = created_at.strftime("%H:%M") if created_at else "?"
            snippet = (content or "").strip().split("\n", 1)[0][:140]
            lines.append(f"[@{author} {ts}] {snippet}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Fleet surfaces section failed: %s", e)
        return ""


def build_interactive_preamble(
    agent_id: str,
    user_message: str = "",
    include_blocks: bool = True,
    tenant_id: str = DEFAULT_TENANT,
    extra_memory_blocks: list[str] | None = None,
    sender_name: str = "",
) -> str:
    """Build a lightweight warmup preamble for interactive (Telegram) sessions.

    Injects core memory blocks (persona, user_profile, working_context) and
    optionally pulls relevant facts based on entities mentioned in the user's message.

    Args:
        agent_id: The agent ID (for history lookup).
        user_message: The user's message (for entity-aware context).
        include_blocks: If True, inject core memory blocks (persona, user_profile,
            working_context). Set to False for ongoing sessions where blocks are
            already in conversation history.
        sender_name: Display name of the current user. When set, injects an
            identity section and excludes the name from entity context search
            to avoid confusing the user with other people sharing the same name.

    Returns:
        Warmup preamble string, or empty string if nothing to inject.
    """
    sections: list[str] = []

    # Sender identity — tell the agent exactly who it's talking to
    if sender_name:
        sections.append(
            f"--- CURRENT USER ---\n"
            f"You are speaking with {sender_name}. Address them by this name.\n"
            f"Do not confuse them with other people who may share the same name."
        )

    # Core memory blocks — only for new sessions (no prior history)
    if include_blocks:
        core_blocks = ["persona", "user_profile", "user_model", "working_context"]
        # Also include agent-configured warmup blocks (e.g. devops_latest_report)
        if extra_memory_blocks:
            core_blocks = list(dict.fromkeys(core_blocks + extra_memory_blocks))
        try:
            blocks_section = _build_memory_blocks_section(core_blocks, tenant_id=tenant_id)
            if blocks_section:
                sections.append(blocks_section)
        except Exception as e:
            logger.debug("Interactive warmup blocks failed: %s", e)

    # Entity-aware context — if user mentions a name, pull relevant facts
    # Exclude the sender's name to avoid pulling facts about other people
    # who share the same name — the sender's identity comes from their
    # tenant's persona/user_profile blocks, not from entity search.
    if user_message and len(user_message) > 5:
        try:
            exclude = {sender_name} if sender_name else None
            context = _build_entity_context(
                user_message, tenant_id=tenant_id, exclude_names=exclude
            )
            if context:
                sections.append(context)
        except Exception as e:
            logger.debug("Interactive warmup entity context failed: %s", e)

    # Dynamic context hooks (date, travel, weather, etc.)
    try:
        situational = _run_context_hooks()
        if situational:
            sections.append(situational)
    except Exception as e:
        logger.debug("Interactive warmup context hooks failed: %s", e)

    # Main-only panoramic sections: open task queue + recent fleet surfaces.
    # These let the supervisor answer "what's going on?" from context alone.
    if agent_id == "main":
        tasks_section = _open_tasks_section(tenant_id=tenant_id)
        if tasks_section:
            sections.append(tasks_section)
        fleet_section = _recent_fleet_surfaces(tenant_id=tenant_id)
        if fleet_section:
            sections.append(fleet_section)

    if not sections:
        return ""

    preamble = "\n\n".join(sections)
    if len(preamble) > MAX_WARMTH_CHARS:
        preamble = preamble[:MAX_WARMTH_CHARS] + "\n[warmup truncated]"
    return preamble


MAX_ENTITY_CONTEXT_CHARS = 1000


def _build_entity_context(
    user_message: str,
    tenant_id: str = DEFAULT_TENANT,
    exclude_names: set[str] | None = None,
) -> str:
    """Extract entities from user message and pull relevant facts.

    Looks for capitalized proper nouns in the message and searches
    memory facts for matching entity references.

    Args:
        exclude_names: Names to skip during entity search (e.g. the current
            user's name, to avoid confusing them with other people).

    Budget: max 1000 chars for this section.
    """
    import re

    # Simple entity extraction: capitalized words that aren't sentence starters
    words = user_message.split()
    candidates = set()
    for i, word in enumerate(words):
        cleaned = re.sub(r"[^\w]", "", word)
        if (
            cleaned
            and cleaned[0].isupper()
            and len(cleaned) > 2
            and (
                i > 0
                or cleaned.lower()
                not in {
                    "the",
                    "what",
                    "how",
                    "when",
                    "where",
                    "why",
                    "can",
                    "does",
                    "did",
                    "hey",
                    "hi",
                }
            )
        ):
            candidates.add(cleaned)

    # Remove excluded names (e.g. the current user's name)
    if exclude_names:
        candidates -= {n for n in exclude_names if n}

    if not candidates:
        return ""

    from psycopg2.extras import RealDictCursor

    from robothor.db import get_connection

    lines = ["--- RELEVANT CONTEXT ---"]
    chars_used = 0

    with get_connection() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        for entity_name in list(candidates)[:3]:
            cur.execute(
                """
                SELECT fact_text, category, importance_score
                FROM memory_facts
                WHERE is_active = TRUE AND %s = ANY(entities)
                  AND tenant_id = %s
                ORDER BY importance_score DESC, created_at DESC
                LIMIT 3
                """,
                (entity_name, tenant_id),
            )
            facts = cur.fetchall()
            for f in facts:
                line = f"- {f['fact_text']}"
                if chars_used + len(line) > MAX_ENTITY_CONTEXT_CHARS:
                    break
                lines.append(line)
                chars_used += len(line)

    return "\n".join(lines) if len(lines) > 1 else ""


def _build_peer_section(peer_agent_ids: list[str]) -> str:
    """Query peer agent schedules for recent status."""
    if not peer_agent_ids:
        return ""

    from robothor.engine.tracking import get_schedule

    lines = ["--- PEER AGENTS ---"]
    for peer_id in peer_agent_ids:
        try:
            schedule = get_schedule(peer_id)
            if not schedule:
                lines.append(f"{peer_id}: no data")
                continue

            status = schedule.get("last_status", "unknown")
            last_run = schedule.get("last_run_at", "")
            run_str = ""
            if last_run:
                if isinstance(last_run, datetime):
                    now = datetime.now(UTC)
                    delta = (
                        now - last_run.replace(tzinfo=UTC)
                        if last_run.tzinfo is None
                        else now - last_run
                    )
                    hours = delta.total_seconds() / 3600
                    run_str = f" ({hours:.1f}h ago)"
                else:
                    run_str = f" (at {last_run})"

            errors = schedule.get("consecutive_errors", 0)
            err_str = f" [{errors} errors]" if errors else ""

            lines.append(f"{peer_id}: {status}{run_str}{err_str}")
        except Exception as e:
            logger.debug("Failed to get peer schedule for %s: %s", peer_id, e)

    return "\n".join(lines) if len(lines) > 1 else ""


# ── Built-in context hooks (always active) ────────────────────────


_holidays_cache: dict[int, Any] = {}


def _get_us_holidays(year: int) -> Any:
    """Get cached US holidays object for a given year."""
    if year not in _holidays_cache:
        import holidays

        _holidays_cache[year] = holidays.US(years=year)
    return _holidays_cache[year]


def _date_context() -> str | None:
    """Current date, day of week, and upcoming US holidays."""
    from datetime import UTC, datetime, timedelta

    today = datetime.now(tz=UTC).date()
    day_name = today.strftime("%A")
    date_str = today.strftime("%Y-%m-%d")
    result = f"Today: {day_name}, {date_str}"

    # Check for upcoming US holidays (next 7 days)
    try:
        us_holidays = _get_us_holidays(today.year)
        upcoming = []
        for delta in range(8):
            check = today + timedelta(days=delta)
            if check in us_holidays:
                name = us_holidays[check]
                if delta == 0:
                    upcoming.append(f"Today is {name}")
                elif delta == 1:
                    upcoming.append(f"Tomorrow is {name}")
                else:
                    upcoming.append(f"{name} in {delta} days ({check.strftime('%a %b %d')})")
        if upcoming:
            result += "\n" + "; ".join(upcoming)
    except ImportError:
        pass  # holidays package not installed — skip

    return result


def _travel_status() -> str | None:
    """Read travel_status memory block if non-empty."""
    try:
        from robothor.memory.blocks import read_block

        _tid = os.environ.get("ROBOTHOR_TENANT_ID", "") or DEFAULT_TENANT
        result = read_block("travel_status", tenant_id=_tid)
        content = (
            result.get("content", "") if isinstance(result, dict) else str(result) if result else ""
        )
        if content and content.strip():
            return f"Travel: {content.strip()[:200]}"
    except Exception:
        pass
    return None


def _weather_context() -> str | None:
    """Read weather status file if present."""
    try:
        _ws = Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
        weather_file = _ws / "brain" / "memory" / "weather-status.md"
        if weather_file.exists():
            content = weather_file.read_text().strip()
            if content:
                return f"Weather: {content[:200]}"
    except Exception:
        pass
    return None


def _run_agent_context_hooks(config: AgentConfig) -> str:
    """Run agent-aware context hooks, collecting results."""
    results: list[str] = []
    for hook in _AGENT_CONTEXT_HOOKS:
        try:
            start = time.monotonic()
            result = hook(config)
            elapsed = time.monotonic() - start
            if elapsed > 0.1:
                logger.debug("Agent hook %s took %.0fms", hook.__name__, elapsed * 1000)
            if result:
                results.append(result)
        except Exception as e:
            logger.debug("Agent context hook %s failed: %s", hook.__name__, e)
    return "\n".join(results) if results else ""


def _git_status_context(config: AgentConfig) -> str | None:
    """Git repo status for agents with git tools."""
    from robothor.engine.tools.constants import GIT_TOOLS

    agent_tools = set(config.tools_allowed) if config.tools_allowed else set()
    if not agent_tools & GIT_TOOLS:
        return None

    import subprocess

    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
    parts: list[str] = []
    try:
        status = subprocess.run(
            ["git", "status", "--short", "--branch"],
            capture_output=True,
            text=True,
            timeout=0.08,
            cwd=str(workspace),
        )
        if status.stdout.strip():
            parts.append(f"Branch & status:\n{status.stdout.strip()}")
    except Exception:
        pass
    try:
        log = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True,
            text=True,
            timeout=0.08,
            cwd=str(workspace),
        )
        if log.stdout.strip():
            parts.append(f"Recent commits:\n{log.stdout.strip()}")
    except Exception:
        pass
    return "Git:\n" + "\n".join(parts) if parts else None


# Register built-in hooks on import
register_context_hook(_date_context)
register_context_hook(_travel_status)
register_context_hook(_weather_context)
register_agent_context_hook(_git_status_context)

from robothor.engine.thread_pool import _thread_pool_context  # noqa: E402

register_agent_context_hook(_thread_pool_context)
