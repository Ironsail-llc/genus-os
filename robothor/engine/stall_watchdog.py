"""Stall watchdog + per-run wall-clock accounting for the agent loop.

Extracted from ``runner.py`` (audit 2026-05-29) — a cohesive, self-contained
unit with no dependency back on the runner, so it lives on its own to shrink the
runner god-object. Owns:

- ``_StallWatchdog``: kills a run that stops making progress (stall) or exceeds
  an absolute wall-clock ceiling.
- ``_active_watchdog_var``: the current task's watchdog, kept in a ContextVar so
  concurrent/nested runs on the singleton runner don't clobber each other.
- ``_build_cancel_diagnostic``: asyncio snapshot for external-cancellation
  post-mortems.
- ``_fleet_wallclock_ceiling``: the default absolute ceiling for uncapped agents.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

# The stall watchdog for the run executing in the *current* task. Stored in a
# ContextVar rather than on the AgentRunner instance because the runner is a
# singleton shared across all concurrent/nested runs — an instance attribute got
# clobbered when a second run (or a nested recovery helper) started, silently
# pointing every run's stall touches at the wrong watchdog (audit 2026-05-29).
# Each asyncio task has its own context, so concurrent runs are isolated; nested
# runs save/restore via the set() token.
_active_watchdog_var: ContextVar[_StallWatchdog | None] = ContextVar(
    "active_watchdog", default=None
)

# Absolute per-run wall-clock ceiling applied to agents that declare
# timeout_seconds=0 ("no cap"). Generous by design — a backstop against
# runaway/slow runs, not a normal limit. Override via env.
_DEFAULT_FLEET_WALLCLOCK_CEILING = 3600


def _fleet_wallclock_ceiling() -> int:
    """Fleet-wide hard wall-clock ceiling (seconds) for uncapped agents."""
    try:
        return max(0, int(os.environ.get("ROBOTHOR_MAX_WALLCLOCK_SECONDS", "")))
    except ValueError:
        return _DEFAULT_FLEET_WALLCLOCK_CEILING


class _StallWatchdog:
    """Kills a run if no activity occurs for stall_timeout seconds.

    Activity is tracked via touch() — call it on every LLM response,
    tool completion, and sub-agent completion. A background task checks
    every 30s and cancels the given asyncio.Task if idle too long.

    If stall_timeout <= 0, the watchdog is disabled (no-op).
    Hard timeout_seconds is kept as an absolute safety net.

    Uses a cooperative abort flag in addition to task.cancel() because
    asyncio task cancellation doesn't reliably propagate through all
    async HTTP libraries (litellm/httpx).
    """

    # Touch descriptions that indicate the model has actually started
    # producing work. Setup/warmup signals don't count — those happen
    # before any LLM round-trip.
    _OUTPUT_TOUCH_PREFIXES: tuple[str, ...] = (
        "llm_response",
        "stream_text",
        "stream_tool",
        "tool:",
    )

    def __init__(
        self,
        stall_timeout: int,
        hard_timeout: int,
        early_stall_timeout: int = 0,
        tick_seconds: float = 30.0,
    ) -> None:
        self._stall_timeout = stall_timeout
        self._hard_timeout = hard_timeout
        # Background-loop tick interval. Default 30s — tests use a
        # smaller value (e.g. 0.1) to avoid waiting for real wall-clock
        # in the early-stall test suite.
        self._tick_seconds = tick_seconds
        # Pre-output stall window. Trips when this many seconds have
        # passed AND no real progress signal has been seen. Fires even
        # while the post-progress _stall_timeout is still being reset
        # by warmup touches. 0 = disabled.
        self._early_stall_timeout = early_stall_timeout
        self._last_activity = time.monotonic()
        self._last_activity_desc: str = "run_start"
        self._task: asyncio.Task[None] | None = None
        self._cancelled = False
        self._abort_event = asyncio.Event()
        self._abort_reason: str = ""
        # Set True on the first touch() that names an output-bearing
        # event (LLM response, stream chunk, tool completion). Setup
        # touches like init_begin / warmup_complete / session_started
        # do not flip this — they are explicitly excluded so the early-
        # stall guard can fire while warmup keeps refreshing the
        # last-activity timestamp.
        self._saw_output_signal = False
        # Set on start() so callers can compute "time since run began" even
        # when the watchdog itself didn't trip (e.g. external cancellation).
        self._start_time: float = time.monotonic()

    def touch(self, description: str = "") -> None:
        """Record activity — resets the stall timer.

        Pass ``description`` to name the progress signal (e.g.
        ``"llm_response:sonnet-4.6"`` or ``"tool:list_tasks"``) so the
        stall abort reason can point at the last thing that worked.

        Touches whose description matches an output-prefix
        (`llm_response`, `stream_text`, `stream_tool`, `tool:`) also
        flip the watchdog's "model started talking" flag, which clears
        the early-stall guard. Setup/warmup touches do not flip it.
        """
        self._last_activity = time.monotonic()
        if description:
            self._last_activity_desc = description
            if not self._saw_output_signal and description.startswith(self._OUTPUT_TOUCH_PREFIXES):
                self._saw_output_signal = True

    @property
    def last_activity_desc(self) -> str:
        return self._last_activity_desc

    def start(self, monitored_task: asyncio.Task[Any]) -> None:
        """Start the watchdog background loop."""
        if self._stall_timeout <= 0 and self._hard_timeout <= 0 and self._early_stall_timeout <= 0:
            return
        self._start_time = time.monotonic()
        self._task = asyncio.create_task(self._watch(monitored_task))

    async def _watch(self, monitored_task: asyncio.Task[Any]) -> None:
        try:
            while not monitored_task.done():
                await asyncio.sleep(self._tick_seconds)
                if monitored_task.done():
                    break
                now = time.monotonic()
                idle = now - self._last_activity
                elapsed = now - self._start_time

                # Hard timeout (absolute safety net — should almost
                # never fire; stall detection is the primary mechanism)
                if self._hard_timeout > 0 and elapsed > self._hard_timeout:
                    logger.warning(
                        "Stall watchdog: hard timeout (%ds) reached after %.0fs; last_activity=%s",
                        self._hard_timeout,
                        elapsed,
                        self._last_activity_desc,
                    )
                    self._abort_reason = (
                        f"Circuit-breaker hard timeout ({self._hard_timeout}s) "
                        f"after {elapsed:.0f}s; last activity: {self._last_activity_desc}"
                    )
                    self._cancelled = True
                    self._abort_event.set()
                    monitored_task.cancel()
                    return

                # Early-stall detection — fires before any output has been
                # produced. Specifically targets the "warmup completes →
                # silence" wedge pattern that the post-progress stall can't
                # see (warmup touches keep resetting `idle`). Safe alongside
                # the save-gate: with no output, nothing can poison the next
                # heartbeat session.
                if (
                    self._early_stall_timeout > 0
                    and not self._saw_output_signal
                    and elapsed > self._early_stall_timeout
                ):
                    logger.warning(
                        "Stall watchdog: early stall (%ds) — no LLM output after %.0fs; "
                        "last_activity=%s",
                        self._early_stall_timeout,
                        elapsed,
                        self._last_activity_desc,
                    )
                    self._abort_reason = (
                        f"Early stall: no LLM output after {elapsed:.0f}s "
                        f"(threshold {self._early_stall_timeout}s); "
                        f"last activity: {self._last_activity_desc}"
                    )
                    self._cancelled = True
                    self._abort_event.set()
                    monitored_task.cancel()
                    return

                # Stall detection (primary mechanism)
                if self._stall_timeout > 0 and idle > self._stall_timeout:
                    logger.warning(
                        "Stall watchdog: no progress for %.0fs (limit %ds), killing run; last_activity=%s",
                        idle,
                        self._stall_timeout,
                        self._last_activity_desc,
                    )
                    self._abort_reason = (
                        f"No progress for {idle:.0f}s (stall limit {self._stall_timeout}s); "
                        f"last activity: {self._last_activity_desc}"
                    )
                    self._cancelled = True
                    self._abort_event.set()
                    monitored_task.cancel()
                    return
        except asyncio.CancelledError:
            pass

    def trip(self, reason: str) -> None:
        """Trip the abort flag from outside the watch task.

        The run loop's wall-clock self-check uses this: when the loop finds
        its own deadline passed while the watchdog failed to act (2026-08-25:
        a run blew through a 1200s ceiling to 3110s with the outer
        ``asyncio.timeout``, the watchdog cancel, AND the deadline warning
        all silent at once), it trips the same flag the watch task would
        have set, so every downstream consumer — the cooperative abort
        check, the TIMEOUT status mapping, the abort-reason reporting —
        behaves exactly as if the watchdog had fired.
        """
        self._abort_reason = reason
        self._cancelled = True
        self._abort_event.set()

    def stop(self) -> None:
        """Stop the watchdog."""
        if self._task and not self._task.done():
            self._task.cancel()

    @property
    def should_abort(self) -> bool:
        """Cooperative abort check — run loop checks this each iteration."""
        return self._abort_event.is_set()

    @property
    def abort_reason(self) -> str:
        """Why the watchdog triggered."""
        return self._abort_reason

    @property
    def was_stall_timeout(self) -> bool:
        return self._cancelled

    @property
    def elapsed_seconds(self) -> float:
        """Seconds since start() was called (0 if never started)."""
        return max(0.0, time.monotonic() - self._start_time)

    @property
    def idle_seconds(self) -> float:
        """Seconds since the last touch() call."""
        return max(0.0, time.monotonic() - self._last_activity)


def _build_cancel_diagnostic(watchdog: _StallWatchdog, agent_id: str) -> str:
    """Capture asyncio context at the moment of an external cancellation.

    Used to investigate the noon-storm symptom (multiple agents die at
    `12:00:00.05–12:00:00.12` with `Run cancelled externally; last
    activity: session_started`). Without context, the post-mortem is a
    guess; with it, we can see whether something else is calling
    `.cancel()` on the run task and what it is.

    Returns a multi-line diagnostic string suitable for
    ``agent_runs.error_traceback``.
    """
    lines: list[str] = []
    try:
        lines.append(f"agent_id={agent_id}")
        lines.append(f"elapsed_since_start={watchdog.elapsed_seconds:.2f}s")
        lines.append(f"idle_since_last_touch={watchdog.idle_seconds:.2f}s")
        lines.append(f"last_activity={watchdog.last_activity_desc}")
        try:
            current = asyncio.current_task()
            current_name = current.get_name() if current else "<no-current-task>"
        except RuntimeError:
            current_name = "<no-running-loop>"
        lines.append(f"current_task={current_name}")
        try:
            alive = list(asyncio.all_tasks())
        except RuntimeError:
            alive = []
        lines.append(f"alive_tasks_count={len(alive)}")
        # Truncate to 20 task names — past that the dump is noise. Sort
        # by name for stable output across runs.
        names = sorted({t.get_name() for t in alive})[:20]
        lines.extend(f"  task: {n}" for n in names)
        # Walltime + pid help correlate with external events (cron,
        # systemd, daemon restart) that show up at the same instant.
        from datetime import UTC, datetime

        lines.append(f"walltime_utc={datetime.now(UTC).isoformat()}")
        lines.append(f"pid={os.getpid()}")
    except Exception as e:  # diagnostic must never raise
        lines.append(f"diagnostic_error={e!r}")
    return "\n".join(lines)
