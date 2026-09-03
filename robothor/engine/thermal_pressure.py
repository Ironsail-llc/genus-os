"""Let the engine see the temperature the shell guard has been acting on alone.

``scripts/thermal-guard.sh`` has run on a 30s timer for months with a proper
four-state hysteresis machine, and it has genuinely fired. Its one lever is the
CPU frequency cap. It cannot shed agent work, because until now no Python in
this repo read a temperature at all -- the scheduler, the pool and the LLM
client had no idea the box was hot.

This is a read-only peer to that guard, not a second one. Same sensors, same
env knobs, same defaults, so there is one policy with two consumers; a test
parses the shell script and fails if the numbers ever drift apart.

The response to heat here is *fewer concurrent runs*. Nothing in this module
can end a run: losing an hour of work to save a few degrees is a bad trade, and
the guard already owns the genuinely protective actions.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

_THERMAL_ROOT = "/sys/class/thermal"
_THERMAL_PATTERN = "thermal_zone*/temp"


def _threshold(env: str, default: int) -> int:
    raw = os.environ.get(env)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, using %d", env, raw, default)
        return default


#: Shared with scripts/thermal-guard.sh -- same names, same defaults.
THROTTLE_C = _threshold("ROBOTHOR_THERMAL_THROTTLE_C", 85)
WARN_C = _threshold("ROBOTHOR_THERMAL_WARN_C", 90)
CRIT_C = _threshold("ROBOTHOR_THERMAL_CRIT_C", 94)
RESTORE_C = _threshold("ROBOTHOR_THERMAL_RESTORE_C", 75)


class ThermalLevel(StrEnum):
    NOMINAL = "nominal"
    THROTTLE = "throttle"
    WARN = "warn"
    CRITICAL = "critical"


#: How much of the slot budget each level allows. Never zero -- a fleet that
#: admits nothing is stalled, and the shell guard owns the protective actions.
_LEVEL_FRACTION = {
    ThermalLevel.NOMINAL: 1.0,
    ThermalLevel.THROTTLE: 0.5,
    ThermalLevel.WARN: 0.5,
    ThermalLevel.CRITICAL: 0.25,
}


@dataclass(frozen=True)
class ThermalReading:
    level: ThermalLevel
    observed_c: float
    threshold_c: int


def read_max_temperature_c() -> float | None:
    """Hottest readable zone in Celsius, or None when nothing can be read.

    None means *unknown*, and callers must treat it as such. A missing sensor
    is not a cool machine -- on a laptop with no readable zones, defaulting to
    "nominal" would run it flat out into a firmware hard-cut.
    """
    hottest: float | None = None
    try:
        for zone in Path(_THERMAL_ROOT).glob(_THERMAL_PATTERN):
            try:
                millidegrees = int(zone.read_text().strip())
            except (OSError, ValueError):
                continue  # one unreadable zone must not blind the others
            celsius = millidegrees / 1000.0
            if hottest is None or celsius > hottest:
                hottest = celsius
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Thermal read failed: %s", e)
        return None
    return hottest


def _level_for(celsius: float) -> tuple[ThermalLevel, int]:
    if celsius >= CRIT_C:
        return ThermalLevel.CRITICAL, CRIT_C
    if celsius >= WARN_C:
        return ThermalLevel.WARN, WARN_C
    if celsius >= THROTTLE_C:
        return ThermalLevel.THROTTLE, THROTTLE_C
    return ThermalLevel.NOMINAL, THROTTLE_C


def thermal_pressure() -> ThermalReading | None:
    """Current thermal state, or None when the machine cannot be read."""
    celsius = read_max_temperature_c()
    if celsius is None:
        return None
    level, threshold = _level_for(celsius)
    return ThermalReading(level=level, observed_c=celsius, threshold_c=threshold)


class ThermalGovernor:
    """Turns temperature into a concurrency budget, with the guard's hysteresis.

    Once engaged, the reduction holds until the machine cools past
    ``RESTORE_C`` -- releasing at the trip point oscillates, which is exactly
    why the shell guard latches.
    """

    def __init__(self) -> None:
        self._engaged = False

    def concurrency_for(self, base_slots: int) -> int:
        """Slots permitted right now. Returns ``base_slots`` when unreadable."""
        reading = thermal_pressure()
        if reading is None:
            return base_slots

        if reading.level is not ThermalLevel.NOMINAL:
            if not self._engaged:
                logger.warning(
                    "Thermal pressure %s at %.1fC (>= %dC): reducing local concurrency",
                    reading.level,
                    reading.observed_c,
                    reading.threshold_c,
                )
            self._engaged = True
        elif self._engaged and reading.observed_c < RESTORE_C:
            logger.info(
                "Thermal pressure cleared at %.1fC (< %dC): restoring local concurrency",
                reading.observed_c,
                RESTORE_C,
            )
            self._engaged = False

        if not self._engaged:
            return base_slots

        fraction = _LEVEL_FRACTION.get(reading.level, 0.5)
        if reading.level is ThermalLevel.NOMINAL:
            fraction = _LEVEL_FRACTION[ThermalLevel.THROTTLE]
        return max(1, int(base_slots * fraction))

    def snapshot(self) -> dict[str, object]:
        reading = thermal_pressure()
        if reading is None:
            return {"available": False, "engaged": self._engaged}
        return {
            "available": True,
            "engaged": self._engaged,
            "level": str(reading.level),
            "observed_c": round(reading.observed_c, 1),
            "threshold_c": reading.threshold_c,
        }
