#!/usr/bin/env python3
"""
Bridge Watchdog — self-healing health check for CRM Bridge service.

Runs every 5 minutes via systemd timer. Checks localhost:9100/health.
- On 2+ consecutive failures: restarts robothor-bridge service
- On success: resets failure counter, auto-resolves stale bridge escalations

State file: memory/bridge-watchdog-state.json
Escalation source: memory/worker-handoff.json
"""

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import requests


# === Defaults ===
def _get_bridge_health_url():
    try:
        from memory_system.service_registry import get_health_url

        url = get_health_url("bridge")
        if url:
            return url
    except ImportError:
        pass
    return "http://localhost:9100/health"


BRIDGE_URL = _get_bridge_health_url()
FAILURE_THRESHOLD = 2
MEMORY_DIR = Path("/home/philip/clawd/memory")
DEFAULT_STATE_FILE = MEMORY_DIR / "bridge-watchdog-state.json"
DEFAULT_HANDOFF_PATH = MEMORY_DIR / "worker-handoff.json"

INFRA_KEYWORDS = ["bridge", "connection refused", "relay failure", "9100"]


def check_bridge_health() -> bool:
    """Check if bridge is responding on localhost:9100/health."""
    try:
        resp = requests.get(BRIDGE_URL, timeout=5)
        return resp.status_code < 500
    except Exception:
        return False


def load_state(state_file: Path) -> dict:
    """Load watchdog state (consecutive failure count)."""
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return {"consecutive_failures": 0}


def save_state(state_file: Path, consecutive_failures: int):
    """Save watchdog state atomically."""
    data = {
        "consecutive_failures": consecutive_failures,
        "last_check": datetime.now().isoformat(),
    }
    fd, tmp = tempfile.mkstemp(dir=state_file.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, state_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def restart_bridge():
    """Restart the robothor-bridge systemd service."""
    try:
        subprocess.run(
            ["sudo", "systemctl", "restart", "robothor-bridge"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(f"[{datetime.now().isoformat()}] Restarted robothor-bridge")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Failed to restart bridge: {e}")


def _is_bridge_escalation(esc: dict) -> bool:
    """Check if an escalation is about bridge/infra failure."""
    if esc.get("sourceId") == "bridge-health":
        return True
    text = (esc.get("summary", "") + " " + esc.get("reason", "")).lower()
    return any(kw in text for kw in INFRA_KEYWORDS)


def resolve_bridge_escalations(handoff_path: Path):
    """Auto-resolve any open bridge-related escalations."""
    try:
        data = json.loads(handoff_path.read_text())
    except Exception:
        return

    now = datetime.now().isoformat()
    changed = False
    for esc in data.get("escalations", []):
        if esc.get("resolvedAt") is not None:
            continue
        if _is_bridge_escalation(esc):
            esc["resolvedAt"] = now
            esc["resolution"] = "Auto-resolved by watchdog: bridge health check passing"
            esc["handled"] = True
            changed = True

    if changed:
        fd, tmp = tempfile.mkstemp(dir=handoff_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, handoff_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def run_watchdog(
    state_file: Path = DEFAULT_STATE_FILE,
    handoff_path: Path = DEFAULT_HANDOFF_PATH,
):
    """Main watchdog loop iteration."""
    healthy = check_bridge_health()
    state = load_state(state_file)
    failures = state["consecutive_failures"]

    if healthy:
        if failures > 0:
            print(f"[{datetime.now().isoformat()}] Bridge recovered after {failures} failure(s)")
        save_state(state_file, 0)
        resolve_bridge_escalations(handoff_path)
    else:
        failures += 1
        save_state(state_file, failures)
        print(f"[{datetime.now().isoformat()}] Bridge health check failed ({failures} consecutive)")

        if failures >= FAILURE_THRESHOLD:
            print(f"[{datetime.now().isoformat()}] Threshold reached, restarting bridge...")
            restart_bridge()


if __name__ == "__main__":
    run_watchdog()
