"""Tests for scripts/check_dropin_drift.sh — the guard that keeps the
git-versioned mirror of the engine's systemd drop-in in sync with the live
file under /etc/systemd/system.

Exit codes: 0 = in sync, 1 = drift (diff printed), 2 = a file is missing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_dropin_drift.sh"

CONF = "Environment=ROBOTHOR_RBAC_ENABLED=1\nEnvironment=ROBOTHOR_RBAC_MODE=enforce\n"


def run(live: Path, mirror: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(live), str(mirror)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), "scripts/check_dropin_drift.sh missing"
    assert SCRIPT.stat().st_mode & 0o111, "drift-check script is not executable"


def test_in_sync_exits_zero(tmp_path: Path):
    live = tmp_path / "live.conf"
    mirror = tmp_path / "mirror.conf"
    live.write_text(CONF)
    mirror.write_text(CONF)
    result = run(live, mirror)
    assert result.returncode == 0, result.stdout + result.stderr


def test_drift_exits_one_and_prints_diff(tmp_path: Path):
    live = tmp_path / "live.conf"
    mirror = tmp_path / "mirror.conf"
    live.write_text(CONF + "Environment=ROBOTHOR_INJECTION_SCAN_MODE=enforce\n")
    mirror.write_text(CONF)
    result = run(live, mirror)
    assert result.returncode == 1
    assert "ROBOTHOR_INJECTION_SCAN_MODE" in result.stdout
    assert "DRIFT" in result.stdout


def test_missing_live_file_exits_two(tmp_path: Path):
    mirror = tmp_path / "mirror.conf"
    mirror.write_text(CONF)
    result = run(tmp_path / "nope.conf", mirror)
    assert result.returncode == 2
    assert "missing" in result.stdout.lower()


def test_missing_mirror_exits_two(tmp_path: Path):
    live = tmp_path / "live.conf"
    live.write_text(CONF)
    result = run(live, tmp_path / "nope.conf")
    assert result.returncode == 2
    assert "missing" in result.stdout.lower()


def test_default_paths_point_at_live_dropin_and_repo_mirror():
    """Without args the script must target the canonical locations."""
    text = SCRIPT.read_text()
    assert "/etc/systemd/system/robothor-engine.service.d/upgrade-rip-flags.conf" in text
    assert "infra/systemd/robothor-engine.service.d/upgrade-rip-flags.conf" in text
