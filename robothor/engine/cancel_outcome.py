"""What a cancellation actually was, and what to call it.

Extracted from ``runner`` rather than growing it past its decomposition
ratchet — the runner is the god-object the ratchet exists to shrink.

Two bugs shared one branch here. The label was derived from
``agent_config.timeout_seconds``, which ``_defaults.yaml`` pins to 0
fleet-wide, so a run that genuinely blew the 3600s fleet ceiling was filed as
"Run cancelled externally" — and ``GENUINE_TIMEOUT_SQL`` then excluded it from
the timeout rate. And both outcomes wrote ``status='timeout'``, which is why
``resume`` (selecting ``status='running'``) recovered nothing from a graceful
restart: the shutdown had already tombstoned the runs as timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass

from robothor.engine.models import RunStatus


@dataclass(frozen=True)
class _CancelOutcome:
    """What a cancellation actually was, and what to call it."""

    status: RunStatus
    reason: str


def _cancel_outcome(
    *,
    timed_out: bool,
    declared_timeout_seconds: int,
    effective_ceiling: int,
    last_activity: str,
    waiting_on: str = "",
) -> _CancelOutcome:
    """Classify a cancellation from EVIDENCE, not from a config value.

    The old branch keyed on ``agent_config.timeout_seconds``, which
    ``_defaults.yaml`` pins to 0 fleet-wide. So a run that genuinely blew the
    3600s fleet ceiling was labelled "Run cancelled externally" and
    ``GENUINE_TIMEOUT_SQL`` then excluded it from the timeout rate -- real
    timeouts reported as deploy artifacts.

    ``timed_out`` says the run's own clock fired; anything else reaching the
    cancel arm came from outside. The reason names the ceiling that was
    actually applied, and the wait it died inside when there was one.
    """
    from robothor.engine.analytics import EXTERNAL_CANCEL_PREFIX

    tail = f"; last activity: {last_activity}"
    if waiting_on:
        tail += f"; waiting on {waiting_on}"
    if timed_out:
        ceiling = declared_timeout_seconds if declared_timeout_seconds > 0 else effective_ceiling
        return _CancelOutcome(
            status=RunStatus.TIMEOUT,
            reason=f"Circuit-breaker hard timeout ({ceiling}s){tail}",
        )
    return _CancelOutcome(
        status=RunStatus.CANCELLED,
        reason=f"{EXTERNAL_CANCEL_PREFIX}{tail}",
    )


def terminal_run(session, outcome, reason, diag, watchdog_fired):
    """Write the terminal row matching the classification.

    ``watchdog_fired`` keeps the run's OWN watchdog authoritative: when it
    supplied the abort reason, the run hit its own limit and is a timeout
    regardless of which exception carried it out.
    """
    if outcome.status is RunStatus.CANCELLED and not watchdog_fired:
        return session.cancelled(reason=reason, traceback=diag)
    return session.timeout(reason=reason, traceback=diag)
