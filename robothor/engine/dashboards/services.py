"""Service health aggregator — polymorphic health checks."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_DIR = Path.home() / "robothor" / "brain" / "memory"


@dataclass
class ServiceStatus:
    id: str
    name: str
    icon: str
    status: str  # "up", "degraded", "down", "off"
    response_ms: int | None = None
    detail: str | None = None
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "icon": self.icon,
            "status": self.status,
            "responseMs": self.response_ms,
            "detail": self.detail,
            "checkedAt": self.checked_at,
        }


@dataclass
class ServiceDef:
    id: str
    name: str
    icon: str
    check: str  # "http", "port", "systemd", "freshness", "rtsp"
    url: str | None = None
    port: int | None = None
    unit: str | None = None
    file: str | None = None
    json_field: str | None = None
    stale_day_mins: int = 15
    stale_night_mins: int = 60


# Service definitions matching the JS status dashboard
SERVICE_DEFS: list[ServiceDef] = [
    ServiceDef(
        id="agent-engine",
        name="Agent Engine",
        icon="\U0001f9e0",
        check="http",
        url="http://localhost:18800/health",
    ),
    ServiceDef(
        id="rag-orchestrator",
        name="RAG Orchestrator",
        icon="\U0001f50d",
        check="http",
        url="http://localhost:9099/health",
    ),
    ServiceDef(
        id="ollama",
        name="Ollama",
        icon="\U0001f999",
        check="http",
        url="http://localhost:11434/api/tags",
    ),
    ServiceDef(id="voice-server", name="Voice Server", icon="\U0001f4de", check="port", port=8765),
    ServiceDef(
        id="bridge-service",
        name="Bridge Service",
        icon="\U0001f517",
        check="http",
        url="http://localhost:9100/health",
    ),
    ServiceDef(
        id="cloudflare-tunnel",
        name="Cloudflare Tunnel",
        icon="\U0001f310",
        check="systemd",
        unit="cloudflared",
    ),
    ServiceDef(
        id="calendar-sync",
        name="Calendar Sync",
        icon="\U0001f4c5",
        check="freshness",
        file=str(MEMORY_DIR / "calendar-log.json"),
        json_field="lastCheckedAt",
        stale_day_mins=15,
        stale_night_mins=60,
    ),
    ServiceDef(
        id="email-sync",
        name="Email Sync",
        icon="\U0001f4e7",
        check="freshness",
        file=str(MEMORY_DIR / "email-log.json"),
        json_field="lastCheckedAt",
        stale_day_mins=15,
        stale_night_mins=60,
    ),
    ServiceDef(
        id="triage-worker",
        name="Triage Worker",
        icon="\u2699\ufe0f",
        check="freshness",
        file=str(MEMORY_DIR / "worker-handoff.json"),
        json_field="lastRunAt",
        stale_day_mins=20,
        stale_night_mins=60,
    ),
    ServiceDef(
        id="supervisor-heartbeat",
        name="Supervisor Heartbeat",
        icon="\U0001f493",
        check="freshness",
        file=str(MEMORY_DIR / "worker-handoff.json"),
        json_field="lastRunAt",
        stale_day_mins=25,
        stale_night_mins=90,
    ),
    ServiceDef(
        id="mediamtx-webcam",
        name="Vision",
        icon="\U0001f441\ufe0f",
        check="rtsp",
        url="rtsp://localhost:8554/webcam",
    ),
]


async def _check_http(url: str, timeout: float = 5.0) -> tuple[str, int | None]:
    """HTTP health check — returns (status, response_ms)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            ms = int(resp.elapsed.total_seconds() * 1000)
            if 200 <= resp.status_code < 500:
                return "up", ms
            return "degraded", ms
    except Exception:
        return "down", None


async def _check_port(port: int, timeout: float = 3.0) -> tuple[str, int | None]:
    """TCP port check."""
    import time

    start = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port),
            timeout=timeout,
        )
        ms = int((time.monotonic() - start) * 1000)
        writer.close()
        await writer.wait_closed()
        return "up", ms
    except Exception:
        return "down", None


def _check_systemd(unit: str) -> tuple[str, int | None]:
    """Check systemd unit status."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=3,
        )
        status = result.stdout.strip()
        return ("up" if status == "active" else "degraded"), None
    except Exception:
        return "down", None


def _check_freshness(
    file_path: str,
    json_field: str,
    stale_day_mins: int,
    stale_night_mins: int,
) -> tuple[str, int | None, str | None]:
    """Check timestamp freshness in a JSON file."""
    try:
        data = json.loads(Path(file_path).read_text())
        val = data.get(json_field)
        if not val:
            return "down", None, "no timestamp"

        ts = datetime.fromisoformat(str(val))
        now = datetime.now(UTC)
        age_mins = (now - ts).total_seconds() / 60

        # Daytime = 8am-10pm ET (UTC-5 roughly)
        et_hour = (now.hour - 5) % 24
        is_daytime = 8 <= et_hour < 22
        threshold = stale_day_mins if is_daytime else stale_night_mins

        if age_mins > threshold * 2:
            status = "down"
        elif age_mins > threshold:
            status = "degraded"
        else:
            status = "up"

        return status, None, f"{int(age_mins)}m ago"
    except Exception as e:
        return "down", None, str(e)


async def _check_rtsp(url: str, timeout: float = 10.0) -> tuple[str, int | None, str | None]:
    """Check RTSP stream via ffmpeg."""
    import time

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-rtsp_transport",
            "tcp",
            "-i",
            url,
            "-frames:v",
            "1",
            "-update",
            "1",
            "-y",
            "/tmp/webcam-status-check.jpg",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        ms = int((time.monotonic() - start) * 1000)
        if proc.returncode == 0:
            return "up", ms, "stream active"
        return "down", ms, None
    except TimeoutError:
        return "down", None, "timeout"
    except Exception:
        return "down", None, None


async def check_service(svc: ServiceDef) -> ServiceStatus:
    """Run a health check for a single service definition."""
    detail = None

    if svc.check == "http":
        status, ms = await _check_http(svc.url or "")
    elif svc.check == "port":
        status, ms = await _check_port(svc.port or 0)
    elif svc.check == "systemd":
        status, ms = _check_systemd(svc.unit or "")
    elif svc.check == "freshness":
        status, ms, detail = _check_freshness(
            svc.file or "",
            svc.json_field or "",
            svc.stale_day_mins,
            svc.stale_night_mins,
        )
    elif svc.check == "rtsp":
        status, ms, detail = await _check_rtsp(svc.url or "")
    else:
        status, ms = "down", None

    return ServiceStatus(
        id=svc.id,
        name=svc.name,
        icon=svc.icon,
        status=status,
        response_ms=ms,
        detail=detail,
    )


async def check_all_services() -> list[ServiceStatus]:
    """Run all service health checks concurrently."""
    return await asyncio.gather(*(check_service(s) for s in SERVICE_DEFS))


def get_overall_status(services: list[ServiceStatus]) -> tuple[str, str]:
    """Return (message, css_class) for overall status."""
    down = sum(1 for s in services if s.status == "down")
    degraded = sum(1 for s in services if s.status == "degraded")

    if down >= 3:
        return "Major Outage", "major"
    if down > 0 or degraded > 0:
        return "Partial Outage", "partial"
    return "All Systems Operational", "ok"


def get_uptime() -> str:
    """Get system uptime."""
    try:
        result = subprocess.run(["uptime", "-p"], capture_output=True, text=True, timeout=3)
        raw = result.stdout.strip().replace("up ", "")
        parts = raw.split(", ")
        return ", ".join(parts[:2])
    except Exception:
        return "unknown"
