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
