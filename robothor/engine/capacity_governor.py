"""Apply what the device can actually do to what the fleet is allowed to do.

``FleetPool.set_limits``, ``ModePolicy`` and ``ThermalGovernor`` each shipped
complete, tested, and connected to nothing. The pool was sized once at daemon boot
from ``max_concurrent_agents`` -- a cloud-shaped constant -- and never retuned, while
the machine underneath it fell back to a single local GPU. ``policy_for`` computed
the right answer for ``/health`` to print and for nobody to act on.

This is the wire. It runs on a timer because both inputs move: the execution mode
changes when a credential caps, and the temperature changes within seconds
(docs/runbooks/THERMAL.md -- the GB10 heats ~2 C/s under 27B prefill).

Two limits come out of one policy:

* the **fleet pool**, which counts agent runs, and
* the **local gate**, which counts inference requests.

They are different populations. One agent run issues many requests, and the memory
pipeline issues requests with no agent run at all, so neither bound substitutes for
the other.

Everything fails OPEN, matching ``admission.py``: a governor that cannot read a
sensor must leave the fleet as it found it, never stall it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How often the policy is re-derived. The shell guard polls at 30s; matching it
#: means the two never disagree for long about how hot the box is.
DEFAULT_INTERVAL_SECONDS = 30.0


def _pool() -> Any:
    """The live fleet pool, or None before the daemon builds one."""
    try:
        from robothor.engine.pool import get_fleet_pool

        return get_fleet_pool()
    except Exception:  # noqa: BLE001 - a governor is a guard, not a dependency
        return None


def _current_mode() -> Any:
    from robothor.engine.execution_mode import current_mode

    return current_mode()


class CapacityGovernor:
    """Turns (mode, host, temperature) into live limits, repeatedly."""

    def __init__(self, cloud_max_concurrent: int = 3) -> None:
        from robothor.engine.thermal_pressure import ThermalGovernor

        self._thermal = ThermalGovernor()
        self._cloud_max = cloud_max_concurrent
        self._last: dict[str, Any] | None = None

    def apply_once(self) -> dict[str, Any] | None:
        """Derive the policy and push it to the pool and the gate. Never raises."""
        try:
            return self._apply()
        except Exception:  # noqa: BLE001
            logger.exception("Capacity governor failed — leaving limits unchanged")
            return None

    def _apply(self) -> dict[str, Any]:
        from robothor.engine.mode_policy import policy_for

        mode = _current_mode()
        policy = policy_for(mode, cloud_max_concurrent=self._cloud_max)

        runs = policy.max_concurrent_runs
        # Heat is PHYSICAL, and it is derated in every mode — including CLOUD.
        #
        # ModePolicy makes the thermal governor inert in CLOUD on the reasoning
        # that someone else's datacentre is not our heat budget. That is true only
        # while the work is actually remote. Measured 2026-08-28 10:36: with the
        # credential capped, every agent fell through to the local 27B while the
        # tracker still read `mode=cloud runs=3` — it needs three consecutive LOCAL
        # completions plus dwell to flip, and the box passed 90C first. The mode
        # signal is a lagging indicator of where work runs; a thermometer is not.
        #
        # So the mode decides MONETARY policy, and the temperature decides thermal
        # policy. When the box is cool this is a no-op and CLOUD keeps its constants.
        runs = self._thermal.concurrency_for(runs)
        runs = max(1, runs)

        pool = _pool()
        if pool is not None:
            reserved = min(policy.reserved_interactive_slots, max(0, runs - 1))
            pool.set_limits(max_concurrent=runs, reserved_slots=reserved)
        else:
            reserved = policy.reserved_interactive_slots

        gate_slots = self._gate_slots()

        state = {
            "mode": str(mode),
            "max_concurrent_runs": runs,
            "reserved_interactive_slots": reserved,
            "gate_slots": gate_slots,
            "thermal": self._thermal.snapshot(),
        }
        if self._last != state:
            logger.info(
                "Capacity: mode=%s runs=%d reserved=%d gate=%s thermal=%s",
                state["mode"],
                runs,
                reserved,
                gate_slots,
                state["thermal"].get("level", "n/a"),
            )
            self._last = state
        return state

    def _gate_slots(self) -> int | None:
        """Size the request gate from the device, derated by heat.

        Independent of mode: local requests (embeddings, reranking, memory
        generation) keep flowing even when agents are served from the cloud.
        """
        try:
            from robothor.engine.host_profile import detect_inference_slots
            from robothor.llm.local_gate import gate

            slots, _ = detect_inference_slots()
            slots = max(1, self._thermal.concurrency_for(slots))
            gate().resize(slots)
            return slots
        except Exception:  # noqa: BLE001
            logger.debug("Could not size the local gate", exc_info=True)
            return None

    def snapshot(self) -> dict[str, Any]:
        return dict(self._last or {})


async def run_forever(
    cloud_max_concurrent: int = 3, interval: float = DEFAULT_INTERVAL_SECONDS
) -> None:
    """Daemon loop. Cancelled with the daemon; never exits on its own."""
    governor = CapacityGovernor(cloud_max_concurrent=cloud_max_concurrent)
    logger.info("Capacity governor started (every %.0fs)", interval)
    while True:
        governor.apply_once()
        await asyncio.sleep(interval)
