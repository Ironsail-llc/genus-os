"""
Graduated Escalation — progressively stronger recovery messages for failing agents.

Tracks consecutive errors and returns escalation messages at thresholds.
Resets on success. Prevents agents from spinning in retry loops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Escalation thresholds
THRESHOLD_DIFFERENT_STRATEGY = 3
THRESHOLD_REDUCE_SCOPE = 4
THRESHOLD_STOP = 5
HARD_ABORT_TOTAL_ERRORS = 10


@dataclass
class EscalationManager:
    """Tracks consecutive errors and produces escalation messages."""

    consecutive_errors: int = 0
    total_errors: int = 0
    _stop_issued: bool = False

    def record_error(self) -> None:
        """Record a tool call error."""
        self.consecutive_errors += 1
        self.total_errors += 1

    def record_success(self) -> None:
        """Record a successful tool call. Resets consecutive count."""
        self.consecutive_errors = 0

    def should_abort(self) -> bool:
        """Whether the agent should be force-stopped."""
        return self.total_errors >= HARD_ABORT_TOTAL_ERRORS

    def get_escalation_message(self) -> str | None:
        """Return the appropriate escalation message, or None if not at threshold.

        Called after recording all errors for a given iteration.
        Messages at levels 1-2 are handled by the basic error feedback loop.
        """
        if self.consecutive_errors >= THRESHOLD_STOP and not self._stop_issued:
            self._stop_issued = True
            return (
                "[ESCALATION — STOP]\n"
                f"You have failed {self.consecutive_errors} consecutive tool calls. "
                "STOP attempting tool calls. Summarize:\n"
                "1. What you were trying to accomplish\n"
                "2. What worked\n"
                "3. What failed and why\n"
                "4. What a human should do next\n"
                "Return this summary as your final response."
            )
        if self.consecutive_errors >= THRESHOLD_REDUCE_SCOPE:
            return (
                "[ESCALATION — REDUCE SCOPE]\n"
                f"You have failed {self.consecutive_errors} consecutive tool calls. "
                "REDUCE SCOPE: Focus on completing only the single most critical "
                "subtask. Skip everything non-essential."
            )
        if self.consecutive_errors >= THRESHOLD_DIFFERENT_STRATEGY:
            return (
                "[ESCALATION — CHANGE STRATEGY]\n"
                f"You have failed {self.consecutive_errors} consecutive tool calls. "
                "Your current approach is not working. Try a COMPLETELY DIFFERENT "
                "strategy — different tools, different arguments, or different order."
            )
        return None
