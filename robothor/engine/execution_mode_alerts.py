"""Tell the operator when the economics change -- once, and in their terms.

The fleet ran 29 hours on the local tier and nobody was told; they found out
because agents felt slow. The opposite failure is equally bad: once admission
starts deferring, fifty deferred ticks must not become fifty pushes. Deferrals
go to ``agent_guardrail_events`` where they can be counted; the operator gets
ONE message when the mode itself changes.

``provider_alerts`` already established what a good page says. A unit name is
not a consequence: "OpenRouter exhausted" makes the operator go and work out
what it means for them, while "now serving locally, background work paced, your
Telegram turns unaffected" does not.
"""

from __future__ import annotations

import logging

from robothor.engine.provider_alerts import _deliver, _in_pytest

logger = logging.getLogger(__name__)

#: One page per entry into a mode. Cleared on exit, so a genuinely new outage
#: later still reaches the operator.
_PAGED: set[str] = set()


def reset_for_test() -> None:
    """Clear the once-only latch. Tests only."""
    _PAGED.clear()


def _humanize(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.0f}h"
    if seconds >= 60:
        return f"{seconds / 60:.0f}m"
    return f"{seconds:.0f}s"


def alert_mode_entered(mode: str, *, background_deferred: int = 0) -> None:
    """Page once: the fleet is now operating under a different set of rules."""
    if _in_pytest() or mode in _PAGED:
        return
    _PAGED.add(mode)
    deferred = (
        f"{background_deferred} background agent(s) paced. "
        if background_deferred
        else "Background work is paced to the device. "
    )
    try:
        _deliver(
            "warning",
            f"Execution mode: {mode.upper()}",
            (
                f"Requests are now being served on the {mode} tier, so the "
                f"{mode} ruleset applies: time budgets scale, and cost governs "
                "differently. "
                + deferred
                + "Interactive turns keep a reserved slot and are unaffected. "
                "No action is required — this reverses itself when the other "
                "tier serves a request again."
            ),
        )
    except Exception:  # noqa: BLE001 - a page must never break the engine
        logger.warning("Could not page execution-mode entry for %s", mode, exc_info=True)


def alert_mode_left(mode: str, *, duration_seconds: float = 0.0, catch_up_count: int = 0) -> None:
    """Page once: the fleet is back, and here is what it cost."""
    _PAGED.discard(mode)
    if _in_pytest():
        return
    try:
        _deliver(
            "info",
            f"Execution mode: left {mode.upper()}",
            (
                f"Served on the {mode} tier for {_humanize(duration_seconds)}. "
                f"{catch_up_count} deferred run(s) will be caught up, paced so "
                "the recovery does not become the next outage."
            ),
        )
    except Exception:  # noqa: BLE001
        logger.warning("Could not page execution-mode exit for %s", mode, exc_info=True)
