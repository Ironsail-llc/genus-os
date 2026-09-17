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

import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from robothor.engine.run_budget import DEADLINE_WARNING_FRACTION

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

#: `off` has to look like the engine that shipped in the journal too, and main
#: emitted its deadline line from the runner's logger. A reader filtering on
#: `robothor.engine.runner` would otherwise see the line disappear from an `off`
#: box and conclude the control had been removed.
_MAIN_LOGGER = logging.getLogger("robothor.engine.runner")

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
    left = int(max(0.0, remaining) / average)
    return (
        f"You have taken {iteration} {_plural(iteration, 'step')} at about "
        f"{average:.0f}s each, so about {left} more {_plural(left, 'step')} fit in "
        "the time left."
    )


def _plural(count: int, noun: str) -> str:
    """The model reads these sentences; "1 steps" is the engine miscounting."""
    return noun if count == 1 else f"{noun}s"


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
    mode: str = "off",
) -> str | None:
    """The text for one rung, or None when there is nothing to say.

    ``mode`` is here for one reason: under ``off`` the 80% rung must be the
    string main produced, with nothing appended. The pace sentence is a feature
    of the ladder, not of the note.
    """
    from robothor.engine.deliverables import (
        deadline_note,
        declared_paths,
        missing_deliverables_note,
    )

    remaining = max(0, int(hard_timeout - elapsed))
    pace = "" if mode == "off" else pace_phrase(elapsed, float(remaining), iteration)

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

    ``off`` is bit-for-bit the engine that shipped: ONE note at 80%, with main's
    exact text and no pace sentence appended, announced with main's exact log
    line at main's level (INFO), and with no iteration guard — main had none.
    Three things that all read as improvements and all belong to the ladder
    rather than to the baseline, because an operator who turns this off has to
    get the previous engine back, and a sweep comparing `off` to `enforce` is
    only measuring the controls if `off` is what it says it is.
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

        ``iteration < 1`` is silent on the ladder's rungs: the pace numbers are
        measured over completed steps, and a run that has taken none has no
        pace. Not under ``off``, which had no such condition.
        """
        if watchdog is None or (iteration < 1 and self.mode != "off"):
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
            mode=self.mode,
        )
        if not note:
            return None

        percent = round(fraction * 100)
        if self.mode == "off":
            # Main's line, at main's level. Raising it to WARNING is item 0's
            # fix and belongs to the ladder: `observe` is the default every
            # existing install gets, so the fix is live without `off` having to
            # stop being the baseline.
            _MAIN_LOGGER.info("Deadline warning issued at iteration %d", iteration)
            return note
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


def mode_for_run(run_id: str) -> str:
    """The rung this run resolved, read once and cached on its session.

    ``step_efficiency_mode()`` goes through the DB-backed flag store (5s TTL, a
    synchronous read on a miss). The pacer and the repeat guard each resolve it
    once per run; the timeout clamp is called from ``exec``, which the profiled
    run invoked 41 times, and a database read per tool call on the event loop is
    not a price a pacing aid gets to charge.

    A call with no live session — an untracked run, a tool called from outside a
    run — falls back to resolving it, which is the pre-cache behaviour.
    """
    from robothor.engine.feature_flags import step_efficiency_mode

    if not run_id:
        return step_efficiency_mode()
    from robothor.engine import session_registry

    session = session_registry.lookup(run_id)
    if session is None:
        return step_efficiency_mode()
    mode = getattr(session, "step_efficiency_mode", None)
    if mode is None:
        mode = step_efficiency_mode()
        with contextlib.suppress(AttributeError):
            session.step_efficiency_mode = mode
    return str(mode)


def clamp_tool_timeout(
    requested: int,
    *,
    watchdog: Any = None,
    mode: str | None = None,
    run_id: str = "",
) -> tuple[int, str]:
    """``(effective_timeout, note)`` for one tool call, note empty if unchanged.

    ``min(requested, remaining - reserve)``, floored, so a single call cannot
    consume the whole run. Every one of the 41 ``exec`` calls in the profiled
    run asked for 900s against a 1200s budget; the only ceiling the engine had
    was the tool's own constant, which knows nothing about the run asking.

    The reserve is what the run needs AFTER the call returns — writing the
    deliverable is the whole point of noticing the deadline at all — and the
    floor is what stops a nearly-expired run from turning every command into a
    guaranteed timeout.

    ``watchdog`` defaults to the live one; no watchdog, or a run with no
    ceiling, means nothing to clamp to and the request stands.
    """
    if mode is None:
        mode = mode_for_run(run_id)
    if mode == "off" or requested <= 0:
        return requested, ""
    if watchdog is None:
        from robothor.engine.stall_watchdog import _active_watchdog_var

        watchdog = _active_watchdog_var.get()
    if watchdog is None:
        return requested, ""
    hard_timeout = float(getattr(watchdog, "_hard_timeout", 0) or 0)
    if hard_timeout <= 0:
        return requested, ""

    remaining = int(hard_timeout - float(getattr(watchdog, "elapsed_seconds", 0) or 0))
    allowed = max(MIN_TOOL_TIMEOUT_SECONDS, remaining - TIMEOUT_RESERVE_SECONDS)
    if allowed >= requested:
        return requested, ""

    note = f"timeout clamped to {allowed}s: the run has {max(0, remaining)}s left"
    if mode != "enforce":
        logger.warning(
            "step-efficiency observe: run %s would have %s (requested %ds)",
            run_id or "?",
            note,
            requested,
        )
        return requested, ""
    logger.warning("Tool timeout clamped to %ds on run %s", allowed, run_id or "?")
    return allowed, note


#: The check-in that already shipped, fired at ``max_iterations``.
_SHIPPED_CHECKIN = (
    "[SYSTEM] Progress check-in (iteration {iteration}): Are you making progress "
    "toward the goal? If you are stuck in a loop or have completed the task, "
    "provide your final answer and stop calling tools. If making progress, continue."
)

#: The added one. "Are you making progress" is a question an agent answers yes
#: to; what is on disk, and where, is checkable — and it is what the graders
#: and the operator actually read.
#:
#: It asks about SHAPE as well as path, measured 2026-09-16. The path-only
#: version fired once on a task that then wrote a five-column file to the right
#: path where the spec required six named columns: every criterion scored 0 and
#: every pacing control in the engine had fallen silent, because the file
#: existed. A check-in that stops at "is it there" certifies exactly the
#: failure it was built to catch.
_DELIVERABLE_CHECKIN = (
    "[SYSTEM] Progress check-in (iteration {iteration}): what have you WRITTEN to "
    "the deliverable path so far? Name the path and say what is in it. If nothing "
    "is written yet, write your current partial answer there NOW before continuing "
    "— an incomplete file at the requested path is worth more than a perfect "
    "answer that was never saved.\n"
    "Then check its SHAPE against the task, not against your own plan: quote the "
    "task's required format back — the exact filename, the exact header or field "
    "names, the exact section headings, the exact directory contents — and show "
    "that what you have written matches it, line for line. If it does not match, "
    "fix it now; a file at the right path in the wrong shape scores the same as "
    "no file at all. If the task is done and the shape matches, give your final "
    "answer and stop calling tools."
)


#: Where in the budget a check-in stops being a question. Before halfway,
#: "nothing written yet" is what working looks like; past it, on a run that
#: has already been asked once, it is the shape that ends at zero.
DIRECTIVE_FRACTION: float = 0.5

#: How many check-ins may find an empty workspace before the engine stops
#: asking. Two, because one is a snapshot and two is a trend — and because the
#: measured runs answered the first one with a plan and kept exploring.
DIRECTIVE_AFTER: int = 2

#: The third rung. It names the path, because "the deliverable path" is what
#: the agent has been inventing; and it forbids the next tool call rather than
#: recommending against it, because the recommendation has demonstrably been
#: read as advice twice already.
_DIRECTIVE_CHECKIN = (
    "[SYSTEM] STOP. You have been asked twice what you have written and the "
    "task's output does not exist: {listed}. More than half this run's time "
    "budget is gone, and anything unwritten when it expires is lost.\n"
    "Your NEXT action is `write_file` to that exact path, with your best "
    "current answer in the shape the task described — however incomplete, and "
    "saying plainly inside it what is missing. Do not read, search, fetch or "
    "run anything else first. Once it exists you may keep improving it by "
    "overwriting."
)


def _nothing_written(task_text: str | None, workspace: str | Path | None) -> list[str]:
    """The declared outputs that are absent, or [] when there is no evidence.

    Evidence-driven on purpose. The trigger could have been the model's own
    answer to the check-in, and that answer is the thing under test: an agent
    that SAYS it has written the file and has not is the exact failure being
    caught. A task that declared no path gives no evidence either way, so it
    never escalates.
    """
    from robothor.engine.deliverables import declared_paths, missing_paths

    paths = declared_paths(task_text)
    if not paths or not workspace:
        return []
    missing = missing_paths(paths, workspace)
    return missing if len(missing) == len(paths) else []


def _directive_or_ask(
    iteration: int,
    session: Any,
    task_text: str | None,
    workspace: str | Path | None,
    fraction: float,
) -> str:
    """The deliverable check-in, escalated if this run keeps writing nothing."""
    missing = _nothing_written(task_text, workspace)
    if not missing or fraction < DIRECTIVE_FRACTION:
        with contextlib.suppress(AttributeError):
            session.empty_checkins = 0
        return _DELIVERABLE_CHECKIN.format(iteration=iteration)
    seen = int(getattr(session, "empty_checkins", 0) or 0) + 1
    with contextlib.suppress(AttributeError):
        session.empty_checkins = seen
    if seen < DIRECTIVE_AFTER:
        return _DELIVERABLE_CHECKIN.format(iteration=iteration)
    logger.warning(
        "Directive check-in issued at iteration %d: %d asks, nothing written", iteration, seen
    )
    return _DIRECTIVE_CHECKIN.format(listed=", ".join(missing))


def checkin_note(
    iteration: int,
    checkin_interval: int,
    mode: str,
    *,
    run_id: str = "",
    session: Any = None,
    task_text: str | None = None,
    workspace: str | Path | None = None,
    fraction: float = 0.0,
) -> str | None:
    """The progress check-in for this iteration, or None.

    The shipped cadence fires at ``max_iterations`` on every rung of the ladder,
    unchanged. ``enforce`` adds one every ``CHECKIN_EVERY`` iterations, because
    the bench agent's ``max_iterations`` is 80 and the run this exists for took
    79 — the shipped check-in never fired once on the run that needed it. Past
    half the budget it also escalates: see ``_directive_or_ask``.
    """
    if iteration < 1:
        return None
    shipped_due = checkin_interval > 0 and iteration % checkin_interval == 0
    cadence_due = mode != "off" and iteration % CHECKIN_EVERY == 0

    # The added cadence wins a collision. An agent whose `max_iterations` is a
    # multiple of 25 (25, 50, 100 …) would otherwise silently lose the
    # deliverable ask at every one of its own cadence points — the control
    # would be off for exactly the agents that check in most often.
    if cadence_due and mode == "enforce":
        logger.warning(
            "Deliverable check-in issued at iteration %d, run %s", iteration, run_id or "?"
        )
        return _directive_or_ask(iteration, session, task_text, workspace, fraction)
    if cadence_due:
        logger.warning(
            "step-efficiency observe: run %s would inject a deliverable check-in at iteration %d",
            run_id or "?",
            iteration,
        )
    if shipped_due:
        return _SHIPPED_CHECKIN.format(iteration=iteration)
    return None
