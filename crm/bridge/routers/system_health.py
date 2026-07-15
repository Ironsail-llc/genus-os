"""Operator-scoped, read-only host health: WAL archiving, backup timers, failed
systemd units, disks. All probes are read-only (no root). Each probe is isolated:
one that raises degrades its own section to 'unknown' — the endpoint never 500s,
and a probe failure never reads as healthy."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime

import psycopg2.extras
from fastapi import APIRouter, Request

from robothor.db.connection import get_connection
from routers._operator import require_operator

router = APIRouter(prefix="/api/health", tags=["health"])

_BACKUP_TIMERS = ("robothor-backup-local.timer", "robothor-backup-offsite.timer")
_WATCH_MOUNTS = ("/", "/mnt/robothor-backup")
_STALE_BACKUP_HOURS = 26.0


def _wal_status() -> dict:
    with get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT archived_count, failed_count, last_archived_time FROM pg_stat_archiver"
            )
            row = cur.fetchone() or {}
    failed = row.get("failed_count") or 0
    last = row.get("last_archived_time")
    return {
        "archived_count": row.get("archived_count") or 0,
        "failed_count": failed,
        "last_archived_time": last.isoformat() if last else None,
        "status": "ok" if failed == 0 and last is not None else "warn",
    }


def _systemctl(*args: str) -> str:
    return subprocess.run(
        ["systemctl", *args], capture_output=True, text=True, timeout=10
    ).stdout.strip()


def _backup_status() -> list[dict]:
    now = datetime.now(UTC)
    out = []
    for unit in _BACKUP_TIMERS:
        raw = _systemctl("show", unit, "-p", "LastTriggerUSec", "--value")
        age_hours = None
        last_iso = None
        # LastTriggerUSec is a human date string like "Mon 2026-07-14 03:00:00 UTC"
        if raw and raw not in ("", "0", "n/a"):
            try:
                dt = datetime.strptime(raw.rsplit(" ", 1)[0], "%a %Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
                age_hours = (now - dt).total_seconds() / 3600.0
                last_iso = dt.isoformat()
            except ValueError:
                pass
        status = "ok" if (age_hours is not None and age_hours <= _STALE_BACKUP_HOURS) else "warn"
        out.append(
            {"unit": unit, "last_trigger": last_iso, "age_hours": age_hours, "status": status}
        )
    return out


def _failed_units() -> list[str]:
    raw = _systemctl("list-units", "--state=failed", "--no-legend", "--plain")
    return [line.split()[0] for line in raw.splitlines() if line.strip()]


def _disk_status() -> list[dict]:
    out = []
    for mount in _WATCH_MOUNTS:
        try:
            u = shutil.disk_usage(mount)
        except (FileNotFoundError, PermissionError):
            out.append({"mount": mount, "used_pct": None, "free_gb": None, "status": "unknown"})
            continue
        used_pct = (u.used / u.total * 100.0) if u.total else 0.0
        out.append(
            {
                "mount": mount,
                "used_pct": round(used_pct, 1),
                "free_gb": round(u.free / 1e9, 1),
                "status": "warn" if used_pct >= 90.0 else "ok",
            }
        )
    return out


def _safe(fn):
    try:
        return fn()
    except Exception as exc:  # a probe failure must never 500 or read as healthy
        return {"status": "unknown", "error": str(exc)}


@router.get("/system")
def system_health(request: Request) -> dict:
    require_operator(request)
    return {
        "wal": _safe(_wal_status),
        "backups": _safe(_backup_status),
        "failed_units": _safe(_failed_units),
        "disks": _safe(_disk_status),
        "generated_at": datetime.now(UTC).isoformat(),
    }
