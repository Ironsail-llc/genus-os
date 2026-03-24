"""Desktop control audit trail — logs all desktop/browser actions with screenshots."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Audit log location
AUDIT_DIR = Path.home() / "robothor" / "brain" / "memory" / "desktop-screenshots"
AUDIT_LOG = Path.home() / "robothor" / "brain" / "memory" / "desktop-audit.jsonl"

# Auto-purge screenshots older than this
SCREENSHOT_RETENTION = timedelta(hours=24)


def log_action(
    *,
    action: str,
    tool_name: str,
    agent_id: str = "",
    run_id: str = "",
    args: dict[str, Any] | None = None,
    screenshot_path: str = "",
) -> None:
    """Log a desktop/browser action to the JSONL audit trail."""
    try:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "action": action,
            "tool_name": tool_name,
            "agent_id": agent_id,
            "run_id": run_id,
        }
        if args:
            # Exclude large fields like screenshots from audit log
            safe_args = {
                k: v for k, v in args.items() if "base64" not in k and "screenshot" not in k
            }
            entry["args"] = safe_args
        if screenshot_path:
            entry["screenshot_path"] = screenshot_path

        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.warning("Failed to write desktop audit log: %s", e)


def save_screenshot(
    data: bytes,
    *,
    run_id: str = "",
    agent_id: str = "",
) -> str:
    """Save a screenshot to the audit directory and return the path."""
    try:
        run_dir = AUDIT_DIR / (run_id or "unknown")
        run_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%f")
        path = run_dir / f"{ts}.png"
        path.write_bytes(data)
        return str(path)
    except Exception as e:
        logger.warning("Failed to save audit screenshot: %s", e)
        return ""


def purge_old_screenshots() -> int:
    """Remove screenshots older than SCREENSHOT_RETENTION. Returns count removed."""
    if not AUDIT_DIR.exists():
        return 0

    cutoff = time.time() - SCREENSHOT_RETENTION.total_seconds()
    removed = 0
    try:
        for run_dir in AUDIT_DIR.iterdir():
            if not run_dir.is_dir():
                continue
            for f in run_dir.iterdir():
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            # Remove empty run directories
            if not any(run_dir.iterdir()):
                run_dir.rmdir()
    except Exception as e:
        logger.warning("Error purging old screenshots: %s", e)
    return removed
