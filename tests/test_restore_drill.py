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

#: The PATH the drill builds for itself, discarding what it inherits.
FIXED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


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
            # Fakes go in through ROBOTHOR_EXTRA_PATH, the drill's test-only
            # seam — never by prepending to PATH. The drill discards the PATH
            # it inherits, so a test that planted its fakes there would be
            # exercising a channel the drill no longer reads.
            "PATH": os.environ["PATH"],
            "ROBOTHOR_EXTRA_PATH": str(tmp_path / "bin"),
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


# ── the drill leaves nothing behind ──────────────────────────────────────────


def install_fake_pg(tmp_path: Path) -> dict[str, str]:
    """psql/createdb/dropdb stand-ins, so the temp-file behaviour can be tested
    on a box with no PostgreSQL and without touching a real database."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    psql = bin_dir / "fake-psql.sh"
    # Every count query answers 5, so the drill takes its success path.
    psql.write_text("#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\necho 5\n")
    psql.chmod(psql.stat().st_mode | stat.S_IEXEC)
    return {
        "ROBOTHOR_RESTORE_DRILL_PSQL": str(psql),
        "ROBOTHOR_RESTORE_DRILL_CREATEDB": "/bin/true",
        "ROBOTHOR_RESTORE_DRILL_DROPDB": "/bin/true",
    }


class TestTheDrillLeavesNoTemporaryFilesBehind:
    """A monthly unit that leaks one temp directory per run leaks twelve a
    year, and the fetched dump inside it is a full copy of production. The
    error log is the same story with a smaller file."""

    def test_the_work_dir_and_error_log_are_removed_on_success(self, tmp_path: Path):
        scratch = tmp_path / "tmp"
        scratch.mkdir()
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-fixture.sql.gz")
        install_recording_notify(tmp_path)
        env = base_env(tmp_path, **install_fake_pg(tmp_path))
        # No configured work dir: the script makes its own with mktemp -d.
        env["ROBOTHOR_RESTORE_DRILL_WORK_DIR"] = ""
        env["TMPDIR"] = str(scratch)

        result = run_drill(env)

        assert result.returncode == 0, result.stdout + result.stderr
        assert list(scratch.iterdir()) == [], (
            f"the drill left temporary files behind: {[p.name for p in scratch.iterdir()]}"
        )

    def test_nothing_is_left_behind_when_the_drill_aborts(self, tmp_path: Path):
        """The abort paths are where a leak actually accumulates: a drill that
        fails every month for a year leaks twelve directories, not one."""
        scratch = tmp_path / "tmp"
        scratch.mkdir()
        install_recording_notify(tmp_path)
        env = base_env(tmp_path, **install_fake_pg(tmp_path))
        env["ROBOTHOR_RESTORE_DRILL_WORK_DIR"] = ""
        env["TMPDIR"] = str(scratch)

        result = run_drill(env)  # no dump anywhere

        assert result.returncode != 0
        assert list(scratch.iterdir()) == [], (
            f"an aborted drill left temporary files behind: {[p.name for p in scratch.iterdir()]}"
        )

    def test_a_configured_work_dir_is_not_deleted(self, tmp_path: Path):
        """Only a directory this run created may be removed — a work dir the
        operator configured may be a real directory with other things in it."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "keep-me").write_text("operator data\n")
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-fixture.sql.gz")
        install_recording_notify(tmp_path)
        env = base_env(tmp_path, **install_fake_pg(tmp_path))
        env["ROBOTHOR_RESTORE_DRILL_WORK_DIR"] = str(work)

        assert run_drill(env).returncode == 0
        assert (work / "keep-me").exists(), "a configured work dir is not the drill's to delete"


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

    def test_the_drill_gets_a_private_tmp(self):
        """The drill fetches a full copy of the production database into a
        temp directory. PrivateTmp gives it a namespace of its own, so a
        crashed run cannot leave that copy in the shared /tmp — the same
        containment robothor-slo.service carries."""
        assert "PrivateTmp=yes" in directives(unit_text("robothor-restore-drill.service"))

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


# ── the tools the drill needs ────────────────────────────────────────────────


class TestTheToolsAreResolvedBeforeTheDrillStarts:
    """`robothor-restore-drill.service` loads the same
    `EnvironmentFile=/etc/robothor/robothor.env` as every other unit, and that
    file sets a PATH with no `/usr/sbin` and no `/sbin`. A tool the drill
    cannot find must say which tool, before it creates a scratch database and
    starts timing a restore that was never going to work — otherwise the
    result reads as "the backup did not restore", which is a very different
    page from "psql is not installed".

    The same PATH is also the reason the drill builds its own rather than
    extending what it inherits: under the unit that PATH begins with a
    user-writable `~/.local/bin`, and the drill runs as root.
    """

    def test_a_missing_tool_names_itself_and_aborts(self, tmp_path: Path):
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-fixture.sql.gz")
        notify_log = install_recording_notify(tmp_path)
        env = base_env(
            tmp_path, ROBOTHOR_RESTORE_DRILL_PSQL="robothor-not-a-real-psql"
        )

        result = run_drill(env)

        output = result.stdout + result.stderr
        assert result.returncode != 0, "a drill that cannot run must not exit 0"
        assert "robothor-not-a-real-psql" in output, (
            f"the abort must name the tool that is missing: {output}"
        )
        assert "drill PASSED" not in output, (
            "nothing may be reported as a passing drill when the restore never ran"
        )
        if notify_log.exists():
            assert "robothor-not-a-real-psql" in notify_log.read_text(), (
                "the notification must carry the real reason too"
            )

    def test_a_drill_that_cannot_find_its_own_directory_says_so(self, tmp_path: Path):
        """`readlink` and `dirname` run before the preflight, and REPO_ROOT is
        derived from them — it is where the built-in notifier looks for the
        interpreter. A drill that cannot locate its own checkout must say so
        and exit, not go on to create a scratch database and report a verdict
        nobody can deliver."""
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-fixture.sql.gz")
        lonely = tmp_path / "lonely"
        lonely.mkdir()
        shutil.copy(DRILL, lonely / DRILL.name)

        env = base_env(tmp_path, **install_fake_pg(tmp_path))

        result = subprocess.run(
            ["bash", str(lonely / DRILL.name)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 2, (
            f"a drill that cannot read its own checkout must exit 2: {result.stdout + result.stderr}"
        )
        assert "backup-state.sh" in result.stderr, (
            f"the failure must name what it could not read: {result.stderr}"
        )
        assert "drill PASSED" not in result.stdout

    def test_the_path_is_fixed_and_an_inherited_directory_is_not_consulted(
        self, tmp_path: Path
    ):
        """The drill runs as root out of a timer whose inherited PATH begins
        with a user-writable ``~/.local/bin``. It builds its own PATH instead,
        keeping ``/usr/local/bin`` because this instance's `rclone` is there.

        Recorded from a child process rather than asserted on the script text:
        what matters is the PATH the tools are actually resolved against.
        """
        write_fixture_dump(tmp_path / "dumps" / "robothor_memory-fixture.sql.gz")
        path_log = tmp_path / "notify-path.txt"
        recorder = tmp_path / "bin" / "record-path.sh"
        recorder.parent.mkdir(parents=True, exist_ok=True)
        recorder.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$PATH" >> "{path_log}"\nexit 0\n')
        recorder.chmod(recorder.stat().st_mode | stat.S_IEXEC)

        # A `date` shim first on the inherited PATH: the drill times the
        # restore with `date +%s`, so this is what a planted binary would ride.
        planted = tmp_path / "planted"
        planted.mkdir()
        sentinel = tmp_path / "planted-ran.txt"
        shim = planted / "date"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f'echo ran >> "{sentinel}"\n'
            'exec /usr/bin/date "$@"\n'
        )
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

        env = base_env(
            tmp_path,
            ROBOTHOR_RESTORE_DRILL_NOTIFY_CMD=str(recorder),
            **install_fake_pg(tmp_path),
        )
        env["PATH"] = f"{planted}:{os.environ['PATH']}"

        run_drill(env)

        assert path_log.exists(), "the drill always reports its result"
        seen = path_log.read_text().strip()
        assert seen == f"{tmp_path / 'bin'}:{FIXED_PATH}", (
            f"the drill must run on the fixed system PATH, not the inherited one: {seen}"
        )
        assert str(planted) not in seen, (
            "a directory the drill merely inherited must not be searched at all"
        )
        assert not sentinel.exists(), (
            "a binary planted on the inherited PATH was executed by a drill "
            "that runs as root"
        )
