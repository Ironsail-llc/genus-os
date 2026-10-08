"""Output filters for delivery: what of an agent's final text reaches a channel.

Split from ``delivery.py`` (module-size ratchet): trivial "nothing to report"
heartbeat output, and ``delivery.line_filter`` for agents whose output
contract is "these lines or nothing".
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

TRIVIAL_PATTERNS = [
    "all clear",
    "all quiet",
    "nothing new",
    "board is clean",
    "no open tasks",
    "standing down",
    "no updates",
    "nothing to report",
    "inbox empty",
    "fleet clean",
    "no new activity",
    "board unchanged",
    "no changes",
    "no movement",
    "nothing actionable",
]


def filter_lines(agent_id: str, text: str, pattern: str) -> str:
    """Keep only the lines matching ``pattern``; an invalid pattern filters nothing."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        logger.warning(
            "Agent %s has an invalid delivery.line_filter %r: %s", agent_id, pattern, exc
        )
        return text
    return "\n".join(line for line in text.splitlines() if regex.search(line.strip())).strip()


def is_trivial_output(text: str) -> bool:
    """Detect 'nothing to report' output that shouldn't be delivered.

    Short messages (<300 chars) containing common filler phrases are suppressed.
    Uses word-boundary matching to avoid false positives on substrings.
    Substantial reports always get through.
    """
    if len(text) > 300:
        return False
    lower = text.lower()
    return any(re.search(r"\b" + re.escape(p) + r"\b", lower) for p in TRIVIAL_PATTERNS)
