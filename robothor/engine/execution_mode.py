"""CLOUD and LOCAL as peer operating modes, entered by observation.

The two modes are bound by different physics. Cloud inference is scarce in
money and provider rate limits, and latency is the product. Local inference is
free of money entirely and scarce instead in heat, resident memory and
inference slots -- a slow answer is still an answer. When the model chain falls
back from one to the other, the ruleset has to fall back with it; before this
module it did not, and the cloud ruleset applied to a device-bound workload is
what produced the outage this was written for.

**Mode is derived from what is actually serving.** Not from an outage flag, and
in particular not from the credential pool: ``KeyPool.exhausted()`` mutates the
state it reports and flips optimistically healthy every six hours against a
weekly cap, so polling it would un-defer the fleet four times a day. The only
evidence that cloud is back is a cloud request that completed.

Hysteresis runs in both directions -- a streak to enter, then dwell plus a
quiet window to leave -- so a single straggler cannot flap the fleet's policy.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


class ExecutionMode(StrEnum):
    CLOUD = "cloud"
    LOCAL = "local"


#: Consecutive local completions before the fleet repolicies. One local
#: completion is a fallback; a streak is an operating mode.
LOCAL_STREAK_TO_ENTER = 3

#: Minimum time in LOCAL before a return is even considered. Re-policying the
#: whole fleet is not free, and outages rarely end within seconds.
DEFAULT_MIN_DWELL_SECONDS = 300

#: Quiet time after the first cloud success, during which any local completion
#: cancels the return. Mid-outage flapping is worse than staying put.
DEFAULT_QUIET_WINDOW_SECONDS = 120

_OVERRIDE_ENV = "ROBOTHOR_EXECUTION_MODE"
_AUTO = "auto"


def _is_local(model: str) -> bool:
    """One predicate, imported lazily so the mode layer stays dependency-light."""
    from robothor.engine.llm_client import is_local_model

    return is_local_model(model)


def _override() -> ExecutionMode | None:
    """Operator pin, or None for evidence-driven.

    A deliberately local-only instance is a supported configuration, not a
    permanent alarm. An unrecognised value falls back to auto rather than
    wedging the fleet in a mode nobody asked for.
    """
    raw = (os.environ.get(_OVERRIDE_ENV) or _AUTO).strip().lower()
    if raw in ("", _AUTO):
        return None
    try:
        return ExecutionMode(raw)
    except ValueError:
        logger.warning(
            "Ignoring unrecognised %s=%r; using observed mode. Valid: auto, cloud, local",
            _OVERRIDE_ENV,
            raw,
        )
        return None


def _export_mode(mode: ExecutionMode) -> None:
    """Publish the mode to metrics. Never raises; never load-bearing."""
    try:
        from robothor.engine.metrics import set_execution_mode

        set_execution_mode(str(mode))
    except Exception:  # pragma: no cover - telemetry only
        logger.debug("Could not export execution mode", exc_info=True)


class ExecutionModeTracker:
    """Observes completions and reports which mode the fleet is operating in."""

    def __init__(
        self,
        clock: Callable[[], float] | None = None,
        min_dwell_seconds: float = DEFAULT_MIN_DWELL_SECONDS,
        quiet_window_seconds: float = DEFAULT_QUIET_WINDOW_SECONDS,
        local_streak: int = LOCAL_STREAK_TO_ENTER,
    ) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._clock = clock
        self._min_dwell = min_dwell_seconds
        self._quiet_window = quiet_window_seconds
        self._streak_required = local_streak

        self._mode = ExecutionMode.CLOUD
        self._local_streak = 0
        self._entered_local_at: float | None = None
        self._cloud_success_at: float | None = None
        self._last_model: str | None = None
        # Publish the starting mode immediately. A labelled Prometheus series
        # does not exist until it is written, so exporting only on transitions
        # left /metrics with a declared gauge and no value -- "which mode are
        # we in" was unanswerable right when it mattered, after a restart.
        _export_mode(self._mode)

    def record_completion(self, model: str) -> None:
        """Register that a request completed on ``model``. The only mode input."""
        if not model:
            return
        self._last_model = model
        local = _is_local(model)
        now = self._clock()

        if local:
            self._local_streak += 1
            # Any local traffic restarts the quiet window: cloud is not back yet.
            self._cloud_success_at = None
            if self._mode is ExecutionMode.CLOUD and self._local_streak >= self._streak_required:
                self._mode = ExecutionMode.LOCAL
                self._entered_local_at = now
                logger.warning(
                    "Execution mode -> LOCAL after %d consecutive local completions; "
                    "device economics now apply (slots, heat, resident memory)",
                    self._local_streak,
                )
                _export_mode(self._mode)
            return

        self._local_streak = 0
        if self._mode is ExecutionMode.LOCAL:
            dwelled = self._entered_local_at is None or (
                now - self._entered_local_at >= self._min_dwell
            )
            if dwelled and self._cloud_success_at is None:
                self._cloud_success_at = now

    def mode(self) -> ExecutionMode:
        """The mode in force, honouring an operator pin over observation."""
        pinned = _override()
        if pinned is not None:
            return pinned
        self._settle()
        return self._mode

    def _settle(self) -> None:
        """Complete a pending return to CLOUD once the quiet window has passed."""
        if self._mode is not ExecutionMode.LOCAL or self._cloud_success_at is None:
            return
        if self._clock() - self._cloud_success_at >= self._quiet_window:
            self._mode = ExecutionMode.CLOUD
            self._entered_local_at = None
            self._cloud_success_at = None
            self._local_streak = 0
            logger.warning("Execution mode -> CLOUD: cloud served a request and stayed quiet")
            _export_mode(self._mode)

    def snapshot(self) -> dict[str, object]:
        """A JSON-safe view for ``/health``, including why the mode is what it is."""
        pinned = _override()
        return {
            "mode": str(self.mode()),
            "source": "override" if pinned is not None else "observed",
            "last_model": self._last_model,
            "local_streak": self._local_streak,
            "returning_to_cloud": self._cloud_success_at is not None,
        }


_TRACKER: ExecutionModeTracker | None = None


def tracker() -> ExecutionModeTracker:
    """The process-wide tracker."""
    global _TRACKER
    if _TRACKER is None:
        _TRACKER = ExecutionModeTracker()
    return _TRACKER


def current_mode() -> ExecutionMode:
    return tracker().mode()


def record_completion(model: str) -> None:
    tracker().record_completion(model)
