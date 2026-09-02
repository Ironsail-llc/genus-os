"""Tests for infra/logrotate/robothor.conf and its installer.

The box carried /etc/logrotate.d/robothor with no source in the repo, and it
covered exactly one path: /var/log/robothor/*.log. Everything the cron jobs
write — they append with plain `>>` redirects, so nothing ever truncates them —
went to brain/memory_system/logs/, which had grown to 205 MB unrotated. A
rebuilt box would have inherited neither the rotation policy nor the knowledge
that it was needed.

`copytruncate` is the load-bearing directive: the writers are shell redirects
held open by long-lived processes, so a renamed file keeps being written to at
the same inode and rotation silently accomplishes nothing.

The config is a TEMPLATE (canonical /opt/robothor placeholders, per
infra/systemd/README.md) rendered through scripts/render-unit.sh, because the
workspace path differs per instance — hardcoding one would be CLAUDE.md rule 1.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = REPO_ROOT / "infra" / "logrotate" / "robothor.conf"
RENDER = REPO_ROOT / "scripts" / "render-unit.sh"
INSTALL_HOST = REPO_ROOT / "scripts" / "install-host-scripts.sh"
BACKUP = REPO_ROOT / "scripts" / "backup-ssd.sh"
GITIGNORE = REPO_ROOT / ".gitignore"

WS = "/srv/genus"
USER = "alice"

_spec = importlib.util.spec_from_file_location(
    "guardrail_watch", REPO_ROOT / "scripts" / "guardrail_watch.py"
)
assert _spec is not None and _spec.loader is not None
gw = importlib.util.module_from_spec(_spec)
sys.modules["guardrail_watch"] = gw
_spec.loader.exec_module(gw)


def base_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBOTHOR_")}
    env.update(
        {
            "ROBOTHOR_WORKSPACE": WS,
            "ROBOTHOR_SERVICE_USER": USER,
            "ROBOTHOR_SERVICE_HOME": "/home/alice",
            "ROBOTHOR_ENV_FILE": "/nonexistent/robothor.env",
        }
    )
    env.update(overrides)
    return env


def render(dest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RENDER), str(TEMPLATE), str(dest)],
        capture_output=True,
        text=True,
        timeout=30,
        env=base_env(),
    )


# ── the template ─────────────────────────────────────────────────────────────


def test_template_exists():
    assert TEMPLATE.exists(), "infra/logrotate/robothor.conf missing"


def test_template_covers_the_unrotated_logs():
    body = TEMPLATE.read_text()
    assert "/var/log/robothor/*.log" in body
    assert "/opt/robothor/brain/memory_system/logs/*.log" in body, (
        "the 205 MB of unrotated cron output lives here"
    )
    assert "/opt/robothor/brain/memory_system/logs/*.jsonl" in body


def test_template_uses_copytruncate():
    """The writers are `>>` redirects held open by long-lived processes: a
    renamed file keeps being written to at the same inode, so rotating without
    copytruncate frees nothing at all."""
    assert "copytruncate" in TEMPLATE.read_text()


def test_template_policy():
    body = TEMPLATE.read_text()
    for directive in ("weekly", "rotate 8", "compress", "delaycompress", "missingok", "notifempty"):
        assert directive in body, f"missing directive: {directive}"


def test_template_carries_no_instance_path():
    """CLAUDE.md rule 1 — the workspace is a placeholder the renderer fills."""
    assert "/home/" not in TEMPLATE.read_text()


# ── rendering ────────────────────────────────────────────────────────────────


def test_render_substitutes_the_workspace(tmp_path: Path):
    dest = tmp_path / "robothor"
    result = render(dest)
    assert result.returncode == 0, result.stdout + result.stderr
    # Comment lines pass through unrendered by design (render-unit.sh) — they
    # may legitimately DISCUSS a placeholder — so gate the directives only.
    directives = "\n".join(
        line for line in dest.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    assert f"{WS}/brain/memory_system/logs/*.log" in directives
    assert "/opt/robothor" not in directives


@pytest.mark.skipif(shutil.which("logrotate") is None, reason="logrotate not installed")
def test_rendered_config_parses(tmp_path: Path):
    """A logrotate config that does not parse rotates nothing, and says so only
    in a log nobody reads."""
    dest = tmp_path / "robothor"
    assert render(dest).returncode == 0
    result = subprocess.run(
        ["logrotate", "-d", "-s", str(tmp_path / "state"), str(dest)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "error" not in result.stderr.lower(), result.stderr


# ── installation ─────────────────────────────────────────────────────────────


def run_install(root: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        ["bash", str(INSTALL_HOST), "--root", str(root)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env or base_env(),
    )


def test_installer_renders_the_config_into_logrotate_d(tmp_path: Path):
    result = run_install(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    dest = tmp_path / "etc" / "logrotate.d" / "robothor"
    assert dest.exists(), result.stdout
    assert dest.stat().st_mode & 0o777 == 0o644
    assert f"{WS}/brain/memory_system/logs/*.jsonl" in dest.read_text()


def test_installer_is_idempotent_for_the_config(tmp_path: Path):
    assert run_install(tmp_path).returncode == 0
    second = run_install(tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    unchanged = [
        line
        for line in second.stdout.splitlines()
        if "unchanged" in line and "logrotate.d/robothor" in line
    ]
    assert unchanged, second.stdout
    assert "updated" not in second.stdout


def test_installer_fails_loudly_when_the_render_env_is_unresolvable(tmp_path: Path):
    """Silently skipping the render would install the ops scripts and leave the
    box with no rotation policy, reporting success either way."""
    env = base_env()
    env.pop("ROBOTHOR_WORKSPACE")
    result = run_install(tmp_path, env)
    assert result.returncode != 0
    assert "ROBOTHOR_WORKSPACE" in result.stdout + result.stderr


# ── drift ────────────────────────────────────────────────────────────────────


def test_logrotate_is_in_the_host_script_drift_pairs():
    """Installed once and never checked is how /etc/logrotate.d/robothor came
    to have no repo source in the first place."""
    pairs = dict(gw.HOST_SCRIPT_DRIFT_PAIRS)
    assert "/etc/logrotate.d/robothor" in pairs
    assert pairs["/etc/logrotate.d/robothor"] == "infra/logrotate/robothor.conf"


# ── the backup log ───────────────────────────────────────────────────────────


def test_backup_log_defaults_outside_the_git_tree():
    """scripts/backup.log was being written INSIDE the checkout, where it is one
    `git add -A` away from being committed — and no logrotate glob covers it."""
    body = BACKUP.read_text()
    assert 'ROBOTHOR_LOG_DIR:-/var/log/robothor' in body, (
        "backup-ssd.sh must default its log directory to /var/log/robothor/, "
        "which the logrotate config covers"
    )
    assert '_default_log="${_log_dir}/backup.log"' in body, body


def test_the_in_tree_log_survives_only_as_the_unwritable_fallback():
    """The default is used by a bare `>>` under `set -euo pipefail`, so a box
    with no writable /var/log/robothor would lose the whole backup to its log
    destination. The old in-tree path is the fallback and nothing else — one
    mention, inside the guarded branch.

    That the fallback actually runs the backup is proved by driving the script:
    tests/test_backup_pages_on_failure.py::
    TestTheVolumeProbeActuallyGatesTheLocalBackup::
    test_an_unwritable_log_directory_does_not_abort_the_backup
    """
    lines = [ln for ln in BACKUP.read_text().splitlines() if "scripts/backup.log" in ln]
    assert len(lines) == 1, lines
    assert lines[0].strip().startswith("_default_log="), lines
    assert lines[0].startswith("    "), (
        "the in-tree path is at top level, so it is the default rather than "
        f"the fallback: {lines[0]!r}"
    )


def test_backup_log_is_gitignored():
    assert "scripts/backup.log" in GITIGNORE.read_text()
