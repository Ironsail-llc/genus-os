"""
Cross-run persistent journal for long-running research agents.

The journal bridges consecutive cron runs of the same agent, allowing
a multi-session experiment to resume exactly where it left off.

Unlike checkpoint.py (which saves message history keyed by run_id),
the journal stores the agent's *intent state*: what experiment is in
progress, what iteration we're on, what to do when we wake up next.

Usage (agent side):
    At startup:  journal is injected as message preamble by runner.py
    At end of run: agent calls write_file("brain/journals/X/current.json", ...)
    On resolve_task: agent calls write_file with "{}" to clear

Usage (engine side):
    journal_state = JournalManager.load(agent_id, journal_path, workspace)
    if journal_state:
        preamble = JournalManager.format_resume_preamble(journal_state)
        message = preamble + "\\n\\n" + message
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 — used at runtime for file ops
from typing import Any

from robothor.engine.sanitize import sanitize_log as _sanitize

logger = logging.getLogger(__name__)

# Max journal file size — anything larger is treated as corrupted / unbounded growth
JOURNAL_MAX_BYTES = 50 * 1024  # 50 KB

# Valid next_action states — validated on load so agents can't inject arbitrary values
VALID_NEXT_ACTIONS = frozenset(
    {
        "MEASURE_BASELINE",
        "HYPOTHESIZE",
        "MEASURE_BEFORE",
        "APPLY_CHANGE",
        "MEASURE_AFTER",
        "COMMIT",
        "ANNOUNCE",
        "RESOLVE",
        "IDLE",
    }
)


@dataclass
class JournalState:
    """Cross-run intent state for a long-running research agent.

    This is intentionally lightweight — it's the agent's "where was I?"
    note, not a full conversation replay.
    """

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    agent_id: str = ""
    experiment_id: str = ""
    iteration: int = 0
    hypothesis: str = ""
    findings_so_far: str = ""
    next_action: str = "MEASURE_BASELINE"
    last_written_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    extra: dict[str, Any] = field(default_factory=dict)  # agent-defined overflow fields


class JournalManager:
    """Static helpers for loading, saving, and clearing the cross-run journal."""

    @staticmethod
    def load(agent_id: str, journal_path: str, workspace: Path) -> JournalState | None:
        """Load journal state from disk.

        Returns None (fresh start) if:
        - The file doesn't exist
        - The file is empty or contains only "{}"
        - The file is larger than JOURNAL_MAX_BYTES (size guardrail)
        - The file fails to parse as valid JSON
        - The session_id is missing (corrupted write)
        - The next_action value is not in VALID_NEXT_ACTIONS
        - The agent_id in the journal doesn't match (stale from a different agent)
        """
        # Validate agent_id to prevent path traversal
        import re

        if not re.fullmatch(r"[a-zA-Z0-9_-]+", agent_id):
            logger.error("Invalid agent_id in JournalManager.load: %s", _sanitize(agent_id))
            return None

        try:
            full_path = (workspace / journal_path).resolve()
            workspace_resolved = workspace.resolve()

            # Path traversal guard
            if not str(full_path).startswith(str(workspace_resolved)):
                logger.error("Journal path traversal blocked: %s", _sanitize(journal_path))
                return None

            if not full_path.exists():
                return None

            size = full_path.stat().st_size
            if size == 0:
                return None

            if size > JOURNAL_MAX_BYTES:
                logger.warning(
                    "Journal file %s is %d bytes (limit %d) — treating as corrupted, fresh start",
                    _sanitize(journal_path),
                    size,
                    JOURNAL_MAX_BYTES,
                )
                return None

            raw = full_path.read_text(encoding="utf-8").strip()
            if not raw or raw in ("{}", "null"):
                return None

            data = json.loads(raw)
            if not isinstance(data, dict) or not data:
                return None

            # Session ID is required — a missing one means a partial write
            if not data.get("session_id"):
                logger.warning(
                    "Journal %s missing session_id — discarding (partial write?)",
                    _sanitize(journal_path),
                )
                return None

            # Validate agent_id matches to catch stale journals
            journal_agent = data.get("agent_id", "")
            if journal_agent and journal_agent != agent_id:
                logger.warning(
                    "Journal agent_id mismatch: expected %s, got %s — discarding",
                    _sanitize(agent_id),
                    _sanitize(journal_agent),
                )
                return None

            # Validate next_action
            next_action = data.get("next_action", "MEASURE_BASELINE")
            if next_action not in VALID_NEXT_ACTIONS:
                logger.warning(
                    "Journal %s has invalid next_action %r — discarding",
                    _sanitize(journal_path),
                    _sanitize(str(next_action)),
                )
                return None

            state = JournalState(
                session_id=data["session_id"],
                agent_id=data.get("agent_id", agent_id),
                experiment_id=data.get("experiment_id", ""),
                iteration=int(data.get("iteration", 0)),
                hypothesis=data.get("hypothesis", ""),
                findings_so_far=data.get("findings_so_far", ""),
                next_action=next_action,
                last_written_at=data.get("last_written_at", ""),
                extra={
                    k: v
                    for k, v in data.items()
                    if k
                    not in {
                        "session_id",
                        "agent_id",
                        "experiment_id",
                        "iteration",
                        "hypothesis",
                        "findings_so_far",
                        "next_action",
                        "last_written_at",
                    }
                },
            )
            logger.info(
                "Journal loaded for %s: experiment=%s iteration=%d next_action=%s",
                _sanitize(agent_id),
                _sanitize(state.experiment_id),
                state.iteration,
                state.next_action,
            )
            return state

        except json.JSONDecodeError as e:
            logger.warning(
                "Journal %s failed JSON parse (%s) — fresh start",
                _sanitize(journal_path),
                _sanitize(e),
            )
            return None
        except Exception as e:
            logger.warning(
                "Journal load failed for %s: %s — fresh start",
                _sanitize(journal_path),
                _sanitize(e),
            )
            return None

    @staticmethod
    def save(state: JournalState, journal_path: str, workspace: Path) -> bool:
        """Atomically save journal state to disk using tmp+rename pattern.

        Returns True on success, False on failure (non-fatal — the run continues).
        Agents can also call write_file directly; this helper is used by the engine.
        """
        try:
            full_path = (workspace / journal_path).resolve()
            workspace_resolved = workspace.resolve()

            if not str(full_path).startswith(str(workspace_resolved)):
                logger.error("Journal save path traversal blocked: %s", _sanitize(journal_path))
                return False

            full_path.parent.mkdir(parents=True, exist_ok=True)
            state.last_written_at = datetime.now(UTC).isoformat()

            payload = asdict(state)
            json_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

            if len(json_bytes) > JOURNAL_MAX_BYTES:
                logger.warning(
                    "Journal state for %s would exceed %d bytes (%d) — truncating findings_so_far",
                    _sanitize(state.agent_id),
                    JOURNAL_MAX_BYTES,
                    len(json_bytes),
                )
                # Trim findings_so_far to fit
                state.findings_so_far = state.findings_so_far[:2000] + "\n[truncated]"
                payload = asdict(state)
                json_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")

            # Atomic write: tmp file → rename
            tmp_path = full_path.with_suffix(".json.tmp")
            tmp_path.write_bytes(json_bytes)
            tmp_path.rename(full_path)

            logger.debug(
                "Journal saved for %s at %s (%d bytes)",
                _sanitize(state.agent_id),
                _sanitize(journal_path),
                len(json_bytes),
            )
            return True

        except Exception as e:
            logger.warning(
                "Journal save failed for %s: %s",
                _sanitize(state.agent_id),
                _sanitize(e),
            )
            return False

    @staticmethod
    def clear(journal_path: str, workspace: Path) -> bool:
        """Clear the journal by writing an empty object.

        Called when the agent resolves its experiment (detect via resolve_task hook
        or agent writing '{}' to the journal path directly).
        """
        try:
            full_path = (workspace / journal_path).resolve()
            workspace_resolved = workspace.resolve()

            if not str(full_path).startswith(str(workspace_resolved)):
                logger.error("Journal clear path traversal blocked: %s", _sanitize(journal_path))
                return False

            if full_path.exists():
                tmp_path = full_path.with_suffix(".json.tmp")
                tmp_path.write_text("{}", encoding="utf-8")
                tmp_path.rename(full_path)
                logger.info("Journal cleared: %s", _sanitize(journal_path))
            return True
        except Exception as e:
            logger.warning("Journal clear failed: %s", _sanitize(e))
            return False

    @staticmethod
    def format_resume_preamble(state: JournalState) -> str:
        """Format journal state as a message preamble for the agent.

        This is injected before the agent's user message so it wakes up
        knowing exactly what it was working on and what to do next.
        """
        lines = [
            "## Session Resume — You Are Continuing a Multi-Session Experiment",
            "",
            f"**Session ID:** {state.session_id}",
            f"**Experiment:** {state.experiment_id or '(not set)'}",
            f"**Iteration:** {state.iteration}",
            f"**Next Action:** {state.next_action}",
            "",
        ]
        if state.hypothesis:
            lines += [
                "**Current Hypothesis:**",
                state.hypothesis,
                "",
            ]
        if state.findings_so_far:
            lines += [
                "**Findings So Far:**",
                state.findings_so_far,
                "",
            ]
        if state.last_written_at:
            lines += [
                f"**Last Active:** {state.last_written_at}",
                "",
            ]
        lines += [
            "---",
            "",
            "**Resume Instructions:**",
            f"- Your next action is `{state.next_action}` — go directly to that step.",
            "- Do NOT re-establish baseline if it is already set.",
            "- Do NOT re-read learnings you already incorporated (check experiment_status).",
            "- Write your updated state back to the journal via write_file at end of run.",
            "- When you call resolve_task, also write `{}` to the journal file to clear it.",
            "",
        ]
        # Append any extra agent-defined fields
        if state.extra:
            lines.append("**Additional Context:**")
            for k, v in state.extra.items():
                lines.append(f"- {k}: {v}")
            lines.append("")

        return "\n".join(lines)
