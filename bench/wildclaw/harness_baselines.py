"""Published head-to-head scores for the other agent harnesses.

`baselines.json` holds OpenClaw's per-task scores, and every standing report
this project has produced reads "Genus vs OpenClaw". WildClawBench also runs
the same 60 tasks under Claude Code, Codex CLI and Hermes Agent, and those
numbers say something the OpenClaw-only framing hides: **OpenClaw is the
best harness on zero of four models, and Hermes on three.**

So the bar for "front-runner" is Hermes, not OpenClaw. On MiMo V2 Pro — the
model `agent.yaml` pins — OpenClaw scores 40.2 and Hermes 48.1. Matching the
number this project has been tracking would still leave it eight points
behind the actual leader.

These are the authors' published aggregates, not measurements taken here.
Genus is absent because the authors never ran it; placing Genus in the table
means running our harness on one of these four models.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_PATH = Path(__file__).with_name("harness_baselines.json")


@lru_cache(maxsize=1)
def _data() -> dict[str, Any]:
    try:
        loaded: dict[str, Any] = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded


def harness_scores_for(model: str) -> dict[str, float]:
    """Every published harness score for one model, or {} if unknown."""
    return dict((_data().get("harness_scores") or {}).get(model) or {})


def best_harness(model: str) -> str | None:
    """Which harness scored highest on this model."""
    scores = harness_scores_for(model)
    if not scores:
        return None
    return max(scores, key=lambda name: scores[name])


def leader_gap(model: str, our_score: float) -> dict[str, Any]:
    """Where a Genus score sits against the leader, not just against OpenClaw.

    Reporting only the OpenClaw delta is what let "near parity" stand in for
    "front-runner". This names the harness actually in front and the distance
    to it.
    """
    scores = harness_scores_for(model)
    if not scores:
        return {"model": model, "leader": None, "behind_leader": None, "scores": {}}
    leader = max(scores, key=lambda name: scores[name])
    return {
        "model": model,
        "leader": leader,
        "leader_score": scores[leader],
        "our_score": our_score,
        "behind_leader": scores[leader] - our_score,
        "scores": scores,
    }
