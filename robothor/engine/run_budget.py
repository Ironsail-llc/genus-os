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

import logging
import os

logger = logging.getLogger(__name__)

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
    # Write-FIRST, improve-after: a graded run got this note with ~220s left
    # yet wrote nothing for ~120 more seconds. The order is the message.
    return (
        f"[SYSTEM] Time budget: {int(elapsed)}s used of {int(hard_timeout)}s, "
        f"about {remaining}s left. FIRST write your current partial answer to "
        "the location the task asked for, even if incomplete — then keep "
        "improving and overwrite it. Say plainly what is missing. Do not "
        "start a new subtask; anything unwritten when the budget expires is "
        "lost."
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


# Tempo-scaled watchdog budgets moved to their own module (how long may a run
# be SILENT, given which model answers). Re-exported so importers keep working.
# The finalization cluster moved to its own module — it answers a different
# question (what may a run spend AFTER its loop ends) and was crowding this
# one. Re-exported so existing importers keep working.
from robothor.engine.finalization_budget import (  # noqa: E402
    FINALIZATION_TIMEOUT as FINALIZATION_TIMEOUT,
)
from robothor.engine.finalization_budget import (  # noqa: E402
    FINALIZATION_TOTAL_BUDGET as FINALIZATION_TOTAL_BUDGET,
)
from robothor.engine.finalization_budget import (  # noqa: E402
    FinalizationBudget as FinalizationBudget,
)
from robothor.engine.finalization_budget import (  # noqa: E402
    bounded_finalization as bounded_finalization,
)
from robothor.engine.watchdog_budgets import (  # noqa: E402
    WatchdogBudgets as WatchdogBudgets,
)
from robothor.engine.watchdog_budgets import (  # noqa: E402
    chain_for as chain_for,
)
from robothor.engine.watchdog_budgets import (  # noqa: E402
    effective_wallclock_ceiling as effective_wallclock_ceiling,
)
from robothor.engine.watchdog_budgets import (  # noqa: E402
    watchdog_budgets_for as watchdog_budgets_for,
)
