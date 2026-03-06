#!/usr/bin/env python3
"""
Engine Watchdog — external health check for Robothor Agent Engine.

Runs every 2 minutes via systemd timer. Checks localhost:18800/health.
- On 2+ consecutive failures: sends Telegram alert (direct Bot API), restarts engine
- On success: resets failure counter, sends recovery alert if previously failed

State file: brain/memory/engine-watchdog-state.json

Key difference from bridge_watchdog.py: alerts go directly via Telegram Bot API
(not through the engine), since the engine itself may be frozen.
"""

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import requests

ENGINE_HEALTH_URL = "http://localhost:18800/health"
FAILURE_THRESHOLD = 2
COOLDOWN_CYCLES = 2  # Skip alerting for N cycles after a restart
MEMORY_DIR = Path("/home/philip/robothor/brain/memory")
STATE_FILE = MEMORY_DIR / "engine-watchdog-state.json"


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"consecutive_failures": 0, "cooldown": 0}


def save_state(state: dict) -> None:
    state["last_check"] = datetime.now().isoformat()
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def check_health() -> bool:
    try:
        resp = requests.get(ENGINE_HEALTH_URL, timeout=5)
        return resp.status_code < 500
    except Exception:
        return False


def send_telegram_alert(message: str) -> None:
    """Send alert directly via Telegram Bot API (bypasses frozen engine)."""
    token = os.environ.get("ROBOTHOR_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("ROBOTHOR_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[{datetime.now().isoformat()}] Cannot send Telegram alert: missing token or chat_id")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Telegram alert failed: {e}")


def restart_engine() -> None:
    try:
        subprocess.run(
            ["sudo", "systemctl", "restart", "robothor-engine"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(f"[{datetime.now().isoformat()}] Restarted robothor-engine")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] Failed to restart engine: {e}")


def run_watchdog() -> None:
    healthy = check_health()
    state = load_state()
    failures = state.get("consecutive_failures", 0)
    cooldown = state.get("cooldown", 0)

    if healthy:
        if failures >= FAILURE_THRESHOLD:
            print(f"[{datetime.now().isoformat()}] Engine recovered after {failures} failure(s)")
            if cooldown <= 0:
                send_telegram_alert(
                    f"*Engine Watchdog — Recovered*\n\n"
                    f"Engine is healthy again after {failures} consecutive failure(s)."
                )
        save_state({"consecutive_failures": 0, "cooldown": 0})
    else:
        failures += 1
        print(f"[{datetime.now().isoformat()}] Engine health check failed ({failures} consecutive)")

        if cooldown > 0:
            save_state({"consecutive_failures": failures, "cooldown": cooldown - 1})
            print(f"[{datetime.now().isoformat()}] In cooldown ({cooldown - 1} cycles remaining)")
            return

        if failures >= FAILURE_THRESHOLD:
            send_telegram_alert(
                f"*Engine Watchdog — ALERT*\n\n"
                f"Engine unresponsive ({failures} consecutive failures).\n"
                f"Restarting robothor-engine..."
            )
            restart_engine()
            save_state({"consecutive_failures": failures, "cooldown": COOLDOWN_CYCLES})
        else:
            save_state({"consecutive_failures": failures, "cooldown": 0})


if __name__ == "__main__":
    run_watchdog()
