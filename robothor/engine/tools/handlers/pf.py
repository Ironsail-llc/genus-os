"""Princess Freya (PF) vessel tool handlers — sensor and system status tools."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from robothor.engine.tools.dispatch import ToolContext

logger = logging.getLogger(__name__)

HANDLERS: dict[str, Any] = {}


def _handler(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        HANDLERS[name] = fn
        return fn

    return decorator


def _run_cmd(cmd: list[str], timeout: int = 10) -> str:
    """Run a shell command and return stdout, or raise on failure."""
    result = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _get_battery_voltage() -> float | None:
    """Read battery voltage from Jetson power rail (INA3221 sensor)."""
    try:
        # Jetson Orin NX exposes voltage via sysfs (INA3221 channel 0 = VDD_IN)
        voltage_path = Path("/sys/bus/i2c/drivers/ina3221/1-0040/hwmon")
        hwmon_dirs = list(voltage_path.glob("hwmon*"))
        if hwmon_dirs:
            mv = int((hwmon_dirs[0] / "in1_input").read_text().strip())
            return mv / 1000.0
    except (OSError, ValueError):
        pass
    return None


def _check_connectivity() -> dict[str, bool]:
    """Check network connectivity status."""
    checks: dict[str, bool] = {}

    # Tailscale — parse JSON properly instead of string matching
    try:
        out = _run_cmd(["tailscale", "status", "--json"])
        data = json.loads(out)
        checks["tailscale"] = (
            data.get("BackendState") == "Running" and data.get("Self", {}).get("Online") is True
        )
    except (subprocess.SubprocessError, FileNotFoundError, json.JSONDecodeError):
        checks["tailscale"] = False

    # Internet (ping Cloudflare DNS)
    try:
        _run_cmd(["ping", "-c", "1", "-W", "3", "1.1.1.1"])
        checks["internet"] = True
    except (subprocess.SubprocessError, FileNotFoundError):
        checks["internet"] = False

    # Parent reachable
    try:
        _run_cmd(["ping", "-c", "1", "-W", "3", "100.91.221.100"])
        checks["parent"] = True
    except (subprocess.SubprocessError, FileNotFoundError):
        checks["parent"] = False

    return checks


@_handler("pf_system_status")
async def _pf_system_status(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """Get PF system status: battery, disk, memory, connectivity, GPS lock."""

    def _run() -> dict[str, Any]:
        try:
            status: dict[str, Any] = {}

            # Battery voltage (from Jetson power sensor)
            voltage = _get_battery_voltage()
            status["battery_v"] = voltage

            # Disk usage
            disk = shutil.disk_usage("/")
            status["disk"] = {
                "total_gb": round(disk.total / (1024**3), 1),
                "free_gb": round(disk.free / (1024**3), 1),
                "used_pct": round((disk.used / disk.total) * 100, 1),
            }

            # Memory
            try:
                meminfo = Path("/proc/meminfo").read_text()
                mem_total = mem_avail = 0
                for line in meminfo.splitlines():
                    if line.startswith("MemTotal:"):
                        mem_total = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        mem_avail = int(line.split()[1])
                status["memory"] = {
                    "total_mb": round(mem_total / 1024, 0),
                    "available_mb": round(mem_avail / 1024, 0),
                    "used_pct": round(((mem_total - mem_avail) / mem_total) * 100, 1)
                    if mem_total
                    else 0,
                }
            except OSError:
                status["memory"] = None

            # CPU temperature (Jetson thermal zone)
            try:
                temp_mc = int(
                    Path("/sys/devices/virtual/thermal/thermal_zone0/temp").read_text().strip()
                )
                status["cpu_temp_c"] = round(temp_mc / 1000.0, 1)
            except (OSError, ValueError):
                status["cpu_temp_c"] = None

            # Connectivity
            status["connectivity"] = _check_connectivity()

            # GPS lock (placeholder — needs NMEA hardware)
            status["gps_lock"] = None

            # Bilge pump (placeholder — needs sensor hardware)
            status["bilge_active"] = None

            # Uptime
            try:
                uptime_secs = float(Path("/proc/uptime").read_text().split()[0])
                hours = int(uptime_secs // 3600)
                mins = int((uptime_secs % 3600) // 60)
                status["uptime"] = f"{hours}h {mins}m"
            except OSError:
                status["uptime"] = None

            return status
        except Exception as e:
            return {"error": f"System status check failed: {e}"}

    return await asyncio.to_thread(_run)
