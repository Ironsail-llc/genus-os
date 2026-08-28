"""One gate every local inference request passes, sized by the device and paced by heat.

Local work is registered at zero cost per token, which is true about money and false
about everything else. On the GB10 it is paid in watts, and watts are the whole story:
package temperature tracks instantaneous GPU power almost linearly, at roughly
0.65 C/W above idle (docs/runbooks/THERMAL.md, measured 2026-08-28).

Two consequences shape this module, and neither is served by a plain semaphore:

* **Bound the streams.** One 27B stream costs ~62W and plateaus near 85C. A second
  parallel stream plus concurrent embedding and reranking is what reached 96C and
  took the box down.
* **Pace between them.** A *single* 7.5k-token request lifts the package 22C in 11
  seconds, and two back-to-back requests cross THROTTLE_C. There is no concurrency
  setting at which continuous 27B work is safe, so the gate also refuses to start new
  work while hot. Recovery is near-instantaneous, which is what makes this cheap.

The primitive is a ``threading.Condition``, deliberately not an ``asyncio.Semaphore``:
an asyncio semaphore binds to the loop that created it, and this gate is reached from
``asyncio.run()`` call sites (``rlm_tool``, ``reranker.rerank_sync``, ``search_facts_compat``)
that would each silently get a fresh, empty gate. It is also resizable, because the right
limit is the device's capacity right now, not a constant.

Everything here fails OPEN, matching ``engine/admission.py``: an unreadable sensor admits.
A missing thermometer is not a cool machine, but a gate that stalls the only tier still
answering during a cloud outage is worse than one that runs a little warm.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from enum import StrEnum

logger = logging.getLogger(__name__)


def _threshold(env: str, default: float) -> float:
    raw = os.environ.get(env)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, using %s", env, raw, default)
        return default


def _thermal_defaults() -> tuple[float, float]:
    """THROTTLE_C/RESTORE_C from the one thermal policy, if it can be read.

    Imported lazily so ``robothor.llm`` keeps no import-time dependency on the engine.
    """
    try:
        from robothor.engine import thermal_pressure as tp

        return float(tp.THROTTLE_C), float(tp.RESTORE_C)
    except Exception:  # noqa: BLE001 - a gate must not fail to import
        return 85.0, 75.0


_THROTTLE_C, _RESTORE_C = _thermal_defaults()

#: Background work stops here. Below the shell guard's THROTTLE_C, so routine
#: cron work yields before the CPU cap engages and long before anything pages.
PACE_BACKGROUND_C = _threshold("ROBOTHOR_LOCAL_PACE_BACKGROUND_C", 80.0)

#: Everything except an operator's turn stops here. Shares the shell guard's
#: THROTTLE_C so there is one number, not a second competing policy.
PACE_ALL_C = _threshold("ROBOTHOR_LOCAL_PACE_ALL_C", _THROTTLE_C)

#: Paced work resumes only after cooling to here. Latched, because releasing at
#: the trip point oscillates -- the same reason the shell guard latches.
RESUME_C = _threshold("ROBOTHOR_LOCAL_PACE_RESUME_C", _RESTORE_C)

#: How long a caller waits for a slot before being told the device is busy.
DEFAULT_WAIT_SECONDS = _threshold("ROBOTHOR_LOCAL_GATE_WAIT_SECONDS", 120.0)

_POLL_SECONDS = 0.025


class Lane(StrEnum):
    """Which traffic this is, and therefore how early it yields to heat."""

    INTERACTIVE = "interactive"  # a person is waiting; never paced
    NORMAL = "normal"
    BACKGROUND = "background"  # cron, sweeps, memory maintenance; yields first


class LocalCapacityBusyError(RuntimeError):
    """The device is full or too hot to start new work.

    Carries ``status_code = 503`` so ``llm_client.is_capacity_error`` recognises it
    and the existing local-capacity retry path applies unchanged.
    """

    status_code = 503


def _read_temperature_c() -> float | None:
    """Hottest zone, or None when it cannot be read. Indirected so tests can drive it."""
    try:
        from robothor.engine.thermal_pressure import read_max_temperature_c

        return read_max_temperature_c()
    except Exception:  # noqa: BLE001
        return None


class LocalInferenceGate:
    """Admission for on-device inference: N streams, and none of them while hot."""

    def __init__(self, slots: int = 1) -> None:
        self._cv = threading.Condition()
        self._slots = max(1, int(slots))
        self._active = 0
        self._paced = False

    # ── capacity ────────────────────────────────────────────────────────
    def resize(self, slots: int) -> None:
        """Retune without a restart. Never below one: a gate of zero is a stalled
        fleet, and the shell guard owns the genuinely protective actions."""
        with self._cv:
            new = max(1, int(slots))
            if new != self._slots:
                logger.info("Local gate resized %d -> %d slots", self._slots, new)
            self._slots = new
            self._cv.notify_all()

    def _try_take(self) -> bool:
        with self._cv:
            if self._active < self._slots:
                self._active += 1
                return True
            return False

    def release(self) -> None:
        with self._cv:
            if self._active > 0:
                self._active -= 1
            self._cv.notify()

    # ── heat ────────────────────────────────────────────────────────────
    def _paced_now(self, lane: Lane) -> bool:
        """Should this lane hold off? Latches until RESUME_C, fails open on no sensor."""
        if lane is Lane.INTERACTIVE:
            return False
        celsius = _read_temperature_c()
        if celsius is None:
            return False
        limit = PACE_BACKGROUND_C if lane is Lane.BACKGROUND else PACE_ALL_C
        with self._cv:
            if self._paced:
                if celsius <= RESUME_C:
                    self._paced = False
                    logger.info("Local gate resuming at %.1fC (<= %.0fC)", celsius, RESUME_C)
                    return False
                return True
            if celsius >= limit:
                self._paced = True
                logger.warning(
                    "Local gate pacing %s at %.1fC (>= %.0fC): holding new work",
                    lane,
                    celsius,
                    limit,
                )
                return True
        return False

    # ── acquisition ─────────────────────────────────────────────────────
    def _acquire_blocking(self, lane: Lane, timeout: float | None) -> None:
        deadline = time.monotonic() + (DEFAULT_WAIT_SECONDS if timeout is None else timeout)
        reason = "no slot"
        while True:
            if self._paced_now(lane):
                reason = "device too hot to start new work"
            elif self._try_take():
                return
            if time.monotonic() >= deadline:
                raise LocalCapacityBusyError(f"local inference gate: {reason}")
            time.sleep(_POLL_SECONDS)

    async def _acquire_async(self, lane: Lane, timeout: float | None) -> None:
        import asyncio

        deadline = time.monotonic() + (DEFAULT_WAIT_SECONDS if timeout is None else timeout)
        reason = "no slot"
        while True:
            if self._paced_now(lane):
                reason = "device too hot to start new work"
            elif self._try_take():
                return
            if time.monotonic() >= deadline:
                raise LocalCapacityBusyError(f"local inference gate: {reason}")
            await asyncio.sleep(_POLL_SECONDS)

    def acquire_sync(self, lane: Lane = Lane.NORMAL, timeout: float | None = None) -> None:
        """Take a slot from synchronous code. Pair with ``release()`` in a finally."""
        self._acquire_blocking(lane, timeout)

    @asynccontextmanager
    async def slot(self, lane: Lane = Lane.NORMAL, timeout: float | None = None):
        """Hold one inference slot for the duration of ONE leaf request.

        Gate the leaf HTTP call, never a composite: a caller that holds a slot while
        awaiting gated children deadlocks the moment the children need the last slot.
        """
        await self._acquire_async(lane, timeout)
        try:
            yield
        finally:
            self.release()

    def snapshot(self) -> dict[str, object]:
        with self._cv:
            return {
                "slots": self._slots,
                "active": self._active,
                "paced": self._paced,
                "pace_background_c": PACE_BACKGROUND_C,
                "pace_all_c": PACE_ALL_C,
                "resume_c": RESUME_C,
            }


_GATE: LocalInferenceGate | None = None
_GATE_LOCK = threading.Lock()


def gate() -> LocalInferenceGate:
    """The process-wide gate, sized from the host on first use.

    Process-wide, not fleet-wide: the engine, orchestrator and vision service are
    separate processes. Ollama's own OLLAMA_NUM_PARALLEL is the cross-process bound;
    what every process shares here is the *temperature*, which is why pacing works
    even though the slot counts do not add up across them.
    """
    global _GATE
    with _GATE_LOCK:
        if _GATE is None:
            _GATE = LocalInferenceGate(slots=_detect_slots())
        return _GATE


def _detect_slots() -> int:
    try:
        from robothor.engine.host_profile import detect_inference_slots

        slots, _source = detect_inference_slots()
        return slots
    except Exception:  # noqa: BLE001
        return 1
