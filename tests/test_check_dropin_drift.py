"""Tests for scripts/check_dropin_drift.sh — the guard that keeps the
git-versioned mirror of the engine's systemd drop-in in sync with the live
file under /etc/systemd/system.

Exit codes: 0 = in sync, 1 = drift (diff printed), 2 = a file is missing
or a templated mirror cannot be rendered.

Since the repo mirrors were genericized into templates, a unit-file mirror
carrying placeholder spellings is rendered through scripts/render-unit.sh
before diffing — otherwise every installed unit would read as drifted.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_dropin_drift.sh"

CONF = "Environment=ROBOTHOR_RBAC_ENABLED=1\nEnvironment=ROBOTHOR_RBAC_MODE=enforce\n"


def run(
    live: Path, mirror: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), str(live), str(mirror)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
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


# --- render-aware comparison: templated unit mirrors --------------------------
#
# The repo mirrors are templates (placeholder spellings per
# infra/systemd/README.md); the live files are rendered. The checker must
# render the mirror before diffing — and must fail LOUD (exit 2), never
# silently pass, when the render env is unresolvable or the renderer is gone.

TEMPLATE = (
    "[Service]\n"
    "User=robothor\n"
    "WorkingDirectory=/opt/robothor\n"
    "ReadWritePaths=/home/robothor/.cache\n"
)

RENDERED = (
    "[Service]\n"
    "User=alice\n"
    "WorkingDirectory=/srv/workspace\n"
    "ReadWritePaths=/srv/alice/.cache\n"
)


def render_env(tmp_path: Path) -> dict[str, str]:
    """Hermetic env: explicit render vars, and an env-file path that does not
    exist so the box's real /etc/robothor/robothor.env can never leak in."""
    return {
        "PATH": os.environ["PATH"],
        "ROBOTHOR_WORKSPACE": "/srv/workspace",
        "ROBOTHOR_SERVICE_USER": "alice",
        "ROBOTHOR_SERVICE_HOME": "/srv/alice",
        "ROBOTHOR_ENV_FILE": str(tmp_path / "no-such.env"),
    }


def test_templated_mirror_matching_rendered_live_is_in_sync(tmp_path: Path):
    mirror = tmp_path / "mirror.conf"
    live = tmp_path / "live.conf"
    mirror.write_text(TEMPLATE)
    live.write_text(RENDERED)
    result = run(live, mirror, env=render_env(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_templated_mirror_still_detects_genuine_drift(tmp_path: Path):
    mirror = tmp_path / "mirror.conf"
    live = tmp_path / "live.conf"
    mirror.write_text(TEMPLATE)
    live.write_text(RENDERED + "ReadWritePaths=/mnt/robothor-backup\n")
    result = run(live, mirror, env=render_env(tmp_path))
    assert result.returncode == 1, result.stdout + result.stderr
    assert "DRIFT" in result.stdout
    assert "/mnt/robothor-backup" in result.stdout


def test_unresolvable_render_env_fails_loud_not_silent_ok(tmp_path: Path):
    """Placeholder mirror + no ROBOTHOR_* env + no env file = exit 2, never 0."""
    mirror = tmp_path / "mirror.conf"
    live = tmp_path / "live.conf"
    mirror.write_text(TEMPLATE)
    live.write_text(RENDERED)
    env = {"PATH": os.environ["PATH"], "ROBOTHOR_ENV_FILE": str(tmp_path / "no-such.env")}
    result = run(live, mirror, env=env)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "render" in result.stdout.lower()


def test_missing_renderer_fails_loud_for_templated_mirror(tmp_path: Path):
    """A copy of the checker with no sibling render-unit.sh must exit 2 on a
    templated mirror, not fall back to a raw (always-drifted) diff."""
    scripts_copy = tmp_path / "isolated" / "scripts_copy"
    scripts_copy.mkdir(parents=True)
    script_copy = scripts_copy / "check_dropin_drift.sh"
    script_copy.write_text(SCRIPT.read_text())
    mirror = tmp_path / "mirror.conf"
    live = tmp_path / "live.conf"
    mirror.write_text(TEMPLATE)
    live.write_text(RENDERED)
    result = subprocess.run(
        ["bash", str(script_copy), str(live), str(mirror)],
        capture_output=True,
        text=True,
        timeout=30,
        env=render_env(tmp_path),
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "renderer missing" in result.stdout


def test_host_scripts_keep_the_raw_diff(tmp_path: Path):
    """*.sh mirrors are never rendered — bash's own ${ROBOTHOR_*} defaults and
    literal paths must compare byte-for-byte (host-script drift check)."""
    body = 'DEST="${ROBOTHOR_BASEBACKUP_DIR:-/mnt/robothor-backup/basebackup}"\n'
    mirror = tmp_path / "pg-basebackup.sh"
    live = tmp_path / "live-pg-basebackup.sh"
    mirror.write_text(body)
    live.write_text(body)
    env = {"PATH": os.environ["PATH"], "ROBOTHOR_ENV_FILE": str(tmp_path / "no-such.env")}
    assert run(live, mirror, env=env).returncode == 0
    live.write_text(body + "extra line\n")
    result = run(live, mirror, env=env)
    assert result.returncode == 1
    assert "DRIFT" in result.stdout


# --- stale backup copies beside the live drop-in ------------------------------
#
# The live directory had accumulated TWELVE `upgrade-rip-flags.conf.bak-*` and
# `.pre-*` files, going back to 2026-05-30. systemd reads only `*.conf`, so
# none of them did anything — which is exactly the problem: the one directory
# carrying the production guardrail posture became unreadable, every rollback
# left another copy, and nothing ever said so. The flip runbook's own rollback
# step is what creates them.
#
# Matched as siblings of the file being checked (`<live>.bak*`, `<live>.pre*`,
# `<live>.orig*`, `<live>.save*`, `<live>~`) rather than "anything in the
# directory": guardrail-watch runs this once per mirrored .conf, and a
# directory-wide scan would print the same list six times.


def stale(tmp_path: Path, *names: str) -> tuple[Path, Path]:
    live = tmp_path / "upgrade-rip-flags.conf"
    mirror = tmp_path / "mirror.conf"
    live.write_text(CONF)
    mirror.write_text(CONF)
    for name in names:
        (tmp_path / name).write_text("old posture\n")
    return live, mirror


def test_stale_backup_copies_are_reported_even_when_in_sync(tmp_path: Path):
    live, mirror = stale(
        tmp_path,
        "upgrade-rip-flags.conf.bak-20260713-174439",
        "upgrade-rip-flags.conf.pre-cutover-20260702",
    )
    result = run(live, mirror)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "STALE" in result.stdout
    assert "upgrade-rip-flags.conf.bak-20260713-174439" in result.stdout
    assert "upgrade-rip-flags.conf.pre-cutover-20260702" in result.stdout


def test_stale_report_names_the_count_and_how_to_clear_it(tmp_path: Path):
    live, mirror = stale(tmp_path, "upgrade-rip-flags.conf.bak-1", "upgrade-rip-flags.conf.bak-2")
    result = run(live, mirror)
    assert "2" in result.stdout
    assert "rm" in result.stdout


def test_stale_copies_do_not_mask_a_real_drift(tmp_path: Path):
    live, mirror = stale(tmp_path, "upgrade-rip-flags.conf.bak-1")
    live.write_text(CONF + "Environment=ROBOTHOR_INJECTION_SCAN_MODE=enforce\n")
    result = run(live, mirror)
    assert result.returncode == 1
    assert "STALE" in result.stdout
    assert "DRIFT" in result.stdout
    assert "ROBOTHOR_INJECTION_SCAN_MODE" in result.stdout


def test_the_live_conf_itself_is_never_reported_as_stale(tmp_path: Path):
    """`.conf` is what systemd loads; a sibling drop-in is not a backup."""
    live, mirror = stale(tmp_path, "zz-sandbox.conf", "hardening.conf")
    result = run(live, mirror)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STALE" not in result.stdout


def test_unrelated_backups_belong_to_their_own_pair(tmp_path: Path):
    """Only siblings OF THIS FILE are reported, so guardrail-watch does not
    print the same list once per mirrored .conf."""
    live, mirror = stale(tmp_path, "zz-sandbox.conf.bak-20260820")
    result = run(live, mirror)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STALE" not in result.stdout
