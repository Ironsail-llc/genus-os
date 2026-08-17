"""The PRIMARY backup must page when it fails. It was the one job that couldn't.

`backup-ssd.sh` writes the nightly Postgres dumps to the encrypted USB volume at
/mnt/robothor-backup. It is careful: it checks `mountpoint -q` and exits 1 if the
disk is not there.

But it ran from **cron**, not systemd:

    30 4 * * * /home/philip/robothor/scripts/backup-ssd.sh >> .../backup.log 2>&1

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
        """The guard is what turns a missing disk into a failure the pager can see."""
        body = (REPO_ROOT / "scripts" / "backup-ssd.sh").read_text()
        assert "mountpoint -q" in body
        assert "exit 1" in body


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
