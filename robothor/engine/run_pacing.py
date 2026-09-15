"""How much time a run has left, and everything that follows from knowing.

Three answers to one question, kept together because they share a single fact —
the live watchdog's ``elapsed`` against its ``_hard_timeout`` — and because
``run_budget`` is at its size ratchet, the same reason the finalization and
tempo-scaling clusters left it:

* the notes the agent is given about its own pace;
* the check-in that asks what it has WRITTEN, not whether it feels productive;
* the ceiling a single tool call may ask for, so one ``exec`` cannot eat a run.

The measurement behind all three (2026-09-15 WildClawBench sweep, the harness's
own budgets): five of twelve Code tasks and one Productivity task hit their
budget and scored ZERO, while every Code task that FINISHED scored 0.9-1.0. The
gap is not capability. One profiled run made 79 LLM calls and 86 tool calls in
1200s, ran the same ``exec`` nine times and re-read one file six times, and was
killed with nothing written. Context was never the constraint — p50 41k tokens.

Before this the engine's entire time-awareness was one note at 80% of the
ceiling, which is late enough to be a eulogy rather than a plan, and it said
nothing about the rate the run was burning. It also announced itself with
``logger.info``, and the benchmark container configures no logging at all — so
Python's ``lastResort`` handler, level WARNING, dropped it. A whole day of
profiling concluded the control had never fired when in fact nobody could see
it. Every line this module emits, including its observe-mode shadow lines, is
therefore WARNING and says so out loud below.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.engine.run_budget import DEADLINE_WARNING_FRACTION

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: Where in the budget the agent is told about it. 80% is the rung that already
#: shipped and is kept exactly where it was; 50% is early enough to change a
#: plan rather than salvage one; 95% is the last moment a write can still land.
#: Three, and only three — a fourth would be noise, and a control that nags gets
#: ignored, which is how a real warning goes unread.
PACE_FRACTIONS: tuple[float, ...] = (0.5, DEADLINE_WARNING_FRACTION, 0.95)

#: Iterations between the progress check-ins. The soft check-in fires at
#: ``max_iterations``, which the bench agent sets to 80 — and the run that
#: prompted this took 79 iterations, so it never fired once. A cadence that only
#: triggers on runs longer than the ones that fail is not a cadence.
CHECKIN_EVERY: int = 25

#: Seconds held back from a tool's timeout so the run can still wrap up after
#: the call returns: writing the deliverable, the finalization pass, the note
#: that says what is missing.
TIMEOUT_RESERVE_SECONDS: int = 30

#: A clamp never goes below this. A 1-second timeout turns every command into a
#: failure and would make a nearly-expired run worse than one with no clamp at
#: all — the point is to leave time for a write, not to guarantee a timeout.
MIN_TOOL_TIMEOUT_SECONDS: int = 5


def pace_phrase(elapsed: float, remaining: float, iteration: int) -> str:
    """ "You have taken N steps at Xs each; about M more fit" — or nothing.

    Empty before the first iteration completes: a rate measured over zero steps
    is not a measurement, and a confident wrong number is worse than silence.
    """
    if iteration < 1 or elapsed <= 0:
        return ""
    average = elapsed / iteration
    if average <= 0:
        return ""
    return (
        f"You have taken {iteration} steps at about {average:.0f}s each, so "
        f"about {int(max(0.0, remaining) / average)} more steps fit in the time left."
    )


def due_fraction(
    elapsed: float,
    hard_timeout: float,
    fired: set[float],
    fractions: tuple[float, ...] = PACE_FRACTIONS,
) -> float | None:
    """The HIGHEST un-fired rung this run has already passed, or None.

    Highest, not lowest: a loop that spends 400s inside one LLM call can cross
    two rungs between checks, and the useful thing to say then is the urgent
    one. The caller marks every rung at or below it spent, so the run cannot
    later be told it is halfway through a budget it has nearly exhausted.
    """
    if hard_timeout <= 0:
        return None
    ratio = elapsed / hard_timeout
    passed = [f for f in fractions if ratio >= f and f not in fired]
    return max(passed) if passed else None


def _budget_line(elapsed: float, hard_timeout: float, remaining: int) -> str:
    return (
        f"[SYSTEM] Time budget: {int(elapsed)}s used of {int(hard_timeout)}s, "
        f"about {remaining}s left."
    )


def note_at(
    fraction: float,
    *,
    elapsed: float,
    hard_timeout: float,
    iteration: int,
    task_text: str | None,
    workspace: str | Path | None,
) -> str | None:
    """The text for one rung, or None when there is nothing to say."""
    from robothor.engine.deliverables import (
        deadline_note,
        declared_paths,
        missing_deliverables_note,
    )

    remaining = max(0, int(hard_timeout - elapsed))
    pace = pace_phrase(elapsed, float(remaining), iteration)

    if fraction == DEADLINE_WARNING_FRACTION:
        # The rung that already shipped, wording untouched: write-FIRST,
        # improve-after, plus the deliverables that are not on disk yet. A
        # graded run got this note with ~220s left and wrote nothing for ~120
        # more seconds, so the order of the sentences is the message.
        base = deadline_note(elapsed, hard_timeout, task_text, workspace)
        if not base:
            return None
        return f"{base}\n{pace}" if pace else base

    missing = missing_deliverables_note(
        declared_paths(task_text), remaining=remaining, workspace=workspace
    )

    if fraction >= 0.95:
        body = (
            "This is your LAST chance to write anything. Stop investigating and "
            "save what you have to the path the task named, however incomplete — "
            "anything unwritten when the budget expires is lost, and partial "
            "results are graded per criterion."
        )
    else:
        body = (
            "You are about halfway. Decide NOW what the smallest complete "
            "deliverable is and get it written to the path the task named; "
            "refine it afterwards by overwriting. Do not start a new subtask "
            "you cannot finish in the time above."
        )

    parts = [f"{_budget_line(elapsed, hard_timeout, remaining)} {body}"]
    if pace:
        parts.append(pace)
    if missing:
        parts.append(missing)
    return "\n".join(parts)


@dataclass
class DeadlinePacer:
    """Per-run state: which rungs this run has had, and which rung is due.

    One instance per run, built from ``feature_flags.step_efficiency_mode()``.
    ``off`` is bit-for-bit the behaviour that already shipped — one note at 80%
    — so an operator who dislikes this can have the old engine back with a flag
    rather than a revert.
    """

    mode: str = "off"
    fired: set[float] = field(default_factory=set)

    @property
    def _rungs(self) -> tuple[float, ...]:
        if self.mode == "off":
            return (DEADLINE_WARNING_FRACTION,)
        return PACE_FRACTIONS

    def note_for(
        self,
        watchdog: Any,
        *,
        iteration: int,
        task_text: str | None,
        workspace: str | Path | None,
        run_id: str = "",
    ) -> str | None:
        """The note to append to the conversation this iteration, or None.

        ``iteration < 1`` is silent on purpose: the pace numbers are measured
        over completed steps, and a run that has taken none has no pace.
        """
        if watchdog is None or iteration < 1:
            return None
        hard_timeout = float(getattr(watchdog, "_hard_timeout", 0) or 0)
        if hard_timeout <= 0:
            return None
        elapsed = float(getattr(watchdog, "elapsed_seconds", 0) or 0)

        fraction = due_fraction(elapsed, hard_timeout, self.fired, self._rungs)
        if fraction is None:
            return None
        # Spend every rung at or below this one, so the cap of three notes holds
        # and a lower rung can never arrive after a higher one.
        self.fired.update(f for f in PACE_FRACTIONS if f <= fraction)

        note = note_at(
            fraction,
            elapsed=elapsed,
            hard_timeout=hard_timeout,
            iteration=iteration,
            task_text=task_text,
            workspace=workspace,
        )
        if not note:
            return None

        percent = round(fraction * 100)
        # The 80% rung is what already shipped, so it injects on every rung of
        # the ladder including `observe` — observe must not take away a control
        # that is already live. The other two are the new behaviour and wait
        # for `enforce`.
        if self.mode == "enforce" or fraction == DEADLINE_WARNING_FRACTION:
            logger.warning(
                "Deadline warning issued at %d%% of budget, iteration %d, run %s",
                percent,
                iteration,
                run_id or "?",
            )
            return note
        logger.warning(
            "step-efficiency observe: run %s would inject the %d%% deadline note "
            "at iteration %d (%s)",
            run_id or "?",
            percent,
            iteration,
            note.replace("\n", " ")[:300],
        )
        return None
