"""
Difficulty-Aware Router — classifies tasks and adjusts execution parameters.

Classification sources (priority order):
1. Manual override via difficulty_class in manifest
2. Planning phase output (if planner ran)
3. Heuristic: message length + tool count

Router v2 (``ROBOTHOR_ROUTER_V2``): for an interactive chat turn the length
rule no longer buys a planner call on its own. A message the length rule calls
complex keeps the complex preset, but its planning defers to the agent's
manifest unless the message itself asks for multi-step work (see
``requests_multistep_work``). The signal costs no LLM call and never adds a
planner call the length rule would not have made.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace

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
    *,
    catalogue_implies_complexity: bool = True,
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
    if msg_len > 500 or (catalogue_implies_complexity and tool_count > 20):
        return "complex"
    return "moderate"


#: Steps a message must list before the router v2 signal asks for a plan.
MULTI_STEP_MIN = 3
# A line that opens with a list marker: "1.", "2)", "-", "*" or a bullet.
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*\u2022])\s+\S", re.MULTILINE)
# Words that chain one action to the next.
_SEQUENCE = re.compile(r"\b(?:then|after that|afterwards|finally)\b", re.IGNORECASE)


def requests_multistep_work(message: str) -> bool:
    """True when the message itself lists at least ``MULTI_STEP_MIN`` steps.

    Counts list-item lines plus sequencing words ("then", "after that",
    "afterwards", "finally"). A regex pass over the text: no LLM call.
    """
    steps = len(_LIST_ITEM.findall(message)) + len(_SEQUENCE.findall(message))
    return steps >= MULTI_STEP_MIN


def get_route_config(
    message: str,
    tool_count: int,
    manual_override: str = "",
    plan_difficulty: str = "",
    *,
    catalogue_implies_complexity: bool = True,
    length_forces_planning: bool = True,
) -> RouteConfig:
    """Get route configuration for a task.

    ``length_forces_planning=False`` (router v2, interactive turns): when the
    message's length is the only reason it is complex, planning defers to the
    agent's config unless ``requests_multistep_work`` says otherwise.
    """
    difficulty = classify_difficulty(
        message,
        tool_count,
        manual_override,
        plan_difficulty,
        catalogue_implies_complexity=catalogue_implies_complexity,
    )
    route = ROUTE_PRESETS.get(difficulty, ROUTE_PRESETS["moderate"])
    complex_by_length = (
        difficulty == "complex"
        and manual_override not in ROUTE_PRESETS
        and plan_difficulty not in ROUTE_PRESETS
        and not (catalogue_implies_complexity and tool_count > 20)
    )
    if not length_forces_planning and complex_by_length and not requests_multistep_work(message):
        return replace(route, planning=None)
    return route
