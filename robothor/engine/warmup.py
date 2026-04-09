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
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from robothor.constants import DEFAULT_TENANT

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)

MAX_WARMTH_CHARS = 4000
MAX_BLOCK_CHARS = 800
MAX_FILE_CHARS = 600

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

    # 1. Session history
    try:
        history = _build_history_section(config.id)
        if history:
            sections.append(history)
    except Exception as e:
        logger.debug("Warmup history failed for %s: %s", config.id, e)

    # 2. Memory blocks
    try:
        blocks = _build_memory_blocks_section(config.warmup_memory_blocks, tenant_id=tenant_id)
        if blocks:
            sections.append(blocks)
    except Exception as e:
        logger.debug("Warmup memory blocks failed for %s: %s", config.id, e)

    # 3. Context files
    try:
        files = _build_context_files_section(config.warmup_context_files, workspace)
        if files:
            sections.append(files)
    except Exception as e:
        logger.debug("Warmup context files failed for %s: %s", config.id, e)

    # 4. Peer agent status
    try:
        peers = _build_peer_section(config.warmup_peer_agents)
        if peers:
            sections.append(peers)
    except Exception as e:
        logger.debug("Warmup peer status failed for %s: %s", config.id, e)

    # 5. Dynamic context hooks (date, travel, weather, etc.)
    try:
        situational = _run_context_hooks()
        if situational:
            sections.append(situational)
    except Exception as e:
        logger.debug("Warmup context hooks failed for %s: %s", config.id, e)

    # 6. Agent-aware context hooks (git status, etc.)
    try:
        agent_ctx = _run_agent_context_hooks(config)
        if agent_ctx:
            sections.append(agent_ctx)
    except Exception as e:
        logger.debug("Warmup agent hooks failed for %s: %s", config.id, e)

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


def build_interactive_preamble(
    agent_id: str,
    user_message: str = "",
    include_blocks: bool = True,
    tenant_id: str = DEFAULT_TENANT,
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

    Returns:
        Warmup preamble string, or empty string if nothing to inject.
    """
    sections: list[str] = []

    # Core memory blocks — only for new sessions (no prior history)
    if include_blocks:
        core_blocks = ["persona", "user_profile", "user_model", "working_context"]
        try:
            blocks_section = _build_memory_blocks_section(core_blocks, tenant_id=tenant_id)
            if blocks_section:
                sections.append(blocks_section)
        except Exception as e:
            logger.debug("Interactive warmup blocks failed: %s", e)

    # Entity-aware context — if user mentions a name, pull relevant facts
    if user_message and len(user_message) > 5:
        try:
            context = _build_entity_context(user_message, tenant_id=tenant_id)
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

    if not sections:
        return ""

    preamble = "\n\n".join(sections)
    if len(preamble) > MAX_WARMTH_CHARS:
        preamble = preamble[:MAX_WARMTH_CHARS] + "\n[warmup truncated]"
    return preamble


MAX_ENTITY_CONTEXT_CHARS = 1000


def _build_entity_context(user_message: str, tenant_id: str = DEFAULT_TENANT) -> str:
    """Extract entities from user message and pull relevant facts.

    Looks for capitalized proper nouns in the message and searches
    memory facts for matching entity references.

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


def _buddy_status_context(config: AgentConfig) -> str | None:
    """Inject buddy gamification status into main agent warmup."""
    if config.id != "main":
        return None
    try:
        from robothor.memory.blocks import read_block

        _tid = os.environ.get("ROBOTHOR_TENANT_ID", "") or DEFAULT_TENANT
        result = read_block("buddy_status", tenant_id=_tid)
        content = result.get("content", "") if isinstance(result, dict) else ""
        if content and content.strip():
            return f"[BUDDY] {content.strip()}"
    except Exception:
        pass
    return None


# Register built-in hooks on import
register_context_hook(_date_context)
register_context_hook(_travel_status)
register_context_hook(_weather_context)
register_agent_context_hook(_git_status_context)
register_agent_context_hook(_buddy_status_context)
