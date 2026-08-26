"""Resuming runs a restart interrupted, instead of reaping them.

The daemon marks every run still `running` at startup as timed out. The work
is lost — even though `CheckpointManager` holds that run's messages and
scratchpad on disk and `_resume_from_checkpoint` can restore them. A
competitive audit of four agent harnesses found this the single durability
axis where a SQLite-backed competitor beats this Postgres-backed platform:
OpenClaw resumes in-flight runs on a charged attempt budget; we reaped.

The selection rules each guard a specific way this goes wrong:

* **Charge the attempt before resuming.** A run that dies *during* resume
  must still have paid for it, or a crash loop resumes forever. The counter
  lives in a column rather than memory because the failure it guards against
  is the restart itself.
* **Only runs with a checkpoint.** Without one there is nothing to restore,
  and "resume" would mean "run again from scratch" — a re-execution nobody
  asked for, with whatever side effects the first attempt already had.
* **Bounded concurrency.** A crash with forty runs in flight must not start
  forty agents during boot.
* **Off by default.** This changes what a restart does to live work, so it
  promotes off -> observe -> enforce like every other control here.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Times a single run may be resumed before it is left to the reaper. Two is
#: too few to survive a restart during a rolling deploy; a large number turns
#: a crash loop into an infinite one.
MAX_RESUME_ATTEMPTS = 3

#: Runs started at once during boot. Resuming is real agent work — a crash
#: with a large backlog must not stampede the engine while it is still
#: coming up.
MAX_RESUME_CONCURRENCY = 4


@dataclass(frozen=True)
class ResumeCandidate:
    """A run that was interrupted, and what we know about resuming it."""

    run_id: str
    agent_id: str
    resume_attempts: int
    has_checkpoint: bool


def resume_enabled() -> bool:
    """Is in-flight resume turned on for this instance?

    Off unless explicitly enabled: this changes what a restart does to live
    work, and a control that alters production behaviour on upgrade should
    be a decision, not a surprise.
    """
    return os.environ.get("ROBOTHOR_RESUME_IN_FLIGHT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resumable(candidates: list[ResumeCandidate]) -> list[ResumeCandidate]:
    """The subset worth resuming: has a checkpoint, still within budget.

    Filters rather than rejecting wholesale — one spent run must not stop the
    others being recovered.
    """
    keep: list[ResumeCandidate] = []
    for c in candidates:
        if not c.has_checkpoint:
            logger.info("Run %s has no checkpoint — leaving it to the reaper", c.run_id)
            continue
        if c.resume_attempts >= MAX_RESUME_ATTEMPTS:
            logger.warning(
                "Run %s has used its %d resume attempts — tombstoning",
                c.run_id,
                MAX_RESUME_ATTEMPTS,
            )
            continue
        keep.append(c)
    return keep


def resume_batch(candidates: list[ResumeCandidate]) -> list[ResumeCandidate]:
    """What to resume in this pass, in a stable order and bounded in size.

    Sorted by run id so the same runs are attempted first every time: an
    arbitrary order lets a crash loop starve one run indefinitely while
    others are retried.
    """
    return sorted(resumable(candidates), key=lambda c: c.run_id)[:MAX_RESUME_CONCURRENCY]
