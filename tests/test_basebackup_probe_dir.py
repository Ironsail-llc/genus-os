"""pg-basebackup.sh must probe the directory it will WRITE, not the mount root.

Incident 2026-09-17: the weekly base backup had silently produced nothing since
2026-08-30. The unit's ExecCondition= probes ``<mount>/robothor`` (group-writable
by the postgres user) and passed; the script's own second probe then ran the
same check against the mount root, which only root can write, and refused with
"cannot create a file" — every week, as the postgres user, with no base backup.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "pg-basebackup.sh"
REAL_PROBE = REPO_ROOT / "scripts" / "backup-volume-check.sh"


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _env(tmp_path: Path, dest: Path, bin_dir: Path, **extra: str) -> dict[str, str]:
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ROBOTHOR_EXTRA_PATH": str(bin_dir),
        "ROBOTHOR_BASEBACKUP_DIR": str(dest),
        "ROBOTHOR_BASEBACKUP_KEEP": "3",
        "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0",
        "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "backup-state"),
        **extra,
    }


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    mount = tmp_path / "mnt" / "backup"
    dest = mount / "robothor" / "basebackup"
    dest.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(
        bin_dir / "pg_basebackup",
        'out=""\n'
        'for a in "$@"; do case "$a" in --pgdata=*) out="${a#--pgdata=}" ;; esac; done\n'
        'mkdir -p "$out"\n'
        ': > "$out/base.tar.gz"\n',
    )
    return mount, dest, bin_dir


def test_the_probe_is_pointed_at_the_directory_the_backup_is_written_under(tmp_path: Path):
    mount, dest, bin_dir = _layout(tmp_path)
    recorded = tmp_path / "probe-argv"
    _stub(bin_dir / "probe.sh", f'printf "%s\\n" "$@" > "{recorded}"\nexit 0\n')
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
        env=_env(tmp_path, dest, bin_dir, ROBOTHOR_VOLUME_CHECK=str(bin_dir / "probe.sh")),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    argv = recorded.read_text().split()
    assert argv[0] == "--rw"
    probed = Path(argv[1])
    assert probed == dest.parent, (
        f"probed {probed}, but the backup is written under {dest.parent}; "
        f"probing the mount root {mount} asks whether the CALLER can write "
        "where only root can, which is not the question"
    )


def test_a_root_owned_mount_root_does_not_block_a_writable_backup_directory(tmp_path: Path):
    """The incident shape: mount root read-only to us, our subtree writable."""
    if os.geteuid() == 0:
        import pytest

        pytest.skip("root can write anywhere; the incident needs an unprivileged caller")
    mount, dest, bin_dir = _layout(tmp_path)
    mount.chmod(0o555)
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
            env=_env(tmp_path, dest, bin_dir, ROBOTHOR_VOLUME_CHECK=str(REAL_PROBE)),
        )
    finally:
        mount.chmod(0o755)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "not a usable backup volume" not in combined
    assert any(p.name.startswith("base-") for p in dest.iterdir()), combined
