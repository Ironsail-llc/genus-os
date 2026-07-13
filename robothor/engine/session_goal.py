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

EvidenceKind = Literal["test_run", "commit", "ci_run", "note", "tool_output", "benchmark_run"]

_VALID_KINDS: frozenset[str] = frozenset(
    {"test_run", "commit", "ci_run", "note", "tool_output", "benchmark_run"}
)

_PYTEST_REF_RE = re.compile(r"^pytest:(passed|failed):\d+$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SHA_RE = re.compile(r"^[0-9a-f]{7,}$", re.IGNORECASE)
# tool_output reference: "<run_id>:<step_number>" — the agent_run_steps row
# a tool_output claim points at. run_id is a UUID; step_number is an int.
_TOOL_OUTPUT_REF_RE = re.compile(
    r"^(?P<run_id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(?P<step>\d+)$",
    re.IGNORECASE,
)


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
    tenant_id: str = DEFAULT_TENANT,
) -> tuple[bool, str]:
    """Per-kind validation. Returns (is_valid, reason_if_invalid).

    ``tenant_id`` scopes the DB-backed kinds (``tool_output``,
    ``benchmark_run``) so an agent can't validate evidence referencing
    another tenant's run step or benchmark row. The git/regex kinds ignore
    it.
    """
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

    if kind == "tool_output":
        match = _TOOL_OUTPUT_REF_RE.match(reference)
        if not match:
            return (
                False,
                "tool_output reference must be 'run_id:step_index' (UUID:int)",
            )
        try:
            exists = dal.run_step_exists(
                match.group("run_id"), int(match.group("step")), tenant_id=tenant_id
            )
        except Exception as exc:  # noqa: BLE001 — DAL/driver errors, not a valid ref
            return False, f"tool_output verification failed: {exc!r}"
        if not exists:
            return False, f"no agent_run_steps row for {reference}"
        return True, ""

    if kind == "benchmark_run":
        if not reference.isdigit():
            return (
                False,
                "benchmark_run reference must be a benchmark_results row id (integer)",
            )
        try:
            exists = dal.benchmark_result_exists(int(reference), tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001 — DAL/driver errors, not a valid ref
            return False, f"benchmark_run verification failed: {exc!r}"
        if not exists:
            return False, f"no benchmark_results row for id {reference}"
        return True, ""

    return False, f"unhandled evidence kind: {kind!r}"


# ─────────────────────────────────────────────────────────────────────────────
# Completion guard
# ─────────────────────────────────────────────────────────────────────────────


def missing_completion_requirements(
    goal: SessionGoal,
    *,
    workspace: str | Path | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> list[str]:
    """Return a list of reasons this goal cannot be completed yet.

    Empty list means: ready to complete. The contract is structured:
      - At least one VALID `test_run` evidence AND
      - At least one VALID `commit` evidence.

    ``tenant_id`` is forwarded to ``validate_evidence`` for the DB-backed
    kinds; the completion guard itself only checks test_run/commit today.
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
            ok, _ = validate_evidence(item, workspace=workspace, tenant_id=tenant_id)
            if ok:
                has_test = True
        elif item.kind == "commit":
            ok, _ = validate_evidence(item, workspace=workspace, tenant_id=tenant_id)
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
    is_valid, reason = validate_evidence(item, workspace=workspace, tenant_id=tenant_id)
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
    missing = missing_completion_requirements(goal, workspace=workspace, tenant_id=tenant_id)
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

    # Loop closure (RIP 14): advance any standing intents this goal was
    # linked to. Best-effort — never block goal completion on it.
    try:
        from robothor.engine.feature_flags import is_rip_enabled

        if is_rip_enabled(14):
            from robothor.memory.intents import attribute_goal_completion

            attribute_goal_completion(int(row["id"]), tenant_id=tenant_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("intent attribution skipped: %s", e)

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


# ─────────────────────────────────────────────────────────────────────────────
# In-place edits — v2 unified model
# ─────────────────────────────────────────────────────────────────────────────


def edit_objective(
    *,
    tenant_id: str = DEFAULT_TENANT,
    agent_id: str,
    objective: str,
) -> SessionGoal:
    """Edit the goal's objective in place. Requires the goal task to exist."""
    objective = (objective or "").strip()
    if not objective:
        raise ValueError("objective cannot be empty")
    row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if not row:
        raise ValueError("no active goal — create one first with `goal set`")
    ok = dal.update_goal_objective(task_id=str(row["id"]), objective=objective, tenant_id=tenant_id)
    if not ok:
        raise RuntimeError("failed to update objective")
    refreshed = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    return _goal_from_row(refreshed) if refreshed else _goal_from_row(row)


def add_criterion(
    *,
    tenant_id: str = DEFAULT_TENANT,
    agent_id: str,
    text: str,
) -> SessionGoal:
    """Append a success criterion to the goal."""
    text = (text or "").strip()
    if not text:
        raise ValueError("criterion text cannot be empty")
    row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if not row:
        raise ValueError("no active goal")
    meta = row.get("session_goal_meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    criteria = list(meta.get("success_criteria") or [])
    criteria.append(text)
    ok = dal.update_goal_criteria(
        task_id=str(row["id"]),
        success_criteria=criteria,
        tenant_id=tenant_id,
    )
    if not ok:
        raise RuntimeError("failed to update criteria")
    refreshed = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    return _goal_from_row(refreshed) if refreshed else _goal_from_row(row)


def set_metric_target(
    *,
    tenant_id: str = DEFAULT_TENANT,
    agent_id: str,
    metric: str,
    target: str,
    weight: float = 1.0,
    window_days: int = 7,
    category: str = "correctness",
    target_id: str | None = None,
) -> SessionGoal:
    """Add or replace a metric target on the goal."""
    metric = (metric or "").strip()
    target = (target or "").strip()
    if not metric or not target:
        raise ValueError("metric and target are required")
    row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if not row:
        raise ValueError("no active goal")
    new_target = {
        "id": (target_id or metric).strip(),
        "category": category,
        "metric": metric,
        "target": target,
        "weight": float(weight),
        "window_days": int(window_days),
        "extras": {},
    }
    ok = dal.add_goal_metric_target(
        task_id=str(row["id"]),
        metric_target=new_target,
        tenant_id=tenant_id,
    )
    if not ok:
        raise RuntimeError("failed to add metric target")
    refreshed = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    return _goal_from_row(refreshed) if refreshed else _goal_from_row(row)


def remove_metric_target(
    *,
    tenant_id: str = DEFAULT_TENANT,
    agent_id: str,
    target_id: str,
) -> SessionGoal:
    """Remove a metric target from the goal by id."""
    if not target_id:
        raise ValueError("target_id required")
    row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if not row:
        raise ValueError("no active goal")
    ok = dal.remove_goal_metric_target(
        task_id=str(row["id"]),
        target_id=target_id,
        tenant_id=tenant_id,
    )
    if not ok:
        raise RuntimeError("failed to remove metric target")
    refreshed = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    return _goal_from_row(refreshed) if refreshed else _goal_from_row(row)


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

    Legacy (v1) owner-only scoping. New code should call
    ``build_agent_goal_context`` instead — every agent has its own goal
    in the v2 unified model.
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


def build_agent_goal_context(
    *,
    tenant_id: str = DEFAULT_TENANT,
    agent_id: str,
    manifest_path: str | None = None,
) -> str:
    """Render the unified per-agent goal block for warmup injection.

    Every agent sees its own goal — the v2 model gives each agent exactly
    one persistent goal task. The block carries:
      - the operator's objective (or a placeholder when never set)
      - success criteria
      - metric_targets the agent is being scored on
      - recent metric values (best-effort, swallows on failure)
      - alignment score (rolling 7d) when present

    Returns '' when no goal task exists (e.g. agent not yet seeded).
    """
    if not agent_id:
        return ""
    row = dal.get_active_session_goal(tenant_id=tenant_id, agent_id=agent_id)
    if not row:
        return ""
    goal = _goal_from_row(row)
    meta_raw = row.get("session_goal_meta") or {}
    metric_targets: list[dict[str, Any]] = []
    if isinstance(meta_raw, dict):
        targets = meta_raw.get("metric_targets") or []
        if isinstance(targets, list):
            metric_targets = [t for t in targets if isinstance(t, dict)]

    # Best-effort current metric values + alignment score.
    metrics: dict[str, Any] = {}
    alignment: float | None = None
    try:
        from robothor.engine.goals import (
            _get_session_goal_alignment_score,
            compute_goal_metrics,
        )

        metrics = compute_goal_metrics(agent_id=agent_id, tenant_id=tenant_id) or {}
        alignment = _get_session_goal_alignment_score(agent_id=agent_id, tenant_id=tenant_id)
    except Exception as exc:
        logger.debug("agent_goal warmup metrics lookup failed: %s", exc)

    return _render_agent_goal_context(
        goal=goal,
        metric_targets=metric_targets,
        current_metrics=metrics,
        alignment_score=alignment,
    )


def _render_agent_goal_context(
    *,
    goal: SessionGoal,
    metric_targets: list[dict[str, Any]],
    current_metrics: dict[str, Any],
    alignment_score: float | None,
) -> str:
    if not goal or not goal.is_active:
        return ""

    objective = goal.objective or "(no objective set yet — operator: edit me)"
    criteria_lines = "\n".join(f"- {c}" for c in goal.success_criteria) or "- none yet"

    target_lines = []
    for t in metric_targets:
        metric = t.get("metric") or "?"
        target = t.get("target") or "?"
        weight = t.get("weight", 1.0)
        current = current_metrics.get(metric)
        cur_str = f"{current}" if current is not None else "—"
        target_lines.append(f"- {metric} {target} (weight {weight}) — current: {cur_str}")
    targets_block = "\n".join(target_lines) or "- (no metric targets defined)"

    alignment_line = (
        f"Buddy alignment score (7d): {alignment_score:.2f}"
        if alignment_score is not None
        else "Buddy alignment score (7d): not yet measured"
    )

    return (
        "--- ACTIVE AGENT GOAL ---\n"
        f"Goal task: {goal.id}\n"
        f"Agent: {goal.agent_id or 'workspace'}\n"
        f"Objective: {objective}\n"
        "Success criteria:\n"
        f"{criteria_lines}\n"
        "Metric targets you're being scored on:\n"
        f"{targets_block}\n"
        f"{alignment_line}\n"
        "Buddy reviews each run for alignment with this objective. Drift triggers "
        "self-improvement tasks. Stay focused on the objective and the metric targets."
    )


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
    tenant_id: str = DEFAULT_TENANT,
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
            goal, workspace=workspace, tenant_id=tenant_id
        ),
        "completion_note": goal.completion_note,
    }
