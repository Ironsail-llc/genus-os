"""The engine systemd unit must arm the watchdog the daemon already feeds (PR-6).

robothor/engine/daemon.py calls sd_notify READY=1 after startup and WATCHDOG=1
every 30s, but the unit was missing Type=notify/WatchdogSec, so the readiness
gate and hang-detection were dead. This guards that they stay in sync.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_UNIT = _ROOT / "infra" / "systemd" / "robothor-engine.service"
_DAEMON = _ROOT / "robothor" / "engine" / "daemon.py"


def test_unit_declares_notify_and_watchdog():
    text = _UNIT.read_text()
    assert "Type=notify" in text
    assert "WatchdogSec=" in text
    assert "NotifyAccess=main" in text


def test_daemon_feeds_what_the_unit_expects():
    daemon = _DAEMON.read_text()
    # Type=notify requires READY=1; WatchdogSec requires periodic WATCHDOG=1.
    assert "READY=1" in daemon
    assert "WATCHDOG=1" in daemon
