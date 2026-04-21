"""Goal-driven self-improvement primitives.

This module turns agent YAML manifest `goals:` blocks into machine-readable
contracts, computes metric values from `agent_runs`, detects persistent
breaches, and maps breaches to corrective-action templates.

See `docs/agents/GOAL_TAXONOMY.md` for the shared vocabulary.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from robothor.constants import DEFAULT_TENANT
from robothor.engine.analytics import get_agent_stats

logger = logging.getLogger(__name__)

# Number of consecutive breached evaluation windows before a goal is
# considered "persistently breached" and enters the improvement backlog.
PERSISTENT_BREACH_DAYS = 3

# Agents that drive the self-improvement loop itself. They still get scored
# and reviewed so you can see them breaching, but no self-improve CRM task
# is ever opened against them — that would let auto-agent silently edit its
# own supervisors' manifests or guardrails.
EXCLUDED_FROM_SELF_IMPROVE: frozenset[str] = frozenset(
    {"buddy", "buddy-grader", "buddy-auditor", "auto-agent", "auto-researcher"}
)


# ─── Dataclasses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class GoalSpec:
    """Declarative goal parsed from a manifest."""

    id: str
    category: str  # reach | quality | efficiency | correctness
    metric: str
    target: str
    weight: float = 1.0
    window_days: int = 7
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GoalBreach:
    """A persistent breach that self-improvement should act on."""

    goal_id: str
    category: str
    metric: str
    target: str
    actual: float | None
    consecutive_days_breached: int
    weight: float
    # Rolling window the breach was evaluated against. Must survive the trip
    # Buddy → task body marker → grader so verification uses the same slice.
    # Defaults to 7 because older call sites predate this field; production
    # `detect_goal_breach` always populates it from the goal's declared window.
    window_days: int = 7

    @property
    def priority_score(self) -> float:
        """Higher = more urgent. weight × consecutive-days-breached."""
        return self.weight * float(self.consecutive_days_breached)


# ─── Target evaluation ────────────────────────────────────────────────


_TARGET_RE = re.compile(r"^\s*(>=|<=|>|<|==|=)\s*(-?\d+(?:\.\d+)?)\s*$")


def _evaluate_target(value: Any, target: str) -> bool:
    """Return True if `value` satisfies the target comparison."""
    if value is None:
        return False
    try:
        value_f = float(value)
    except (TypeError, ValueError):
        return False
    m = _TARGET_RE.match(target)
    if not m:
        logger.warning("Cannot parse goal target %r", target)
        return False
    op, threshold_str = m.group(1), m.group(2)
    threshold = float(threshold_str)
    if op == ">":
        return value_f > threshold
    if op == ">=":
        return value_f >= threshold
    if op == "<":
        return value_f < threshold
    if op == "<=":
        return value_f <= threshold
    if op in ("==", "="):
        return value_f == threshold
    return False


# ─── Manifest parsing ─────────────────────────────────────────────────


_KNOWN_CATEGORIES = {"reach", "quality", "efficiency", "correctness"}


def parse_goals_from_manifest(manifest: dict[str, Any]) -> list[GoalSpec]:
    """Extract GoalSpecs from a manifest dict.

    Handles both the new 4-category shape and the legacy flat-list shape
    (falling back to category='correctness' for legacy entries).
    """
    raw = manifest.get("goals")
    if not raw:
        return []

    specs: list[GoalSpec] = []

    def _append(entry: Any, *, category: str) -> None:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-dict goal entry in category %r: %r", category, entry)
            return
        specs.append(_goal_from_dict(entry, category=category))

    if isinstance(raw, list):
        # Legacy flat list — treat as correctness goals for back-compat.
        for entry in raw:
            _append(entry, category="correctness")
        return specs

    if isinstance(raw, dict):
        for category, entries in raw.items():
            if category not in _KNOWN_CATEGORIES:
                logger.warning("Unknown goal category %r — skipping", category)
                continue
            if not isinstance(entries, list):
                continue
            for entry in entries:
                _append(entry, category=category)

    return specs


def _goal_from_dict(entry: dict[str, Any], *, category: str) -> GoalSpec:
    extras = {
        k: v
        for k, v in entry.items()
        if k not in {"id", "metric", "target", "weight", "window_days"}
    }
    return GoalSpec(
        id=str(entry.get("id", "")),
        category=category,
        metric=str(entry.get("metric", "")),
        target=str(entry.get("target", "")),
        weight=float(entry.get("weight", 1.0)),
        window_days=int(entry.get("window_days", 7)),
        extras=extras,
    )


# ─── Metric computation ───────────────────────────────────────────────


def compute_goal_metrics(
    agent_id: str,
    window_days: int = 7,
    tenant_id: str = DEFAULT_TENANT,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Return a flat dict of metric values for an agent over a rolling window.

    Delegates to `analytics.get_agent_stats` for the heavy lifting, then adds
    derived metrics the goals system uses. ``as_of`` anchors the window's
    right edge (default: now).
    """
    stats = get_agent_stats(agent_id, days=window_days, tenant_id=tenant_id, as_of=as_of) or {}
    metrics: dict[str, Any] = dict(stats)

    total = stats.get("total_runs") or 0
    if total > 0:
        timeouts = stats.get("timeouts") or 0
        metrics["timeout_rate"] = round(timeouts / total, 4)

    return metrics


def _get_daily_metric_history(
    agent_id: str,
    metric: str,
    window_days: int,
    lookback_days: int = 14,
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
    """Return one metrics dict per day over the lookback period.

    Each entry is a trailing-``window_days`` snapshot anchored at a distinct
    day, so consecutive entries differ — ``detect_goal_breach`` relies on
    this to count real consecutive breach days.

    Most recent day is LAST in the returned list.
    """
    now = datetime.now(UTC)
    history: list[dict[str, Any]] = []
    for days_ago in range(lookback_days, 0, -1):
        # days_ago counts down from lookback_days..1 — subtract (days_ago-1)
        # so the final iteration (days_ago=1) lands on "now".
        as_of = now - timedelta(days=days_ago - 1)
        snapshot = compute_goal_metrics(
            agent_id, window_days=window_days, tenant_id=tenant_id, as_of=as_of
        )
        history.append(snapshot)
    return history


def detect_goal_breach(
    agent_id: str,
    goals: list[GoalSpec],
    tenant_id: str = DEFAULT_TENANT,
) -> list[GoalBreach]:
    """Return goals that have been in breach for PERSISTENT_BREACH_DAYS or more."""
    breaches: list[GoalBreach] = []
    for goal in goals:
        history = _get_daily_metric_history(
            agent_id, goal.metric, goal.window_days, tenant_id=tenant_id
        )
        if not history:
            continue

        # Count consecutive breach days, walking back from the most recent.
        consecutive = 0
        actual_latest: float | None = None
        for snapshot in reversed(history):
            val = snapshot.get(goal.metric)
            if actual_latest is None and val is not None:
                try:
                    actual_latest = float(val)
                except (TypeError, ValueError):
                    actual_latest = None
            if _evaluate_target(val, goal.target):
                break  # goal satisfied — streak ends
            consecutive += 1

        if consecutive >= PERSISTENT_BREACH_DAYS:
            breaches.append(
                GoalBreach(
                    goal_id=goal.id,
                    category=goal.category,
                    metric=goal.metric,
                    target=goal.target,
                    actual=actual_latest,
                    consecutive_days_breached=consecutive,
                    weight=goal.weight,
                    window_days=goal.window_days,
                )
            )
    breaches.sort(key=lambda b: b.priority_score, reverse=True)
    return breaches


# ─── Corrective-action templates ──────────────────────────────────────


# Hard-coded fallback templates if the YAML file is missing. The rich,
# metric-specific templates live in `docs/agents/corrective-actions.yaml`
# and are loaded lazily on first call.
_CATEGORY_FALLBACK: dict[str, list[str]] = {
    "reach": [
        "Verify delivery channel health: bot token valid, endpoint reachable.",
        "Check output capture — is output_text empty despite 'delivered' status?",
        "Audit route config: chat_id / recipient still correct.",
    ],
    "quality": [
        "Compare top-quartile vs bottom-quartile runs side by side.",
        "Revise instruction file to enforce output structure and completeness.",
        "Verify warmup files provide sufficient context.",
        "Consider model upgrade if instruction-level fixes don't stick.",
    ],
    "efficiency": [
        "Classify timeouts by in-flight tool call.",
        "Lower stall_timeout_seconds to fail fast on wedged calls.",
        "Reduce max_iterations if agent hits the cap without converging.",
        "Trim instruction file and warmup size.",
    ],
    "correctness": [
        "Group errors by tool + error type.",
        "Review guardrails — is an over-strict rule blocking legitimate calls?",
        "Check for tool implementation regressions.",
        "Validate schemas at boundaries.",
    ],
}

_TEMPLATE_CACHE: dict[str, Any] | None = None


def _load_templates() -> dict[str, Any]:
    """Lazy-load templates from docs/agents/corrective-actions.yaml."""
    global _TEMPLATE_CACHE
    if _TEMPLATE_CACHE is not None:
        return _TEMPLATE_CACHE

    import os
    from pathlib import Path

    import yaml

    workspace = os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))
    path = Path(workspace) / "docs" / "agents" / "corrective-actions.yaml"
    try:
        data = yaml.safe_load(path.read_text()) or {}
        _TEMPLATE_CACHE = data
    except (FileNotFoundError, OSError, yaml.YAMLError) as e:
        logger.warning("Could not load corrective-actions.yaml (%s) — using fallback", e)
        _TEMPLATE_CACHE = {}
    return _TEMPLATE_CACHE


def suggest_corrective_actions(breach: GoalBreach) -> list[str]:
    """Return an ordered list of investigation + fix suggestions for a breach.

    Looks up the specific "<category>.<metric>" template first, then falls
    back to the category-level default, then the built-in fallback.
    """
    templates = _load_templates()
    key_specific = f"{breach.category}.{breach.metric}"
    key_default = f"{breach.category}.default"

    for key in (key_specific, key_default):
        entry = templates.get(key)
        if isinstance(entry, dict):
            steps = entry.get("investigation_steps") or []
            remeds = entry.get("remediations_in_order") or []
            out: list[str] = []
            out.extend(steps)
            out.extend(remeds)
            if out:
                return out

    return list(_CATEGORY_FALLBACK.get(breach.category, []))


# ─── Auto-review writer ───────────────────────────────────────────────


def register_review(
    agent_id: str,
    rating: int,
    categories: dict[str, Any],
    feedback: str,
    action_items: list[str],
    *,
    reviewer: str = "auto-review",
    reviewer_type: str = "system",
    run_id: str | None = None,
    tenant_id: str = DEFAULT_TENANT,
) -> str | None:
    """Insert an agent_reviews row.

    Thin wrapper around `robothor.crm.dal.create_review` — kept so the goal
    module can offer a focused signature, but the DB logic (UUID generation,
    rating clamping, SQL) lives in the DAL to avoid the dual-path anti-pattern.
    """
    from robothor.crm.dal import create_review

    return create_review(
        agent_id=agent_id,
        reviewer=reviewer,
        reviewer_type=reviewer_type,
        rating=rating,
        categories=categories,
        feedback=feedback,
        action_items=action_items,
        run_id=run_id,
        tenant_id=tenant_id,
    )


# ─── Fleet-wide goal sweep ────────────────────────────────────────────


def sweep_all_goals(
    manifests: list[dict[str, Any]],
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, list[GoalBreach]]:
    """Run detect_goal_breach across every manifest with a goals: block.

    Returns {agent_id: [breaches]}. Use by the nightly review hook.
    """
    result: dict[str, list[GoalBreach]] = {}
    for manifest in manifests:
        agent_id = manifest.get("id")
        if not agent_id:
            continue
        goals = parse_goals_from_manifest(manifest)
        if not goals:
            continue
        breaches = detect_goal_breach(agent_id, goals, tenant_id=tenant_id)
        if breaches:
            result[agent_id] = breaches
    return result


# ─── Achievement scoring + nightly review ─────────────────────────────


def compute_achievement_score(
    agent_id: str,
    goals: list[GoalSpec],
    tenant_id: str = DEFAULT_TENANT,
) -> dict[str, Any]:
    """Compute weighted goal-achievement score [0.0, 1.0] for an agent.

    Returns dict with: score, rating (1-5), satisfied_goals, breached_goals,
    per_goal (list of {id, metric, target, actual, satisfied}).

    Each goal is evaluated against its own ``window_days`` — groups goals by
    window and issues one ``compute_goal_metrics`` call per distinct window so
    a 30-day revert-rate goal is not silently scored against a 7-day snapshot.
    """
    distinct_windows = {g.window_days for g in goals}
    snapshots: dict[int, dict[str, Any]] = {
        w: compute_goal_metrics(agent_id, window_days=w, tenant_id=tenant_id)
        for w in distinct_windows
    }

    total_weight = 0.0
    weighted_satisfied = 0.0
    per_goal: list[dict[str, Any]] = []
    satisfied_ids: list[str] = []
    breached_ids: list[str] = []

    for goal in goals:
        metric_value = snapshots[goal.window_days].get(goal.metric)
        is_satisfied = _evaluate_target(metric_value, goal.target)
        total_weight += goal.weight
        if is_satisfied:
            weighted_satisfied += goal.weight
            satisfied_ids.append(goal.id)
        else:
            breached_ids.append(goal.id)
        per_goal.append(
            {
                "id": goal.id,
                "category": goal.category,
                "metric": goal.metric,
                "target": goal.target,
                "actual": metric_value,
                "satisfied": is_satisfied,
                "weight": goal.weight,
            }
        )

    score = (weighted_satisfied / total_weight) if total_weight > 0 else 0.0
    # Map [0, 1] → rating [1, 5]
    if score >= 0.95:
        rating = 5
    elif score >= 0.80:
        rating = 4
    elif score >= 0.60:
        rating = 3
    elif score >= 0.40:
        rating = 2
    else:
        rating = 1

    return {
        "score": round(score, 4),
        "rating": rating,
        "satisfied_goals": satisfied_ids,
        "breached_goals": breached_ids,
        "per_goal": per_goal,
    }


def run_nightly_auto_review(
    manifests: list[dict[str, Any]],
    tenant_id: str = DEFAULT_TENANT,
) -> list[dict[str, Any]]:
    """Write an auto-review row for every agent with goals.

    Returns a list of {agent_id, review_id, rating, score} summaries.
    """
    results: list[dict[str, Any]] = []
    for manifest in manifests:
        agent_id = manifest.get("id")
        if not agent_id:
            continue
        goals = parse_goals_from_manifest(manifest)
        if not goals:
            continue

        achievement = compute_achievement_score(agent_id, goals, tenant_id=tenant_id)
        breaches = detect_goal_breach(agent_id, goals, tenant_id=tenant_id)

        # Build feedback text + action items from breaches.
        feedback_lines = [
            f"Goal achievement: {achievement['score']:.2f} "
            f"({len(achievement['satisfied_goals'])}/"
            f"{len(achievement['satisfied_goals']) + len(achievement['breached_goals'])}"
            " goals satisfied)."
        ]
        if achievement["breached_goals"]:
            feedback_lines.append("Breached: " + ", ".join(achievement["breached_goals"]) + ".")

        action_items: list[str] = []
        for breach in breaches[:3]:  # cap at top-3 priority breaches
            feedback_lines.append(
                f"Persistent breach: {breach.goal_id} "
                f"({breach.metric} = {breach.actual}, target {breach.target}, "
                f"{breach.consecutive_days_breached}d)"
            )
            # Pull the top remediation from the template as an action item
            actions = suggest_corrective_actions(breach)
            if actions:
                action_items.append(f"[{breach.goal_id}] {actions[0]}")

        categories = {
            "score": achievement["score"],
            "satisfied": achievement["satisfied_goals"],
            "breached": achievement["breached_goals"],
            "persistent_breaches": [b.goal_id for b in breaches],
        }

        review_id = register_review(
            agent_id=agent_id,
            rating=achievement["rating"],
            categories=categories,
            feedback="\n".join(feedback_lines),
            action_items=action_items,
            reviewer="auto-review",
            reviewer_type="system",
            tenant_id=tenant_id,
        )
        results.append(
            {
                "agent_id": agent_id,
                "review_id": review_id,
                "rating": achievement["rating"],
                "score": achievement["score"],
                "breaches": len(breaches),
            }
        )
    return results


# ─── CLI entry point ──────────────────────────────────────────────────


def _main() -> None:
    """python -m robothor.engine.goals nightly-review

    Loads all manifests, computes achievement, writes agent_reviews rows.
    """
    import argparse
    import sys

    from robothor.engine.config import load_all_manifests

    parser = argparse.ArgumentParser(prog="robothor-goals")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("nightly-review", help="Run nightly auto-review sweep")
    audit = sub.add_parser("audit", help="Print per-agent goal achievement")
    audit.add_argument("--agent", help="Only audit one agent")
    args = parser.parse_args()

    import os
    from pathlib import Path

    workspace = os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor"))
    manifests = load_all_manifests(Path(workspace) / "docs" / "agents")

    if args.cmd == "nightly-review":
        results = run_nightly_auto_review(manifests)
        for r in results:
            print(
                f"{r['agent_id']:22} rating={r['rating']} "
                f"score={r['score']:.2f} breaches={r['breaches']}"
            )
        return

    if args.cmd == "audit":
        import json as _json

        for m in manifests:
            agent_id = m.get("id")
            if not agent_id:
                continue
            if args.agent and agent_id != args.agent:
                continue
            goals = parse_goals_from_manifest(m)
            if not goals:
                continue
            achievement = compute_achievement_score(agent_id, goals)
            print(f"\n=== {agent_id} ===")
            print(_json.dumps(achievement, indent=2, default=str))
        return

    sys.exit(0)


if __name__ == "__main__":
    _main()
