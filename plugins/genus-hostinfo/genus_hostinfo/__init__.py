"""Host thermal, GPU and memory state, as a Genus tool.

A plugin rather than a core tool on purpose. Reading `/sys/class/thermal`,
shelling to `nvidia-smi` and parsing `/proc/meminfo` are facts about ONE
machine; core ships only what every instance needs (root CLAUDE.md, and the
core-vs-plugin boundary this project already drew). Putting it here keeps a
host-shaped capability out of the platform while still giving the fleet access
to it.

It also fills a real blind spot. This instance runs a thermal control loop in
bash on a systemd timer — with hysteresis, a latch and a pager, and it has
fired — but NO Python anywhere reads a temperature. The scheduler, the fleet
pool and the LLM client have no idea the box is hot. This is the seam through
which they can find out.

Everything degrades to `None` rather than guessing. A laptop with no GPU, a
container with no `/sys`, a host without `nvidia-smi` all produce a valid
answer; the caller is told what is unknown instead of being handed a number
that is wrong.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["PLUGIN", "host_state"]

_THERMAL = Path("/sys/class/thermal")
_MEMINFO = Path("/proc/meminfo")


def _cpu_temps_c() -> list[float]:
    """Every readable thermal zone, in Celsius. [] when none are readable."""
    out: list[float] = []
    try:
        for zone in sorted(_THERMAL.glob("thermal_zone*/temp")):
            try:
                out.append(int(zone.read_text().strip()) / 1000.0)
            except (OSError, ValueError):
                continue
    except OSError:
        return []
    return out


def _available_memory_gb() -> float | None:
    """Available RAM in GiB, or None when it cannot be determined.

    Same contract as the engine's own probe: None means UNKNOWN, and a caller
    must skip rather than guess.
    """
    try:
        for line in _MEMINFO.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


def _gpu() -> dict[str, Any] | None:
    """GPU name, temperature and utilisation, or None when there is no GPU.

    Deliberately does NOT report VRAM: on unified-memory parts (GB10) nvidia-smi
    returns [N/A] for memory.used/total, and a caller that trusted it would be
    reading nothing as zero.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        raw = subprocess.run(  # noqa: S603 - fixed argv, resolved binary
            [exe, "--query-gpu=name,temperature.gpu,utilization.gpu", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (raw.stdout or "").strip().splitlines()
    if not line:
        return None
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 3:
        return None

    def _num(v: str) -> float | None:
        try:
            return float(v.split()[0])
        except (ValueError, IndexError):
            return None

    return {"name": parts[0], "temperature_c": _num(parts[1]), "utilization_pct": _num(parts[2])}


async def host_state(args: dict[str, Any] | None = None, ctx: Any = None) -> dict[str, Any]:
    """Report what this machine is doing right now.

    Returns temperatures, GPU state and available memory. Any field the host
    cannot answer is None — never a substituted default.
    """
    temps = _cpu_temps_c()
    return {
        "cpu_zone_temps_c": temps or None,
        "cpu_max_temp_c": max(temps) if temps else None,
        "gpu": _gpu(),
        "available_memory_gb": _available_memory_gb(),
    }


HOST_STATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "host_state",
        "description": (
            "Current host thermal, GPU and memory state. Fields the machine "
            "cannot report are null rather than zero."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

PLUGIN = {
    "genus_contract_version": "1.0",
    "handlers": {"host_state": host_state},
    "schemas": {"host_state": HOST_STATE_SCHEMA},
    "read_only": ["host_state"],
    # Also offered as a SERVICE, so another tool can read host state without
    # going through a tool call. `genus.services` is the only group that named
    # no consumer: `ToolContext.get_service` existed and nothing anywhere
    # called it, which made the tenth group a registry rather than an
    # extension point. This is its first real provider.
    "services": {"host_state": host_state},
}
