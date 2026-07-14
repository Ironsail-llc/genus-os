"""Tests for the OnFailure paging path:

- scripts/send_failure_alert.sh — posts a Telegram message when a systemd
  unit fails (invoked as robothor-alert@<unit>.service).
- scripts/install_onfailure_alerts.sh — installs an OnFailure= drop-in for
  each named unit under a systemd root.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SEND = REPO_ROOT / "scripts" / "send_failure_alert.sh"
INSTALL = REPO_ROOT / "scripts" / "install_onfailure_alerts.sh"
ALERT_UNIT = REPO_ROOT / "infra" / "systemd" / "robothor-alert@.service"


def fake_curl(tmp_path: Path) -> Path:
    """A curl stand-in that records its argv and succeeds."""
    log = tmp_path / "curl-args.txt"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    curl.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{log}"\n')
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return log


def run_send(
    tmp_path: Path, unit: str, env_extra: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
    }
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(SEND), unit], capture_output=True, text=True, timeout=30, env=env
    )


def test_scripts_exist_and_are_executable():
    for script in (SEND, INSTALL):
        assert script.exists(), f"{script} missing"
        assert script.stat().st_mode & 0o111, f"{script} not executable"


def test_alert_template_unit_exists_and_invokes_sender():
    assert ALERT_UNIT.exists(), "infra/systemd/robothor-alert@.service missing"
    text = ALERT_UNIT.read_text()
    assert "send_failure_alert.sh %i" in text
    assert "Type=oneshot" in text


def test_send_posts_unit_name_to_telegram(tmp_path: Path):
    log = fake_curl(tmp_path)
    result = run_send(
        tmp_path,
        "robothor-engine.service",
        {"ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123", "ROBOTHOR_TELEGRAM_CHAT_ID": "42"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    args = log.read_text()
    assert "api.telegram.org/bottok123/sendMessage" in args
    assert "robothor-engine.service" in args
    assert "42" in args


def test_send_fails_loudly_without_token(tmp_path: Path):
    fake_curl(tmp_path)
    result = run_send(tmp_path, "robothor-engine.service", {})
    assert result.returncode != 0
    assert "ROBOTHOR_TELEGRAM_BOT_TOKEN" in result.stdout + result.stderr


def test_install_creates_onfailure_dropins(tmp_path: Path):
    root = tmp_path / "systemd"
    result = subprocess.run(
        [
            "bash",
            str(INSTALL),
            "--root",
            str(root),
            "robothor-engine.service",
            "robothor-bridge.service",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for unit in ("robothor-engine.service", "robothor-bridge.service"):
        dropin = root / f"{unit}.d" / "onfailure.conf"
        assert dropin.exists(), f"missing drop-in for {unit}"
        content = dropin.read_text()
        assert "OnFailure=robothor-alert@%n.service" in content


def test_install_is_idempotent(tmp_path: Path):
    root = tmp_path / "systemd"
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(INSTALL), "--root", str(root), "robothor-engine.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
    assert (root / "robothor-engine.service.d" / "onfailure.conf").exists()
