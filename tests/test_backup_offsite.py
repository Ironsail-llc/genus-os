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
import os
import re
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
        # This script pages the operator on failure, and several cases here
        # FAIL on purpose. Without this the suite delivers fixture failures to
        # a real phone -- it did, on 2026-08-27, including a fake "CORRUPT
        # offsite" that reads like a data-integrity emergency.
        "ROBOTHOR_ALERT_SUPPRESS": "1",
        "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",  # never resolves
        # The pager writes DURABLE state of its own, and suppression is not a
        # substitute for pinning it: ROBOTHOR_ALERT_SUPPRESS covers the call
        # sites that set it, and every case below that reaches the sender
        # WITHOUT it (see test_missing_remote_config_fails_loudly) would
        # otherwise spool a fixture page into /var/lib/robothor/alert-spool,
        # which root's liveness tick delivers for real five minutes later, and
        # stamp a cooldown that suppresses a genuine page for an hour.
        "ROBOTHOR_ALERT_SPOOL_DIR": str(tmp_path / "alert-spool"),
        "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
        "ROBOTHOR_ALERT_FALLBACK_STATE_DIR": str(tmp_path / "alert-cooldown-fallback"),
        # /run/robothor/secrets.env is real and readable on a live box: with
        # no override the sender recovers the operator's actual Telegram
        # credentials and pages for real, whatever the API base says.
        "ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env"),
        # The last-good marker lands on NVMe, not on the backup volume, so a
        # wedged volume cannot erase the evidence of when it last worked.
        # Redirect it here or the suite writes into /var/lib/robothor.
        "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "backup-state"),
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
            # This case reaches fail(), which runs the REAL pager. With no
            # secrets override the sender recovers the operator's actual
            # credentials from /run/robothor/secrets.env and delivers
            # "offsite-backup: ROBOTHOR_OFFSITE_REMOTE is not set" to their
            # phone; with no spool pin an undelivered one is parked in
            # /var/lib/robothor/alert-spool for root's next drain to send.
            "ROBOTHOR_ALERT_SUPPRESS": "1",
            "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
            "ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env"),
            "ROBOTHOR_ALERT_SPOOL_DIR": str(tmp_path / "alert-spool"),
            "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
            "ROBOTHOR_ALERT_FALLBACK_STATE_DIR": str(tmp_path / "alert-cooldown-fallback"),
        },
    )
    assert result.returncode != 0
    assert "ROBOTHOR_OFFSITE_REMOTE" in result.stdout + result.stderr


def test_missing_source_fails_loudly(tmp_path: Path):
    result = _run(tmp_path, tmp_path / "nonexistent", tmp_path / "remote")
    assert result.returncode != 0
    assert "source" in (result.stdout + result.stderr).lower()


def test_uploads_only_the_generations_it_intends_to_keep(tmp_path: Path):
    """Do not ship dumps that retention deletes minutes later.

    Copying the whole source and pruning afterwards uploads (and pays for)
    generations that are immediately discarded — at ~1.1 GB and ~4.5 MB/s per
    dump that is roughly 45 wasted minutes a night on a 17-dump source.
    """
    src = _make_source(tmp_path, days=5)  # 5 dumps on disk
    dest = tmp_path / "remote"

    result = _run(tmp_path, src, dest)  # KEEP=2
    assert result.returncode == 0, result.stdout + result.stderr

    uploaded = sorted(f.name for f in (dest / "db").glob("*.sql.gz"))
    assert len(uploaded) == 2, f"uploaded {len(uploaded)} dumps but KEEP=2: {uploaded}"
    # and they are the newest two, not an arbitrary pair
    newest = sorted(f.name for f in src.glob("*.sql.gz"))[-2:]
    assert uploaded == newest


def test_verify_only_checks_only_the_retained_generations(tmp_path: Path):
    """Verification must compare like with like.

    Retention keeps N generations offsite while the local disk keeps many more.
    A one-way check of the WHOLE source against the remote therefore reports
    every un-replicated older dump as a "difference" and fails — every single
    run. That is not a broken backup, it is a broken check, and it would page
    the operator weekly until they learned to ignore it. Which is how a real
    backup failure gets missed.
    """
    src = _make_source(tmp_path, days=5)  # 5 on disk
    dest = tmp_path / "remote"

    assert _run(tmp_path, src, dest).returncode == 0  # KEEP=2 -> 2 offsite

    result = _run(tmp_path, src, dest, ROBOTHOR_OFFSITE_VERIFY_ONLY="1")
    assert result.returncode == 0, (
        "verification failed against a healthy backup — it compared all 5 local "
        f"dumps to the 2 retained offsite: {result.stdout + result.stderr}"
    )


# ── Retention must not eat a live generation ─────────────────────────────────
# 2026-08-23: the offsite copy had one object whose name does not follow the
# generation convention — robothor_memory-prereboot-20260714.sql.gz, uploaded
# by hand before a reboot in July. Its local counterpart was reaped at
# -mtime +30, so it existed ONLY on the remote.
#
# The prune picked victims by sorting every remote *.sql.gz lexicographically
# and deleting the lowest `excess`. 'p' (0x70) sorts after '2' (0x32), so the
# orphan was never a candidate — it just permanently occupied a retention slot
# and forced the deletion of the oldest REAL generation, every single night.
# The weekly verify then demanded the file the nightly run had just deleted.
#
# Two definitions of "the retained set" in one script: verify derives it from
# LOCAL filenames (:52-54), prune derived it from REMOTE ones (:96-103). They
# can never be guaranteed consistent. The prune must select from the same set
# verify checks, and must never treat an unrecognized object as a generation.

FOREIGN = "robothor_memory-prereboot-20260714.sql.gz"


def _seed_remote_orphan(dest: Path, name: str = FOREIGN) -> Path:
    """Put an object on the remote that the local source does not have."""
    db = dest / "db"
    db.mkdir(parents=True, exist_ok=True)
    orphan = db / name
    with gzip.open(orphan, "wb") as fh:
        fh.write(b"hand-uploaded-before-a-reboot" * 100)
    return orphan


def test_prune_never_deletes_a_retained_generation_when_remote_has_a_foreign_file(
    tmp_path: Path,
):
    """The production condition, verbatim.

    KEEP=2, three local dumps, plus one non-conforming object already offsite.
    Every generation the verify will demand must still be there afterwards.
    """
    src = _make_source(tmp_path, days=3)
    dest = tmp_path / "remote"
    _seed_remote_orphan(dest)

    result = _run(tmp_path, src, dest)  # KEEP=2
    assert result.returncode == 0, result.stdout + result.stderr

    retained = sorted(f.name for f in src.glob("*.sql.gz"))[-2:]
    offsite = {f.name for f in (dest / "db").glob("*.sql.gz")}
    missing = [g for g in retained if g not in offsite]
    assert not missing, (
        f"the prune deleted retained generation(s) {missing} — an unrecognized "
        f"remote object consumed a retention slot. Offsite now: {sorted(offsite)}"
    )


def test_a_pruned_remote_still_verifies(tmp_path: Path):
    """The end-to-end shape of the incident: replicate, prune, then verify.

    The nightly run reported success at 05:35 and the weekly verify failed at
    06:30 on the file the nightly run had itself just deleted.
    """
    src = _make_source(tmp_path, days=3)
    dest = tmp_path / "remote"
    _seed_remote_orphan(dest)

    assert _run(tmp_path, src, dest).returncode == 0

    result = _run(tmp_path, src, dest, ROBOTHOR_OFFSITE_VERIFY_ONLY="1")
    assert result.returncode == 0, (
        "verification failed immediately after a successful replication — the "
        f"prune deleted something verify requires: {result.stdout + result.stderr}"
    )


def test_unrecognized_remote_object_is_reported_not_pruned(tmp_path: Path):
    """An object that is not a generation is never a retention slot, and never
    a prune victim — deleting it would destroy the only copy of something a
    human deliberately put there. Say it out loud instead."""
    src = _make_source(tmp_path, days=3)
    dest = tmp_path / "remote"
    orphan = _seed_remote_orphan(dest)

    result = _run(tmp_path, src, dest)
    assert result.returncode == 0, result.stdout + result.stderr

    assert orphan.exists(), "an unrecognized remote object was pruned"
    output = result.stdout + result.stderr
    assert FOREIGN in output and "unrecognized" in output.lower(), (
        f"the unrecognized object was not reported: {output}"
    )


def test_verify_distinguishes_missing_from_corrupt(tmp_path: Path):
    """'Missing offsite' and 'the bytes differ' are different emergencies.

    Collapsing both into 'verification MISMATCH' is what made the 2026-08-23
    page unreadable: it looked identical to real corruption, so the operator
    could not tell a benign retention bug from data loss without logging in.
    """
    src = _make_source(tmp_path, days=3)
    dest = tmp_path / "remote"
    assert _run(tmp_path, src, dest).returncode == 0

    offsite = sorted((dest / "db").glob("*.sql.gz"))
    assert len(offsite) == 2, [f.name for f in offsite]

    # (a) a generation deleted from the remote
    gone = offsite[0].name
    offsite[0].unlink()
    missing_run = _run(tmp_path, src, dest, ROBOTHOR_OFFSITE_VERIFY_ONLY="1")
    assert missing_run.returncode != 0
    missing_out = missing_run.stdout + missing_run.stderr
    assert "missing" in missing_out.lower(), missing_out
    assert gone in missing_out, f"the page must name the absent generation: {missing_out}"

    # (b) a generation whose bytes no longer match
    _run(tmp_path, src, dest)  # restore the remote
    intact = sorted((dest / "db").glob("*.sql.gz"))[0]
    intact.write_bytes(b"corrupted")
    corrupt_run = _run(tmp_path, src, dest, ROBOTHOR_OFFSITE_VERIFY_ONLY="1")
    assert corrupt_run.returncode != 0
    corrupt_out = corrupt_run.stdout + corrupt_run.stderr
    assert "missing" not in corrupt_out.lower(), (
        f"corruption was reported as a missing file: {corrupt_out}"
    )
    assert intact.name in corrupt_out, f"the page must name the bad generation: {corrupt_out}"


# ── last-good markers ────────────────────────────────────────────────────────


STATE_LIB = REPO_ROOT / "scripts" / "backup-state.sh"
UNKNOWN = "unknown (no successful run recorded)"
# `date -Is` — local time WITH the offset, e.g. 2026-09-02T04:30:11+02:00.
# The offset is the point: a bare local timestamp is unorderable against
# anything, and a guard that compares it to `now` on a box that changed zone
# reads hours of drift as a stale backup.
TS_RE = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}"


def _state_dir(tmp_path: Path) -> Path:
    return tmp_path / "backup-state"


class TestTheOffsiteRunRecordsWhenItLastWorked:
    """"When did this last actually work?" had no answer anywhere.

    Every backup job's success was a line in a log file, so the only signal a
    wedged volume produced was a failing unit — and the fix for the paging
    storm is to stop those units failing. Something has to carry the "it has
    been N hours since a good run" signal instead, and it cannot live on the
    backup volume: the disk that breaks must not be the disk that holds the
    evidence. These markers live on NVMe under
    ${ROBOTHOR_BACKUP_STATE_DIR:-/var/lib/robothor/backup-state}.
    """

    def test_a_successful_run_records_last_offsite_ok(self, tmp_path: Path):
        src = _make_source(tmp_path)
        result = _run(tmp_path, src, tmp_path / "remote")
        assert result.returncode == 0, result.stdout + result.stderr

        marker = _state_dir(tmp_path) / "last-offsite-ok"
        assert marker.exists(), (
            "nothing recorded that the offsite replication worked, so a "
            "freshness guard has nothing to quote"
        )
        assert marker.read_text().strip(), "the marker is empty"
        assert UNKNOWN not in marker.read_text()

    def test_the_marker_is_a_timestamp_a_guard_can_compare(self, tmp_path: Path):
        src = _make_source(tmp_path)
        _run(tmp_path, src, tmp_path / "remote")
        stamp = (_state_dir(tmp_path) / "last-offsite-ok").read_text().strip()
        ts, _, identifier = stamp.partition(" ")
        assert re.fullmatch(TS_RE, ts), stamp
        assert identifier, (
            "the marker says WHEN but not WHAT — a freshness guard that pages "
            "'offsite is 40 hours stale' has to be able to name the generation "
            f"it is talking about\n{stamp}"
        )

    def test_the_marker_names_the_object_that_went_offsite(self, tmp_path: Path):
        src = _make_source(tmp_path)
        dest = tmp_path / "remote"
        _run(tmp_path, src, dest)
        stamp = (_state_dir(tmp_path) / "last-offsite-ok").read_text().strip()
        newest = sorted(f.name for f in src.glob("*.sql.gz"))[-1]
        assert newest in stamp, (
            "the identifier must be the object that actually landed offsite\n"
            f"{stamp}"
        )

    def test_a_failed_run_records_nothing(self, tmp_path: Path):
        """A marker written on failure is worse than no marker: it makes a
        broken backup look fresh."""
        src = _make_source(tmp_path)
        dest = tmp_path / "remote"
        dest.mkdir()
        dest.chmod(0o500)  # rclone cannot write here
        try:
            result = _run(tmp_path, src, dest)
        finally:
            dest.chmod(0o700)

        assert result.returncode != 0, result.stdout + result.stderr
        assert not (_state_dir(tmp_path) / "last-offsite-ok").exists(), (
            "a failed replication recorded a successful run — the freshness "
            "guard would report a broken offsite copy as healthy"
        )

    def test_a_verify_only_run_does_not_stamp_a_replication(self, tmp_path: Path):
        """Verify-only uploads nothing, so it says nothing about whether
        replication still works. Stamping there would let a dead upload path
        look fresh forever."""
        src = _make_source(tmp_path)
        dest = tmp_path / "remote"
        _run(tmp_path, src, dest)
        marker = _state_dir(tmp_path) / "last-offsite-ok"
        marker.unlink(missing_ok=True)

        result = _run(tmp_path, src, dest, ROBOTHOR_OFFSITE_VERIFY_ONLY="1")

        assert result.returncode == 0, result.stdout + result.stderr
        assert not marker.exists()


class TestTheMarkerHelper:
    """scripts/backup-state.sh is shared by all four backup jobs, so its
    failure modes are everyone's failure modes."""

    @staticmethod
    def _sh(tmp_path: Path, body: str, **env_extra) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": os.environ["PATH"],
            "ROBOTHOR_BACKUP_STATE_DIR": str(_state_dir(tmp_path)),
        }
        env.update(env_extra)
        return subprocess.run(
            ["bash", "-c", f'set -euo pipefail\nsource "{STATE_LIB}"\n{body}'],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_reading_a_marker_that_was_never_written_says_so(self, tmp_path: Path):
        result = self._sh(tmp_path, 'backup_state_last last-basebackup || true')
        assert UNKNOWN in result.stdout, (
            "an absent marker must read as unknown, never as an empty string a "
            "caller can mistake for a fresh timestamp\n" + result.stdout + result.stderr
        )

    def test_an_unknown_marker_reports_a_nonzero_status(self, tmp_path: Path):
        result = self._sh(tmp_path, 'backup_state_last last-basebackup')
        assert result.returncode != 0, (
            "a guard must be able to branch on 'no successful run recorded' "
            "without string-matching"
        )

    def test_a_recorded_marker_reads_back(self, tmp_path: Path):
        result = self._sh(
            tmp_path,
            "backup_state_mark last-local-dump dump-20260902.sql.gz\n"
            "backup_state_last last-local-dump",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert UNKNOWN not in result.stdout
        assert re.search(TS_RE, result.stdout), result.stdout

    def test_the_marker_file_is_a_timestamp_then_an_identifier(self, tmp_path: Path):
        """The whole contract, in one line on disk.

        It used to be a bare UTC timestamp, so every marker said WHEN a job
        last worked and nothing about WHAT it produced. A freshness page that
        cannot name the dump, the offsite object or the base backup it is
        talking about sends the operator to the box to find out.
        """
        self._sh(tmp_path, "backup_state_mark last-local-dump dump-20260902.sql.gz")
        line = (_state_dir(tmp_path) / "last-local-dump").read_text()
        assert line.endswith("\n"), "the marker must be one newline-terminated line"
        ts, _, identifier = line.strip().partition(" ")
        assert re.fullmatch(TS_RE, ts), line
        assert identifier == "dump-20260902.sql.gz", line

    def test_reading_a_marker_keeps_the_identifier(self, tmp_path: Path):
        """backup_state_last stripped ALL whitespace, so the moment a marker
        carried two fields the reader glued them into one unparseable token."""
        result = self._sh(
            tmp_path,
            "backup_state_mark last-local-dump dump-20260902.sql.gz\n"
            "backup_state_last last-local-dump",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        ts, _, identifier = result.stdout.strip().partition(" ")
        assert re.fullmatch(TS_RE, ts), result.stdout
        assert identifier == "dump-20260902.sql.gz", result.stdout

    def test_the_timestamp_can_be_read_on_its_own(self, tmp_path: Path):
        """A guard doing date arithmetic wants field 1 and nothing else."""
        result = self._sh(
            tmp_path,
            "backup_state_mark last-local-dump dump-20260902.sql.gz\n"
            "backup_state_last_ts last-local-dump",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert re.fullmatch(TS_RE, result.stdout.strip()), result.stdout

    def test_an_unwritten_marker_has_no_timestamp_either(self, tmp_path: Path):
        result = self._sh(tmp_path, "backup_state_last_ts last-basebackup || true")
        assert UNKNOWN in result.stdout, result.stdout + result.stderr
        result = self._sh(tmp_path, "backup_state_last_ts last-basebackup")
        assert result.returncode != 0, (
            "a guard must be able to branch on 'no successful run recorded'"
        )

    def test_recording_never_fails_the_backup_that_calls_it(self, tmp_path: Path):
        """The marker is bookkeeping. A backup that succeeded must not be
        reported as failed because /var/lib was read-only."""
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            result = self._sh(
                tmp_path,
                'backup_state_mark last-local-dump dump.sql.gz\necho survived',
                ROBOTHOR_BACKUP_STATE_DIR=str(blocked / "state"),
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "survived" in result.stdout
        finally:
            blocked.chmod(0o700)


class TestEveryBackupJobRecordsItsMarker:
    """backup-ssd.sh rsyncs the whole system, so it is not run here; this is a
    wiring check that it calls the shared helper with the agreed name."""

    @pytest.mark.parametrize(
        ("script", "marker"),
        [
            ("backup-ssd.sh", "last-local-dump"),
            ("backup-offsite.sh", "last-offsite-ok"),
            ("wal-offsite.sh", "last-wal-offsite-ok"),
            ("pg-basebackup.sh", "last-basebackup"),
        ],
    )
    def test_the_job_records_its_own_marker(self, script: str, marker: str):
        body = (REPO_ROOT / "scripts" / script).read_text()
        assert f"backup_state_mark {marker}" in body, (
            f"{script} never records {marker}; the freshness guard cannot tell "
            "a job that stopped running from one that is merely quiet"
        )


class TestAMissingHelperFailsBeforeTheWork:
    """backup-offsite.sh runs `set -uo pipefail` WITHOUT -e.

    That is deliberate (the retention and verification steps decide their own
    failure handling), but it means a `source` of a missing
    scripts/backup-state.sh does not stop the script — it carries on, does the
    whole replication, and then dies on the last line with "backup_state_mark:
    command not found" and exit 127. A successful backup reported as a failed
    one is a page for nothing, which is the exact behaviour this branch exists
    to end.
    """

    def test_it_refuses_to_start_without_the_marker_helper(self, tmp_path: Path):
        lone = tmp_path / "scripts"
        lone.mkdir()
        shutil.copy2(SCRIPT, lone / SCRIPT.name)  # deliberately no backup-state.sh

        src = _make_source(tmp_path)
        dest = tmp_path / "remote"
        result = subprocess.run(
            ["bash", str(lone / SCRIPT.name)],
            capture_output=True,
            text=True,
            timeout=120,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(tmp_path),
                "ROBOTHOR_OFFSITE_REMOTE": str(dest),
                "ROBOTHOR_OFFSITE_SOURCE": str(src),
                "ROBOTHOR_OFFSITE_LOG": str(tmp_path / "offsite.log"),
                "ROBOTHOR_ALERT_SUPPRESS": "1",
                "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
                "ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env"),
                "ROBOTHOR_ALERT_SPOOL_DIR": str(tmp_path / "alert-spool"),
                "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
                "ROBOTHOR_ALERT_FALLBACK_STATE_DIR": str(tmp_path / "alert-cooldown-fallback"),
                "ROBOTHOR_BACKUP_STATE_DIR": str(_state_dir(tmp_path)),
            },
        )

        assert result.returncode != 0, result.stdout + result.stderr
        assert not dest.exists(), (
            "the script replicated everything and only then noticed it could "
            "not load backup-state.sh — an hour of upload followed by a page "
            f"for a backup that worked\n{result.stdout}{result.stderr}"
        )
        assert "backup-state.sh" in result.stdout + result.stderr, (
            "the page must name the missing file, not read as "
            f"'command not found'\n{result.stdout}{result.stderr}"
        )
