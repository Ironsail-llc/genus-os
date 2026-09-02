"""A wedged backup volume must make the backup units SKIP, not FAIL.

The encrypted USB volume goes ``emergency_ro`` when the drive drops off the
bus. Every guard the backup chain had — ``mountpoint -q`` in backup-ssd.sh and
pg-basebackup.sh, ``[[ -d ]]`` in wal-offsite.sh — still passes in that state:
``stat()`` keeps working, only ``readdir()`` fails. So the units RAN, wrote
nothing, and FAILED. ``robothor-wal-offsite`` runs every 15 minutes: 96
OnFailure triggers a day, ~22 Telegram pages whose entire content was a unit
name.

``scripts/backup-volume-check.sh`` is the probe that actually touches the
volume: mount options, a real ``readdir``, and (for ``--rw``) a real write.
Wired as ``ExecCondition=``, its exit 1 makes systemd record
``Result=exec-condition`` and SKIP the unit — no OnFailure, no page — while
exit 255 is still a genuine unit failure that pages.

That 1-vs-255 split is the whole point, so it is asserted directly here:
anything that makes the probe itself unusable (no ``timeout``, no ``findmnt``)
must be 255, and every "the volume is not healthy" answer must be 1.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "backup-volume-check.sh"
# Resolved from the real environment: several cases below hand the script a
# deliberately stripped PATH, which must not also strip the interpreter.
BASH = shutil.which("bash") or "/bin/bash"

# systemd 255 ExecCondition= semantics, restated because everything below
# depends on them: exit 1-254 => the unit is SKIPPED (OnFailure does NOT
# fire); exit 0 => the unit runs; exit 255 => the unit FAILED (OnFailure
# fires).
SKIP = 1
USAGE = 2
BROKEN_PROBE = 255


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _run(
    *args: str, env_extra: dict[str, str] | None = None, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": path if path is not None else os.environ["PATH"],
        # This script never pages, but a test that grows one later must not
        # start doing so silently.
        "ROBOTHOR_ALERT_SUPPRESS": "1",
        "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
        # A pytest tmp_path is on the root filesystem and an unprivileged test
        # cannot create a real mount, so the "is the volume mounted at all?"
        # step is relaxed here. TestAnUnmountedVolumeIsNotAHealthyOne runs
        # WITHOUT this override and is what proves the step is armed by default.
        "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0",
    }
    env.update(env_extra or {})
    return subprocess.run(
        [BASH, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


class TestTheProbeExists:
    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.exists(), "scripts/backup-volume-check.sh missing"
        assert SCRIPT.stat().st_mode & 0o111, (
            "systemd ExecCondition= execs the file directly — a non-executable "
            "probe makes every backup unit fail to start"
        )


class TestHealthyVolume:
    def test_readable_directory_is_healthy(self, tmp_path: Path) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        result = _run("--ro", str(vol))
        assert result.returncode == 0, _output(result)

    def test_writable_directory_is_healthy_under_rw(self, tmp_path: Path) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        result = _run("--rw", str(vol))
        assert result.returncode == 0, _output(result)

    def test_the_rw_probe_leaves_nothing_behind(self, tmp_path: Path) -> None:
        """A probe that litters the backup volume is a probe that gets disabled."""
        vol = tmp_path / "vol"
        vol.mkdir()
        assert _run("--rw", str(vol)).returncode == 0
        assert list(vol.iterdir()) == [], "the write probe left files on the volume"

    def test_mode_defaults_to_read_only(self, tmp_path: Path) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        vol.chmod(0o555)
        try:
            result = _run(str(vol))
            assert result.returncode == 0, _output(result)
        finally:
            vol.chmod(0o755)


class TestUnhealthyVolumeSkipsRatherThanFails:
    def test_emergency_ro_in_the_mount_options_is_unhealthy_and_named(
        self, tmp_path: Path
    ) -> None:
        """The exact state of the 2026-08-27 outage.

        ext4 flips to ``emergency_ro`` when the underlying device disappears.
        The directory still stats fine, so every previous guard passed.
        """
        vol = tmp_path / "vol"
        vol.mkdir()
        bin_dir = tmp_path / "bin"
        _stub(bin_dir / "findmnt", 'echo "rw,relatime,emergency_ro,errors=remount-ro"')

        result = _run(
            "--ro", str(vol), path=f"{bin_dir}:{os.environ['PATH']}"
        )
        assert result.returncode == SKIP, (
            "a wedged volume must SKIP the unit (exit 1), never fail it\n"
            + _output(result)
        )
        assert "emergency_ro" in _output(result), (
            "the operator gets one journal line — it has to name what is wrong\n"
            + _output(result)
        )

    def test_missing_target_is_unhealthy(self, tmp_path: Path) -> None:
        result = _run("--ro", str(tmp_path / "not-there"))
        assert result.returncode == SKIP, _output(result)

    def test_unreadable_directory_is_unhealthy(self, tmp_path: Path) -> None:
        """The readdir probe: emergency_ro breaks readdir and nothing else."""
        vol = tmp_path / "vol"
        vol.mkdir()
        vol.chmod(0o000)
        try:
            result = _run("--ro", str(vol))
            assert result.returncode == SKIP, _output(result)
        finally:
            vol.chmod(0o755)

    def test_rw_probe_fails_on_a_read_only_directory(self, tmp_path: Path) -> None:
        """A volume you can read but not write is not a place to put a backup."""
        vol = tmp_path / "vol"
        vol.mkdir()
        vol.chmod(0o555)
        try:
            result = _run("--rw", str(vol))
            assert result.returncode == SKIP, _output(result)
        finally:
            vol.chmod(0o755)

    def test_rw_requires_rw_in_the_mount_options(self, tmp_path: Path) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        bin_dir = tmp_path / "bin"
        _stub(bin_dir / "findmnt", 'echo "ro,relatime"')

        result = _run("--rw", str(vol), path=f"{bin_dir}:{os.environ['PATH']}")
        assert result.returncode == SKIP, _output(result)


class TestAHungProbeIsNotAHungUnit:
    def test_a_readdir_that_never_returns_times_out_as_unhealthy(
        self, tmp_path: Path
    ) -> None:
        """A dropped USB device blocks readdir forever.

        Without a timeout the probe inherits the hang and the unit sits in
        activating until TimeoutStartSec (3600s for the local backup) — worse
        than the failure it replaced.
        """
        vol = tmp_path / "vol"
        vol.mkdir()
        bin_dir = tmp_path / "bin"
        _stub(bin_dir / "ls", "sleep 60")

        started = time.monotonic()
        result = _run(
            "--ro",
            str(vol),
            env_extra={"ROBOTHOR_VOLUME_PROBE_TIMEOUT": "1"},
            path=f"{bin_dir}:{os.environ['PATH']}",
        )
        elapsed = time.monotonic() - started

        assert result.returncode == SKIP, _output(result)
        assert elapsed < 20, (
            f"the probe took {elapsed:.1f}s — each step must run under "
            "timeout ${ROBOTHOR_VOLUME_PROBE_TIMEOUT:-20}"
        )


class TestABrokenProbeFailsLoudly:
    """255 is the one exit code that still pages. Reserve it for "this probe
    cannot answer the question", never for "the answer is no" — otherwise the
    paging storm comes straight back."""

    def test_missing_timeout_is_255_not_a_silent_skip(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "findmnt").symlink_to("/usr/bin/findmnt")
        vol = tmp_path / "vol"
        vol.mkdir()

        result = _run("--ro", str(vol), path=str(bin_dir))
        assert result.returncode == BROKEN_PROBE, (
            "without `timeout` the probe cannot bound a hung readdir; that is a "
            "broken guard and must page, not quietly skip every backup\n"
            + _output(result)
        )

    def test_missing_findmnt_is_255(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "timeout").symlink_to("/usr/bin/timeout")
        vol = tmp_path / "vol"
        vol.mkdir()

        result = _run("--ro", str(vol), path=str(bin_dir))
        assert result.returncode == BROKEN_PROBE, _output(result)


class TestUsage:
    @pytest.mark.parametrize(
        "args",
        [
            (),
            ("--rw",),
            ("--sideways", "/tmp"),
            ("--rw", "/tmp", "/var"),
        ],
    )
    def test_bad_invocation_is_exit_2(self, args: tuple[str, ...]) -> None:
        result = _run(*args)
        assert result.returncode == USAGE, (
            "a misconfigured ExecCondition= must be distinguishable from an "
            f"unhealthy volume\nargs={args}\n" + _output(result)
        )


class TestAnUnmountedVolumeIsNotAHealthyOne:
    """The `mountpoint -q` guard this probe replaces must not be weakened.

    When the encrypted volume is not mounted at all, /mnt/robothor-backup is
    just an empty directory on the root filesystem. It stats fine, reads fine
    and writes fine — so mount options, readdir and the write probe all pass.
    A backup written there looks like success and silently fills the root disk;
    pg-basebackup.sh has carried a comment about exactly that since 2026-07-14.

    findmnt --target resolves a path to the mount CONTAINING it, so an
    unmounted /mnt/robothor-backup resolves to `/`. That is the signal.

    Every other test here sets ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT=0,
    because a pytest tmp_path is on the root filesystem and an unprivileged
    test cannot create a real mount. This case deliberately does NOT set it:
    it is the one that proves the guard is armed by default rather than being
    a flag nobody turns on.
    """

    def test_a_directory_on_the_root_filesystem_is_unhealthy_by_default(
        self, tmp_path: Path
    ) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()

        result = subprocess.run(
            [BASH, str(SCRIPT), "--rw", str(vol)],
            capture_output=True,
            text=True,
            timeout=60,
            env={"PATH": os.environ["PATH"]},  # nothing overridden
        )

        assert result.returncode == SKIP, (
            "an unmounted backup volume passed every check — a base backup "
            "written there goes to the root disk and looks like success\n"
            + result.stdout
            + result.stderr
        )
        assert "root filesystem" in (result.stdout + result.stderr), (
            "the journal line must say the volume is not mounted, not just "
            "'unhealthy'\n" + result.stdout + result.stderr
        )

    def test_the_check_can_be_relaxed_for_an_instance_without_a_separate_volume(
        self, tmp_path: Path
    ) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        result = _run(
            "--rw",
            str(vol),
            env_extra={"ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0"},
        )
        assert result.returncode == 0, _output(result)


class TestEveryStepIsBounded:
    """"Each step under timeout" includes the cheap one.

    A device that has dropped off the bus can block stat() too, not only
    readdir(). A bash `[[ -d ]]` cannot be interrupted, so the probe would hang
    and the unit would sit in `activating` until TimeoutStartSec — 3600s for
    the nightly backup, which is worse than the failure being replaced.
    """

    def test_a_stat_that_never_returns_times_out_as_unhealthy(
        self, tmp_path: Path
    ) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        bin_dir = tmp_path / "bin"
        _stub(bin_dir / "test", "sleep 60")

        started = time.monotonic()
        result = _run(
            "--ro",
            str(vol),
            env_extra={"ROBOTHOR_VOLUME_PROBE_TIMEOUT": "1"},
            path=f"{bin_dir}:{os.environ['PATH']}",
        )
        elapsed = time.monotonic() - started

        assert result.returncode == SKIP, _output(result)
        assert elapsed < 20, f"the probe hung for {elapsed:.1f}s on the stat step"
