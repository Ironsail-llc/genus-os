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


def fake_curl_failing(tmp_path: Path) -> Path:
    """A curl stand-in that records its argv and fails, like a network outage."""
    log = tmp_path / "curl-args.txt"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    curl.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{log}"\nexit 1\n')
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return log


def curl_call_count(log: Path) -> int:
    """Each invocation writes exactly one Telegram API URL arg; count those."""
    if not log.exists():
        return 0
    return log.read_text().count("api.telegram.org")


def run_send(
    tmp_path: Path, unit: str, env_extra: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        # Isolate the cooldown state dir per test — the default lives under
        # /run/robothor, which is real and writable on this box, and a test
        # run pointed at it would leave a stamp that could suppress a real
        # page later.
        "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
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
    # Point the secrets lookup at a path that does not exist. The sender now
    # recovers its credentials from /run/robothor/secrets.env when they are absent
    # from the environment (so it can page during a cold-boot failure, when that is
    # the ONLY place the token lives). On the live box that file is real — without
    # this override the test would source the operator's actual credentials and send
    # a genuine Telegram message.
    result = run_send(
        tmp_path,
        "robothor-engine.service",
        {"ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env")},
    )
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


class TestCooldownDedup:
    """A unit stuck in a crash loop on a short timer must not page every
    invocation — today's incident was a failing job on a 15-minute timer
    paging the operator 96x/day for the same underlying failure.
    """

    ENV = {"ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123", "ROBOTHOR_TELEGRAM_CHAT_ID": "42"}

    def test_first_call_pages(self, tmp_path: Path):
        log = fake_curl(tmp_path)
        result = run_send(tmp_path, "robothor-engine.service", dict(self.ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_call_count(log) == 1

    def test_second_call_within_cooldown_is_suppressed_without_curling(
        self, tmp_path: Path
    ):
        log = fake_curl(tmp_path)
        env = dict(self.ENV)
        first = run_send(tmp_path, "robothor-engine.service", env)
        assert first.returncode == 0, first.stdout + first.stderr
        assert curl_call_count(log) == 1

        second = run_send(tmp_path, "robothor-engine.service", env)
        assert second.returncode == 0, second.stdout + second.stderr
        assert curl_call_count(log) == 1, (
            "a second page for the same unit within the cooldown must not curl again"
        )
        assert "suppressed duplicate page for robothor-engine.service" in (
            second.stdout + second.stderr
        )

    def test_a_different_unit_is_not_suppressed_by_the_first_units_cooldown(
        self, tmp_path: Path
    ):
        log = fake_curl(tmp_path)
        env = dict(self.ENV)
        run_send(tmp_path, "robothor-engine.service", env)
        second = run_send(tmp_path, "robothor-bridge.service", env)
        assert second.returncode == 0, second.stdout + second.stderr
        assert curl_call_count(log) == 2

    def test_call_after_cooldown_expiry_pages_again(self, tmp_path: Path):
        log = fake_curl(tmp_path)
        env = dict(self.ENV)
        env["ROBOTHOR_ALERT_COOLDOWN_SECONDS"] = "60"

        first = run_send(tmp_path, "robothor-engine.service", env)
        assert first.returncode == 0, first.stdout + first.stderr
        assert curl_call_count(log) == 1

        stamp = tmp_path / "alert-cooldown" / "robothor-engine.service"
        assert stamp.exists(), "a successful page must leave a cooldown stamp"
        subprocess.run(["touch", "-d", "-120 seconds", str(stamp)], check=True)

        second = run_send(tmp_path, "robothor-engine.service", env)
        assert second.returncode == 0, second.stdout + second.stderr
        assert curl_call_count(log) == 2, "a stamp older than the TTL must page again"

    def test_failed_send_does_not_create_a_stamp(self, tmp_path: Path):
        log = fake_curl_failing(tmp_path)
        env = dict(self.ENV)
        result = run_send(tmp_path, "robothor-engine.service", env)
        assert result.returncode != 0
        assert curl_call_count(log) == 1
        stamp = tmp_path / "alert-cooldown" / "robothor-engine.service"
        assert not stamp.exists(), "a failed send must not suppress the retry"
