"""Buddy Grader — closed-loop verification for self-improve tasks.

When `auto-agent` marks a `nightwatch+self-improve+<agent>+<metric>` task as
DONE, Buddy doesn't take the fix on faith. 48 hours later, the grader:

1. Parses the `buddy-baseline:` marker from the task body (the metric + target
   + baseline value Buddy wrote when opening the task).
2. Re-computes the metric with `compute_goal_metrics`.
3. If the metric hit target → tag `verified_resolved` and write a resolution note.
   Else → tag `verify_failed`, increment `escalation:N`, re-open the task.
4. At `escalation:2`, re-route the task to `auto-researcher`.
5. At `escalation:3`, tag `requires_human=true` and stop auto-escalating.

Separately, 7 days after a task is `verified_resolved`, the grader re-checks
the same metric. If it stuck → `held_7d=true`. If it regressed → `held_7d=false`.
The weekly `buddy-auditor` reads the `held_7d` rate to decide whether the
whole self-improvement pipeline should auto-pause (Phase 9).

All events are journaled to `brain/journals/buddy/YYYY-MM-DD.jsonl`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.goals import _evaluate_target, compute_goal_metrics

logger = logging.getLogger(__name__)


JOURNAL_DIR = Path(__file__).resolve().parent.parent.parent / "brain" / "journals" / "buddy"

VERIFICATION_DELAY_HOURS = 48
HOLD_CHECK_DAYS = 7
MAX_ESCALATIONS = 3  # At this level → requires_human


def _dryrun_enabled() -> bool:
    """Dry-run mode — computes verdicts but writes no tags, status, or notifications.

    Useful for operators to simulate 48h-verifications on demand: set
    ROBOTHOR_BUDDY_GRADER_DRYRUN=1 in the shell, call `run_verification_pass`,
    inspect returned outcomes without side effects.
    """
    return os.environ.get("ROBOTHOR_BUDDY_GRADER_DRYRUN", "").lower() in ("1", "true", "yes")


# Regex that extracts the machine-readable baseline marker Buddy embeds in
# every self-improve task body when it opens the finding.
_BASELINE_RE = re.compile(r"<!--\s*buddy-baseline:\s*(\{.*?\})\s*-->", re.DOTALL)

# Escalation tag format: "escalation:N" (N is an int).
_ESCALATION_RE = re.compile(r"^escalation:(\d+)$")

# Grader stamps a "verified_at:<iso>" companion tag when it marks a task
# `verified_resolved`. The 7-day hold check reads this rather than
# `task.updated_at`, which drifts if anything else touches the task.
_VERIFIED_AT_RE = re.compile(r"^verified_at:(.+)$")

# Default window when an in-flight task body predates the window_days key.
DEFAULT_GRADER_WINDOW_DAYS = 7


# ─── Dataclasses ──────────────────────────────────────────────────────


@dataclass
class VerificationOutcome:
    task_id: str
    agent_id: str
    metric: str
    target: str
    baseline: float | None
    current: float | None
    satisfied: bool
    escalation_level: int  # after this verification
    requires_human: bool
    new_tags_added: list[str]


# ─── Baseline parsing ────────────────────────────────────────────────


def parse_baseline_from_body(body: str) -> dict[str, Any] | None:
    """Extract the `<!-- buddy-baseline: {...} -->` JSON blob from a task body."""
    if not body:
        return None
    match = _BASELINE_RE.search(body)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as e:
        logger.warning("buddy-baseline JSON parse failed: %s", e)
        return None


def extract_escalation_level(tags: list[str] | None) -> int:
    """Return the max escalation:N in the tag list, or 0 if none present."""
    if not tags:
        return 0
    best = 0
    for t in tags:
        m = _ESCALATION_RE.match(str(t).strip())
        if m:
            best = max(best, int(m.group(1)))
    return best


# ─── Verification ────────────────────────────────────────────────────


def verify_resolved_task(
    task: dict[str, Any],
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> VerificationOutcome | None:
    """Check if a DONE self-improve task actually fixed the metric.

    `task` is a dict from `list_tasks` (or `get_task`). Must have status=DONE
    and an age past VERIFICATION_DELAY_HOURS. Returns None if not ready yet
    or if the baseline marker is missing. Otherwise returns a VerificationOutcome
    and applies the tags / requires_human flag / re-route to the task.
    """
    now = now or datetime.now(UTC)
    status = (task.get("status") or "").upper()
    if status != "DONE":
        return None

    resolved_at = task.get("resolved_at") or task.get("updated_at")
    if not isinstance(resolved_at, datetime):
        return None
    if resolved_at.tzinfo is None:
        resolved_at = resolved_at.replace(tzinfo=UTC)
    if now - resolved_at < timedelta(hours=VERIFICATION_DELAY_HOURS):
        return None

    tags = list(task.get("tags") or [])
    if "verified_resolved" in tags or "verify_failed" in tags:
        return None  # already verified; next pass is hold_check_7d

    body = task.get("body") or ""
    baseline = parse_baseline_from_body(body)
    if baseline is None or not baseline.get("metric") or not baseline.get("target"):
        logger.warning(
            "Task %s missing parseable buddy-baseline marker — skipping verification",
            task.get("id"),
        )
        return None

    # Derive agent_id from tags (each self-improve task has one agent-id tag).
    agent_id = _agent_id_from_tags(tags)
    if not agent_id:
        logger.warning("Task %s has no agent tag — skipping verification", task.get("id"))
        return None

    metric = str(baseline["metric"])
    target = str(baseline["target"])
    baseline_val = baseline.get("baseline")
    window_days = _window_days_from_baseline(baseline)

    try:
        snapshot = compute_goal_metrics(agent_id, window_days=window_days, tenant_id=tenant_id)
        current_raw = snapshot.get(metric)
        current_val = float(current_raw) if current_raw is not None else None
    except Exception as e:
        logger.warning("compute_goal_metrics failed for %s/%s: %s", agent_id, metric, e)
        current_val = None

    satisfied = _evaluate_target(current_val, target)

    current_escalation = extract_escalation_level(tags)
    new_tags: list[str] = []
    next_escalation = current_escalation
    requires_human = bool(task.get("requires_human"))

    if satisfied:
        new_tags.append("verified_resolved")
        # Stamp a durable verification timestamp so the 7-day hold check has
        # a fixed anchor (otherwise it reads task.updated_at, which drifts
        # any time something else edits the task).
        new_tags.append(f"verified_at:{now.isoformat()}")
    else:
        new_tags.append("verify_failed")
        next_escalation = current_escalation + 1
        # Clear any existing escalation:N tag so we don't stack duplicates
        kept = [t for t in tags if not _ESCALATION_RE.match(str(t))]
        new_tags.append(f"escalation:{next_escalation}")
        if next_escalation >= MAX_ESCALATIONS:
            requires_human = True
        tags = kept

    merged_tags = _dedup_tags([*tags, *new_tags])

    if _dryrun_enabled():
        logger.info(
            "DRYRUN verify: task=%s metric=%s current=%s satisfied=%s escalation=%s",
            task["id"],
            metric,
            current_val,
            satisfied,
            next_escalation,
        )
    else:
        _apply_verification(
            task_id=task["id"],
            new_tags=merged_tags,
            satisfied=satisfied,
            current_val=current_val,
            baseline_val=baseline_val,
            metric=metric,
            target=target,
            next_escalation=next_escalation,
            requires_human=requires_human,
            tenant_id=tenant_id,
        )

    outcome = VerificationOutcome(
        task_id=str(task["id"]),
        agent_id=agent_id,
        metric=metric,
        target=target,
        baseline=float(baseline_val) if baseline_val is not None else None,
        current=current_val,
        satisfied=satisfied,
        escalation_level=next_escalation,
        requires_human=requires_human,
        new_tags_added=new_tags,
    )

    _journal(
        "verify",
        {
            "task_id": outcome.task_id,
            "agent_id": outcome.agent_id,
            "metric": outcome.metric,
            "target": outcome.target,
            "baseline": outcome.baseline,
            "current": outcome.current,
            "satisfied": outcome.satisfied,
            "escalation_level": outcome.escalation_level,
            "requires_human": outcome.requires_human,
        },
    )
    return outcome


def _apply_verification(
    *,
    task_id: str,
    new_tags: list[str],
    satisfied: bool,
    current_val: float | None,
    baseline_val: Any,
    metric: str,
    target: str,
    next_escalation: int,
    requires_human: bool,
    tenant_id: str,
) -> None:
    """Persist the verification outcome: tags, requires_human, resolution, re-route."""
    from robothor.crm.dal import update_task

    resolution = (
        f"Buddy verified: {metric}={current_val} satisfies {target} (was {baseline_val})"
        if satisfied
        else (
            f"Buddy re-opened: {metric}={current_val} still doesn't satisfy {target} "
            f"(baseline {baseline_val}). Escalation:{next_escalation}."
        )
    )

    # Resolved & satisfied → keep DONE, just tag.
    # Failed verification → re-open (IN_PROGRESS) and route.
    update_kwargs: dict[str, Any] = {
        "tags": new_tags,
        "tenant_id": tenant_id,
        "changed_by": "buddy-grader",
    }

    if not satisfied:
        update_kwargs["status"] = "IN_PROGRESS"
        update_kwargs["resolution"] = resolution
        if next_escalation >= 2:
            update_kwargs["assigned_to_agent"] = "auto-researcher"
        if requires_human:
            update_kwargs["requires_human"] = True
    else:
        update_kwargs["resolution"] = resolution

    try:
        update_task(task_id, **update_kwargs)
    except Exception as e:
        logger.warning("Failed to apply verification to task %s: %s", task_id, e)


# ─── Hold check (7 days after verified_resolved) ─────────────────────


def hold_check_7d(
    task: dict[str, Any],
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> VerificationOutcome | None:
    """7 days after `verified_resolved`, re-check that the metric stuck.

    Only runs on tasks tagged `verified_resolved` that don't yet have a
    `held_7d=true` or `held_7d=false` tag, and whose verified_at is 7+ days ago.
    """
    now = now or datetime.now(UTC)
    tags = list(task.get("tags") or [])
    if "verified_resolved" not in tags:
        return None
    if any(t.startswith("held_7d=") for t in tags):
        return None

    # Prefer the durable verified_at:<iso> tag stamped at verify time. Fall
    # back to task.updated_at only for in-flight tasks verified before this
    # field existed.
    verified_at = _extract_verified_at(tags) or task.get("updated_at")
    if not isinstance(verified_at, datetime):
        return None
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=UTC)
    if now - verified_at < timedelta(days=HOLD_CHECK_DAYS):
        return None

    baseline = parse_baseline_from_body(task.get("body") or "")
    if baseline is None:
        return None
    agent_id = _agent_id_from_tags(tags)
    if not agent_id:
        return None

    metric = str(baseline["metric"])
    target = str(baseline["target"])
    window_days = _window_days_from_baseline(baseline)

    try:
        snapshot = compute_goal_metrics(agent_id, window_days=window_days, tenant_id=tenant_id)
        current_val = float(snapshot.get(metric)) if snapshot.get(metric) is not None else None
    except Exception as e:
        logger.warning("compute_goal_metrics failed for %s/%s: %s", agent_id, metric, e)
        return None

    held = _evaluate_target(current_val, target)
    hold_tag = f"held_7d={'true' if held else 'false'}"
    merged_tags = _dedup_tags([*tags, hold_tag])

    if _dryrun_enabled():
        logger.info(
            "DRYRUN hold-check: task=%s metric=%s current=%s held=%s",
            task["id"],
            metric,
            current_val,
            held,
        )
    else:
        try:
            from robothor.crm.dal import update_task

            update_task(
                task["id"],
                tags=merged_tags,
                tenant_id=tenant_id,
                changed_by="buddy-grader",
            )
        except Exception as e:
            logger.warning("Failed to apply hold-check to task %s: %s", task["id"], e)

    outcome = VerificationOutcome(
        task_id=str(task["id"]),
        agent_id=agent_id,
        metric=metric,
        target=target,
        baseline=float(baseline.get("baseline")) if baseline.get("baseline") is not None else None,
        current=current_val,
        satisfied=held,
        escalation_level=extract_escalation_level(tags),
        requires_human=bool(task.get("requires_human")),
        new_tags_added=[hold_tag],
    )

    _journal(
        "hold_check",
        {
            "task_id": outcome.task_id,
            "agent_id": outcome.agent_id,
            "metric": outcome.metric,
            "target": outcome.target,
            "current": outcome.current,
            "held": held,
        },
    )
    return outcome


# ─── Helpers ─────────────────────────────────────────────────────────


def _window_days_from_baseline(baseline: dict[str, Any]) -> int:
    """Read window_days from a parsed baseline marker, falling back to the
    grader default. In-flight tasks opened before this field existed carry
    no key (or the legacy `window_days_hint: null`) — both resolve to 7."""
    raw = baseline.get("window_days")
    if raw is None:
        raw = baseline.get("window_days_hint")
    try:
        value = int(raw) if raw is not None else DEFAULT_GRADER_WINDOW_DAYS
    except (TypeError, ValueError):
        return DEFAULT_GRADER_WINDOW_DAYS
    return value if value > 0 else DEFAULT_GRADER_WINDOW_DAYS


def _extract_verified_at(tags: list[str]) -> datetime | None:
    """Parse the first verified_at:<iso> tag back into a datetime."""
    if not tags:
        return None
    for t in tags:
        m = _VERIFIED_AT_RE.match(str(t).strip())
        if not m:
            continue
        raw = m.group(1).strip()
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return None


def _agent_id_from_tags(tags: list[str]) -> str | None:
    """Infer the agent_id from a self-improve task's tag list.

    Convention (see buddy_critic.Finding.task_tags): tags always contain
    `nightwatch`, `self-improve`, `<agent_id>`, `<metric_name>`. Anything
    outside those four slots that starts with 'escalation:', 'verified_',
    'verify_failed', 'held_7d=', 'requires_human' is ignored.
    """
    known_meta = {"nightwatch", "self-improve", "verified_resolved", "verify_failed"}
    candidates = [
        t
        for t in tags
        if t not in known_meta
        and not t.startswith("escalation:")
        and not t.startswith("held_7d=")
        and not _is_known_metric_name(t)
    ]
    return candidates[0] if candidates else None


# Metric names never contain underscores in the middle that look like agent
# ids — but several metrics DO share a format with agents. Keep the list
# exhaustive against docs/agents/GOAL_TAXONOMY.md's metric vocabulary so the
# agent_id heuristic doesn't misfire.
_METRIC_NAMES: frozenset[str] = frozenset(
    {
        "delivery_success_rate",
        "inbox_read_rate",
        "min_output_chars",
        "required_sections_present",
        "operator_rating_avg",
        "substantive_output_rate",
        "avg_duration_ms",
        "p95_duration_ms",
        "avg_cost_usd",
        "p95_cost_usd",
        "timeout_rate",
        "error_rate",
        "tool_success_rate",
        "recovery_rate",
        "task_completion_rate",
        "experiment_measure_success_rate",
        "experiment_completion_rate",
        "experiment_deadlock_rate",
        "experiments_improving_metric_rate",
        "pr_merge_rate",
        "pr_revert_rate",
        "satisfaction_rate",
    }
)


def _is_known_metric_name(tag: str) -> bool:
    return tag in _METRIC_NAMES


def _dedup_tags(tags: list[str]) -> list[str]:
    """De-duplicate while preserving order. Also collapses `escalation:N` to the max."""
    seen: set[str] = set()
    out: list[str] = []
    max_escalation: int | None = None
    for t in tags:
        t = str(t).strip()
        m = _ESCALATION_RE.match(t)
        if m:
            lvl = int(m.group(1))
            max_escalation = lvl if max_escalation is None else max(max_escalation, lvl)
            continue
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    if max_escalation is not None:
        out.append(f"escalation:{max_escalation}")
    return out


# ─── Journal writing ─────────────────────────────────────────────────


def _journal(event: str, payload: dict[str, Any]) -> None:
    try:
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(UTC).date().isoformat()
        path = JOURNAL_DIR / f"{today}.jsonl"
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "event": event,
            **payload,
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.debug("Grader journal write failed (%s): %s", event, e)


# ─── Top-level passes ────────────────────────────────────────────────


def run_verification_pass(
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Iterate every unresolved self-improve task and run the appropriate check.

    Returns a summary dict.
    """
    from robothor.crm.dal import list_tasks

    verified = 0
    failed = 0
    held_ok = 0
    held_regressed = 0
    requires_human_count = 0

    # DONE self-improve tasks eligible for verify_resolved_task.
    done_tasks = list_tasks(
        status="DONE",
        tags=["self-improve"],
        tenant_id=tenant_id,
        limit=200,
        exclude_resolved=False,
    )
    for task in done_tasks:
        outcome = verify_resolved_task(task, now=now, tenant_id=tenant_id)
        if outcome is None:
            continue
        if outcome.satisfied:
            verified += 1
        else:
            failed += 1
        if outcome.requires_human:
            requires_human_count += 1

    # Already-verified tasks eligible for 7-day hold check. They're still in
    # DONE state (verified_resolved is a tag, not a status transition).
    for task in done_tasks:
        outcome = hold_check_7d(task, now=now, tenant_id=tenant_id)
        if outcome is None:
            continue
        if outcome.satisfied:
            held_ok += 1
        else:
            held_regressed += 1

    return {
        "verified_resolved": verified,
        "verify_failed": failed,
        "held_7d_true": held_ok,
        "held_7d_false": held_regressed,
        "requires_human_promoted": requires_human_count,
        "ts": datetime.now(UTC).isoformat(),
    }
