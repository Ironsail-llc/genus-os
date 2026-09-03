"""What this machine can actually do — discovered, never configured in.

Local execution is bounded by the device: how much memory it has, how many
inference slots the local server will serve concurrently, whether anything can
report its temperature. Before this module those facts lived as constants
scattered through the codebase, each one measured on the machine the author
happened to be sitting at.

Two rules make this portable:

* **An unknown is ``None``.** Callers skip rather than guess. This follows the
  contract ``memory.lifecycle._available_memory_gb`` already established: a
  missing ``/proc``, a foreign OS, or a garbled file all mean "don't know",
  and "don't know" must never be confused with "zero".
* **Every value records its source.** ``/health`` can then say *why* it
  believes a number, which is the difference between a profile and a guess.

Deliberately stdlib-only: ``psutil`` is not a dependency, and memory comes from
``/proc/meminfo`` rather than the GPU tool, because on unified-memory parts the
GPU tool reports ``[N/A]`` for both used and total.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where a reading came from, so ``/health`` can explain itself.
PROBED = "probed"
CONFIGURED = "configured"
DEFAULT = "default"

#: One slot. Anything higher is a claim about hardware we have not measured;
#: the true value is discovered below. A default of 2 would encode the box this
#: was written on.
DEFAULT_INFERENCE_SLOTS = 1

_MEMINFO_PATH = "/proc/meminfo"
_NVIDIA_VERSION_PATH = "/proc/driver/nvidia/version"
_ROCM_PATH = "/sys/class/kfd"
_THERMAL_ROOT = "/sys/class/thermal"
_THERMAL_PATTERN = "thermal_zone*/temp"
_SYSTEMCTL_TIMEOUT_S = 5


@dataclass(frozen=True)
class Reading:
    """One observed fact plus the provenance of how it was obtained."""

    value: object | None
    source: str

    @property
    def known(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class HostProfile:
    """A description of the machine, with every field's provenance attached."""

    accelerator: Reading
    total_memory_gb: Reading
    available_memory_gb: Reading
    inference_slots: Reading
    thermal_sensors: Reading

    def readings(self) -> dict[str, Reading]:
        return {
            "accelerator": self.accelerator,
            "total_memory_gb": self.total_memory_gb,
            "available_memory_gb": self.available_memory_gb,
            "inference_slots": self.inference_slots,
            "thermal_sensors": self.thermal_sensors,
        }

    def describe(self) -> dict[str, object]:
        """A JSON-safe view for ``/health``: value and source, side by side."""
        return {name: {"value": r.value, "source": r.source} for name, r in self.readings().items()}


def _meminfo_gb(field: str) -> float | None:
    """Read one ``/proc/meminfo`` field in GiB, or None if it can't be read."""
    try:
        with Path(_MEMINFO_PATH).open() as f:
            for line in f:
                if line.startswith(f"{field}:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except Exception as e:
        logger.debug("Could not read %s from %s: %s", field, _MEMINFO_PATH, e)
    return None


def _available_memory_gb() -> float | None:
    return _meminfo_gb("MemAvailable")


def _total_memory_gb() -> float | None:
    return _meminfo_gb("MemTotal")


def detect_accelerator() -> tuple[str | None, str]:
    """Identify the local accelerator by driver presence, not by vendor tooling.

    Returns (kind, source). ``cpu`` is a real answer — a machine with no
    accelerator can still run small local models — whereas ``None`` means the
    question could not be answered at all.
    """
    try:
        if Path(_NVIDIA_VERSION_PATH).exists():
            return "cuda", PROBED
        if Path(_ROCM_PATH).exists():
            return "rocm", PROBED
        if sys.platform == "darwin":
            return "metal", PROBED
        return "cpu", PROBED
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Accelerator probe failed: %s", e)
        return None, DEFAULT


def thermal_sensors_available() -> bool:
    """Whether any thermal zone is readable. False is a fact; it is not an error."""
    try:
        return any(Path(_THERMAL_ROOT).glob(_THERMAL_PATTERN))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Thermal sensor probe failed: %s", e)
        return False


def _systemctl_ollama_environment() -> str | None:
    """The local model server's unit Environment, or None on any non-systemd host.

    Best effort by construction: a missing ``systemctl``, a missing unit, a
    timeout and a non-zero exit are all the same answer — "can't tell" — and
    none of them may propagate as an exception into engine startup.
    """
    try:
        if shutil.which("systemctl") is None:
            return None
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["systemctl", "show", "ollama.service", "--property=Environment"],
            capture_output=True,
            text=True,
            timeout=_SYSTEMCTL_TIMEOUT_S,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except Exception as e:
        logger.debug("systemctl probe failed: %s", e)
        return None


def _positive_int(raw: str | None) -> int | None:
    """Parse a slot count, refusing values that would deadlock the fleet."""
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("Ignoring non-numeric inference-slot setting %r", raw)
        return None
    if value < 1:
        logger.warning("Ignoring inference-slot setting %r: fewer than one slot", raw)
        return None
    return value


def detect_inference_slots() -> tuple[int, str]:
    """How many local model requests may be in flight at once.

    Most specific wins: an explicit operator setting, then the model server's
    own configured concurrency (from this process's environment, then from its
    systemd unit), then a conservative default of one.
    """
    configured = _positive_int(os.environ.get("ROBOTHOR_LOCAL_MAX_CONCURRENT"))
    if configured is not None:
        return configured, CONFIGURED

    from_env = _positive_int(os.environ.get("OLLAMA_NUM_PARALLEL"))
    if from_env is not None:
        return from_env, PROBED

    unit_env = _systemctl_ollama_environment()
    if unit_env:
        for token in unit_env.replace("Environment=", " ").split():
            key, _, value = token.strip('"').partition("=")
            if key == "OLLAMA_NUM_PARALLEL":
                from_unit = _positive_int(value)
                if from_unit is not None:
                    return from_unit, PROBED

    return DEFAULT_INFERENCE_SLOTS, DEFAULT


def detect_host_profile() -> HostProfile:
    """Probe the machine. Never raises: a bare host still yields a valid profile."""
    accelerator, accel_source = detect_accelerator()
    slots, slots_source = detect_inference_slots()
    total = _total_memory_gb()
    available = _available_memory_gb()
    return HostProfile(
        accelerator=Reading(accelerator, accel_source),
        total_memory_gb=Reading(total, PROBED if total is not None else DEFAULT),
        available_memory_gb=Reading(available, PROBED if available is not None else DEFAULT),
        inference_slots=Reading(slots, slots_source),
        thermal_sensors=Reading(thermal_sensors_available(), PROBED),
    )
