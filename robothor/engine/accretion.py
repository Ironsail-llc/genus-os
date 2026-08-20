"""Skill-accretion gate + ledger (Wave-2, W2-24).

The accretion engine lets agents author reusable skills from their own
trajectories. The danger is reward-hacking, so promotion of an agent-authored
skill clears a TWO-KEY GATE before it is trusted:

  1. no safety regression on the benchmark suite (content-only), AND
  2. held-out goal_achievement (the goal-judge, W2-22) >= the pre-change baseline.

Only ADDITIVE accretion is autonomous — destructive curator consolidations and
delivery-agent changes require operator approval (handled elsewhere). Nothing in
this loop optimizes the judge's number; it only gates.

Flag: ROBOTHOR_ACCRETION_ENABLED.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def accretion_enabled() -> bool:
    return os.environ.get("ROBOTHOR_ACCRETION_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def accretion_gate(
    *,
    has_safety_regression: bool,
    judge_score: float,
    baseline_score: float,
) -> tuple[bool, str]:
    """Decide whether an agent-authored skill may be promoted (the two-key gate).

    Returns ``(promote, reason)``. Promote only when there is no safety
    regression AND the held-out judge score is at least the pre-change baseline.
    """
    if has_safety_regression:
        return (False, "blocked: benchmark safety regression")
    if judge_score < baseline_score:
        return (
            False,
            f"blocked: judge score {judge_score:.2f} below baseline {baseline_score:.2f}",
        )
    return (
        True,
        f"promoted: no regression, judge {judge_score:.2f} >= baseline {baseline_score:.2f}",
    )


def get_accretion_ledger(limit: int = 30) -> dict[str, Any]:
    """Return the ledger of agent-authored skills: git history + usage counts.

    Best-effort and read-only — pairs the version-controlled skill files with
    their runtime usage telemetry so the operator can audit what the fleet has
    learned.
    """
    import subprocess
    from pathlib import Path

    from robothor.engine.skills import read_skill_view

    workspace = Path(os.environ.get("ROBOTHOR_WORKSPACE", str(Path.home() / "robothor")))
    skills_dir = workspace / "agents" / "skills"
    entries: list[dict[str, Any]] = []
    try:
        for meta_path in sorted(skills_dir.glob("*/meta.json")):
            name = meta_path.parent.name
            view = read_skill_view(name, base=skills_dir)
            if view is None:
                continue
            if view.get("created_by") in (None, "operator", "human"):
                continue  # ledger tracks AGENT-authored skills
            entries.append(
                {
                    "skill": name,
                    "created_by": view.get("created_by"),
                    "created_at": view.get("created_at"),
                    "usage_count": view.get("usage_count", 0),
                    "last_used": view.get("last_used"),
                }
            )
            if len(entries) >= limit:
                break
    except Exception as e:
        logger.debug("accretion ledger scan failed: %s", e)

    git_log: list[str] = []
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "log",
                "--oneline",
                "-n",
                str(limit),
                "--",
                "agents/skills",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode == 0:
            git_log = [ln for ln in out.stdout.splitlines() if ln.strip()]
    except Exception as e:
        logger.debug("accretion ledger git log failed: %s", e)

    return {"skills": entries, "count": len(entries), "recent_commits": git_log}
