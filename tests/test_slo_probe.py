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


def write_dump(dump_dir: Path, age_hours: float, name: str = "robothor_memory-fixture.sql.gz"):
    dump_dir.mkdir(parents=True, exist_ok=True)
    path = dump_dir / name
    path.write_bytes(b"fixture")
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))
    return path


def healthy_tree(tmp_path: Path, age_hours: float = 1) -> None:
    """A backup tier that is entirely within budget."""
    write_all_markers(tmp_path / "backup-state", age_hours)
    write_dump(tmp_path / "dumps", age_hours)


# ── the environment ──────────────────────────────────────────────────────────


def base_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    """Hermetic: no live volume, no live marker dir, no live cooldown state,
    no real Telegram endpoint, no database."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ROBOTHOR_")}
    env.update(
        {
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            # The two live paths this probe would otherwise read.
            "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "backup-state"),
            "ROBOTHOR_SLO_LOCAL_DUMP_DIR": str(tmp_path / "dumps"),
            # Seams: no volume probe, no rclone, no psql by default.
            "ROBOTHOR_SLO_VOLUME_CHECK_CMD": "/bin/true",
            "ROBOTHOR_SLO_RCLONE_CMD": "/bin/false",
            "ROBOTHOR_SLO_DB_CHECKS": "0",
            # Sender isolation — a stamp written into the real /run/robothor
            # cooldown dir by a test could suppress a REAL page later.
            "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
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


def with_recording_alert(tmp_path: Path, env: dict[str, str]) -> Path:
    log = install_recording_alert(tmp_path)
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

    def test_an_unreadable_directory_pages_even_when_the_marker_looks_fresh(
        self, tmp_path: Path
    ):
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
        with_recording_alert(tmp_path, env)
        install_recording_alert(tmp_path, exit_code=1)
        result = run_probe(env)
        assert result.returncode != 0
        assert "not delivered" in (result.stdout + result.stderr).lower()


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
        assert "OnFailure=robothor-alert@%n.service" in directives(unit_text("robothor-slo.service"))

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
