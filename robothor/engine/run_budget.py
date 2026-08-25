"""What a single run is allowed to spend, and what it is told about it.

Three limits that all answer the same question — how much may this run
consume before something intervenes — and that were accumulating in the
runner alongside everything else:

* a token ceiling, so a loop that has escaped cannot bill indefinitely;
* a compaction trigger, so a long conversation stops re-sending itself;
* a wall-clock warning, so an agent about to be killed can save its work.

The last one exists because the alternative is losing the work entirely. A
run that hits its ceiling is killed outright, and whatever it had done but
not written is gone — the effort happened, the artefact never existed.
"""

from __future__ import annotations

import os

DEADLINE_WARNING_FRACTION = 0.8


def deadline_warning(elapsed: float, hard_timeout: float) -> str | None:
    """The note to inject when a run is close to its ceiling, or None.

    A run that hits its wall-clock limit is killed outright, and whatever it
    had done but not yet written is lost: the work happened, the artefact
    never existed, and the run scores as though nothing was attempted.
    Measured on WildClawBench's Productivity Flow tasks — three runs at
    925s/900s, 1020s/900s and 1320s/1200s, all scoring zero — while the
    graders award per criterion, so partial output would have earned partial
    credit.

    Every scheduled agent here carries a timeout, so this is not a benchmark
    quirk: the fleet loses whole runs the same way.

    Deliberately not a hard stop. The agent is told how long is left and asked
    to save what it has; what "save" means belongs to the task, not to the
    engine.
    """
    if hard_timeout <= 0:
        return None
    if elapsed < hard_timeout * DEADLINE_WARNING_FRACTION:
        return None
    remaining = max(0, int(hard_timeout - elapsed))
    return (
        f"[SYSTEM] Time budget: {int(elapsed)}s used of {int(hard_timeout)}s, "
        f"about {remaining}s left. Save your partial work NOW — write whatever "
        "results you already have to the location the task asked for, even if "
        "incomplete, and say plainly what is missing. Do not start a new "
        "subtask; anything unwritten when the budget expires is lost."
    )


def proactive_compaction_threshold(max_input_tokens: int) -> int:
    """The token estimate at which the in-loop compaction fires.

    Two bounds, take the smaller:

    * half the sizing model's window — the original overflow guard, still the
      binding constraint on small-window fallbacks (a 40K fallback must
      compact at 20K, not wait for an absolute budget it can never hold);
    * an absolute budget (ROBOTHOR_COMPACTION_TRIGGER_TOKENS, default 80,000)
      — because on the fleet primary's 1M window the fraction alone was
      524,288 tokens, 7.4x the p95 per-call input, and the entire graduated
      compaction system sat shipped-and-inert with ZERO firings in 7 days
      while re-sent history was 28% of all input (audit 2026-08-24).

    0 disables the absolute budget (window fraction only) — a documented
    escape hatch, not a silent one.
    """
    fraction = int(max_input_tokens * 0.50)
    raw = os.environ.get("ROBOTHOR_COMPACTION_TRIGGER_TOKENS", "80000")
    try:
        budget = int(raw)
    except ValueError:
        budget = 80_000
    if budget <= 0:
        return fraction
    return min(fraction, budget)


def effective_wallclock_ceiling(timeout_seconds: int) -> int:
    """The hard wall-clock bound a run actually gets.

    An agent's own ``timeout_seconds`` when positive; the fleet ceiling when
    the agent declares 0 ("no cap") — nothing runs unbounded. One derivation,
    used by both the watchdog setup and the run loop's self-check, so the two
    can never disagree about what the ceiling is.
    """
    from robothor.engine.stall_watchdog import _fleet_wallclock_ceiling

    return timeout_seconds if timeout_seconds > 0 else _fleet_wallclock_ceiling()
