"""A dead-man for the backup tier: it must fire on STALENESS, not on an exit code.

THE FAILURE THIS CLOSES

    2026-08-27: the encrypted USB backup volume dropped off the bus and stayed
    off for two days. Every unit in the backup chain pages on failure, and they
    did — ~22 Telegram messages whose entire content was a unit name. Not one
    of them answered the only question that matters:

        how old is the newest good backup?

    Worse, once ``scripts/backup-volume-check.sh`` landed as ``ExecCondition=``
    the units stopped failing at all: a wedged volume makes them SKIP
    (Result=exec-condition), which fires no OnFailure= and pages nobody. A
    timer that stops firing entirely fails nothing either.

    Both of those are *edge-triggered* signals — they can only speak when a
    run happens. A dead-man is *level-triggered*: it looks at the age of the
    newest good backup on a timer of its own and keeps paging while that age
    is out of budget. Fix the volume and it goes quiet by itself; ignore it
    and it comes back tomorrow.

TWO PROPERTIES THIS FILE PINS

  1. AN UNREADABLE DUMP DIRECTORY IS A BREACH, NEVER A SKIP.
     ext4's ``emergency_ro`` keeps answering stat(): the mountpoint is still a
     mountpoint and ``[[ -d ]]`` still passes. Only readdir() fails. Every
     guard the backup chain had was a stat() guard, which is precisely why two
     days went unnoticed. A probe that treats "I could not read the directory"
     as "no news" reproduces the outage exactly, so that case is tested
     head-on.

  2. THE COOLDOWN IS THE SENDER'S, KEYED PER SLO.
     ``send_failure_alert.sh`` already dedups by key. The probe runs hourly and
     calls the sender every hour while a breach stands; the sender's 12h stamp
     on ``slo:backup-freshness`` turns that into a re-page, not a page storm.
     Dedup lives in exactly one place, as it does for the liveness watchdog.

Test hygiene: on this box ``/mnt/robothor-backup`` is the LIVE backup volume
and ``/var/lib/robothor/backup-state`` the LIVE marker directory. Every seam is
pinned to a tmp_path here, and the sender is either replaced by a recording
stub or driven through a fake curl — nothing in this file can page the operator
or read the real volume.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE = REPO_ROOT / "scripts" / "slo_probe.sh"
UNIT_DIR = REPO_ROOT / "infra" / "systemd"

#: Every marker the freshness dead-man reads, and the budget each one carries.
MARKERS = ("last-local-dump", "last-offsite-ok", "last-basebackup")


# ── fakes ────────────────────────────────────────────────────────────────────


def install_fake_curl(tmp_path: Path) -> Path:
    """A curl stand-in for the sender only — this probe curls nothing itself.

    Mirrors the real thing closely enough for the sender's delivery check:
    ``-w '%{http_code}'`` must print a status, because ``send_failure_alert.sh``
    treats an HTTP 401 as an UNDELIVERED page even though curl exits 0.
    """
    log = tmp_path / "curl-args.txt"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    curl.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{log}"\n'
        'send_rc="${FAKE_CURL_SEND_RC:-0}"\n'
        "want_code=0\n"
        'for a in "$@"; do [ "$a" = \'%{http_code}\' ] && want_code=1; done\n'
        'if [ "$want_code" = 1 ]; then\n'
        "    [ \"$send_rc\" = 0 ] && printf '200' || printf '000'\n"
        "fi\n"
        'exit "$send_rc"\n'
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return log


def install_recording_alert(tmp_path: Path, exit_code: int = 0) -> Path:
    """A pager stand-in that records argv — for asserting WHAT gets paged
    without dragging the real sender (and journalctl) into the test."""
    log = tmp_path / "alert-args.txt"
    alert = tmp_path / "bin" / "fake-alert.sh"
    alert.parent.mkdir(parents=True, exist_ok=True)
    alert.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "--- ${{ROBOTHOR_ALERT_COOLDOWN_SECONDS:-unset}}" >> "{log}"\n'
        f'printf \'%s\\n\' "$@" >> "{log}"\n'
        f"exit {exit_code}\n"
    )
    alert.chmod(alert.stat().st_mode | stat.S_IEXEC)
    return log


def install_fake_psql(
    tmp_path: Path, *, beats: int = 5, failures: int = 0, exit_code: int = 0
) -> Path:
    """A psql stand-in answering the probe's two count queries.

    The real thing is never invoked from a test: this box's psql would reach
    the LIVE database under peer auth. It logs argv so the caller can assert
    which identity the query ran as, and can be made to fail outright — the
    case that used to print UNEVALUATED forever and page nobody.
    """
    log = tmp_path / "psql-args.txt"
    psql = tmp_path / "bin" / "psql"
    psql.parent.mkdir(parents=True, exist_ok=True)
    psql.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{log}"\n'
        f"[ {exit_code} = 0 ] || exit {exit_code}\n"
        'case "$*" in\n'
        f"    *heartbeat*) echo {beats} ;;\n"
        f"    *'All models failed'*) echo {failures} ;;\n"
        "    *) echo 0 ;;\n"
        "esac\n"
    )
    psql.chmod(psql.stat().st_mode | stat.S_IEXEC)
    return log


def install_fake_id(tmp_path: Path, uid: str = "0", name: str = "root") -> None:
    """Make the probe believe it runs as root, which is how the unit runs it."""
    fake = tmp_path / "bin" / "id"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        f"    -u) echo {uid} ;;\n"
        f"    -un) echo {name} ;;\n"
        f"    *) echo {name} ;;\n"
        "esac\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)


def install_fake_runuser(tmp_path: Path) -> Path:
    """Records the account it was asked to become, then runs the command."""
    log = tmp_path / "runuser-args.txt"
    fake = tmp_path / "bin" / "runuser"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{log}"\n'
        'while [ "${1:-}" != "--" ] && [ $# -gt 0 ]; do shift; done\n'
        "shift || true\n"
        'exec "$@"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return log


def install_fake_getent(tmp_path: Path, *known: str) -> Path:
    """A `getent passwd <name>` stand-in where only ``known`` accounts exist.

    The distinction this seam exists for: ``ROBOTHOR_DB_USER`` on this box is a
    libpq ROLE, and `getent passwd` on a role returns nothing. A probe that
    hops to it never runs a query at all.
    """
    fake = tmp_path / "bin" / "getent"
    fake.parent.mkdir(parents=True, exist_ok=True)
    arms = "".join(f'    {name}) echo "{name}:x:1000:1000::/home/{name}:/bin/bash" ;;\n' for name in known)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        '[ "${1:-}" = passwd ] || exit 2\n'
        'case "${2:-}" in\n'
        f"{arms}"
        "    *) exit 2 ;;\n"
        "esac\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


#: What a healthy box answers `systemctl show` with, per unit and property.
def systemctl_stamp(age_hours: float) -> str:
    """systemd's own timestamp spelling, e.g. ``Mon 2026-09-01 03:00:12 EDT``."""
    when = dt.datetime.now().astimezone() - dt.timedelta(hours=age_hours)
    return when.strftime("%a %Y-%m-%d %H:%M:%S %Z")


def healthy_units() -> dict[str, dict[str, str]]:
    return {
        "robothor-guardrail-watch.service": {
            "Result": "success",
            "ExecMainStatus": "0",
            "ExecMainExitTimestamp": systemctl_stamp(2),
        },
        "robothor-liveness.service": {"Result": "success"},
        "robothor-liveness.timer": {"LastTriggerUSec": systemctl_stamp(0.1)},
    }


def install_fake_systemctl(tmp_path: Path, units: dict[str, dict[str, str]] | None = None) -> Path:
    """A `systemctl show <unit> -p A,B` stand-in.

    Pinned by base_env for every test in this file: without it S5 and S8 would
    read the LIVE units on this box, so the suite's verdict would depend on
    whether the operator's own timers happened to be healthy this morning.
    Unknown properties come back empty, exactly as systemctl reports them.
    """
    units = healthy_units() if units is None else units
    arms = "".join(
        f'        "{unit}={prop}") echo "{prop}={value}" ;;\n'
        for unit, props in units.items()
        for prop, value in props.items()
    )
    log = tmp_path / "systemctl-args.txt"
    fake = tmp_path / "bin" / "systemctl"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf \'%s\\n\' "$@" >> "{log}"\n'
        '[ "${1:-}" = show ] || exit 1\n'
        'unit="$2"\n'
        'props="$4"\n'
        'IFS=, read -ra want <<<"$props"\n'
        'for prop in "${want[@]}"; do\n'
        '    case "${unit}=${prop}" in\n'
        f"{arms}"
        '        *) echo "${prop}=" ;;\n'
        "    esac\n"
        "done\n"
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def send_attempts(log: Path) -> int:
    """Telegram sends the pipeline actually attempted — one `.../sendMessage`
    argument per curl invocation, logged whether or not the send succeeds."""
    if not log.exists():
        return 0
    return log.read_text().count("/sendMessage")


# ── fixtures on disk ─────────────────────────────────────────────────────────


def write_marker(state_dir: Path, name: str, age_hours: float, identifier: str = "fixture") -> None:
    """Stamp a backup-state marker in scripts/backup-state.sh's own format:
    ``<date -Is> <identifier>`` on one line."""
    state_dir.mkdir(parents=True, exist_ok=True)
    when = dt.datetime.now().astimezone() - dt.timedelta(hours=age_hours)
    (state_dir / name).write_text(f"{when.isoformat(timespec='seconds')} {identifier}\n")


def write_all_markers(state_dir: Path, age_hours: float = 1) -> None:
    for name in MARKERS:
        write_marker(state_dir, name, age_hours)


def write_guardrail_marker(tmp_path: Path, age_hours: float) -> Path:
    """The marker scripts/guardrail_watch.py stamps when the daily report
    FINISHES — the run history systemd forgets across a reboot. Same one-line
    ``<date -Is> <identifier>`` shape as the backup markers."""
    state_dir = tmp_path / "slo-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    when = dt.datetime.now().astimezone() - dt.timedelta(hours=age_hours)
    path = state_dir / "last-guardrail-watch"
    path.write_text(f"{when.isoformat(timespec='seconds')} guardrail-watch\n")
    return path


def write_uptime(tmp_path: Path, up_hours: float) -> Path:
    """/proc/uptime's own format: seconds up, then seconds idle."""
    path = tmp_path / "uptime"
    path.write_text(f"{up_hours * 3600:.2f} {up_hours * 3600 * 3:.2f}\n")
    return path


def write_dump(dump_dir: Path, age_hours: float, name: str = "robothor_memory-fixture.sql.gz"):
    dump_dir.mkdir(parents=True, exist_ok=True)
    path = dump_dir / name
    path.write_bytes(b"fixture")
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))
    return path


def write_basebackup(base_dir: Path, age_hours: float, stamp: str = "20260901T000000") -> Path:
    """A base backup as `scripts/pg-basebackup.sh` leaves it: a `base-<stamp>/`
    directory holding the tarball, plus a `base-<stamp>.backup_label` file
    beside it."""
    base_dir.mkdir(parents=True, exist_ok=True)
    out = base_dir / f"base-{stamp}"
    out.mkdir(exist_ok=True)
    (out / "base.tar.gz").write_bytes(b"fixture")
    label = base_dir / f"base-{stamp}.backup_label"
    label.write_text("START WAL LOCATION: 0/3000028\n")
    when = time.time() - age_hours * 3600
    for path in (out / "base.tar.gz", out, label):
        os.utime(path, (when, when))
    return out


def healthy_tree(tmp_path: Path, age_hours: float = 1) -> None:
    """A backup tier that is entirely within budget."""
    write_all_markers(tmp_path / "backup-state", age_hours)
    write_dump(tmp_path / "dumps", age_hours)


# ── the environment ──────────────────────────────────────────────────────────


#: The PATH both scripts build for themselves, discarding what they inherit.
FIXED_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"



def _pinned_cooldown_dir(tmp_path: Path) -> Path:
    """Pre-create the pinned cooldown dir. If it does not exist when the sender
    runs, send_failure_alert.sh falls back to $XDG_RUNTIME_DIR/robothor-alert-cooldown
    — a real, shared directory — which is exactly the leak these tests must never
    reach (observed once as a cross-test flake)."""
    d = tmp_path / "alert-cooldown"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _pinned_dir(tmp_path: Path, name: str) -> Path:
    """Pre-create one of the sender's durable directories inside tmp_path.

    Same reasoning as the cooldown dir above: the sender falls back to a real,
    shared path whenever the pinned one is missing or unusable, so the pin has
    to name a directory that already exists.
    """
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Hermetic: no live volume, no live marker dir, no live cooldown state,
    no real Telegram endpoint, no database."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBOTHOR_")}
    env.update(
        {
            # The fakes are injected through ROBOTHOR_EXTRA_PATH, the probe's
            # own test-only seam — NOT by prepending to PATH. The probe
            # deliberately discards the PATH it inherits (under the unit that
            # PATH begins with a user-writable ~/.local/bin), so a test that
            # planted its fakes there would be testing a channel the probe no
            # longer reads.
            "PATH": os.environ["PATH"],
            "ROBOTHOR_EXTRA_PATH": str(tmp_path / "bin"),
            "HOME": str(tmp_path),
            # The two live paths this probe would otherwise read.
            "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "backup-state"),
            # S8's post-reboot fallback reads a marker under the LIVE
            # /var/lib/robothor/slo-state and the LIVE /proc/uptime. Both are
            # pinned so the suite's verdict cannot depend on when this box last
            # booted or on whether the operator's daily report ran this
            # morning. The default uptime is deliberately long: "this box has
            # been up for a month" is the state in which every pre-existing S8
            # case was written, so pinning it keeps those cases measuring what
            # they always measured.
            "ROBOTHOR_SLO_STATE_DIR": str(tmp_path / "slo-state"),
            "ROBOTHOR_SLO_UPTIME_FILE": str(write_uptime(tmp_path, 720)),
            "ROBOTHOR_SLO_LOCAL_DUMP_DIR": str(tmp_path / "dumps"),
            "ROBOTHOR_SLO_BASEBACKUP_DIR": str(tmp_path / "basebackup"),
            # Seams: no volume probe, no rclone, no psql by default, and a
            # systemctl that answers for a healthy box — never the live units.
            "ROBOTHOR_SLO_VOLUME_CHECK_CMD": "/bin/true",
            "ROBOTHOR_SLO_SYSTEMCTL_CMD": str(install_fake_systemctl(tmp_path)),
            "ROBOTHOR_SLO_RCLONE_CMD": "/bin/false",
            "ROBOTHOR_SLO_DB_CHECKS": "0",
            # Sender isolation — a stamp written into the real /run/robothor
            # cooldown dir by a test could suppress a REAL page later.
            "ROBOTHOR_ALERT_STATE_DIR": str(_pinned_cooldown_dir(tmp_path)),
            # A page this probe cannot deliver is SPOOLED, not dropped, and the
            # real spool (/var/lib/robothor/alert-spool) is drained every five
            # minutes by root's liveness tick — so an unpinned spool does not
            # avoid paging the operator with a fixture failure, it delays it.
            "ROBOTHOR_ALERT_SPOOL_DIR": str(_pinned_dir(tmp_path, "alert-spool")),
            # And where the cooldown stamp lands when the primary state dir is
            # not writable, which is every cron-driven page on this box.
            "ROBOTHOR_ALERT_FALLBACK_STATE_DIR": str(
                _pinned_dir(tmp_path, "alert-fallback")
            ),
            "ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env"),
            "ROBOTHOR_ALERT_MAX_ATTEMPTS": "1",
            "ROBOTHOR_ALERT_RETRY_DELAY": "0",
            "ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123",
            "ROBOTHOR_TELEGRAM_CHAT_ID": "42",
            "ROBOTHOR_TELEGRAM_API_BASE": "http://127.0.0.1:1",
        }
    )
    env.update(extra)
    return env


def run_probe(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PROBE)], capture_output=True, text=True, timeout=120, env=env
    )


def run_probe_report(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """--report: the machine-readable rows the daily surface consumes."""
    return subprocess.run(
        ["bash", str(PROBE), "--report"], capture_output=True, text=True, timeout=120, env=env
    )


def with_recording_alert(tmp_path: Path, env: dict[str, str], exit_code: int = 0) -> Path:
    """Point the probe's pager seam at the recording stub.

    ``exit_code=1`` is a sender that could not deliver — the case where the
    probe has to fail its own unit.
    """
    log = install_recording_alert(tmp_path, exit_code)
    env["ROBOTHOR_SLO_ALERT_CMD"] = str(tmp_path / "bin" / "fake-alert.sh")
    return log


# ── S4: the backup-freshness dead-man ────────────────────────────────────────


class TestFreshBackupsPageNobody:
    def test_probe_script_exists_and_is_executable(self):
        assert PROBE.exists(), "scripts/slo_probe.sh missing"
        assert PROBE.stat().st_mode & 0o111, f"{PROBE} is not executable"

    def test_a_backup_tier_inside_budget_is_silent(self, tmp_path: Path):
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)
        result = run_probe(env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not log.exists(), f"a healthy backup tier paged: {log.read_text()}"
        assert "OK" in result.stdout


class TestStalenessPages:
    def test_a_dump_older_than_the_budget_pages(self, tmp_path: Path):
        """27h > the 26h budget: one nightly dump has been missed."""
        healthy_tree(tmp_path, age_hours=1)
        write_marker(tmp_path / "backup-state", "last-local-dump", age_hours=27)
        write_dump(tmp_path / "dumps", age_hours=27)
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists(), "a 27h-old dump must page — this is the dead-man"
        body = log.read_text()
        assert "slo:backup-freshness" in body, "the page must be keyed per SLO"
        assert "27" in body, "the page must carry the AGE, not just a unit name"

    def test_the_page_names_the_tier_and_the_budget(self, tmp_path: Path):
        healthy_tree(tmp_path, age_hours=1)
        write_marker(tmp_path / "backup-state", "last-local-dump", age_hours=40)
        write_dump(tmp_path / "dumps", age_hours=40)
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        body = log.read_text()
        assert "local dump" in body.lower()
        assert "26" in body, "the budget the age was measured against must be in the page"

    def test_a_stale_basebackup_pages_on_its_own_eight_day_budget(self, tmp_path: Path):
        """The basebackup tier is weekly, not nightly — 30h is fine, 9d is not."""
        healthy_tree(tmp_path, age_hours=1)
        write_marker(tmp_path / "backup-state", "last-basebackup", age_hours=30)
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)
        assert run_probe(env).returncode == 0
        assert not log.exists(), "a 30h-old base backup is inside its 8-day budget"

        write_marker(tmp_path / "backup-state", "last-basebackup", age_hours=24 * 9)
        run_probe(env)
        assert log.exists() and "basebackup" in log.read_text().lower()

    def test_a_basebackup_on_disk_answers_when_the_marker_is_gone(self, tmp_path: Path):
        """The marker is evidence a run happened; the base-* directory is the
        thing PITR actually starts from.

        Reading only the marker made the missing-marker case page "PITR has no
        starting point" — a sentence that is simply false while a week-old base
        backup sits on the volume. The marker directory is on NVMe and the
        backup is not: restoring the box, or losing /var/lib, loses the marker
        and keeps the base. A dead-man that cries about a backup it is standing
        on gets muted like any other."""
        healthy_tree(tmp_path)
        (tmp_path / "backup-state" / "last-basebackup").unlink()
        write_basebackup(tmp_path / "basebackup", age_hours=30)
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        assert not log.exists(), (
            "a 30h-old base backup is inside the 8-day budget whether or not "
            f"a marker recorded it: {log.read_text() if log.exists() else ''}"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "marker absent" in result.stdout, (
            "the report must say the age came from the artifact, not a marker"
        )

    def test_a_stale_basebackup_on_disk_still_pages(self, tmp_path: Path):
        """The fallback measures; it does not excuse."""
        healthy_tree(tmp_path)
        (tmp_path / "backup-state" / "last-basebackup").unlink()
        write_basebackup(tmp_path / "basebackup", age_hours=24 * 9)
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)

        run_probe(env)

        assert log.exists(), "a 9-day-old base backup is outside its 8-day budget"
        assert "basebackup" in log.read_text().lower()

    def test_the_backup_label_file_is_not_mistaken_for_a_base_backup(self, tmp_path: Path):
        """`pg-basebackup.sh` writes `base-<stamp>/` AND `base-<stamp>.backup_label`
        beside it. The label is a few hundred bytes of text; it is not a
        restorable copy, and a glob that counts it reports a base backup that
        does not exist."""
        healthy_tree(tmp_path)
        (tmp_path / "backup-state" / "last-basebackup").unlink()
        base = tmp_path / "basebackup"
        base.mkdir(parents=True, exist_ok=True)
        label = base / "base-20260901T000000.backup_label"
        label.write_text("START WAL LOCATION: 0/3000028\n")
        os.utime(label, (time.time(), time.time()))
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)

        run_probe(env)

        assert log.exists(), "a lone backup_label is not a base backup — that must page"
        assert "basebackup" in log.read_text().lower()

    def test_a_marker_that_was_never_written_is_a_breach(self, tmp_path: Path):
        """An absent marker reads as "recent" to anything that only checks for
        a non-empty string. It means the opposite: no run has EVER succeeded."""
        healthy_tree(tmp_path)
        (tmp_path / "backup-state" / "last-offsite-ok").unlink()
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists(), "a never-written marker must page, not read as fresh"
        assert "offsite" in log.read_text().lower()


class TestAnUnreadableDirectoryIsABreach:
    """The exact 2026-08-27 failure: stat() answers, readdir() does not."""

    @pytest.fixture(autouse=True)
    def _skip_as_root(self):
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions; the EIO case cannot be staged")

    def test_an_unreadable_dump_directory_pages(self, tmp_path: Path):
        healthy_tree(tmp_path)
        dumps = tmp_path / "dumps"
        dumps.chmod(0o000)
        try:
            env = base_env(tmp_path)
            log = with_recording_alert(tmp_path, env)
            run_probe(env)
            assert log.exists(), (
                "an unreadable dump directory must PAGE, never skip — a probe "
                "that reads it as 'no news' reproduces the two-day outage"
            )
            assert "read" in log.read_text().lower()
        finally:
            dumps.chmod(0o755)

    def test_an_unreadable_directory_pages_even_when_the_marker_looks_fresh(self, tmp_path: Path):
        """The markers live on NVMe and the dumps on the USB volume. A marker
        stamped an hour before the drive fell off the bus stays fresh forever,
        so it must not be allowed to vouch for a directory nobody can read."""
        healthy_tree(tmp_path, age_hours=0.1)
        dumps = tmp_path / "dumps"
        dumps.chmod(0o000)
        try:
            env = base_env(tmp_path)
            log = with_recording_alert(tmp_path, env)
            result = run_probe(env)
            assert log.exists(), (
                "a fresh marker must not vouch for an unreadable volume — that "
                "is the failure mode, not the fix"
            )
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            dumps.chmod(0o755)

    def test_an_empty_dump_directory_pages(self, tmp_path: Path):
        """A readable but empty directory is what an unmounted volume looks
        like: the glob matches nothing and there is no restorable copy."""
        healthy_tree(tmp_path)
        for path in (tmp_path / "dumps").iterdir():
            path.unlink()
        (tmp_path / "backup-state" / "last-local-dump").unlink()
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists()


class TestTheVolumeProbeIsPartOfTheDeadMan:
    def test_an_unhealthy_volume_pages(self, tmp_path: Path):
        """scripts/backup-volume-check.sh exits 1 for "not usable". That is a
        SKIP for the backup units by design — it must be a PAGE here, because
        this probe is the loud half of that arrangement."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path, ROBOTHOR_SLO_VOLUME_CHECK_CMD="/bin/false")
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists() and "volume" in log.read_text().lower()


# ── the cooldown belongs to the sender ───────────────────────────────────────


class TestCooldownIsTheSenders:
    def test_a_standing_breach_pages_once_inside_the_cooldown(self, tmp_path: Path):
        """Hourly probe, 12h cooldown: a breach that lasts all day is one page,
        not twelve. Dedup lives in exactly one place — the sender."""
        healthy_tree(tmp_path)
        write_marker(tmp_path / "backup-state", "last-local-dump", age_hours=27)
        write_dump(tmp_path / "dumps", age_hours=27)
        curl_log = install_fake_curl(tmp_path)
        env = base_env(tmp_path)
        for _ in range(4):
            run_probe(env)
        assert send_attempts(curl_log) == 1, (
            "the sender's per-key cooldown must dedup the standing breach"
        )

    def test_the_page_is_keyed_per_slo_not_per_unit(self, tmp_path: Path):
        """`slo:backup-freshness`, not a systemd unit name. A unit-keyed stamp
        would let an unrelated unit's page mute this one, and vice versa."""
        healthy_tree(tmp_path)
        write_marker(tmp_path / "backup-state", "last-local-dump", age_hours=27)
        write_dump(tmp_path / "dumps", age_hours=27)
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.read_text().splitlines()[1] == "slo:backup-freshness"

    def test_the_backup_cooldown_defaults_to_twelve_hours(self, tmp_path: Path):
        """12h on an hourly probe re-pages at least daily until it is fixed —
        the defining property of a dead-man, versus OnFailure's one-shot."""
        healthy_tree(tmp_path)
        write_marker(tmp_path / "backup-state", "last-local-dump", age_hours=27)
        write_dump(tmp_path / "dumps", age_hours=27)
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert "--- 43200" in log.read_text(), "the sender must be handed a 12h cooldown"


class TestAnUndeliveredPageIsNotSuccess:
    def test_a_failed_send_fails_the_unit(self, tmp_path: Path):
        """`delivered = bool(sent)`, per robothor/engine/alerts.py. A breach
        whose page did not land must fail the unit so its own OnFailure= fires."""
        healthy_tree(tmp_path)
        write_marker(tmp_path / "backup-state", "last-local-dump", age_hours=27)
        write_dump(tmp_path / "dumps", age_hours=27)
        env = base_env(tmp_path)
        with_recording_alert(tmp_path, env, exit_code=1)
        result = run_probe(env)
        assert result.returncode != 0
        assert "not delivered" in (result.stdout + result.stderr).lower()


# ── S5 / S8: the two SLOs that used to be hardcoded OK ───────────────────────


def with_units(tmp_path: Path, env: dict[str, str], units: dict[str, dict[str, str]]) -> None:
    """Repoint the systemctl seam at a differently-answering box."""
    env["ROBOTHOR_SLO_SYSTEMCTL_CMD"] = str(install_fake_systemctl(tmp_path, units))


class TestTheGuardrailWatchStalenessSlo:
    """S8 was the string "this report is the evidence" — printed by the very
    report whose absence it was supposed to detect. A daily unit that stops
    running produces no report, so nothing said so."""

    def test_a_daily_report_that_has_not_run_in_26h_pages(self, tmp_path: Path):
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        units = healthy_units()
        units["robothor-guardrail-watch.service"]["ExecMainExitTimestamp"] = systemctl_stamp(30)
        with_units(tmp_path, env, units)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists(), "a daily report 30h since its last run must page"
        body = log.read_text()
        assert "slo:guardrail-watch-stale" in body
        assert "--- 43200" in body, "S8 carries a 12h cooldown"

    def test_an_unexpected_exit_status_pages_even_when_it_is_recent(self, tmp_path: Path):
        """Status 2 is not a vocabulary the daily report has: it died."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        units = healthy_units()
        units["robothor-guardrail-watch.service"]["Result"] = "exit-code"
        units["robothor-guardrail-watch.service"]["ExecMainStatus"] = "2"
        with_units(tmp_path, env, units)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists() and "slo:guardrail-watch-stale" in log.read_text()

    def test_a_unit_that_never_completed_is_a_breach_not_silence(self, tmp_path: Path):
        """No exit timestamp, no marker, and a box that has been up for a
        month: the report really has never completed here."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        units = healthy_units()
        units["robothor-guardrail-watch.service"]["ExecMainExitTimestamp"] = ""
        with_units(tmp_path, env, units)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists(), "no run has ever completed — that is the breach, not no news"

    def test_a_healthy_daily_report_is_silent(self, tmp_path: Path):
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)
        assert run_probe(env).returncode == 0
        assert not log.exists()


class TestS8MeasuresWhetherTheReportRanNotWhetherItFoundNothing:
    """`robothor-guardrail-watch.service` is a Type=oneshot that exits 1 BY
    DESIGN whenever it has findings — a drifted drop-in, an invalid manifest,
    a guardrail whose effective mode is not the one its manifest records. That
    exit code is the unit's own OnFailure= pager firing, and it has already
    reached the operator by the time S8 looks.

    Reading it as ``Result != success`` therefore made S8 page "the daily
    report is failing, so the drift checks are not reaching anyone" on exactly
    the mornings the drift checks DID reach someone. Two pages for one event,
    the second one wrong, on the surface whose whole job is to be trusted when
    it finally says something.

    S8 asks one question: did the report RUN, recently? A completed run with a
    fresh exit timestamp answers yes whatever it found. Only a run that did
    not complete — timeout, signal, core dump — or one too old, or none at
    all, is a breach.
    """

    @staticmethod
    def _watch(**props: str) -> dict[str, dict[str, str]]:
        units = healthy_units()
        units["robothor-guardrail-watch.service"].update(props)
        return units

    def test_a_fresh_run_that_reported_findings_is_not_a_breach(self, tmp_path: Path):
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        with_units(
            tmp_path,
            env,
            self._watch(
                Result="exit-code",
                ExecMainStatus="1",
                ExecMainExitTimestamp=systemctl_stamp(2),
            ),
        )
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        assert not log.exists(), (
            "the report ran two hours ago and said what it found; S8 must not "
            f"page a second time on top of its OnFailure=: {log.read_text() if log.exists() else ''}"
        )
        assert result.returncode == 0, result.stdout + result.stderr

    @pytest.mark.parametrize("result_value", ["timeout", "signal", "core-dump"])
    def test_a_run_that_did_not_complete_is_a_breach(self, tmp_path: Path, result_value: str):
        """These are the Results that mean the report stopped mid-way: nothing
        it carries reached anyone, and no OnFailure= says which half ran."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        with_units(
            tmp_path,
            env,
            self._watch(
                Result=result_value,
                ExecMainStatus="1",
                ExecMainExitTimestamp=systemctl_stamp(2),
            ),
        )
        log = with_recording_alert(tmp_path, env)

        run_probe(env)

        assert log.exists(), f"Result={result_value} is a report that never finished"
        body = log.read_text()
        assert "slo:guardrail-watch-stale" in body
        assert result_value in body, "the page must name what systemd actually said"

    def test_a_stale_run_is_a_breach_however_it_exited(self, tmp_path: Path):
        """Freshness is the measurement. A findings-exit 30h ago is still a
        report that has not run since yesterday."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        with_units(
            tmp_path,
            env,
            self._watch(
                Result="exit-code",
                ExecMainStatus="1",
                ExecMainExitTimestamp=systemctl_stamp(30),
            ),
        )
        log = with_recording_alert(tmp_path, env)

        run_probe(env)

        assert log.exists(), "30h since the last completed run is the breach S8 exists for"
        assert "slo:guardrail-watch-stale" in log.read_text()


class TestS8SurvivesAReboot:
    """`ExecMainExitTimestamp` is per-unit RUNTIME state, and a reboot empties
    it.

    THE FAILURE THIS CLOSES

        2026-09-03, 03:01. The box had rebooted overnight. S8 asked systemd
        when robothor-guardrail-watch.service last exited, got an empty
        property back, and paged:

            S8 BREACHED: robothor-guardrail-watch.service has no completed run
            on this box.

        It had one. The daily report ran at 08:30 the previous morning and
        finished cleanly; systemd simply does not carry a oneshot's exit
        timestamp across a boot. So the dead-man for the daily watchdog cried
        wolf on every reboot day — on the surface whose entire value is being
        believed the one time it speaks.

    THE FIX

        The run history systemd forgets is written down. guardrail_watch.py
        stamps ``${ROBOTHOR_SLO_STATE_DIR}/last-guardrail-watch`` when it
        finishes, on NVMe, for the same reason the backup jobs stamp theirs:
        the evidence of when something last worked must outlive the thing that
        forgets it. With no timestamp AND no marker, the question becomes how
        long this box has been up — because a report whose 08:30 slot has not
        come round yet is not a report that stopped running.
    """

    @staticmethod
    def _forgotten_stamp(tmp_path: Path, env: dict[str, str]) -> None:
        """A box that has just booted: systemd answers with an empty
        ExecMainExitTimestamp, exactly as it did at 03:01."""
        units = healthy_units()
        units["robothor-guardrail-watch.service"]["ExecMainExitTimestamp"] = ""
        with_units(tmp_path, env, units)

    def test_a_fresh_marker_answers_for_the_timestamp_systemd_forgot(self, tmp_path: Path):
        """(a) Yesterday's 08:30 run, this morning's reboot. Not a breach."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        self._forgotten_stamp(tmp_path, env)
        write_guardrail_marker(tmp_path, age_hours=19)
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        assert not log.exists(), (
            "the report completed 19h ago and said so on disk; paging here is "
            f"the false alarm this test exists for: {log.read_text() if log.exists() else ''}"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "19h" in result.stdout, (
            "the journal must show the age it actually measured, and where it "
            f"came from: {result.stdout}"
        )
        assert "marker" in result.stdout.lower()

    def test_a_stale_marker_still_breaches(self, tmp_path: Path):
        """(b) The fallback is a measurement, not an excuse: a marker 30h old
        is still a daily report that has not run since the day before."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        self._forgotten_stamp(tmp_path, env)
        write_guardrail_marker(tmp_path, age_hours=30)
        log = with_recording_alert(tmp_path, env)

        run_probe(env)

        assert log.exists(), "30h since the last completed run is a breach however it was measured"
        body = log.read_text()
        assert "slo:guardrail-watch-stale" in body
        assert "30" in body, "the page must carry the AGE"

    def test_a_box_that_just_booted_is_unevaluated_not_a_breach(self, tmp_path: Path):
        """(c) No timestamp, no marker, up two hours: the 08:30 slot has not
        come round yet. Nothing has been proven wrong, so nobody is woken."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        self._forgotten_stamp(tmp_path, env)
        env["ROBOTHOR_SLO_UPTIME_FILE"] = str(write_uptime(tmp_path, 2))
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        assert not log.exists(), (
            "a box that booted two hours ago has not missed a daily run yet — "
            f"this is the 03:01 page: {log.read_text() if log.exists() else ''}"
        )
        output = result.stdout + result.stderr
        assert "UNEVALUATED" in output, "not measurable yet must be said in that word, not implied"
        assert "S8" in output and "2h" in output, output
        assert result.returncode == 0, (
            "exit 1 fires the unit's own OnFailure= — that would page the "
            "operator HOURLY for the first 26h of every boot, which is a worse "
            f"version of the false page this branch removes: {output}"
        )

    def test_a_long_uptime_with_no_marker_is_still_a_breach(self, tmp_path: Path):
        """(d) Up 40 hours, no marker, no timestamp: the daily report really
        has not completed here, and the boot excuse has expired."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        self._forgotten_stamp(tmp_path, env)
        env["ROBOTHOR_SLO_UPTIME_FILE"] = str(write_uptime(tmp_path, 40))
        log = with_recording_alert(tmp_path, env)

        run_probe(env)

        assert log.exists(), (
            "40h of uptime with no completed run and no marker is the breach "
            "S8 exists for — the reboot fallback must not swallow it"
        )
        assert "slo:guardrail-watch-stale" in log.read_text()

    def test_an_unreadable_uptime_source_falls_back_to_the_breach(self, tmp_path: Path):
        """A probe that cannot read /proc/uptime must not invent an excuse.
        Unknown uptime reads as the old behaviour, which errs towards paging."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        self._forgotten_stamp(tmp_path, env)
        env["ROBOTHOR_SLO_UPTIME_FILE"] = str(tmp_path / "no-such-uptime")
        log = with_recording_alert(tmp_path, env)

        run_probe(env)

        assert log.exists(), "an unknown uptime must not become a silent pass"

    def test_report_mode_shows_the_same_reasoning(self, tmp_path: Path):
        """--report is the daily surface's only view of S8, and it must not be
        a second implementation: same marker, same words."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        self._forgotten_stamp(tmp_path, env)
        write_guardrail_marker(tmp_path, age_hours=19)

        rows = [r for r in run_probe_report(env).stdout.splitlines() if "S8" in r]

        assert rows, "S8 must still emit a row in report mode"
        assert rows[0].endswith("OK"), rows[0]
        assert "marker" in rows[0].lower() and "19h" in rows[0], rows[0]

    def test_report_mode_says_not_yet_measurable_after_a_boot(self, tmp_path: Path):
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        self._forgotten_stamp(tmp_path, env)
        env["ROBOTHOR_SLO_UPTIME_FILE"] = str(write_uptime(tmp_path, 2))

        rows = [r for r in run_probe_report(env).stdout.splitlines() if "S8" in r]

        assert rows and rows[0].endswith("UNEVALUATED"), rows
        assert "2h" in rows[0], rows[0]


class TestTheLivenessSlo:
    """S5 was the string "enforced by robothor-liveness.timer" — an assertion
    that the watchdog exists, not a measurement that it ran."""

    def test_a_liveness_timer_that_stopped_firing_pages(self, tmp_path: Path):
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        units = healthy_units()
        units["robothor-liveness.timer"]["LastTriggerUSec"] = systemctl_stamp(4)
        with_units(tmp_path, env, units)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists(), "a 5-minute timer that last fired 4h ago is not watching anything"
        assert "slo:liveness-stale" in log.read_text()

    def test_a_failing_liveness_probe_pages(self, tmp_path: Path):
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        units = healthy_units()
        units["robothor-liveness.service"]["Result"] = "timeout"
        with_units(tmp_path, env, units)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists() and "slo:liveness-stale" in log.read_text()

    def test_a_timer_that_has_never_fired_is_a_breach(self, tmp_path: Path):
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        units = healthy_units()
        units["robothor-liveness.timer"]["LastTriggerUSec"] = "n/a"
        with_units(tmp_path, env, units)
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists() and "slo:liveness-stale" in log.read_text()


class TestASystemctlThatCannotAnswerIsUnevaluated:
    def test_no_systemctl_is_unevaluated_and_loud(self, tmp_path: Path):
        healthy_tree(tmp_path)
        env = base_env(tmp_path, ROBOTHOR_SLO_SYSTEMCTL_CMD="/bin/false")
        log = with_recording_alert(tmp_path, env)
        result = run_probe(env)
        output = result.stdout + result.stderr
        assert "UNEVALUATED" in output
        assert not log.exists(), "an unevaluated SLO is not a breach — it must not page"
        assert result.returncode != 0, (
            "S5 and S8 unmeasured is the inert-control state; the unit's "
            "OnFailure= is the only voice it has"
        )


# ── S2 / S6: the database-backed SLOs ────────────────────────────────────────


def db_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """base_env with the DB-backed half switched ON and pointed at a fake psql.

    Everything above pins ``ROBOTHOR_SLO_DB_CHECKS=0``, which is how S2 and S6
    shipped untested: the unit runs the probe as root, psql fails peer auth,
    and both printed UNEVALUATED forever while paging nobody.
    """
    env = base_env(tmp_path, ROBOTHOR_SLO_DB_CHECKS="1")
    env.update(extra)
    return env


class TestTheLlmAvailabilitySloPages:
    def test_five_all_models_failed_in_one_hour_pages(self, tmp_path: Path):
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, failures=5)
        env = db_env(tmp_path, ROBOTHOR_SLO_PSQL_CMD=str(tmp_path / "bin" / "psql"))
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists(), "5 runs in an hour with every model exhausted must page"
        body = log.read_text()
        assert "slo:llm-availability" in body, "the page must be keyed per SLO"
        assert "--- 21600" in body, "S6 carries a 6h cooldown, not the backup tier's 12h"

    def test_four_in_one_hour_stays_inside_the_threshold(self, tmp_path: Path):
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, failures=4)
        env = db_env(tmp_path, ROBOTHOR_SLO_PSQL_CMD=str(tmp_path / "bin" / "psql"))
        log = with_recording_alert(tmp_path, env)
        result = run_probe(env)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not log.exists(), f"4 < 5 must not page: {log.read_text() if log.exists() else ''}"


class TestTheHeartbeatDeliverySloPages:
    def test_zero_heartbeat_runs_in_24h_pages(self, tmp_path: Path):
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, beats=0)
        env = db_env(tmp_path, ROBOTHOR_SLO_PSQL_CMD=str(tmp_path / "bin" / "psql"))
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert log.exists(), "an operator-facing agent that has not run in 24h must page"
        assert "slo:heartbeat-delivery" in log.read_text()

    def test_a_heartbeat_that_ran_is_silent(self, tmp_path: Path):
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, beats=12)
        env = db_env(tmp_path, ROBOTHOR_SLO_PSQL_CMD=str(tmp_path / "bin" / "psql"))
        log = with_recording_alert(tmp_path, env)
        assert run_probe(env).returncode == 0
        assert not log.exists()


class TestAnUnevaluatedSloIsLoud:
    """An inert dead-man must be loud. A probe that cannot reach the database
    prints UNEVALUATED and pages nothing — so the ONLY way that reaches an
    operator is a non-zero exit firing the unit's own OnFailure=."""

    def test_a_psql_failure_pages_nothing_and_fails_the_unit(self, tmp_path: Path):
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, exit_code=2)
        env = db_env(tmp_path, ROBOTHOR_SLO_PSQL_CMD=str(tmp_path / "bin" / "psql"))
        log = with_recording_alert(tmp_path, env)
        result = run_probe(env)
        output = result.stdout + result.stderr
        assert "UNEVALUATED" in output, output
        assert not log.exists(), "an unevaluated SLO is not a breach — it must not page"
        assert result.returncode != 0, (
            "a database the probe cannot reach leaves S2 and S6 unmeasured; "
            "exiting 0 makes the whole DB half of the dead-man inert and silent"
        )

    def test_the_disabled_switch_is_not_a_failure(self, tmp_path: Path):
        """ROBOTHOR_SLO_DB_CHECKS=0 is a deliberate operator choice, not an
        outage — it must stay exit 0 or every test above would page."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path)
        assert run_probe(env).returncode == 0

    def test_the_disabled_switch_says_so_loudly(self, tmp_path: Path):
        """The mute exists for THIS test file — every test above pins it,
        because the probe would otherwise query the live database.

        Set on a real box it silently retires half the dead-man: S2 and S6
        stop being measured and nothing pages, which is indistinguishable from
        two SLOs that are permanently fine. A one-line parenthetical in the
        journal is how a switch like that survives for months, so it announces
        itself in the same words the runbook uses.
        """
        healthy_tree(tmp_path)
        env = base_env(tmp_path)

        result = run_probe(env)

        assert result.returncode == 0
        assert "ROBOTHOR_SLO_DB_CHECKS=0" in result.stderr, (
            "a mute that only whispers on stdout gets skimmed past — the "
            f"warning belongs on stderr: {result.stdout + result.stderr}"
        )
        loud = result.stderr
        assert "S2" in loud and "S6" in loud, "name the SLOs that stopped being measured"
        assert "NOT" in loud, "say plainly that they are not being measured"
        assert "production" in loud, "say plainly where this must never be set"


class TestTheQueryRunsAsAnAccountPeerAuthAccepts:
    """pg_hba uses peer auth on the Unix socket: the OS user must equal the PG
    role. The unit runs as root (it must — the pager recovers the secrets with
    the root-readable age key), and root is not a role, so an un-hopped psql
    fails every hour and reports nothing."""

    def test_a_root_probe_hops_to_the_service_account(self, tmp_path: Path):
        """The simple arrangement: the OS account and the role share a name."""
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, failures=5)
        install_fake_id(tmp_path)
        install_fake_getent(tmp_path, "alice")
        runuser_log = install_fake_runuser(tmp_path)
        env = db_env(
            tmp_path,
            ROBOTHOR_DB_USER="alice",
            ROBOTHOR_SLO_OS_USER="alice",
            ROBOTHOR_SLO_GETENT_CMD=str(tmp_path / "bin" / "getent"),
            ROBOTHOR_SLO_RUNUSER_CMD=str(tmp_path / "bin" / "runuser"),
            PGDATABASE="robothor_memory",
        )
        log = with_recording_alert(tmp_path, env)
        run_probe(env)
        assert runuser_log.exists(), (
            "as root the query must hop to the DB account, or peer auth "
            "rejects it and S2/S6 are UNEVALUATED forever"
        )
        assert "alice" in runuser_log.read_text()
        assert log.exists() and "slo:llm-availability" in log.read_text(), (
            "the hop must still deliver the measurement, not just run"
        )

    def test_a_probe_already_running_as_the_db_account_does_not_hop(self, tmp_path: Path):
        healthy_tree(tmp_path)
        psql_log = install_fake_psql(tmp_path, failures=5)
        runuser_log = install_fake_runuser(tmp_path)
        env = db_env(tmp_path)
        with_recording_alert(tmp_path, env)
        run_probe(env)
        assert psql_log.exists(), "the query must run"
        assert not runuser_log.exists(), (
            "a probe already running as an account peer auth accepts must not "
            "shell out through runuser"
        )


class TestTheHopTargetsAnOsAccountNeverTheRole:
    """`runuser -u <name>` takes an OS ACCOUNT. The database role is not one.

    On this box ``ROBOTHOR_DB_USER`` is a libpq role that ``pg_ident`` maps the
    service user's OS account onto — ``getent passwd`` on it finds nothing. A
    probe that hands the role to ``runuser`` gets "user <role> does not exist"
    on every run: S2 and S6 report UNEVALUATED forever *and* the unit exits
    non-zero, so its ``OnFailure=`` pages hourly while measuring nothing. That
    is worse than the inert state it replaced — a pager that only ever cries
    wolf gets muted, and then the real breach is silent too.
    """

    @staticmethod
    def _hop_env(tmp_path: Path, **extra: str) -> dict[str, str]:
        return db_env(
            tmp_path,
            ROBOTHOR_DB_USER="db_role",
            ROBOTHOR_SLO_OS_USER="svcuser",
            ROBOTHOR_SLO_GETENT_CMD=str(tmp_path / "bin" / "getent"),
            ROBOTHOR_SLO_RUNUSER_CMD=str(tmp_path / "bin" / "runuser"),
            **extra,
        )

    def test_the_hop_becomes_the_os_user_and_carries_the_role_as_pguser(self, tmp_path: Path):
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, failures=5)
        install_fake_id(tmp_path)
        install_fake_getent(tmp_path, "svcuser")
        runuser_log = install_fake_runuser(tmp_path)
        env = self._hop_env(tmp_path)
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        assert runuser_log.exists(), "as root the query must hop, or peer auth rejects it"
        argv = runuser_log.read_text().splitlines()
        assert argv[:2] == ["-u", "svcuser"], (
            f"the hop must target the OS account, not the database role: {argv}"
        )
        assert "PGUSER=db_role" in argv, (
            "the role has to survive the hop in the environment — pg_ident maps "
            f"the OS user onto it: {argv}"
        )
        assert log.exists() and "slo:llm-availability" in log.read_text(), (
            "the hop must deliver a measurement, not merely run"
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_a_missing_os_account_is_unevaluated_and_names_it(self, tmp_path: Path):
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, failures=5)
        install_fake_id(tmp_path)
        install_fake_getent(tmp_path)  # no account exists
        runuser_log = install_fake_runuser(tmp_path)
        env = self._hop_env(tmp_path)
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        output = result.stdout + result.stderr
        assert "UNEVALUATED" in output, output
        assert "svcuser" in output, (
            f"the reason must name the account that is missing, not just fail: {output}"
        )
        assert not runuser_log.exists(), (
            "hopping to an account that does not exist buys nothing but a "
            "confusing error"
        )
        assert not log.exists(), "an unevaluated SLO is not a breach — it must not page"
        assert result.returncode != 0, "an unmeasurable SLO must fail its own unit"

    def test_a_hop_that_fails_is_unevaluated_and_names_the_identity(self, tmp_path: Path):
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, failures=5)
        install_fake_id(tmp_path)
        install_fake_getent(tmp_path, "svcuser")
        # An account that exists but cannot be become (nologin shell, PAM, ...).
        broken = tmp_path / "bin" / "runuser"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("#!/usr/bin/env bash\necho 'runuser: PAM refused' >&2\nexit 1\n")
        broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
        env = self._hop_env(tmp_path)
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        output = result.stdout + result.stderr
        assert "UNEVALUATED" in output, output
        assert "svcuser" in output, f"the reason must name the identity it tried: {output}"
        assert not log.exists()
        assert result.returncode != 0

    def test_no_os_account_configured_is_unevaluated_not_silence(self, tmp_path: Path):
        """Root with nothing to hop to cannot measure S2/S6 at all."""
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, failures=5)
        install_fake_id(tmp_path)
        install_fake_getent(tmp_path, "svcuser")
        # Present and working: the thing missing here is the ACCOUNT to hop to,
        # not the tool that would do the hopping.
        install_fake_runuser(tmp_path)
        env = db_env(
            tmp_path,
            ROBOTHOR_DB_USER="db_role",
            ROBOTHOR_SLO_GETENT_CMD=str(tmp_path / "bin" / "getent"),
            ROBOTHOR_SLO_RUNUSER_CMD=str(tmp_path / "bin" / "runuser"),
        )
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        output = result.stdout + result.stderr
        assert "UNEVALUATED" in output, output
        assert "ROBOTHOR_SLO_OS_USER" in output, (
            f"the reason must name the knob that fixes it: {output}"
        )
        assert not log.exists()
        assert result.returncode != 0

    def test_the_service_user_from_the_env_file_is_the_default_hop_target(self, tmp_path: Path):
        """The unit loads /etc/robothor/robothor.env, which already names the
        OS account. Nothing extra should have to be configured for the hop."""
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path, failures=5)
        install_fake_id(tmp_path)
        install_fake_getent(tmp_path, "svcuser")
        runuser_log = install_fake_runuser(tmp_path)
        env = db_env(
            tmp_path,
            ROBOTHOR_DB_USER="db_role",
            ROBOTHOR_SERVICE_USER="svcuser",
            ROBOTHOR_SLO_GETENT_CMD=str(tmp_path / "bin" / "getent"),
            ROBOTHOR_SLO_RUNUSER_CMD=str(tmp_path / "bin" / "runuser"),
        )
        with_recording_alert(tmp_path, env)

        run_probe(env)

        assert runuser_log.exists(), "ROBOTHOR_SERVICE_USER is the natural default"
        assert runuser_log.read_text().splitlines()[:2] == ["-u", "svcuser"]


class TestTheToolsAreResolvedBeforeAnythingIsMeasured:
    """Every unit loads `EnvironmentFile=/etc/robothor/robothor.env`, and that
    file sets a PATH with **no `/usr/sbin` and no `/sbin`**. `runuser` lives in
    `/usr/sbin`.

    So under systemd the hop resolved to nothing at all, and the failure came
    back as `db_query` returning non-zero — reported as "the query did not
    answer (database unreachable?)". A tool that is not on PATH is not a
    database outage, and a probe that cannot tell those apart sends a page an
    operator cannot act on. The same PATH already made the backup volume guard
    misidentify its own mapper by losing `dmsetup`.
    """

    def test_the_path_is_the_fixed_system_one_and_drops_what_it_inherited(
        self, tmp_path: Path
    ):
        """The unit's inherited PATH begins with a user-writable
        ``~/.local/bin``. This runs hourly AS ROOT, so anything on that PATH
        that shadows `date`, `find`, `grep` or `psql` runs as root — the probe
        must build its PATH from scratch instead of trusting what it is
        handed. ``/usr/local/bin`` stays on it because this instance's
        `rclone` lives there.
        """
        healthy_tree(tmp_path)
        planted = tmp_path / "planted"
        planted.mkdir()
        env = base_env(tmp_path, ROBOTHOR_SLO_SYSTEMCTL_CMD="robothor-not-a-real-systemctl")
        env["PATH"] = f"{planted}:{os.environ['PATH']}"

        result = run_probe(env)

        match = re.search(r"do not resolve on PATH=(\S+)", result.stderr)
        assert match, f"the preflight must report the PATH it searched: {result.stderr}"
        seen = match.group(1)
        assert seen == f"{tmp_path / 'bin'}:{FIXED_PATH}", (
            f"the probe must run on the fixed system PATH, not the inherited one: {seen}"
        )
        assert str(planted) not in seen, (
            "a directory the probe merely inherited must not be searched at all"
        )

    def test_a_binary_planted_on_the_inherited_path_is_never_run(self, tmp_path: Path):
        """Not "the PATH string looks right" — "the planted binary never ran".

        A `date` shim first on the inherited PATH is the whole attack: the
        probe runs `date` on every tier, hourly, as root.
        """
        healthy_tree(tmp_path)
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
        env = base_env(tmp_path)
        env["PATH"] = f"{planted}:{os.environ['PATH']}"
        with_recording_alert(tmp_path, env)

        result = run_probe(env)

        assert not sentinel.exists(), (
            "a binary planted on the inherited PATH was executed by a probe "
            "that runs as root every hour"
        )
        assert result.returncode == 0, result.stdout + result.stderr

    def test_the_identity_check_has_its_own_seam(self, tmp_path: Path):
        """`id` decides whether the probe hops at all, and the fixed PATH
        means a test can no longer shadow it by prepending a directory. The
        seam is how the root path stays testable off a root box."""
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path)
        seams = tmp_path / "seams"
        seams.mkdir()
        fake_id = seams / "id"
        fake_id.write_text(
            "#!/usr/bin/env bash\n"
            'case "${1:-}" in\n'
            "    -u) echo 0 ;;\n"
            "    *) echo root ;;\n"
            "esac\n"
        )
        fake_id.chmod(fake_id.stat().st_mode | stat.S_IEXEC)
        # `seams` is on no PATH the probe builds; only the seam can reach it.
        env = db_env(
            tmp_path,
            ROBOTHOR_SLO_ID_CMD=str(fake_id),
            ROBOTHOR_DB_USER="db_role",
        )
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        output = result.stdout + result.stderr
        assert "ROBOTHOR_SLO_OS_USER" in output, (
            "the seam said uid 0, so the probe must take the root path and "
            f"report that it has nothing to hop to: {output}"
        )
        assert not log.exists(), "a configuration gap is not an SLO breach"

    def test_a_missing_tool_names_itself_and_is_not_an_outage(self, tmp_path: Path):
        """A binary that is not installed must exit loudly naming the binary,
        not be laundered into an UNEVALUATED row about the database."""
        healthy_tree(tmp_path)
        install_fake_psql(tmp_path)
        env = db_env(
            tmp_path,
            ROBOTHOR_SLO_RUNUSER_CMD="robothor-not-a-real-runuser",
            ROBOTHOR_SLO_PSQL_CMD=str(tmp_path / "bin" / "psql"),
        )
        log = with_recording_alert(tmp_path, env)

        result = run_probe(env)

        output = result.stdout + result.stderr
        assert "robothor-not-a-real-runuser" in output, (
            f"the failure must name the tool that is missing: {output}"
        )
        assert result.returncode != 0, "a probe that cannot run must fail its own unit"
        assert not log.exists(), (
            "a missing binary is a misconfiguration, not an SLO breach — it "
            "must not page as one"
        )

    def test_a_probe_that_cannot_find_its_own_directory_says_so(self, tmp_path: Path):
        """`readlink` and `dirname` run BEFORE the preflight, so nothing has
        checked they resolved. `source "${SCRIPT_DIR}/backup-state.sh"` then
        fails, and with `set -uo pipefail` (no `-e`) the probe carries on with
        every marker helper undefined — which is a run that measures a healthy
        volume as an S4 breach and pages for it. A false S4 page is exactly
        the outcome this dead-man exists to be trusted not to produce.
        """
        healthy_tree(tmp_path)
        lonely = tmp_path / "lonely"
        lonely.mkdir()
        shutil.copy(PROBE, lonely / PROBE.name)

        env = base_env(tmp_path)
        log = with_recording_alert(tmp_path, env)

        result = subprocess.run(
            ["bash", str(lonely / PROBE.name)],
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )

        assert result.returncode == 2, (
            f"a probe that cannot read its own helpers must exit 2: {result.stdout + result.stderr}"
        )
        assert "backup-state.sh" in result.stderr, (
            f"the failure must name what it could not read: {result.stderr}"
        )
        assert not log.exists(), "a broken checkout is not an SLO breach and must not page"

    def test_a_missing_tool_stops_the_run_before_anything_is_measured(self, tmp_path: Path):
        """Half a measurement is worse than none: it puts OK rows in the daily
        report for tiers the probe never actually reached."""
        healthy_tree(tmp_path)
        env = base_env(tmp_path, ROBOTHOR_SLO_SYSTEMCTL_CMD="robothor-not-a-real-systemctl")
        with_recording_alert(tmp_path, env)

        result = run_probe(env)

        assert result.returncode != 0
        assert "robothor-not-a-real-systemctl" in result.stdout + result.stderr
        assert "S4 backup freshness" not in result.stdout, (
            "the preflight runs before the first measurement"
        )


class TestTheUnitCanReachTheDatabase:
    """The unit is the other half of the fix: a seam nothing configures is a
    seam that does nothing."""

    def test_the_service_names_the_database_and_the_role(self):
        lines = directives(unit_text("robothor-slo.service"))
        assert "Environment=PGDATABASE=robothor_memory" in lines, (
            "without PGDATABASE psql connects to a database named after the "
            "OS user, which does not exist"
        )
        assert any(line.startswith("Environment=PGUSER=") for line in lines), (
            "the DB-backed SLOs need a role; the template carries the "
            "placeholder account, rendered per instance at install time"
        )

    def test_the_service_names_the_os_account_to_hop_to(self):
        """The role and the OS account are two different things, so the unit
        has to carry both. `runuser -u <role>` fails with "user does not
        exist" and leaves S2/S6 unmeasured while paging every hour."""
        lines = directives(unit_text("robothor-slo.service"))
        assert "Environment=ROBOTHOR_SLO_OS_USER=robothor" in lines, (
            "the unit runs as root; without an OS account to hop to, peer auth "
            "rejects every query. The placeholder is the service account, "
            "rendered per instance at install time."
        )


# ── unit templates ───────────────────────────────────────────────────────────


def unit_text(name: str) -> str:
    path = UNIT_DIR / name
    assert path.exists(), f"infra/systemd/{name} missing"
    return path.read_text()


def directives(text: str) -> list[str]:
    return [line for line in text.splitlines() if line and not line.lstrip().startswith(("#", ";"))]


class TestSloUnits:
    def test_the_units_exist(self):
        unit_text("robothor-slo.service")
        unit_text("robothor-slo.timer")

    def test_service_runs_the_probe(self):
        lines = directives(unit_text("robothor-slo.service"))
        execs = [line for line in lines if line.startswith("ExecStart=")]
        assert len(execs) == 1, execs
        assert "/opt/robothor/scripts/slo_probe.sh" in execs[0]
        assert "Type=oneshot" in lines

    def test_service_allows_the_sender_its_full_retry_budget(self):
        lines = directives(unit_text("robothor-slo.service"))
        timeouts = [line for line in lines if line.startswith("TimeoutStartSec=")]
        assert timeouts, "TimeoutStartSec must be set explicitly"
        assert int(re.sub(r"\D", "", timeouts[0]) or 0) >= 600

    def test_service_pages_if_the_probe_itself_dies(self):
        assert "OnFailure=robothor-alert@%n.service" in directives(
            unit_text("robothor-slo.service")
        )

    def test_service_loads_the_instance_env_and_optional_secrets(self):
        lines = directives(unit_text("robothor-slo.service"))
        assert "EnvironmentFile=/etc/robothor/robothor.env" in lines
        assert "EnvironmentFile=-/run/robothor/secrets.env" in lines

    def test_the_dead_man_does_not_depend_on_the_volume_it_watches(self):
        """RequiresMountsFor= on the backup volume would make systemd refuse to
        start the probe exactly when the volume is gone — the one moment it has
        something to say."""
        text = "\n".join(directives(unit_text("robothor-slo.service")))
        assert "RequiresMountsFor=" not in text
        assert "ExecCondition=" not in text, (
            "an ExecCondition on the backup volume would SKIP the dead-man on a "
            "wedged disk, which is the failure it exists to report"
        )

    def test_timer_probes_hourly_and_from_boot(self):
        lines = directives(unit_text("robothor-slo.timer"))
        active = [line for line in lines if line.startswith("OnUnitActiveSec=")]
        assert active, "OnUnitActiveSec must set the probe interval"
        assert any(line.startswith("OnBootSec=") for line in lines), (
            "without OnBootSec a box that boots into a stale backup tier says nothing"
        )
        assert "WantedBy=timers.target" in lines


# ── shell hygiene ────────────────────────────────────────────────────────────


def test_probe_script_parses():
    result = subprocess.run(["bash", "-n", str(PROBE)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_probe_script_is_shellcheck_clean():
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(PROBE)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_probe_carries_no_instance_paths():
    text = PROBE.read_text()
    for home in re.findall(r"/home/[A-Za-z0-9._-]+", text):
        assert home == "/home/robothor", f"{home} is an instance path"
