"""
Graduated Escalation — progressively stronger recovery messages for failing agents.

Tracks consecutive errors and returns escalation messages at thresholds.
Prevents agents from spinning in retry loops.

Success is credited, in two different ways. The consecutive count resets
outright; the hard-abort budget is paid back one error at a time
(``ERRORS_FORGIVEN_PER_SUCCESS``), so a run that recovers from every failure
never dies of them while a run that fails faster than it recovers still does.
This docstring used to say "Resets on success" and only the first of those was
true — the abort counted cumulative errors and never reset, which killed a
measured run at 341 seconds of a 1,200-second budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from robothor.engine.models import ErrorType

logger = logging.getLogger(__name__)

# Escalation thresholds
THRESHOLD_DIFFERENT_STRATEGY = 3
THRESHOLD_REDUCE_SCOPE = 4
THRESHOLD_STOP = 5
HARD_ABORT_TOTAL_ERRORS = 10

#: How much of the abort budget one successful tool call pays back.
#:
#: MEASURED 2026-09-15: a benchmark run ended `"Too many errors (11 total).
#: Summarize progress."` at 341 seconds of a 1,200-second budget with a score
#: of 0.0. The count was cumulative and never reset, so an agent that hit ten
#: errors spread across a whole run was force-stopped even when the last nine
#: calls had all succeeded — and against the web these tasks use (403s from one
#: conference site, 429s from two APIs, a bot check on a third) ten cumulative
#: failures is a low bar. The whole point of retrying a hostile network is that
#: some of the retries work.
#:
#: One, not a full reset: an agent that fails ten times, succeeds once and
#: fails ten more is still a broken run, and a reset would make the abort
#: unreachable for any agent that ever succeeds — which is every agent.
ERRORS_FORGIVEN_PER_SUCCESS = 1


@dataclass
class EscalationManager:
    """Tracks consecutive errors and produces escalation messages."""

    consecutive_errors: int = 0
    #: Every error this run has hit. Reported to operators and read by the run
    #: summary, so it stays the TRUE total — forgiveness belongs to the abort
    #: decision, never to the record of what happened.
    total_errors: int = 0
    #: Errors charged against the hard-abort budget. Diverges from
    #: ``total_errors`` the moment a call succeeds.
    _abort_charge: int = 0
    _stop_issued: bool = False
    _last_error_type: ErrorType = ErrorType.UNKNOWN
    _error_type_counts: dict[ErrorType, int] = field(default_factory=dict)
    # (tool_name, error_msg_prefix) -> occurrence count for per-kind STOP RETRYING hints
    _error_kind_counts: dict[tuple[str, str], int] = field(default_factory=dict)

    def record_error(self, error_type: ErrorType = ErrorType.UNKNOWN) -> None:
        """Record a tool call error with optional type classification."""
        self.consecutive_errors += 1
        self.total_errors += 1
        self._abort_charge += 1
        self._last_error_type = error_type
        self._error_type_counts[error_type] = self._error_type_counts.get(error_type, 0) + 1

    def record_error_kind(self, tool_name: str, error_msg: str) -> None:
        """Track (tool_name, error_msg_prefix) pairs for repeated-error hints.

        Called alongside record_error() for every failing tool call.
        The prefix is the first 120 chars of the message so minor variations
        in trailing details (e.g. IDs) don't defeat deduplication.
        """
        prefix = (error_msg or "")[:120].strip()
        key = (tool_name, prefix)
        self._error_kind_counts[key] = self._error_kind_counts.get(key, 0) + 1

    def record_success(self) -> None:
        """Record a successful tool call.

        Resets the consecutive count, and pays back one error of the
        hard-abort budget — see ``ERRORS_FORGIVEN_PER_SUCCESS``. Never below
        zero: a run that succeeds a hundred times must not bank a hundred free
        failures, which would leave the threshold meaning nothing for the rest
        of it.
        """
        self.consecutive_errors = 0
        self._abort_charge = max(0, self._abort_charge - ERRORS_FORGIVEN_PER_SUCCESS)

    def should_abort(self) -> bool:
        """Whether the agent should be force-stopped.

        Reads the charge, not the total: the question is whether this run is
        failing, not whether it has ever failed.
        """
        return self._abort_charge >= HARD_ABORT_TOTAL_ERRORS

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

    def get_repeated_error_hints(self, threshold: int = 2) -> list[str]:
        """Return STOP RETRYING hints for error types that have occurred >= threshold times.

        Each hint tells the agent the exact error kind it's repeating and suggests
        an alternative action. Keeps agents from burning iterations on blocked paths.

        Per-kind hints (tool_name + error_msg_prefix) are emitted first — they are
        more specific and actionable than the coarser per-ErrorType hints below.
        """
        hints = []

        # Per-kind hints: same tool + same error message repeated >= threshold times
        for (tool_name, msg_prefix), count in self._error_kind_counts.items():
            if count >= threshold:
                hints.append(
                    f"STOP RETRYING {tool_name}: {msg_prefix}. "
                    "Try a different tool, different arguments, or skip this step."
                )

        for error_type, count in self._error_type_counts.items():
            if count < threshold:
                continue
            if error_type == ErrorType.AUTH:
                hints.append(
                    f"STOP RETRYING: authentication error has occurred {count}x. "
                    "This request is unauthorized — check credentials or mark requires_human."
                )
            elif error_type == ErrorType.RATE_LIMIT:
                hints.append(
                    f"STOP RETRYING: rate-limit has occurred {count}x. "
                    "Back off and try a lighter-weight approach, or skip this step."
                )
            elif error_type == ErrorType.NOT_FOUND:
                hints.append(
                    f"STOP RETRYING: not-found has occurred {count}x. "
                    "The resource does not exist — use list_directory or search to find the correct path."
                )
            elif error_type == ErrorType.PERMISSION:
                hints.append(
                    f"STOP RETRYING: permission-denied has occurred {count}x. "
                    "You do not have access — try an alternative approach or mark requires_human."
                )
            elif error_type == ErrorType.TIMEOUT:
                hints.append(
                    f"STOP RETRYING: timeout has occurred {count}x. "
                    "This operation consistently times out — reduce scope or skip and note the limitation."
                )
            elif error_type == ErrorType.DEPENDENCY:
                hints.append(
                    f"STOP RETRYING: dependency error has occurred {count}x. "
                    "A required module or service is missing — skip this step or log it."
                )
            else:
                hints.append(
                    f"STOP RETRYING: {error_type.value if hasattr(error_type, 'value') else error_type} "
                    f"has occurred {count}x. Stop retrying this path — try a "
                    "different approach or skip this step."
                )
        return hints

    @property
    def at_change_strategy_threshold(self) -> bool:
        """Whether we're at or past the CHANGE_STRATEGY threshold."""
        return self.consecutive_errors >= THRESHOLD_DIFFERENT_STRATEGY
