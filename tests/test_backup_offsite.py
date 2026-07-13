"""Offsite backup replication — the copy that survives losing the box.

Today every backup lives on a LUKS SSD attached to the same machine as
production: one fire, theft, or PSU surge takes prod *and* every backup.
`scripts/backup-offsite.sh` pushes the recoverable core (DB dumps + the
systemd drop-ins that carry the guardrail posture + instance config) to an
rclone remote, verifies it landed, prunes old generations, and pages the
operator when it fails — a silent backup failure is the same as no backup.

These tests drive the script against a *local* rclone remote, so the whole
pipeline (upload, verify, retention, failure paths) is proven without needing
cloud credentials.
"""

from __future__ import annotations

import gzip
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backup-offsite.sh"

pytestmark = pytest.mark.skipif(shutil.which("rclone") is None, reason="rclone not installed")


def _make_source(tmp_path: Path, *, days: int = 3) -> Path:
    """A stand-in for the nightly dump directory."""
    src = tmp_path / "db"
    src.mkdir(parents=True)
    for d in range(days):
        f = src / f"robothor_memory-2026071{d}.sql.gz"
        with gzip.open(f, "wb") as fh:
            fh.write(f"dump-{d}".encode() * 100)
    return src


def _run(tmp_path: Path, src: Path, dest: Path, **env_extra) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ROBOTHOR_OFFSITE_REMOTE": str(dest),  # a plain path = rclone local backend
        "ROBOTHOR_OFFSITE_SOURCE": str(src),
        "ROBOTHOR_OFFSITE_KEEP": "2",
        "ROBOTHOR_OFFSITE_LOG": str(tmp_path / "offsite.log"),
    }
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=env
    )


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), "scripts/backup-offsite.sh missing"
    assert SCRIPT.stat().st_mode & 0o111


def test_uploads_the_latest_dump(tmp_path: Path):
    src = _make_source(tmp_path)
    dest = tmp_path / "remote"

    result = _run(tmp_path, src, dest)

    assert result.returncode == 0, result.stdout + result.stderr
    uploaded = list((dest / "db").glob("*.sql.gz"))
    assert uploaded, "nothing was replicated offsite"
    # newest dump must be present
    assert any("20260712" in f.name for f in uploaded)


def test_verifies_the_copy_and_fails_on_corruption(tmp_path: Path):
    """A copy that didn't land intact must fail loudly, not silently pass."""
    src = _make_source(tmp_path)
    dest = tmp_path / "remote"

    assert _run(tmp_path, src, dest).returncode == 0

    # corrupt the remote copy, then re-run with verify-only: it must fail
    for f in (dest / "db").glob("*.sql.gz"):
        f.write_bytes(b"corrupted")

    result = _run(tmp_path, src, dest, ROBOTHOR_OFFSITE_VERIFY_ONLY="1")
    assert result.returncode != 0, "corrupted offsite copy was reported as healthy"
    assert "verif" in (result.stdout + result.stderr).lower()


def test_retention_prunes_old_generations(tmp_path: Path):
    src = _make_source(tmp_path, days=5)
    dest = tmp_path / "remote"

    assert _run(tmp_path, src, dest).returncode == 0

    remaining = sorted(f.name for f in (dest / "db").glob("*.sql.gz"))
    assert len(remaining) == 2, f"KEEP=2 not honored, got {remaining}"
    # the newest two survive
    assert remaining == sorted(remaining)[-2:]


def test_includes_the_guardrail_dropin(tmp_path: Path):
    """The systemd drop-in IS the security posture — it must survive the box."""
    src = _make_source(tmp_path)
    dest = tmp_path / "remote"
    dropin = tmp_path / "dropins"
    dropin.mkdir()
    (dropin / "upgrade-rip-flags.conf").write_text("Environment=ROBOTHOR_RBAC_MODE=enforce\n")

    result = _run(tmp_path, src, dest, ROBOTHOR_OFFSITE_DROPIN_DIR=str(dropin))

    assert result.returncode == 0, result.stdout + result.stderr
    copied = list((dest / "systemd").glob("*.conf"))
    assert copied, "guardrail drop-in was not replicated"
    assert "RBAC_MODE=enforce" in copied[0].read_text()


def test_missing_remote_config_fails_loudly(tmp_path: Path):
    src = _make_source(tmp_path)
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "ROBOTHOR_OFFSITE_SOURCE": str(src),
            "ROBOTHOR_OFFSITE_LOG": str(tmp_path / "offsite.log"),
        },
    )
    assert result.returncode != 0
    assert "ROBOTHOR_OFFSITE_REMOTE" in result.stdout + result.stderr


def test_missing_source_fails_loudly(tmp_path: Path):
    result = _run(tmp_path, tmp_path / "nonexistent", tmp_path / "remote")
    assert result.returncode != 0
    assert "source" in (result.stdout + result.stderr).lower()
