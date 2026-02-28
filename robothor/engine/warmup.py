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
from datetime import UTC, datetime
from pathlib import Path

from robothor.engine.models import AgentConfig

logger = logging.getLogger(__name__)

MAX_WARMTH_CHARS = 4000
MAX_BLOCK_CHARS = 800
MAX_FILE_CHARS = 600


def build_warmth_preamble(
    config: AgentConfig,
    workspace: Path,
    tenant_id: str = "robothor-primary",
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
        blocks = _build_memory_blocks_section(config.warmup_memory_blocks)
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


def _build_memory_blocks_section(block_names: list[str]) -> str:
    """Read memory blocks and format them."""
    if not block_names:
        return ""

    from robothor.memory.blocks import read_block

    lines = ["--- MEMORY BLOCKS ---"]
    for name in block_names:
        try:
            result = read_block(name)
            content = (
                result.get("content", "")
                if isinstance(result, dict)
                else str(result)
                if result
                else ""
            )
            if content:
                truncated = content[:MAX_BLOCK_CHARS]
                if len(content) > MAX_BLOCK_CHARS:
                    truncated += "..."
                lines.append(f"[{name}]\n{truncated}")
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
            lines.append(f"[{rel_path}]\n{truncated}")
        except Exception as e:
            logger.debug("Failed to read context file %s: %s", rel_path, e)

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
