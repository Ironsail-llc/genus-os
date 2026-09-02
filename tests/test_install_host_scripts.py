"""Tests for scripts/install-host-scripts.sh — the installer that replaces
the hand-copy workflow for the host ops scripts under /usr/local/bin.

A permission fix sat in the repo for a month while the installed copy at
/usr/local/bin/robothor-pg-basebackup.sh kept failing, because nothing ever
copied the fix over and nothing checked for drift. This installer is the
copy step, made idempotent and testable; scripts/guardrail_watch.py checks
for drift against it daily (see tests/test_host_script_drift.py).
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install-host-scripts.sh"

INSTALLED_NAMES = {
    "wal-archive.sh": "robothor-wal-archive.sh",
}

#: Mirrors this installer used to write and must now remove.
#:
#: Nothing ever invoked them: robothor-basebackup.service and
#: robothor-wal-offsite.service both ExecStart the WORKSPACE copy. Then both
#: scripts grew `source "$SCRIPT_DIR/backup-state.sh"`, and /usr/local/bin has
#: no sibling of that name — so each mirror is now a file that aborts on its
#: first line while looking, to anyone reading the directory, like the
#: installed backup.
RETIRED_NAMES = {
    "pg-basebackup.sh": "robothor-pg-basebackup.sh",
    "wal-offsite.sh": "robothor-wal-offsite.sh",
}


def run(root: Path, *extra_args: str, env_extra: dict[str, str] | None = None):
    # The installer also renders infra/logrotate/robothor.conf through
    # scripts/render-unit.sh, which requires the workspace and service account
    # and fails loudly rather than installing a policy for the wrong paths.
    # Pinned here so a developer's shell or a real /etc/robothor/robothor.env
    # can never decide a test's outcome.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBOTHOR_")}
    env.update(
        {
            "ROBOTHOR_WORKSPACE": "/srv/genus",
            "ROBOTHOR_SERVICE_USER": "alice",
            "ROBOTHOR_SERVICE_HOME": "/home/alice",
            "ROBOTHOR_ENV_FILE": "/nonexistent/robothor.env",
        }
    )
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", str(SCRIPT), "--root", str(root), *extra_args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def fake_id(tmp_path: Path, postgres_groups: list[str] | None) -> Path:
    """A PATH-hijacked `id` stand-in so the group-membership check does not
    depend on this machine's real postgres system user.

    postgres_groups=None means "no postgres user exists" (the --root/CI
    case the installer must skip gracefully).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "id"
    if postgres_groups is None:
        body = (
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "postgres" || ( "$1" == "-nG" && "$2" == "postgres" ) ]]; then\n'
            "    exit 1\n"
            "fi\n"
            'exec /usr/bin/id "$@"\n'
        )
    else:
        groups = " ".join(postgres_groups)
        body = (
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "-nG" && "$2" == "postgres" ]]; then\n'
            f'    echo "{groups}"\n'
            "    exit 0\n"
            'elif [[ "$1" == "postgres" ]]; then\n'
            "    exit 0\n"
            "fi\n"
            'exec /usr/bin/id "$@"\n'
        )
    stub.write_text(body)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return bin_dir


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), "scripts/install-host-scripts.sh missing"
    assert SCRIPT.stat().st_mode & 0o111, "installer is not executable"


def test_the_retired_mirrors_are_not_installed(tmp_path: Path):
    """They were dead on arrival — no unit runs them — and are now broken as
    well, because they source a sibling that only exists in the workspace."""
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    for dest_name in RETIRED_NAMES.values():
        dest = tmp_path / "usr" / "local" / "bin" / dest_name
        assert not dest.exists(), f"{dest} was installed but nothing invokes it"


def test_an_existing_retired_mirror_is_removed_and_logged(tmp_path: Path):
    """Dropping the install line leaves the broken copy sitting on every box
    that ever ran the old installer. Say what was removed, or the operator
    learns nothing from a silent deletion under /usr/local/bin."""
    bin_dir = tmp_path / "usr" / "local" / "bin"
    bin_dir.mkdir(parents=True)
    for dest_name in RETIRED_NAMES.values():
        (bin_dir / dest_name).write_text("#!/usr/bin/env bash\n# stale mirror\n")

    result = run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    for dest_name in RETIRED_NAMES.values():
        assert not (bin_dir / dest_name).exists(), f"{dest_name} survived the cleanup"
        assert dest_name in result.stdout, result.stdout
    assert "removed" in result.stdout, result.stdout


def test_the_cleanup_is_idempotent(tmp_path: Path):
    """A second run has nothing to remove and must not claim it did."""
    assert run(tmp_path).returncode == 0
    second = run(tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "removed" not in second.stdout, second.stdout


def test_installs_the_remaining_scripts_0755_and_byte_identical(tmp_path: Path):
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr

    for src_name, dest_name in INSTALLED_NAMES.items():
        src = REPO_ROOT / "scripts" / src_name
        dest = tmp_path / "usr" / "local" / "bin" / dest_name
        assert dest.exists(), f"{dest} was not installed"
        assert dest.read_bytes() == src.read_bytes(), f"{dest} does not match {src}"
        mode = dest.stat().st_mode & 0o777
        assert mode == 0o755, f"{dest} mode is {oct(mode)}, expected 0755"


def test_second_run_reports_unchanged(tmp_path: Path):
    first = run(tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr

    second = run(tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    for dest_name in INSTALLED_NAMES.values():
        assert "unchanged" in second.stdout, second.stdout
        assert dest_name in second.stdout


def test_modified_target_is_reported_and_restored(tmp_path: Path):
    first = run(tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr

    target = tmp_path / "usr" / "local" / "bin" / "robothor-wal-archive.sh"
    target.write_text(target.read_text() + "\n# hand edit, should be overwritten\n")

    second = run(tmp_path)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "updated" in second.stdout
    assert "robothor-wal-archive.sh" in second.stdout

    src = REPO_ROOT / "scripts" / "wal-archive.sh"
    assert target.read_bytes() == src.read_bytes(), (
        "a drifted installed copy must be restored to match the repo"
    )


def test_group_check_warns_when_postgres_is_not_a_member(tmp_path: Path):
    bin_dir = fake_id(tmp_path, postgres_groups=["postgres", "ssl-cert"])
    env_extra = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = run(tmp_path, "--group", "robothor-backup", env_extra=env_extra)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sudo usermod -aG robothor-backup postgres" in result.stdout


def test_group_check_silent_when_postgres_is_a_member(tmp_path: Path):
    bin_dir = fake_id(tmp_path, postgres_groups=["postgres", "robothor-backup"])
    env_extra = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = run(tmp_path, "--group", "robothor-backup", env_extra=env_extra)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usermod" not in result.stdout


def test_group_check_skips_gracefully_when_postgres_user_does_not_exist(tmp_path: Path):
    bin_dir = fake_id(tmp_path, postgres_groups=None)
    env_extra = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = run(tmp_path, "--group", "robothor-backup", env_extra=env_extra)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usermod" not in result.stdout


def test_group_check_is_skipped_when_no_group_configured(tmp_path: Path):
    result = run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usermod" not in result.stdout


def test_group_env_var_is_honored_without_the_flag(tmp_path: Path):
    bin_dir = fake_id(tmp_path, postgres_groups=["postgres"])
    env_extra = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "ROBOTHOR_BACKUP_GROUP": "robothor-backup",
    }
    result = run(tmp_path, env_extra=env_extra)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "sudo usermod -aG robothor-backup postgres" in result.stdout
