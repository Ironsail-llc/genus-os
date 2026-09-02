"""A backup nobody has restored is a hope, not a backup.

WHAT THIS AUTOMATES, AND WHAT IT IS NOT

    ``robothor-backup-verify.timer`` sounds like a drill and is not one. It is
    ``backup-offsite.sh`` with ``ROBOTHOR_OFFSITE_VERIFY_ONLY=1``: an rclone
    byte-comparison of local dumps against the remote. That proves the bytes
    match. It proves nothing about whether those bytes reconstitute a database
    — a dump truncated at source is byte-identical offsite and restores into
    nothing.

    ``docs/runbooks/RESTORE_DRILL.md`` is the procedure that answers the real
    question, and it has been run by hand twice in five months. This puts it on
    a timer.

THE GUARD THE RUNBOOK LEARNED THE HARD WAY

    On 2026-08-24 the backup SSD had USB-disconnected, the dump glob matched
    NOTHING, and the drill pipeline "succeeded" in 0.09s against an empty
    database. An empty dump variable must abort, loudly and non-zero. That is
    the first thing tested here, because a drill that passes without a dump is
    worse than no drill: it manufactures evidence.

SAFETY

    The drill creates and drops a scratch database and nothing else. The live
    database name is refused outright — the one destructive verb in this whole
    task is ``dropdb``, and it must never be able to reach ``robothor_memory``.
    Tests that need a real database use their own uniquely-named scratch DB and
    skip entirely when no server is reachable.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DRILL = REPO_ROOT / "scripts" / "restore-drill.sh"
UNIT_DIR = REPO_ROOT / "infra" / "systemd"

SCRATCH_DB = f"robothor_restore_drill_test_{os.getpid()}"


def database_is_reachable() -> bool:
    if not all(shutil.which(t) for t in ("psql", "createdb", "dropdb")):
        return False
    probe = subprocess.run(
        ["psql", "-d", "postgres", "-tAc", "SELECT 1"], capture_output=True, timeout=30
    )
    return probe.returncode == 0


needs_db = pytest.mark.skipif(
    not database_is_reachable(), reason="no reachable PostgreSQL server for the drill"
)


# ── fakes ────────────────────────────────────────────────────────────────────


def install_recording_notify(tmp_path: Path) -> Path:
    """The drill's result goes out as a notification. Record it instead of
    writing a row, so no test ever lands in the operator's inbox."""
    log = tmp_path / "notify.txt"
    script = tmp_path / "bin" / "fake-notify.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{log}"\nexit 0\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return log


def write_fixture_dump(path: Path) -> Path:
    """A gzipped dump that creates one table with known contents. Generic
    fixture data — no instance rows ever enter a test dump."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sql = (
        "CREATE TABLE drill_fixture (id integer, name text);\n"
        "INSERT INTO drill_fixture VALUES (1, 'alice'), (2, 'bob'), (3, 'carol');\n"
    )
    path.write_bytes(gzip.compress(sql.encode()))
    return path


def base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBOTHOR_")}
    env.update(
        {
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "ROBOTHOR_RESTORE_DRILL_DB": SCRATCH_DB,
            "ROBOTHOR_RESTORE_DRILL_LOCAL_DIR": str(tmp_path / "dumps"),
            "ROBOTHOR_RESTORE_DRILL_WORK_DIR": str(tmp_path / "work"),
            # No remote by default: the offsite fetch is the preferred source
            # but must not reach the real one from a test.
            "ROBOTHOR_OFFSITE_REMOTE": "",
            "ROBOTHOR_RESTORE_DRILL_RCLONE_CMD": "/bin/false",
            "ROBOTHOR_RESTORE_DRILL_NOTIFY_CMD": str(tmp_path / "bin" / "fake-notify.sh"),
            # Sender isolation, in case anything reaches the pager.
            "ROBOTHOR_ALERT_SUPPRESS": "1",
            "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
            "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
        }
    )
    env.update(extra)
    return env


def run_drill(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(DRILL)], capture_output=True, text=True, timeout=600, env=env
    )


@pytest.fixture(autouse=True)
def _drop_scratch_db():
    yield
    if database_is_reachable():
        subprocess.run(["dropdb", "--if-exists", SCRATCH_DB], capture_output=True, timeout=60)


# ── the empty-glob guard ─────────────────────────────────────────────────────


class TestAMissingDumpAborts:
    def test_the_script_exists_and_is_executable(self):
        assert DRILL.exists(), "scripts/restore-drill.sh missing"
        assert DRILL.stat().st_mode & 0o111, f"{DRILL} is not executable"

    def test_no_dump_anywhere_is_a_non_zero_abort(self, tmp_path: Path):
        """2026-08-24: the glob matched nothing and the drill "succeeded" in
        0.09s against an empty database. A drill that passes without a dump
        manufactures evidence."""
        (tmp_path / "dumps").mkdir(parents=True)
        env = base_env(tmp_path)
        log = install_recording_notify(tmp_path)
        result = run_drill(env)
        assert result.returncode != 0, "an empty dump set must abort non-zero"
        combined = (result.stdout + result.stderr).lower()
        assert "no dump" in combined or "no restorable" in combined
        assert log.exists(), "the operator must still be told the drill could not run"
        assert "fail" in log.read_text().lower()

    def test_a_missing_dump_directory_is_also_an_abort(self, tmp_path: Path):
        """An absent directory and an empty one are the same news: no dump."""
        install_recording_notify(tmp_path)
        assert run_drill(base_env(tmp_path)).returncode != 0


class TestTheDrillCannotTouchTheLiveDatabase:
    """`dropdb` is the one destructive verb in this script."""

    @pytest.mark.parametrize("live", ["robothor_memory", "postgres"])
    def test_a_live_database_name_is_refused(self, tmp_path: Path, live: str):
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-20260101.sql.gz")
        install_recording_notify(tmp_path)
        result = run_drill(base_env(tmp_path, ROBOTHOR_RESTORE_DRILL_DB=live))
        assert result.returncode != 0, f"the drill must refuse to target {live}"
        assert "refus" in (result.stdout + result.stderr).lower()

    def test_the_default_scratch_name_is_the_runbooks(self):
        """docs/runbooks/RESTORE_DRILL.md names robothor_restore_drill. A second
        spelling would leave an orphan database nobody cleans up."""
        assert "robothor_restore_drill" in DRILL.read_text()


# ── the drill itself ─────────────────────────────────────────────────────────


@needs_db
class TestAFixtureDumpRestoresAndIsVerified:
    def test_the_dump_restores_and_the_verify_query_runs(self, tmp_path: Path):
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-20260101.sql.gz")
        log = install_recording_notify(tmp_path)
        result = run_drill(base_env(tmp_path))
        assert result.returncode == 0, result.stdout + result.stderr
        out = result.stdout
        assert "drill_fixture" in out or "table" in out.lower(), (
            "the verify step must actually query the restored database"
        )
        assert log.exists(), "the drill result must reach the operator"
        body = log.read_text()
        assert "robothor_memory-20260101.sql.gz" in body, (
            "the notification must name the generation that was drilled"
        )

    def test_the_drill_reports_how_long_the_restore_took(self, tmp_path: Path):
        """RTO is the number the drill exists to measure; the runbook's
        baseline table is a column of durations."""
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-20260101.sql.gz")
        log = install_recording_notify(tmp_path)
        run_drill(base_env(tmp_path))
        assert re.search(r"\d+s", log.read_text()), "no restore duration in the result"

    def test_the_scratch_database_is_dropped_afterwards(self, tmp_path: Path):
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-20260101.sql.gz")
        install_recording_notify(tmp_path)
        run_drill(base_env(tmp_path))
        listing = subprocess.run(
            ["psql", "-d", "postgres", "-tAc", "SELECT datname FROM pg_database"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert SCRATCH_DB not in listing.stdout, (
            "a drill that leaves its scratch database behind fills the disk one month at a time"
        )

    def test_an_empty_restore_fails_even_though_psql_exits_zero(self, tmp_path: Path):
        """The 2026-08-24 shape: the pipeline runs, psql is happy, and zero
        tables exist. Exit status is not evidence of a restore."""
        empty = tmp_path / "dumps" / "robothor_memory-20260101.sql.gz"
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_bytes(gzip.compress(b"-- a dump with no schema in it\n"))
        install_recording_notify(tmp_path)
        result = run_drill(base_env(tmp_path))
        assert result.returncode != 0, (
            "0 tables restored must fail the drill — psql exiting 0 says only that it read the file"
        )


# ── the offsite path ─────────────────────────────────────────────────────────


class TestOffsiteIsThePreferredSource:
    def test_the_remote_is_tried_before_the_local_copy(self, tmp_path: Path):
        """A box-loss restores from offsite, so that is the path worth
        exercising — the 2026-08-24 drill did exactly that, hours after the
        local SSD had physically disconnected."""
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-20260101.sql.gz")
        rclone_log = tmp_path / "rclone.txt"
        fake = tmp_path / "bin" / "fake-rclone.sh"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{rclone_log}"\nexit 1\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        install_recording_notify(tmp_path)
        run_drill(
            base_env(
                tmp_path,
                ROBOTHOR_OFFSITE_REMOTE="fixture:bucket",
                ROBOTHOR_RESTORE_DRILL_RCLONE_CMD=str(fake),
            )
        )
        assert rclone_log.exists() and "lsf" in rclone_log.read_text(), (
            "the offsite remote must be listed before falling back to local"
        )

    def test_an_unreachable_remote_falls_back_to_local_rather_than_skipping(self, tmp_path: Path):
        """A drill that skips itself when the network is down is a drill that
        never runs. Fall back, and say which source was used."""
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-20260101.sql.gz")
        install_recording_notify(tmp_path)
        result = run_drill(base_env(tmp_path, ROBOTHOR_OFFSITE_REMOTE="fixture:bucket"))
        combined = result.stdout + result.stderr
        assert "local" in combined.lower(), "the source actually used must be named"


# ── unit templates ───────────────────────────────────────────────────────────


def unit_text(name: str) -> str:
    path = UNIT_DIR / name
    assert path.exists(), f"infra/systemd/{name} missing"
    return path.read_text()


def directives(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.lstrip().startswith(("#", ";"))]


class TestRestoreDrillUnits:
    def test_the_units_exist(self):
        unit_text("robothor-restore-drill.service")
        unit_text("robothor-restore-drill.timer")

    def test_service_runs_the_drill_script(self):
        lines = directives(unit_text("robothor-restore-drill.service"))
        execs = [line for line in lines if line.startswith("ExecStart=")]
        assert len(execs) == 1, execs
        assert "/opt/robothor/scripts/restore-drill.sh" in execs[0]
        assert "Type=oneshot" in lines

    def test_service_pages_on_failure(self):
        assert "OnFailure=robothor-alert@%n.service" in directives(
            unit_text("robothor-restore-drill.service")
        )

    def test_a_restore_is_allowed_to_take_longer_than_the_default_timeout(self):
        """The measured baselines are 9m01s and 6m00s. systemd's default
        TimeoutStartSec of 90s would SIGTERM the drill mid-restore and report
        a failure that says nothing about the backup."""
        lines = directives(unit_text("robothor-restore-drill.service"))
        timeouts = [line for line in lines if line.startswith("TimeoutStartSec=")]
        assert timeouts, "TimeoutStartSec must be set explicitly"
        assert int(re.sub(r"\D", "", timeouts[0]) or 0) >= 3600

    def test_timer_runs_monthly_and_catches_up(self):
        lines = directives(unit_text("robothor-restore-drill.timer"))
        calendar = [line for line in lines if line.startswith("OnCalendar=")]
        assert calendar, "OnCalendar must set the schedule"
        assert "Persistent=true" in lines, (
            "a monthly drill missed because the box was off must run when it "
            "comes back, or 'quarterly by hand' becomes 'twice in five months'"
        )
        assert "WantedBy=timers.target" in lines


# ── hygiene ──────────────────────────────────────────────────────────────────


def test_drill_script_parses():
    result = subprocess.run(["bash", "-n", str(DRILL)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_drill_script_is_shellcheck_clean():
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(DRILL)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_drill_carries_no_instance_paths():
    text = DRILL.read_text()
    for home in re.findall(r"/home/[A-Za-z0-9._-]+", text):
        assert home == "/home/robothor", f"{home} is an instance path"
