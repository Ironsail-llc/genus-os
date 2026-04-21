"""Engine-level post-run summary log for cross-session visibility.

Some agents (notably ``main``) run in multiple isolated sessions — heartbeat
(cron) and Telegram — that share no conversation history. Without a shared
surface, a heartbeat run has no way to know what a concurrent Telegram run just
did minutes earlier, which has produced duplicated work in the past (e.g. two
separate bursts of Q2 meeting invites within 50 minutes on 2026-04-19).

This module writes a durable, ring-buffered "what main just did" log file that
heartbeat warmup surfaces via ``context_files`` so the next run sees it.

The write is performed by the engine (not the agent), so it cannot be skipped
by prompt drift.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Agents this log covers. Only agents with cross-session isolation benefit.
_TRACKED_AGENT_IDS: set[str] = {"main"}

# Tools whose calls are worth summarizing.
_NOTABLE_TOOLS: set[str] = {
    "gws_calendar_create",
    "gws_calendar_delete",
    "gws_gmail_send",
    "gws_gmail_reply",
    "create_task",
    "update_task",
    "resolve_task",
    "spawn_agent",
    "spawn_agents",
}

# Ring-buffer window. Newest entries at top; oldest trimmed.
_MAX_ENTRIES = 20

# Path is instance-scoped (brain/ is gitignored, per root CLAUDE.md).
_RELATIVE_PATH = "brain/memory/main-recent-actions.md"


def _workspace_root() -> Path:
    env = os.environ.get("ROBOTHOR_WORKSPACE")
    if env:
        return Path(env).expanduser()
    return Path.home() / "robothor"


def log_path() -> Path:
    """Resolve the recent-actions log path."""
    return _workspace_root() / _RELATIVE_PATH


def _summarize_step(step: Any) -> str | None:
    """One-clause description of a notable tool call, or None if not notable."""
    name = getattr(step, "tool_name", None)
    if name not in _NOTABLE_TOOLS:
        return None
    args = getattr(step, "tool_input", None) or {}
    out = getattr(step, "tool_output", None) or {}

    if name == "gws_calendar_create":
        if isinstance(out, dict) and out.get("status") == "deduped":
            return f"calendar_deduped: {out.get('summary') or args.get('summary', '?')}"
        summary = args.get("summary", "?")
        start = args.get("start", "?")
        return f"calendar_create: {summary!r} @ {start}"
    if name == "gws_calendar_delete":
        return f"calendar_delete: event={args.get('event_id', '?')}"
    if name in ("gws_gmail_send", "gws_gmail_reply"):
        thread = args.get("thread_id") or (out.get("threadId") if isinstance(out, dict) else "")
        to = args.get("to", "?")
        subj = (args.get("subject") or "").strip() or "(no subject)"
        return f"email_{name.split('_')[-1]}: to={to} subj={subj!r} thread={thread or '-'}"
    if name == "create_task":
        title = (args.get("title") or "").strip()
        return f"create_task: {title!r}"
    if name == "update_task":
        return f"update_task: id={args.get('id', '?')} status={args.get('status', '-')}"
    if name == "resolve_task":
        return f"resolve_task: id={args.get('id', '?')}"
    if name in ("spawn_agent", "spawn_agents"):
        child = args.get("agent_id") or "?"
        return f"{name}: child={child}"
    return None


def summarize_run(run: Any) -> str:
    """Compact one-line summary of a finished run's notable actions.

    Format: ``<iso-utc> [<trigger>] <clause>; <clause>; …``
    If no notable actions occurred, returns an empty string (caller skips write).
    """
    from datetime import datetime

    steps = getattr(run, "steps", []) or []
    clauses: list[str] = []
    for s in steps:
        c = _summarize_step(s)
        if c:
            clauses.append(c)
    if not clauses:
        return ""

    trigger = getattr(run, "trigger_type", "") or ""
    if hasattr(trigger, "value"):
        trigger = trigger.value
    ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%MZ")
    return f"{ts} [{trigger}] " + " · ".join(clauses)


def _read_existing_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except Exception as e:
        logger.debug("recent_actions: read failed for %s: %s", path, e)
        return []
    # Skip header line(s) and blanks; keep content lines only.
    return [
        line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]


def record_run(run: Any, agent_config: Any = None) -> None:
    """Append this run's summary to the log, newest-first, ring-buffered.

    No-op if the agent isn't tracked, the run had nothing notable, or the
    path can't be written. Engine never raises on logging failure.
    """
    agent_id = getattr(run, "agent_id", "")
    if agent_id not in _TRACKED_AGENT_IDS:
        return

    line = summarize_run(run)
    if not line:
        return

    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = _read_existing_lines(path)
        new_lines = [line] + existing
        new_lines = new_lines[:_MAX_ENTRIES]
        header = (
            "# main-recent-actions.md — engine-written, do not edit by hand\n"
            "# Newest first. Ring buffer of last "
            f"{_MAX_ENTRIES} notable actions across all main sessions.\n"
        )
        path.write_text(header + "\n".join(new_lines) + "\n")
    except Exception as e:
        logger.warning("recent_actions: write failed: %s", e)
