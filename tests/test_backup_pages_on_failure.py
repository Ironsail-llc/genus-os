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

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT_DIR = REPO_ROOT / "infra" / "systemd"

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
        body = (REPO_ROOT / "scripts" / "backup-ssd.sh").read_text()
        assert "backup-volume-check.sh" in body, (
            "`mountpoint -q` passes on a wedged emergency_ro volume — the "
            "backup then runs, writes nothing, and fails"
        )
        assert "exit 1" in body

    def test_basebackup_guards_the_volume_the_same_way(self) -> None:
        body = (REPO_ROOT / "scripts" / "pg-basebackup.sh").read_text()
        assert "backup-volume-check.sh" in body
        code = [
            line
            for line in body.splitlines()
            if not line.lstrip().startswith("#")
        ]
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
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ROBOTHOR_WAL_ARCHIVE_DIR": str(archive_dir),
            "ROBOTHOR_BASEBACKUP_DIR": str(basebackup_dir),
            "ROBOTHOR_OFFSITE_REMOTE": "remote:bucket",
            "ROBOTHOR_DB_NAME": "robothor_memory",
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
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "ROBOTHOR_WAL_ARCHIVE_DIR": str(archive_dir),
            "ROBOTHOR_BASEBACKUP_DIR": str(basebackup_dir),
            "ROBOTHOR_OFFSITE_REMOTE": "remote:bucket",
            "ROBOTHOR_DB_NAME": "robothor_memory",
            "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "state"),
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
