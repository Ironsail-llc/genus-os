"""Persistent session goals — operator objectives that survive across runs.

A session goal is a long-running operator objective. Storage: a `crm_tasks`
row tagged `session_goal` (workspace-scoped) or `session_goal` plus
`agent:<id>` (agent-scoped). Structured payload (success criteria, typed
evidence, completion note) lives in the `session_goal_meta` JSONB column
added in migration 065.

This module is intentionally separate from ``robothor.engine.goals``: the
existing goals module scores agents against benchmark suites; session goals
capture the operator's active objective and the evidence required before an
agent may call the work complete.

Key contracts
-------------
- The completion guard is real: at least one valid `test_run` evidence
  (matching `pytest:(passed|failed):\\d+` or a UUID) AND at least one valid
  `commit` evidence (validated via `git cat-file -e`).
- Auto-injection is owner-only: workspace goals inject only into the `main`
  agent; agent-scoped goals inject only into the named agent. Workers
  (delivery: none) never see other agents' goals.
- `brain/GOAL.md` is a denormalized read-cache, regenerated whenever the
  underlying task mutates. Hand-edits are advisory only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from robothor.constants import DEFAULT_TENANT
from robothor.crm import dal

logger = logging.getLogger(__name__)

OWNER_AGENT_ID = "main"

EvidenceKind = Literal["test_run", "commit", "ci_run", "note"]

_VALID_KINDS: frozenset[str] = frozenset({"test_run", "commit", "ci_run", "note"})

_PYTEST_REF_RE = re.compile(r"^pytest:(passed|failed):\d+$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SHA_RE = re.compile(r"^[0-9a-f]{7,}$", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GoalEvidence:
    kind: str
    summary: str
    reference: str = ""
    recorded_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    valid: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "summary": self.summary,
            "reference": self.reference,
            "recorded_at": self.recorded_at,
            "valid": self.valid,
        }


@dataclass(frozen=True)
class SessionGoal:
    id: str
    objective: str
    success_criteria: list[str]
    agent_id: str = ""
    status: str = "active"  # active | complete
    evidence: list[GoalEvidence] = field(default_factory=list)
    completion_note: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"


# ─────────────────────────────────────────────────────────────────────────────
# Workspace resolution
# ─────────────────────────────────────────────────────────────────────────────


def _resolve_workspace(workspace: str | Path | None = None) -> Path:
    """Return the canonical workspace path.

    Resolution order:
      1. Explicit ``workspace`` argument when truthy.
      2. ``ROBOTHOR_WORKSPACE`` environment variable.
      3. ``~/robothor`` fallback.
    """
    if workspace:
        return Path(workspace).expanduser().resolve()
    env_path = os.environ.get("ROBOTHOR_WORKSPACE")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return (Path.home() / "robothor").resolve()


# ─────────────────────────────────────────────────────────────────────────────
# Evidence validation
# ─────────────────────────────────────────────────────────────────────────────


def validate_evidence(
    item: GoalEvidence,
    *,
    workspace: str | Path | None = None,
) -> tuple[bool, str]:
    """Per-kind validation. Returns (is_valid, reason_if_invalid)."""
    kind = (item.kind or "").strip()
    reference = (item.reference or "").strip()
    summary = (item.summary or "").strip()
    if kind not in _VALID_KINDS:
        return False, f"unknown evidence kind: {kind!r}"
    if not summary:
        return False, "summary is required"

    if kind == "note":
        return True, ""

    if kind == "test_run":
        if _PYTEST_REF_RE.match(reference):
            return True, ""
        if _UUID_RE.match(reference):
            return True, ""
        return (
            False,
            "test_run reference must match 'pytest:(passed|failed):N' or be a UUID",
        )

    if kind == "commit":
        if not _SHA_RE.match(reference):
            return False, "commit reference must be 7+ hex chars"
        ws = _resolve_workspace(workspace)
        try:
            result = subprocess.run(
                ["git", "cat-file", "-e", reference],
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return False, f"git verification failed: {exc!r}"
        if result.returncode != 0:
            return False, f"git rejected commit reference {reference}"
        return True, ""

    if kind == "ci_run":
        if reference.startswith("https://"):
            return True, ""
        return False, "ci_run reference must be an https:// URL"

    return False, f"unhandled evidence kind: {kind!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Completion guard
# ─────────────────────────────────────────────────────────────────────────────


def missing_completion_requirements(
    goal: SessionGoal,
    *,
    workspace: str | Path | None = None,
) -> list[str]:
    """Return a list of reasons this goal cannot be completed yet.

    Empty list means: ready to complete. The contract is structured:
      - At least one VALID `test_run` evidence AND
      - At least one VALID `commit` evidence.
    """
    missing: list[str] = []
    if not goal.success_criteria:
        missing.append("no success criteria recorded")
    if not goal.evidence:
        missing.append("no evidence recorded")
        return missing

    has_test, has_commit = False, False
    for item in goal.evidence:
        if item.kind == "test_run":
            ok, _ = validate_evidence(item, workspace=workspace)
            if ok:
                has_test = True
        elif item.kind == "commit":
            ok, _ = validate_evidence(item, workspace=workspace)
            if ok:
                has_commit = True
    if not has_test:
        missing.append("no valid test_run evidence (need pytest summary or run UUID)")
    if not has_commit:
        missing.append("no valid commit evidence (need a SHA git resolves)")
    return missing


# ─────────────────────────────────────────────────────────────────────────────
# Row → dataclass
# ─────────────────────────────────────────────────────────────────────────────


def _agent_id_from_tags(tags: list[str]) -> str:
    for t in tags or []:
        if isinstance(t, str) and t.startswith("agent:"):
            return t.split(":", 1)[1]
    return ""


def _goal_from_row(row: dict[str, Any]) -> SessionGoal:
    meta = row.get("session_goal_meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    evidence = [
        GoalEvidence(
            kind=str(raw.get("kind", "note")),
            summary=str(raw.get("summary", "")),
            reference=str(raw.get("reference", "")),
            recorded_at=str(raw.get("recorded_at", "")) or datetime.now(UTC).isoformat(),
            valid=bool(raw.get("valid", True)),
        )
        for raw in (meta.get("evidence") or [])
        if isinstance(raw, dict)
    ]
    status = "complete" if (row.get("status") == "DONE") else "active"
    return SessionGoal(
        id=str(row.get("id") or f"task-{uuid.uuid4().hex[:12]}"),
        objective=str(row.get("objective") or "").strip(),
        success_criteria=list(meta.get("success_criteria") or []),
        agent_id=_agent_id_from_tags(row.get("tags") or []),
        status=status,
        evidence=evidence,
        completion_note=str(meta.get("completion_note") or ""),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lifecycle — DAL-backed
# ─────────────────────────────────────────────────────────────────────────────


def create_active_goal(
    *,
    tenant_id: str = DEFAULT_TENANT,
    objective: str,
    criteria: list[str] | None = None,
    agent_id: str = "",
) -> SessionGoal:
    """Create a session goal. Refuses if one is already active for the scope."""
    objective = (objective or "").strip()
    if not objective:
        raise ValueError("goal objective cannot be empty")
    cleaned_criteria = [c.strip() for c in (criteria or default_success_criteria(objective))]
    cleaned_criteria = [c for c in cleaned_criteria if c]
    if not cleaned_criteria:
        raise ValueError("goal must have at least one success criterion")

    existing = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if existing:
        raise ValueError("active goal already exists")

    task_id = dal.create_session_goal(
        tenant_id=tenant_id,
        objective=objective,
        success_criteria=cleaned_criteria,
        agent_id=agent_id,
    )
    if not task_id:
        raise RuntimeError("failed to create session goal task")

    return SessionGoal(
        id=task_id,
        objective=objective,
        success_criteria=cleaned_criteria,
        agent_id=agent_id,
        status="active",
        evidence=[],
        completion_note="",
    )


def add_evidence(
    *,
    tenant_id: str = DEFAULT_TENANT,
    kind: str,
    summary: str,
    reference: str = "",
    agent_id: str = "",
    workspace: str | Path | None = None,
) -> SessionGoal:
    """Append evidence to the active goal. Validates per-kind, records validity flag."""
    row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if not row:
        raise ValueError("no active goal")
    item = GoalEvidence(kind=kind, summary=summary, reference=reference)
    is_valid, reason = validate_evidence(item, workspace=workspace)
    ok = dal.add_session_goal_evidence(
        task_id=str(row["id"]),
        kind=kind,
        summary=summary,
        reference=reference,
        valid=is_valid,
        tenant_id=tenant_id,
    )
    if not ok:
        raise RuntimeError("failed to append evidence")
    if not is_valid:
        logger.warning("session goal evidence recorded but invalid: %s", reason)
    refreshed = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    return _goal_from_row(refreshed) if refreshed else _goal_from_row(row)


def complete_goal(
    *,
    tenant_id: str = DEFAULT_TENANT,
    note: str,
    agent_id: str = "",
    workspace: str | Path | None = None,
) -> SessionGoal:
    """Mark the active goal complete. Refuses if completion guard fails."""
    row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if not row:
        raise ValueError("no active goal")
    goal = _goal_from_row(row)
    missing = missing_completion_requirements(goal, workspace=workspace)
    if missing:
        joined = "; ".join(missing)
        raise ValueError(f"goal is not ready to complete: {joined}")
    ok = dal.complete_session_goal(
        task_id=str(row["id"]),
        completion_note=note.strip(),
        tenant_id=tenant_id,
    )
    if not ok:
        raise RuntimeError("failed to complete session goal")
    return SessionGoal(
        id=goal.id,
        objective=goal.objective,
        success_criteria=goal.success_criteria,
        agent_id=goal.agent_id,
        status="complete",
        evidence=goal.evidence,
        completion_note=note.strip(),
    )


def get_active_goal(
    *,
    tenant_id: str = DEFAULT_TENANT,
    agent_id: str = "",
) -> SessionGoal | None:
    """Return the currently-active session goal for a scope, or None."""
    row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    return _goal_from_row(row) if row else None


def default_success_criteria(objective: str) -> list[str]:
    text = (objective or "").strip()
    criteria = [
        "The feature behaviour matches the requested objective.",
        "The implementation is covered by focused automated tests.",
        "Relevant tests have been run and their result is recorded as evidence.",
        "The goal is not marked complete until verification evidence satisfies every criterion.",
    ]
    if re.search(r"\blong\b|\bcontinue\b|\bacross turns\b|\bsame way\b", text, re.IGNORECASE):
        criteria.insert(
            1,
            "The goal remains active across runs so work can continue until explicitly completed.",
        )
    return criteria


# ─────────────────────────────────────────────────────────────────────────────
# Owner-only scoping for prompt injection
# ─────────────────────────────────────────────────────────────────────────────


def build_goal_context(
    *,
    tenant_id: str = DEFAULT_TENANT,
    agent_id: str,
) -> str:
    """Render the goal context block for an agent run, or '' if none applies.

    Resolution rules:
      1. Look up an agent-scoped goal (tag `agent:<agent_id>`). If present
         and active, render and return.
      2. If no agent-scoped goal AND the agent is the owner (`main`), look up
         the workspace goal (no `agent:*` tag) and render.
      3. Otherwise return '' — workers never see other agents' goals.
    """
    if agent_id:
        agent_row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
        if agent_row:
            return _render_context(_goal_from_row(agent_row))

    if agent_id == OWNER_AGENT_ID:
        workspace_row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id="")
        if workspace_row:
            return _render_context(_goal_from_row(workspace_row))

    return ""


def _render_context(goal: SessionGoal) -> str:
    if not goal or not goal.is_active:
        return ""
    return "--- ACTIVE SHORT-TERM GOAL ---\n" + format_goal_for_prompt(goal)


def format_goal_for_prompt(goal: SessionGoal) -> str:
    criteria = "\n".join(f"- {item}" for item in goal.success_criteria)
    valid_evidence = [e for e in goal.evidence[-5:] if e.valid]
    if valid_evidence:
        evidence = "\n".join(
            f"- {e.kind}: {e.summary}" + (f" [{e.reference}]" if e.reference else "")
            for e in valid_evidence
        )
    else:
        evidence = "- none yet"
    return (
        "[ACTIVE SESSION GOAL]\n"
        f"ID: {goal.id}\n"
        f"Agent: {goal.agent_id or 'workspace'}\n"
        f"Objective: {goal.objective}\n"
        "Success criteria:\n"
        f"{criteria}\n"
        "Recent evidence (validated):\n"
        f"{evidence}\n"
        "Completion requires at least one validated test_run AND one validated commit."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Denormalized read-cache: brain/GOAL.md
# ─────────────────────────────────────────────────────────────────────────────


def regenerate_goal_md_cache(
    *,
    tenant_id: str = DEFAULT_TENANT,
    workspace: str | Path | None = None,
    agent_id: str = "",
) -> Path | None:
    """Rewrite ``brain/GOAL.md`` to mirror the active goal task.

    When no active goal exists, the cache is removed. The write is atomic:
    a tempfile in the same directory is written then ``os.replace`` swaps it
    in. Returns the cache path on write, None on removal/no-op.
    """
    ws = _resolve_workspace(workspace)
    target = ws / "brain" / ("GOAL.md" if not agent_id else f"goals/{_safe_agent_id(agent_id)}.md")

    row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if not row:
        if target.exists():
            try:
                target.unlink()
            except OSError as exc:
                logger.warning("failed to remove stale goal cache %s: %s", target, exc)
        return None

    goal = _goal_from_row(row)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = _render_cache_markdown(goal, tenant_id=tenant_id)

    fd, tmp_path = tempfile.mkstemp(prefix=".GOAL.md.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        Path(tmp_path).replace(target)
    except Exception:
        with contextlib.suppress(OSError):
            Path(tmp_path).unlink()
        raise
    return target


def _render_cache_markdown(goal: SessionGoal, *, tenant_id: str) -> str:
    criteria = "\n".join(f"- {c}" for c in goal.success_criteria) or "- none"
    if goal.evidence:
        evidence_lines = []
        for e in goal.evidence:
            tag = "" if e.valid else "  (UNVERIFIED)"
            ref = f" [{e.reference}]" if e.reference else ""
            evidence_lines.append(f"- {e.kind}: {e.summary}{ref}{tag}")
        evidence = "\n".join(evidence_lines)
    else:
        evidence = "- none yet"

    return (
        "# Active Goal (read-cache)\n\n"
        f"task_id: {goal.id}\n"
        f"agent_id: {goal.agent_id}\n"
        f"tenant_id: {tenant_id}\n"
        f"status: {goal.status}\n\n"
        "## Objective\n"
        f"{goal.objective}\n\n"
        "## Success Criteria\n"
        f"{criteria}\n\n"
        "## Evidence\n"
        f"{evidence}\n\n"
        "## Completion Note\n"
        f"{goal.completion_note}\n\n"
        "## Notes\n"
        "This file is regenerated from the underlying crm_task. Hand-edits will be\n"
        "overwritten on the next mutation. Use `robothor goal …` (CLI), `/goal …`\n"
        "(Telegram), or the create_goal/get_goal/update_goal tools.\n"
    )


def _safe_agent_id(agent_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", agent_id.strip()).strip(".-")
    if not safe:
        raise ValueError("agent_id cannot be empty")
    return safe


# ─────────────────────────────────────────────────────────────────────────────
# CLI / API summary
# ─────────────────────────────────────────────────────────────────────────────


def summarize_goal(
    goal: SessionGoal,
    *,
    workspace: str | Path | None = None,
) -> dict[str, Any]:
    return {
        "id": goal.id,
        "objective": goal.objective,
        "agent_id": goal.agent_id,
        "status": goal.status,
        "success_criteria": goal.success_criteria,
        "evidence_count": len(goal.evidence),
        "valid_evidence_count": sum(1 for e in goal.evidence if e.valid),
        "missing_completion_requirements": missing_completion_requirements(
            goal, workspace=workspace
        ),
        "completion_note": goal.completion_note,
    }
