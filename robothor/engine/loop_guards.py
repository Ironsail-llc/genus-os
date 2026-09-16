"""May the run loop take another iteration?

Extracted from `_run_loop`, which is 1,059 lines inside a 2,938-line file. The
competitive analysis calls that god-object "the disease, not a symptom" and
puts decomposing it first, because it hides correctness bugs. These guards are
a fair example: until they moved out here they could only be exercised by
driving the whole loop, so their ORDER — which is load-bearing — was asserted
nowhere.

The order, and why each step sits where it does:

    wallclock -> steer -> interrupt -> watchdog -> runaway

The wallclock branch does NOT end the run itself when a watchdog exists. It
TRIPS the watchdog and falls through, so the watchdog branch below is what
ends it, and `execute()` maps the result to TIMEOUT exactly as if the watchdog
had fired on its own. Making wallclock return directly would silently change a
timed-out run's terminal state to ERROR.

A steer is consumed before the interrupt check so that a steer arriving in the
same pass as a halt is not left pending — it would otherwise survive into a
resumed run and be applied at a moment the operator never chose.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class GuardState:
    """Guard state the loop carries between iterations.

    Both fields are one-shot latches for things that must be SAID once while
    the behaviour behind them continues: the loop runs its checks every
    iteration, and without a latch the 500K warning would fire on every one.
    """

    runaway_alerted: bool = False
    #: Whether the agent has already been told a credential was found. The
    #: REDACTION that accompanies it is not latched — only the notice is.
    secret_notified: bool = False


def check_iteration_guards(
    session: Any,
    agent_config: Any,
    *,
    watchdog: Any,
    wallclock_deadline: float | None,
    wallclock_ceiling: int,
    state: GuardState,
) -> bool:
    """True when the loop must stop. Side effects happen here, as they did inline."""
    if _wallclock_expired(session, watchdog, wallclock_deadline, wallclock_ceiling):
        return True
    _absorb_steer(session)
    if _interrupted(session):
        return True
    if _watchdog_aborted(session, watchdog):
        return True
    return _runaway(session, agent_config, state)


def _wallclock_expired(session: Any, watchdog: Any, deadline: float | None, ceiling: int) -> bool:
    """The loop reading its own clock.

    On 2026-08-25 a run blew through its 1200s ceiling to 3110s with THREE
    layers silent at once: the outer asyncio.timeout, the watchdog's
    task.cancel(), and the deadline warning. All three live BESIDE the loop —
    an outer context manager, a sibling task, a message injection — and can go
    silent together while the loop keeps iterating. This check cannot be
    cancelled, starved or unhooked without also ending the loop it is part of.
    """
    if deadline is None or time.monotonic() < deadline:
        return False

    last = getattr(watchdog, "last_activity_desc", None) if watchdog else None
    reason = (
        f"Circuit-breaker hard timeout ({ceiling}s) — loop self-check; "
        f"last activity: {last or 'unknown'}"
    )
    logger.warning("Run loop self-check: %s", reason)
    if watchdog is not None:
        # Trip the flag so the watchdog branch below ends the run, and
        # execute() maps it to TIMEOUT rather than ERROR.
        watchdog.trip(reason)
        return False
    session.record_error(reason)
    return True


def _absorb_steer(session: Any) -> None:
    text = session.consume_pending_steer()
    if not text:
        return
    session.messages.append({"role": "user", "content": f"[operator steering update]\n{text}"})
    logger.info("Live steer injected into run %s", session.run_id)


def _interrupted(session: Any) -> bool:
    """A halt is CANCELLED, not FAILED — deliberately not `record_error`.

    The message may be "" when the operator halted without text, or None when
    no interrupt is pending; only the latter means "keep going".
    """
    message = session.consume_interrupt()
    if message is None:
        return False
    note = f"Run interrupted by operator: {message}" if message else "Run interrupted by operator"
    session.messages.append({"role": "user", "content": f"[operator interrupt] {note}"})
    session.run.outcome_notes = (
        f"{session.run.outcome_notes}; {note}" if session.run.outcome_notes else note
    )
    session.mark_interrupted(note)
    logger.info("Run %s interrupted by operator", session.run_id)
    return True


def _watchdog_aborted(session: Any, watchdog: Any) -> bool:
    if not (watchdog and watchdog.should_abort):
        return False
    logger.warning("Run loop aborting: watchdog flagged abort — %s", watchdog.abort_reason)
    session.record_error(watchdog.abort_reason)
    return True


def _runaway(session: Any, agent_config: Any, state: GuardState) -> bool:
    """Fleet-wide token guard: alert at 500K, hard stop at 5M."""
    from robothor.engine.runner import (
        RUNAWAY_TOKEN_ALERT,
        RUNAWAY_TOKEN_HARD_CAP,
        _send_soft_runaway_alert,
    )

    used = (session.run.input_tokens or 0) + (session.run.output_tokens or 0)

    if used >= RUNAWAY_TOKEN_HARD_CAP:
        reason = f"runaway_token_cap_hit ({used}/{RUNAWAY_TOKEN_HARD_CAP})"
        logger.error(
            "Runaway-token hard cap hit: agent=%s run=%s tokens=%d",
            agent_config.id,
            session.run_id,
            used,
        )
        _spawn_hard_cap_alert(session, agent_config, used)
        session.run.budget_exhausted = True
        session.record_error(reason)
        return True

    if not state.runaway_alerted and used >= RUNAWAY_TOKEN_ALERT:
        state.runaway_alerted = True
        logger.warning(
            "Runaway-token alert: agent=%s run=%s tokens=%d",
            agent_config.id,
            session.run_id,
            used,
        )
        try:
            _send_soft_runaway_alert(
                agent_config.id,
                str(session.run_id),
                used,
                session.run.model_used,
                session.run.total_cost_usd or 0.0,
            )
        except Exception:
            logger.debug("Runaway-token alert dispatch failed", exc_info=True)
    return False


def _spawn_hard_cap_alert(session: Any, agent_config: Any, used: int) -> None:
    """Fire-and-forget, through the task registry.

    NOT a bare `create_task`: the loop only weakly references those and can GC
    one before it runs, losing exactly the alert we can least afford to lose
    (audit 2026-05-29).
    """
    try:
        from robothor.engine.alerts import alert as _alert
        from robothor.engine.task_registry import get_task_registry

        get_task_registry().spawn(
            _alert(
                "critical",
                f"Runaway-token hard cap: {agent_config.id}",
                f"run_id={session.run_id} tokens={used:,} model={session.run.model_used}",
            ),
            name=f"runaway-hardcap-alert:{agent_config.id}",
        )
    except Exception:
        logger.debug("Runaway-token alert dispatch failed", exc_info=True)


def append_engine_note(session: Any, note: str | None, workspace: str | None = None) -> None:
    """Put an engine-context note in front of the model, or nothing if there is none.

    Given a ``workspace``, a check-in becomes a COMPARISON where the task
    stated a shape: "are you making progress" is a question an agent answers
    yes to, and "your header is X and the task requires Y" is not — it is
    checkable, and it is what the graders and the operator actually read.
    Silent, and cheap, on every task that stated no shape, which is most of
    them.
    """
    from robothor.engine.session import ENGINE_CONTEXT_ROLE

    if not note:
        return
    with contextlib.suppress(Exception):
        from robothor.engine.deliverable_contract import contract_checkin_note
        from robothor.engine.feature_flags import deliverable_contract_mode

        if deliverable_contract_mode() != "off":
            text = str(getattr(session, "originating_message", "") or "")
            comparison = contract_checkin_note(text, workspace)
            if comparison:
                note = f"{note}\n{comparison}"
    session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": note})


def nudge_for_missing_deliverable(session: Any, workspace: str | None = None) -> bool:
    """The agent stopped; does it still owe a correct deliverable?

    Two questions, asked in order: is the artifact the task named there at all,
    and — if it is — is it the shape the task described. True means "do not end
    this iteration": a message has been appended and the loop should continue.
    This is a guard in the same sense as the others here.

    It has to live on this path rather than in `run_finalizer`, where the same
    verdicts land: the finalizer runs AFTER the loop and can record a missing or
    misshapen artifact but never prevent one. WildClawBench task_4 spent 333
    requests and 704 seconds, reported "completed", and wrote nothing; three
    Productivity tasks a year's engineering later reported "completed" with the
    file present and every grader criterion at 0.

    Both budgets are kept on the SESSION so the loop needs no counter of its own.
    """
    from robothor.engine.deliverable_contract import deliverable_nudge
    from robothor.engine.session import ENGINE_CONTEXT_ROLE

    used = int(getattr(session, "_deliverable_nudges", 0) or 0)
    nudge = deliverable_nudge(session, used)
    if nudge:
        session._deliverable_nudges = used + 1
        session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": nudge})
        logger.info("Deliverable nudge: the artifact the task named is absent")
        return True
    return reask_for_wrong_deliverable_shape(session, workspace)


def reask_for_wrong_deliverable_shape(session: Any, workspace: str | None = None) -> bool:
    """The artifact exists; is it the SHAPE the task described?

    The guard above asks whether the file is there. Measured 2026-09-16, three
    benchmark tasks scored 0 with the file there: the right path with the
    agent's own TSV columns, the right directory left empty while the answer
    went to a name of its own, the right document with the headings renamed.
    An existence check cannot see any of that.

    True means "do not end this iteration". Ladder-gated, unlike the guard
    above, because this one CHANGES the run: at `enforce` it re-asks once with
    the report; at `observe` it records the verdict in the log and lets the run
    end; at `off` it is never computed. The budget is one for the reason the
    nudge budget is one — an unbounded "you are not done" is a loop.
    """
    from robothor.engine.deliverable_contract import contract_reask_note, contract_report_for_run
    from robothor.engine.feature_flags import deliverable_contract_mode
    from robothor.engine.session import ENGINE_CONTEXT_ROLE

    mode = deliverable_contract_mode()
    if mode == "off":
        return False
    run = getattr(session, "run", None)
    report = contract_report_for_run(run, session, workspace)
    if report is None or report.satisfied:
        return False
    # Kept on the session so the run finalizer can tell a shape that was never
    # re-asked about from one the agent was given a chance to fix.
    session._deliverable_contract_report = report
    if mode != "enforce":
        logger.warning(
            "deliverable contract %s: run %s would be held for %s",
            mode,
            getattr(run, "id", "?"),
            report.message.replace("\n", " | "),
        )
        return False
    if int(getattr(session, "_deliverable_contract_reasks", 0) or 0) >= 1:
        return False
    note = contract_reask_note(report)
    if not note:
        return False
    session._deliverable_contract_reasks = 1
    session.messages.append({"role": ENGINE_CONTEXT_ROLE, "content": note})
    logger.warning(
        "deliverable contract enforce: run %s re-asked once for %s",
        getattr(run, "id", "?"),
        report.message.replace("\n", " | "),
    )
    return True
