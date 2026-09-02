"""The PRIMARY backup must page when it fails. It was the one job that couldn't.

`backup-ssd.sh` writes the nightly Postgres dumps to the encrypted USB volume at
/mnt/robothor-backup. It is careful: it checks `mountpoint -q` and exits 1 if the
disk is not there.

But it ran from **cron**, not systemd:

    30 4 * * * /opt/robothor/scripts/backup-ssd.sh >> .../backup.log 2&1

So the exit 1 went nowhere. Cron mails root; nothing reads root's mail. The
offsite-replication and verify jobs are systemd units *and* carry
`OnFailure=robothor-alert@%n.service`, so they page. The job that actually makes
the backups did not.

This is not hypothetical. On 2026-07-14 the USB volume **physically dropped off
the bus** mid-write ("Underlying device for crypt device robothor-backup
disappeared", EXT4 I/O errors on the superblock) and stayed unmounted. It happened
at 10:25, after that morning's 04:30 run — pure luck. Had it dropped six hours
earlier, the backup would have failed, exited 1, written a line to a log file no
one reads, and the operator would have believed they had a backup.

`fstab` carries `nofail`, so a missing backup disk does not even stop the boot.
Nothing about a missing backup disk is loud. This makes the primary backup a
systemd timer with the same OnFailure paging as its two siblings.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO_ROOT / "infra" / "systemd"


def _pager_pins(tmp_path: Path) -> dict[str, str]:
    """The five seams every subprocess in this file must pin.

    Every script under test here (wal-offsite.sh, pg-basebackup.sh,
    backup-ssd.sh) can reach send_failure_alert.sh on failure. Without these,
    a fixture failure spools a real page that root's next liveness drain
    delivers to the operator's phone — see tests/test_alert_never_pages_from_tests.py.
    """
    return {
        "ROBOTHOR_ALERT_SPOOL_DIR": str(tmp_path / "alert-spool"),
        "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-state"),
        "ROBOTHOR_ALERT_FALLBACK_STATE_DIR": str(tmp_path / "alert-fallback"),
        "ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env"),
        "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
    }


def _code_lines(body: str) -> list[str]:
    """Shell source minus comments.

    Grepping a script for a call it also DOCUMENTS proves nothing: the
    assertion below was satisfied by backup-ssd.sh's comment about
    backup-volume-check.sh, so deleting the actual call would have left it
    green.
    """
    return [line for line in body.splitlines() if not line.lstrip().startswith("#")]

SERVICE = UNIT_DIR / "robothor-backup-local.service"
TIMER = UNIT_DIR / "robothor-backup-local.timer"


class TestThePrimaryBackupIsASystemdJob:
    def test_service_unit_exists(self) -> None:
        assert SERVICE.exists(), (
            "the nightly backup ran from cron, so its exit 1 reached nobody — "
            "it must be a systemd unit like the offsite and verify jobs"
        )

    def test_timer_exists_and_runs_before_the_offsite_replication(self) -> None:
        assert TIMER.exists()
        body = TIMER.read_text()
        assert "OnCalendar" in body
        assert "Persistent=true" in body, (
            "a missed run (box asleep/off) must still fire, or a backup is skipped silently"
        )
        # offsite replication runs at 05:30 and copies what this produces.
        assert "04:" in body, "must run before the 05:30 offsite replication"

    def test_service_invokes_the_real_backup_script(self) -> None:
        assert "backup-ssd.sh" in SERVICE.read_text()


class TestItPagesWhenItFails:
    def test_service_has_onfailure_wired_to_the_pager(self) -> None:
        body = SERVICE.read_text()
        assert "OnFailure=robothor-alert@" in body, (
            "a failed backup must page the operator. The disk physically dropped off "
            "the bus on 2026-07-14; if that had happened before 04:30 the backup would "
            "have failed into a log file nobody reads and the operator would have "
            "believed they had a backup."
        )

    def test_it_is_oneshot_so_a_nonzero_exit_is_a_failure(self) -> None:
        """Type=oneshot: exit 1 => unit failed => OnFailure fires. Any other Type
        can exit non-zero without the unit being considered failed."""
        assert "Type=oneshot" in SERVICE.read_text()


class TestTheScriptStillGuardsTheMount:
    def test_backup_script_fails_when_the_disk_is_absent(self) -> None:
        """The guard is what turns a missing disk into a failure the pager can see.

        It used to be `mountpoint -q`, which is a stat() check — and stat()
        keeps succeeding on the `emergency_ro` volume the USB drive leaves
        behind when it drops off the bus. The guard now runs
        scripts/backup-volume-check.sh, which does a real readdir and a real
        write.
        """
        code = _code_lines((REPO_ROOT / "scripts" / "backup-ssd.sh").read_text())
        assert any("backup-volume-check.sh" in line for line in code), (
            "`mountpoint -q` passes on a wedged emergency_ro volume — the "
            "backup then runs, writes nothing, and fails. (Comment lines do "
            "not count: this assertion was satisfied by the comment that "
            "MENTIONS the probe, so deleting the call would not have failed it)"
        )
        assert any("exit 1" in line for line in code)

    def test_basebackup_guards_the_volume_the_same_way(self) -> None:
        code = _code_lines((REPO_ROOT / "scripts" / "pg-basebackup.sh").read_text())
        assert any("backup-volume-check.sh" in line for line in code), (
            "the only mention of the probe was in a comment — the call could "
            "be deleted without failing this test"
        )
        assert not [line for line in code if "mountpoint -q" in line], (
            "a stat()-only guard next to the real one is a guard that will be "
            "trusted when the real one is removed"
        )


class TestWalOffsiteSurvivesAnOffsiteFailure:
    """A failing rclone step must never hold the WAL prune and disk guard
    hostage.

    Today's incident (2026-08-17): the offsite rclone step failed for hours
    while a 148GB WAL backlog sat unpruned behind it — because
    `rclone ... || fail` exits the whole script before §3 (prune) and §4
    (disk guard) ever run. The pager fired every 15 minutes for the rclone
    failure while the actual disk-filling problem went uninstrumented.
    """

    SCRIPT = REPO_ROOT / "scripts" / "wal-offsite.sh"

    @staticmethod
    def _stub(path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def test_prune_runs_and_exit_is_nonzero_even_when_rclone_fails(
        self, tmp_path: Path
    ) -> None:
        archive_dir = tmp_path / "wal_archive"
        archive_dir.mkdir()
        basebackup_dir = tmp_path / "basebackup"
        basebackup_dir.mkdir()

        # A minimal, real-shaped backup_label: the script's awk pulls the WAL
        # start position out of the "(file ...)" parenthetical on this line.
        backup_label = basebackup_dir / "base-20260817-000000.backup_label"
        backup_label.write_text(
            "START WAL LOCATION: 0/2000028 (file 000000010000000000000002)\n"
            "CHECKPOINT LOCATION: 0/2000060\n"
        )

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()

        # psql: report a healthy archiver (no failures) so §1 never trips.
        self._stub(bin_dir / "psql", 'echo "5|0|-"')
        # rclone: always fails — this is the fault under test.
        self._stub(bin_dir / "rclone", "exit 1")
        # pg_archivecleanup: record that it ran, so we can prove §3 executed.
        prune_log = tmp_path / "pg_archivecleanup-args.txt"
        self._stub(
            bin_dir / "pg_archivecleanup",
            f'printf \'%s\\n\' "$@" >> "{prune_log}"',
        )

        env = {
            **_pager_pins(tmp_path),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ROBOTHOR_WAL_ARCHIVE_DIR": str(archive_dir),
            "ROBOTHOR_BASEBACKUP_DIR": str(basebackup_dir),
            "ROBOTHOR_OFFSITE_REMOTE": "remote:bucket",
            "ROBOTHOR_DB_NAME": "robothor_memory",
            # tmp_path is on the root filesystem; see
            # tests/test_backup_volume_check.py for what guards this step.
            "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0",
        }
        result = subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert prune_log.exists(), (
            "pg_archivecleanup never ran — a failing rclone step held the WAL "
            "prune hostage, exactly like the 2026-08-17 incident\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert result.returncode == 1, (
            "the script must still exit non-zero so systemd's OnFailure hook "
            f"pages the operator about the offsite failure\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


class TestWalOffsiteDegradesWhenTheBackupVolumeIsWedged:
    """A wedged USB volume must not stop the 15-minute WAL push — or page.

    2026-08-27: the encrypted backup volume went `emergency_ro`. Its four
    sibling backup units now SKIP in that state (ExecCondition=), but this one
    must not: the WAL archive lives on NVMe and this push IS the RPO. Skipping
    it would trade a paging storm for real data loss.

    So wal-offsite.sh runs the same probe itself and degrades. The two things
    that touch the backup volume — replicating the base backups, and reading
    the newest backup_label to decide the WAL prune horizon — are skipped; the
    WAL still goes offsite; the script exits 0, so `robothor-wal-offsite`
    stops firing OnFailure 96 times a day about a disk it does not need.

    The prune being skipped is deliberate and safe in that direction: pruning
    WAL below a base backup you cannot read is how you get an archive that
    restores to nothing. §4's disk guard still pages if the unpruned archive
    actually threatens to fill the disk.
    """

    SCRIPT = REPO_ROOT / "scripts" / "wal-offsite.sh"

    @staticmethod
    def _stub(path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _run(self, tmp_path: Path):
        archive_dir = tmp_path / "wal_archive"
        archive_dir.mkdir()
        (archive_dir / "000000010000000000000003").write_text("wal")

        basebackup_dir = tmp_path / "basebackup"
        basebackup_dir.mkdir()
        (basebackup_dir / "base-20260827-000000.backup_label").write_text(
            "START WAL LOCATION: 0/2000028 (file 000000010000000000000002)\n"
        )
        # The fault under test: the volume is mounted and stats fine, but
        # readdir() fails. chmod 000 reproduces exactly that from userspace.
        basebackup_dir.chmod(0o000)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self._stub(bin_dir / "psql", 'echo "5|0|-"')
        rclone_log = tmp_path / "rclone-args.txt"
        self._stub(bin_dir / "rclone", f'printf \'%s\\n\' "$@" >> "{rclone_log}"')
        prune_log = tmp_path / "pg_archivecleanup-args.txt"
        self._stub(
            bin_dir / "pg_archivecleanup",
            f'printf \'%s\\n\' "$@" >> "{prune_log}"',
        )
        # Keep §4's disk guard hermetic: this test is about the volume probe,
        # not about how full the machine running the suite happens to be.
        self._stub(bin_dir / "df", 'echo "1M-blocks"; echo "9999999"')

        env = {
            **_pager_pins(tmp_path),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ROBOTHOR_WAL_ARCHIVE_DIR": str(archive_dir),
            "ROBOTHOR_BASEBACKUP_DIR": str(basebackup_dir),
            "ROBOTHOR_OFFSITE_REMOTE": "remote:bucket",
            "ROBOTHOR_DB_NAME": "robothor_memory",
            "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "state"),
            # tmp_path is on the root filesystem; see
            # tests/test_backup_volume_check.py for what guards this step.
            "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0",
        }
        try:
            result = subprocess.run(
                ["bash", str(self.SCRIPT)],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        finally:
            basebackup_dir.chmod(0o755)
        return result, rclone_log, prune_log, archive_dir

    def test_it_exits_zero_so_the_unit_stops_paging_every_15_minutes(
        self, tmp_path: Path
    ) -> None:
        result, _, _, _ = self._run(tmp_path)
        assert result.returncode == 0, (
            "a wedged backup volume made this unit fail every 15 minutes — 96 "
            "OnFailure triggers a day whose entire content was a unit name\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_it_says_what_it_skipped(self, tmp_path: Path) -> None:
        result, _, _, _ = self._run(tmp_path)
        assert "skipping basebackup replication" in result.stdout, (
            "exiting 0 without saying why is how a degraded run becomes an "
            f"invisible one\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_the_wal_prune_is_skipped(self, tmp_path: Path) -> None:
        result, _, prune_log, _ = self._run(tmp_path)
        assert not prune_log.exists(), (
            "the prune horizon is read from the newest base backup on the "
            "unreadable volume; pruning WAL against a horizon you cannot "
            "verify is how an archive ends up restoring to nothing\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_the_wal_still_goes_offsite(self, tmp_path: Path) -> None:
        """The whole point of degrading rather than skipping."""
        result, rclone_log, _, archive_dir = self._run(tmp_path)
        assert rclone_log.exists(), (
            "no rclone call at all — the 15-minute RPO was dropped\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        args = rclone_log.read_text()
        assert str(archive_dir) in args, f"WAL was not pushed offsite\n{args}"
        assert "remote:bucket/wal" in args

    def test_the_backup_volume_is_not_touched_by_rclone(self, tmp_path: Path) -> None:
        result, rclone_log, _, _ = self._run(tmp_path)
        assert "remote:bucket/basebackup" not in rclone_log.read_text(), (
            "rclone was pointed at the unreadable volume anyway\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_a_degraded_run_still_records_that_the_wal_went_offsite(
        self, tmp_path: Path
    ) -> None:
        """Exiting 0 is only safe if something else carries the freshness
        signal. The marker lives on NVMe, not on the volume that broke."""
        self._run(tmp_path)
        marker = tmp_path / "state" / "last-wal-offsite-ok"
        assert marker.exists(), (
            "the unit stopped failing and recorded nothing instead — the "
            "paging storm would be replaced by silence"
        )
        assert "000000010000000000000003" in marker.read_text(), (
            "the marker must name the newest WAL segment that went offsite, "
            "or a freshness page cannot say how far behind the archive is\n"
            + marker.read_text()
        )


class TestTheBaseBackupRecordsWhenItLastWorked:
    """pg-basebackup.sh drives real binaries, so it is stubbed rather than
    grepped: a marker that is written by code nobody ever executes is the
    inert control this whole change exists to avoid."""

    SCRIPT = REPO_ROOT / "scripts" / "pg-basebackup.sh"

    @staticmethod
    def _stub(path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _run(self, tmp_path: Path, dest: Path):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # pg_basebackup writes a directory at --pgdata=...; that is all the
        # rest of the script needs from it.
        self._stub(
            bin_dir / "pg_basebackup",
            'for a in "$@"; do case "$a" in --pgdata=*) mkdir -p "${a#--pgdata=}";; esac; done',
        )
        env = {
            **_pager_pins(tmp_path),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ROBOTHOR_BASEBACKUP_DIR": str(dest),
            "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "state"),
            # tmp_path is on the root filesystem; see
            # tests/test_backup_volume_check.py for what guards this step.
            "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0",
        }
        return subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

    def test_a_successful_base_backup_records_last_basebackup(
        self, tmp_path: Path
    ) -> None:
        dest = tmp_path / "robothor" / "basebackup"
        dest.mkdir(parents=True)
        result = self._run(tmp_path, dest)

        assert result.returncode == 0, result.stdout + result.stderr
        marker = tmp_path / "state" / "last-basebackup"
        assert marker.exists(), result.stdout + result.stderr
        assert marker.read_text().strip()
        made = sorted(d.name for d in dest.iterdir() if d.is_dir())
        assert made, result.stdout + result.stderr
        assert made[-1] in marker.read_text(), (
            "the marker must name the base backup directory it recorded, so a "
            f"freshness page can point at it\n{marker.read_text()}"
        )

    def test_a_wedged_volume_records_nothing_and_does_not_run(
        self, tmp_path: Path
    ) -> None:
        """The volume probe refuses first, so there is no half-written base
        backup and no marker claiming one exists."""
        dest = tmp_path / "robothor" / "basebackup"
        dest.mkdir(parents=True)
        (tmp_path / "robothor").chmod(0o000)
        try:
            result = self._run(tmp_path, dest)
        finally:
            (tmp_path / "robothor").chmod(0o755)

        assert result.returncode != 0, result.stdout + result.stderr
        assert not (tmp_path / "state" / "last-basebackup").exists(), (
            "a base backup that never ran recorded itself as successful"
        )


class TestTheWalMarkerOnlyMeansTheWalWentOffsite:
    """``last-wal-offsite-ok`` answers one question: did the WAL reach the
    remote?

    It was stamped whenever ``OFFSITE_FAILED`` was 0 — and that variable is
    initialised to 0 and never touched when ``ROBOTHOR_OFFSITE_REMOTE`` is
    unset (the "archiving locally only" path falls straight through). So an
    instance with no offsite destination at all stamped a fresh
    "WAL is offsite" marker every 15 minutes, forever. A freshness guard
    reading it would report the RPO as healthy on a box that has never sent a
    single byte anywhere.

    A DEGRADED run is the opposite case and must still stamp: when the backup
    volume is wedged only the basebackup replication is skipped — the WAL
    itself still goes offsite, which is exactly what this marker is about.
    """

    SCRIPT = REPO_ROOT / "scripts" / "wal-offsite.sh"

    @staticmethod
    def _stub(path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _run(self, tmp_path: Path, *, volume_down: bool = False, **env_extra):
        archive_dir = tmp_path / "wal_archive"
        archive_dir.mkdir()
        (archive_dir / "000000010000000000000003").write_text("wal")

        basebackup_dir = tmp_path / "basebackup"
        basebackup_dir.mkdir()
        (basebackup_dir / "base-20260902-000000.backup_label").write_text(
            "START WAL LOCATION: 0/2000028 (file 000000010000000000000002)\n"
        )
        if volume_down:
            # Mounted, stats fine, readdir fails — the emergency_ro shape.
            basebackup_dir.chmod(0o000)

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self._stub(bin_dir / "psql", 'echo "5|0|-"')
        self._stub(bin_dir / "rclone", "exit 0")
        self._stub(bin_dir / "pg_archivecleanup", "exit 0")
        self._stub(bin_dir / "df", 'echo "1M-blocks"; echo "9999999"')

        env = {
            **_pager_pins(tmp_path),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ROBOTHOR_WAL_ARCHIVE_DIR": str(archive_dir),
            "ROBOTHOR_BASEBACKUP_DIR": str(basebackup_dir),
            "ROBOTHOR_DB_NAME": "robothor_memory",
            "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "state"),
            "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0",
        }
        env.update(env_extra)
        try:
            result = subprocess.run(
                ["bash", str(self.SCRIPT)],
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        finally:
            if volume_down:
                basebackup_dir.chmod(0o755)
        return result

    def test_no_offsite_remote_configured_records_nothing(self, tmp_path: Path):
        result = self._run(tmp_path)  # ROBOTHOR_OFFSITE_REMOTE deliberately unset
        assert result.returncode == 0, result.stdout + result.stderr
        assert "archiving locally only" in result.stdout, result.stdout
        assert not (tmp_path / "state" / "last-wal-offsite-ok").exists(), (
            "an instance with no offsite destination stamped 'the WAL is "
            "offsite' anyway — the freshness guard would call an RPO of "
            "infinity healthy\n" + result.stdout + result.stderr
        )

    def test_a_degraded_run_with_a_working_remote_still_records(
        self, tmp_path: Path
    ):
        result = self._run(
            tmp_path, volume_down=True, ROBOTHOR_OFFSITE_REMOTE="remote:bucket"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "skipping basebackup replication" in result.stdout, result.stdout
        assert (tmp_path / "state" / "last-wal-offsite-ok").exists(), (
            "the WAL push succeeded and the marker was withheld — only the "
            "basebackup replication is skipped when the volume is wedged\n"
            + result.stdout
            + result.stderr
        )


class TestWalOffsiteRefusesToGuessWhenTheProbeIsBroken:
    """Exit 255 from the volume probe means "I cannot answer the question".

    wal-offsite.sh treated every non-zero probe exit the same way: degrade,
    log a line, exit 0. So a probe with no ``timeout`` or no ``findmnt``
    installed — the two cases the probe reserves 255 for — turned into a
    permanently degraded run that never replicated a base backup, never pruned
    WAL, and never failed. The four sibling units page in that state (systemd
    fails a unit on ExecCondition= 255); this one went quiet instead.

    Only exit 1 (the volume is genuinely unhealthy) degrades.
    """

    SCRIPT = REPO_ROOT / "scripts" / "wal-offsite.sh"

    @staticmethod
    def _stub(path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _run(
        self,
        tmp_path: Path,
        probe_exit: int,
        env_extra: dict[str, str | None] | None = None,
    ):
        archive_dir = tmp_path / "wal_archive"
        archive_dir.mkdir()
        (archive_dir / "000000010000000000000003").write_text("wal")
        basebackup_dir = tmp_path / "basebackup"
        basebackup_dir.mkdir()

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        self._stub(bin_dir / "psql", 'echo "5|0|-"')
        self._stub(bin_dir / "rclone", "exit 0")
        self._stub(bin_dir / "pg_archivecleanup", "exit 0")
        self._stub(bin_dir / "df", 'echo "1M-blocks"; echo "9999999"')
        probe = tmp_path / "fake-volume-check.sh"
        self._stub(probe, f"exit {probe_exit}")

        return subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **_pager_pins(tmp_path),
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "ROBOTHOR_WAL_ARCHIVE_DIR": str(archive_dir),
                "ROBOTHOR_BASEBACKUP_DIR": str(basebackup_dir),
                "ROBOTHOR_OFFSITE_REMOTE": "remote:bucket",
                "ROBOTHOR_DB_NAME": "robothor_memory",
                "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "state"),
                "ROBOTHOR_VOLUME_CHECK": str(probe),
                "ROBOTHOR_VOLUME_REQUIRE_SEPARATE_MOUNT": "0",
            },
        )

    def test_a_broken_probe_fails_the_unit(self, tmp_path: Path):
        result = self._run(tmp_path, 255)
        assert result.returncode != 0, (
            "a probe that cannot answer the question was read as 'the volume "
            "is down', so the unit degraded quietly and forever\n"
            + result.stdout
            + result.stderr
        )
        assert "refusing to guess" in result.stdout + result.stderr, (
            "the page must say the PROBE is broken, not the volume\n"
            + result.stdout
            + result.stderr
        )

    def test_a_broken_probe_records_no_marker(self, tmp_path: Path):
        self._run(tmp_path, 255)
        assert not (tmp_path / "state" / "last-wal-offsite-ok").exists()

    def test_an_unhealthy_volume_still_only_degrades(self, tmp_path: Path):
        result = self._run(tmp_path, 1)
        assert result.returncode == 0, (
            "exit 1 means the volume is wedged, which this unit must survive: "
            "the WAL archive is on NVMe and this push IS the 15-minute RPO\n"
            + result.stdout
            + result.stderr
        )
        assert "skipping basebackup replication" in result.stdout, result.stdout


class TestTheVolumeProbeActuallyGatesTheLocalBackup:
    """Grepping backup-ssd.sh for the probe's name proves only that the name
    appears in it.

    So this drives the script. A fake probe on the ``ROBOTHOR_VOLUME_CHECK``
    seam answers 1 (the volume is wedged) or 0 (it is fine), and the assertions
    are about what lands on the destination — nothing at all in the first case,
    a real dump in the second.

    Everything the backup shells out to is stubbed, and the destination is a
    tmp_path. That last part is load-bearing: this box has the real encrypted
    volume mounted at /mnt/robothor-backup, writable by the user running the
    suite, and backup-ssd.sh would happily rsync into it.
    """

    SCRIPT = REPO_ROOT / "scripts" / "backup-ssd.sh"

    @staticmethod
    def _stub(path: Path, body: str) -> None:
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def _run(
        self,
        tmp_path: Path,
        probe_exit: int,
        env_extra: dict[str, str | None] | None = None,
    ):
        # A test that runs the real backup against the real mount would write
        # into the live encrypted volume. If the seam is ever removed, fail
        # here rather than finding out afterwards.
        code = "\n".join(_code_lines(self.SCRIPT.read_text()))
        assert "ROBOTHOR_BACKUP_MOUNT" in code, (
            "backup-ssd.sh has no destination seam, so this test cannot run "
            "without pointing the real backup at the real /mnt/robothor-backup"
        )

        mount = tmp_path / "mnt"
        mount.mkdir()
        home = tmp_path / "home"
        home.mkdir()
        (home / ".bashrc").write_text("# fixture\n")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        # rsync creates its destination, which the manifest's `du` then reads.
        self._stub(bin_dir / "rsync", 'mkdir -p "${@: -1}"')
        # The script sudo's for /etc and the docker socket; neither is this
        # test's subject, and neither may touch the box running the suite.
        self._stub(bin_dir / "sudo", "exit 0")
        self._stub(bin_dir / "docker", "exit 1")
        self._stub(bin_dir / "pg_dump", 'echo "-- fixture dump"')
        self._stub(bin_dir / "crontab", 'echo "# fixture crontab"')
        self._stub(bin_dir / "ollama", 'echo "fixture-model"')
        # Keep the free-space guard hermetic: 95GB, in df's KB.
        self._stub(bin_dir / "df", 'echo "avail"; echo "99999999"')

        probe_log = tmp_path / "probe-args.txt"
        probe = tmp_path / "fake-volume-check.sh"
        self._stub(
            probe, f'printf \'%s\\n\' "$@" >> "{probe_log}"\nexit {probe_exit}'
        )

        env = {
            **_pager_pins(tmp_path),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HOME": str(home),
            "ROBOTHOR_BACKUP_MOUNT": str(mount),
            "ROBOTHOR_BACKUP_LOG": str(tmp_path / "backup.log"),
            "ROBOTHOR_VOLUME_CHECK": str(probe),
            "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "state"),
        }
        for key, value in (env_extra or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value

        result = subprocess.run(
            ["bash", str(self.SCRIPT)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
        return result, mount, probe_log

    def test_a_wedged_volume_stops_the_backup_before_it_writes_anything(
        self, tmp_path: Path
    ) -> None:
        result, mount, _ = self._run(tmp_path, probe_exit=1)

        assert result.returncode != 0, (
            "the probe said the volume was unusable and the backup ran anyway\n"
            + result.stdout
            + result.stderr
        )
        assert list(mount.iterdir()) == [], (
            "the backup wrote to a volume its own probe had just rejected: "
            f"{[p.name for p in mount.iterdir()]}"
        )
        assert not (tmp_path / "state" / "last-local-dump").exists(), (
            "a backup that never ran recorded itself as successful"
        )

    def test_the_probe_is_asked_for_write_access_to_the_mount(
        self, tmp_path: Path
    ) -> None:
        _, mount, probe_log = self._run(tmp_path, probe_exit=1)
        args = probe_log.read_text().split()
        assert "--rw" in args, (
            "a read-only probe passes on an emergency_ro volume — this job "
            f"WRITES\n{args}"
        )
        assert str(mount) in args, args

    def test_a_healthy_volume_lets_the_backup_through(self, tmp_path: Path) -> None:
        result, mount, _ = self._run(tmp_path, probe_exit=0)

        assert result.returncode == 0, result.stdout + result.stderr
        dumps = sorted((mount / "robothor" / "db").glob("*.sql.gz"))
        assert dumps, (
            "the probe passed and no dump was produced — the guard is not a "
            f"guard, it is a wall\n{result.stdout}{result.stderr}"
        )
        marker = tmp_path / "state" / "last-local-dump"
        assert marker.exists(), result.stdout + result.stderr
        assert dumps[-1].name in marker.read_text(), marker.read_text()

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root ignores the directory mode this test sets"
    )
    def test_an_unwritable_log_directory_does_not_abort_the_backup(
        self, tmp_path: Path
    ) -> None:
        """The log default moved to /var/log/robothor/backup.log, and it is
        used by a bare `>>` under `set -euo pipefail`. On an instance where
        that directory is absent or unwritable the FIRST log line kills the
        script — before the volume probe, before the free-space guard, before
        anything. A log destination must never be able to cancel the backup.

        scripts/backup-offsite.sh already solved this: create the directory,
        prove the file is writable, otherwise fall back to the old in-tree path
        and say so on stderr.
        """
        # Without the seam this test would drive the real backup at the real
        # /var/log/robothor. Fail here rather than writing to it.
        code = "\n".join(_code_lines(self.SCRIPT.read_text()))
        assert "ROBOTHOR_LOG_DIR" in code, (
            "backup-ssd.sh has no log-directory seam, so this test cannot run "
            "without pointing it at the real /var/log/robothor"
        )

        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            result, mount, _ = self._run(
                tmp_path,
                probe_exit=0,
                env_extra={
                    "ROBOTHOR_BACKUP_LOG": None,
                    "ROBOTHOR_LOG_DIR": str(locked / "robothor"),
                },
            )
        finally:
            locked.chmod(0o700)

        assert result.returncode == 0, (
            "an unwritable log directory aborted the whole backup\n"
            + result.stdout
            + result.stderr
        )
        assert sorted((mount / "robothor" / "db").glob("*.sql.gz")), (
            "the backup did not run\n" + result.stdout + result.stderr
        )
        fallback = tmp_path / "home" / "robothor" / "scripts" / "backup.log"
        assert fallback.exists(), (
            "no fallback log was written, so the run left no record at all\n"
            + result.stdout
            + result.stderr
        )
        assert fallback.read_text().strip(), "the fallback log is empty"
        assert str(fallback) in result.stderr, (
            "a silently relocated log is a log nobody finds\n" + result.stderr
        )
