"""
Difficulty-Aware Router — classifies tasks and adjusts execution parameters.

Classification sources (priority order):
1. Manual override via difficulty_class in manifest
2. Planning phase output (if planner ran)
3. Heuristic: message length + tool count
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RouteConfig:
    """Execution parameters adjusted by the router."""

    difficulty: str = "moderate"  # simple, moderate, complex
    max_iterations_override: int | None = None
    planning: bool | None = None  # None = use agent config
    verification: bool | None = None
    checkpoint: bool | None = None
    scratchpad: bool | None = None


# Presets per difficulty level
ROUTE_PRESETS: dict[str, RouteConfig] = {
    "simple": RouteConfig(
        difficulty="simple",
        max_iterations_override=5,
        planning=False,
        verification=False,
        checkpoint=False,
        scratchpad=False,
    ),
    "moderate": RouteConfig(
        difficulty="moderate",
        # None values = defer to agent config
    ),
    "complex": RouteConfig(
        difficulty="complex",
        planning=True,
        verification=True,
        checkpoint=True,
        scratchpad=True,
    ),
}


def classify_difficulty(
    message: str,
    tool_count: int,
    manual_override: str = "",
    plan_difficulty: str = "",
) -> str:
    """Classify task difficulty. Returns: simple, moderate, complex."""
    # Priority 1: manual override from manifest
    if manual_override in ("simple", "moderate", "complex"):
        return manual_override

    # Priority 2: planning phase output
    if plan_difficulty in ("simple", "moderate", "complex"):
        return plan_difficulty

    # Priority 3: heuristic
    msg_len = len(message)
    if msg_len < 100 and tool_count <= 5:
        return "simple"
    if msg_len > 500 or tool_count > 20:
        return "complex"
    return "moderate"


def get_route_config(
    message: str,
    tool_count: int,
    manual_override: str = "",
    plan_difficulty: str = "",
) -> RouteConfig:
    """Get route configuration for a task."""
    difficulty = classify_difficulty(message, tool_count, manual_override, plan_difficulty)
    return ROUTE_PRESETS.get(difficulty, ROUTE_PRESETS["moderate"])
