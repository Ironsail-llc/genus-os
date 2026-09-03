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
import signal
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
    *args: str,
    env_extra: dict[str, str] | None = None,
    path: str | None = None,
    script: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": path if path is not None else os.environ["PATH"],
        # The probe SETS its own PATH: it runs as ExecCondition= under units
        # that load an EnvironmentFile= whose PATH begins with the operator's
        # user-writable directories and has no /usr/sbin (see
        # infra/systemd/README.md). So a stub directory can no longer be handed
        # over by prepending it to PATH — it goes through the documented seam,
        # which is the FIRST entry of whatever `path` a test supplies.
        **(
            {"ROBOTHOR_EXTRA_PATH": path.split(":")[0]}
            if path is not None
            else {}
        ),
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
        [BASH, str(script or SCRIPT), *args],
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

    # The probe no longer inherits PATH, so a tool cannot be hidden from it by
    # handing over a stripped one — /usr/bin is always on the PATH it sets for
    # itself. What CAN be exercised is the preflight at its own call site: the
    # tool name it looks for is swapped, in a copy, for one that cannot
    # resolve. The names it actually looks for are pinned separately, below.
    @staticmethod
    def _guard_missing(tmp_path: Path, tool: str) -> Path:
        source = SCRIPT.read_text()
        marker = "for tool in timeout findmnt mktemp; do"
        assert marker in source, "the probe has no tool preflight to exercise"
        absent = f"robothor-absent-{tool}"
        copy = tmp_path / f"probe-without-{tool}" / SCRIPT.name
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_text(source.replace(marker, marker.replace(tool, absent), 1))
        return copy

    def test_missing_timeout_is_255_not_a_silent_skip(self, tmp_path: Path) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        copy = self._guard_missing(tmp_path, "timeout")

        result = _run("--ro", str(vol), script=copy)
        assert result.returncode == BROKEN_PROBE, (
            "without `timeout` the probe cannot bound a hung readdir; that is a "
            "broken guard and must page, not quietly skip every backup\n"
            + _output(result)
        )
        assert "robothor-absent-timeout" in result.stderr, _output(result)

    def test_missing_findmnt_is_255(self, tmp_path: Path) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        copy = self._guard_missing(tmp_path, "findmnt")

        result = _run("--ro", str(vol), script=copy)
        assert result.returncode == BROKEN_PROBE, _output(result)

    def test_missing_mktemp_is_255(self, tmp_path: Path) -> None:
        """The --rw probe proves a write LANDS, and it opens that write with
        mktemp. A mktemp that is not found fails exactly like a full disk —
        one of which is an answer about the volume and the other is not."""
        vol = tmp_path / "vol"
        vol.mkdir()
        copy = self._guard_missing(tmp_path, "mktemp")

        result = _run("--rw", str(vol), script=copy)
        assert result.returncode == BROKEN_PROBE, _output(result)

    def test_the_preflight_names_the_tools_the_probe_actually_uses(self) -> None:
        """The list is the point: a probe that checks two of the three tools
        leaves the third to fail as an empty answer."""
        line = [
            stripped
            for stripped in (raw.strip() for raw in SCRIPT.read_text().splitlines())
            if stripped.startswith("for tool in ")
        ]
        assert line, "the probe has no tool preflight at all"
        checked = set(line[0].removeprefix("for tool in ").rstrip("; do").split())
        assert {"timeout", "findmnt", "mktemp"} <= checked, checked


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


class TestTheSeparateMountGuardIsOnlyDisabledByExactlyZero:
    """The escape hatch has to be hard to trip by accident.

    ``ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT`` was armed only when it was
    exactly ``"1"``, so every other value disarmed it — including ``true``,
    ``yes``, ``on`` and a stray trailing space. Those all read as "yes, require
    a separate mount" to whoever typed them, and every one of them silently
    turned the guard off, which is how a backup ends up on the root disk
    looking like success.

    Disarming is the dangerous direction, so only the one unambiguous value
    disarms it.
    """

    @pytest.mark.parametrize("value", ["true", "yes", "on", "1 ", "", "00", "-0"])
    def test_anything_but_zero_keeps_the_guard_armed(
        self, tmp_path: Path, value: str
    ) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        result = _run(
            "--rw",
            str(vol),
            env_extra={"ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": value},
        )
        assert result.returncode == SKIP, (
            f"ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT={value!r} disarmed the "
            "guard; a backup on the root filesystem then looks like success\n"
            + _output(result)
        )
        assert "root filesystem" in _output(result), _output(result)

    def test_exactly_zero_disarms_it(self, tmp_path: Path) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        result = _run(
            "--rw",
            str(vol),
            env_extra={"ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0"},
        )
        assert result.returncode == 0, _output(result)


class TestAMalformedTimeoutMustNotSkipEveryBackup:
    """Exit 2 from an ExecCondition= is a SKIP, not aconfiguration error the operator sees.

    A typo in ``ROBOTHOR_VOLUME_PROBE_TIMEOUT`` (``20s``, ``5m``, an empty
    override) exited 2 — which systemd reads as "the condition does not hold"
    and quietly skips the unit. One malformed environment line in
    /etc/robothor/robothor.env would therefore stop all four backup units,
    forever, without a single failure or page.

    The value is a bound on how long each step may hang; there is a perfectly
    good default. Say the value is bad, use the default, and let the backup
    run.
    """

    @pytest.mark.parametrize("value", ["20s", "5m", "abc", "-1", "0", "2.5"])
    def test_a_bad_value_falls_back_to_the_default(
        self, tmp_path: Path, value: str
    ) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        result = _run(
            "--rw", str(vol), env_extra={"ROBOTHOR_VOLUME_PROBE_TIMEOUT": value}
        )
        assert result.returncode == 0, (
            "a typo in one environment variable skipped the backup\n"
            + _output(result)
        )
        assert value in _output(result), (
            "the bad value must be named in the journal or nobody will ever "
            "find the typo\n" + _output(result)
        )
        assert "20" in _output(result), (
            "say which default was used instead\n" + _output(result)
        )

    def test_a_good_value_is_still_honoured(self, tmp_path: Path) -> None:
        """The fallback must not swallow a legitimate override — the hang
        tests above depend on it."""
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
        assert result.returncode == SKIP, _output(result)
        assert time.monotonic() - started < 20


class TestAStackedMountCannotCollapseTheGuard:
    """``findmnt --target`` can print more than one row.

    The output was whitespace-stripped whole, so two rows of ``/`` became the
    single token ``//`` — which is not ``/``, so the "is this the root
    filesystem?" comparison stopped matching and an unmounted backup directory
    passed the guard. The same stripping concatenated two option rows into one
    unparseable string.

    Only the first row describes the mount containing the target.
    """

    @staticmethod
    def _findmnt(bin_dir: Path, targets: str, options: str) -> None:
        _stub(
            bin_dir / "findmnt",
            'case "$*" in\n'
            f"  *TARGET*) printf '{targets}' ;;\n"
            f"  *OPTIONS*) printf '{options}' ;;\n"
            "esac",
        )

    def test_two_root_rows_do_not_become_a_non_root_path(
        self, tmp_path: Path
    ) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        bin_dir = tmp_path / "bin"
        self._findmnt(bin_dir, r"/\n/\n", r"rw,relatime\n rw,relatime\n")

        result = _run(
            "--rw",
            str(vol),
            env_extra={"ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "1"},
            path=f"{bin_dir}:{os.environ['PATH']}",
        )
        assert result.returncode == SKIP, (
            "two rows of '/' collapsed to '//' and walked straight past the "
            "root-filesystem guard\n" + _output(result)
        )
        assert "root filesystem" in _output(result), _output(result)

    def test_a_second_options_row_does_not_decide_the_answer(
        self, tmp_path: Path
    ) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        bin_dir = tmp_path / "bin"
        # Row 1 is the mount that actually contains the target and it is fine.
        # Row 2 belongs to something else and must not be read at all.
        self._findmnt(
            bin_dir,
            r"/mnt/vol\n/mnt/other\n",
            r"rw,relatime\nro,relatime,emergency_ro\n",
        )

        result = _run("--rw", str(vol), path=f"{bin_dir}:{os.environ['PATH']}")
        assert result.returncode == 0, (
            "a second findmnt row was glued onto the first, so an unrelated "
            "mount's options answered the question\n" + _output(result)
        )


class TestTheWriteProbeSurvivesASignal:
    """``.robothor-volume-probe.XXXXXX`` must never outlive the probe.

    The cleanup was a plain statement after the write, so a SIGTERM (systemd
    hitting TimeoutStartSec, a `systemctl stop` during a nightly run) between
    the mktemp and the rm left the file on the backup volume — once per run,
    forever. A probe that litters the volume it is protecting is a probe that
    gets disabled.
    """

    def test_a_signal_mid_write_leaves_nothing_on_the_volume(
        self, tmp_path: Path
    ) -> None:
        real_timeout = shutil.which("timeout")
        assert real_timeout, "coreutils timeout is required for this test"

        vol = tmp_path / "vol"
        vol.mkdir()
        bin_dir = tmp_path / "bin"
        # Hang the WRITE step only; every other step gets the real timeout.
        _stub(
            bin_dir / "timeout",
            'for a in "$@"; do\n'
            '  case "$a" in *"printf ok"*) sleep 60; exit 1 ;; esac\n'
            "done\n"
            f'exec {real_timeout} "$@"',
        )

        proc = subprocess.Popen(
            [BASH, str(SCRIPT), "--rw", str(vol)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                # The probe sets its own PATH, so the stub `timeout` that hangs
                # the write step reaches it through the documented seam.
                "ROBOTHOR_EXTRA_PATH": str(bin_dir),
                "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0",
            },
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if list(vol.glob(".robothor-volume-probe.*")):
                    break
                time.sleep(0.05)
            else:  # pragma: no cover - the fixture never reached the write
                raise AssertionError("the write probe file was never created")

            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:  # pragma: no cover
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.wait(timeout=10)

        assert list(vol.iterdir()) == [], (
            "a signal during the write left "
            f"{[p.name for p in vol.iterdir()]} on the backup volume"
        )
