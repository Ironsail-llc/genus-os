"""Tests for the OnFailure paging path:

- scripts/send_failure_alert.sh — posts a Telegram message when a systemd
  unit fails (invoked as robothor-alert@<unit>.service).
- scripts/install_onfailure_alerts.sh — installs an OnFailure= drop-in for
  each named unit under a systemd root.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SEND = REPO_ROOT / "scripts" / "send_failure_alert.sh"
INSTALL = REPO_ROOT / "scripts" / "install_onfailure_alerts.sh"
ALERT_UNIT = REPO_ROOT / "infra" / "systemd" / "robothor-alert@.service"


def fake_curl(tmp_path: Path) -> Path:
    """A curl stand-in that records its argv and succeeds."""
    log = tmp_path / "curl-args.txt"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    # Real curl with -w '%{http_code}' always prints a status; a silent double
    # makes a status-checking caller look broken. See TestHttpErrorIsNotDelivery
    # in test_pager_hardening.py for the defect this hid.
    curl.write_text(
        f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{log}"\n'
        "for a in \"$@\"; do [ \"$a\" = '%{http_code}' ] && printf '200'; done\n"
        # Explicit success: the loop's last `[ ... ] && printf` returns 1 when
        # the final arg is not the -w format, and that would become the script's
        # exit status.
        "exit 0\n"
    )
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return log


def fake_curl_failing(tmp_path: Path) -> Path:
    """A curl stand-in that records its argv and fails, like a network outage."""
    log = tmp_path / "curl-args.txt"
    curl = tmp_path / "bin" / "curl"
    curl.parent.mkdir(parents=True, exist_ok=True)
    curl.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{log}"\nexit 1\n')
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC)
    return log


def curl_call_count(log: Path) -> int:
    """Each invocation writes exactly one Telegram API URL arg; count those."""
    if not log.exists():
        return 0
    return log.read_text().count("api.telegram.org")


def stamp_files(tmp_path: Path) -> list[Path]:
    """The cooldown stamp file(s) under this test's isolated state dir.

    Deliberately does not assume a filename shape (sanitized unit name, hash
    suffix, or otherwise) — tests should assert suppression behavior keyed
    by unit name, not the stamp's on-disk naming scheme.
    """
    state_dir = tmp_path / "alert-cooldown"
    if not state_dir.exists():
        return []
    return sorted(p for p in state_dir.iterdir() if p.is_file())


def page_text(log: Path) -> str:
    """The Telegram message body exactly as the sender composed it.

    The fake curl writes one argv entry per line, so the multi-line
    ``text=`` payload spans several lines; the API URL is the final argv
    entry, and nothing follows it.
    """
    raw = log.read_text()
    assert "\ntext=" in raw, f"no text= payload recorded in {raw!r}"
    body = raw.split("\ntext=", 1)[1]
    # Drop the trailing API-URL argv line (and the trailing newline).
    return body.rsplit("\n", 2)[0]


def run_send(
    tmp_path: Path, unit: str, env_extra: dict[str, str], body: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        # Isolate the cooldown state dir per test — the default lives under
        # /run/robothor, which is real and writable on this box, and a test
        # run pointed at it would leave a stamp that could suppress a real
        # page later.
        "ROBOTHOR_ALERT_STATE_DIR": str(tmp_path / "alert-cooldown"),
        # Same reasoning for the backup markers the consequence line quotes:
        # the default dir is real on this box, and a test must not read the
        # operator's actual backup state into an assertion.
        "ROBOTHOR_BACKUP_STATE_DIR": str(tmp_path / "backup-state"),
        # These tests predate the boot-window retry loop and assert on exact
        # curl call counts — pin a single fast attempt so they keep testing
        # what they always tested. The retry behavior itself is covered in
        # tests/test_pager_hardening.py.
        "ROBOTHOR_ALERT_MAX_ATTEMPTS": "1",
        "ROBOTHOR_ALERT_RETRY_DELAY": "0",
    }
    env.update(env_extra)
    argv = ["bash", str(SEND), unit]
    if body is not None:
        argv.append(body)
    return subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)


def test_scripts_exist_and_are_executable():
    for script in (SEND, INSTALL):
        assert script.exists(), f"{script} missing"
        assert script.stat().st_mode & 0o111, f"{script} not executable"


def test_alert_template_unit_exists_and_invokes_sender():
    assert ALERT_UNIT.exists(), "infra/systemd/robothor-alert@.service missing"
    text = ALERT_UNIT.read_text()
    assert "send_failure_alert.sh %i" in text
    # Type=exec, not oneshot: systemd forbids Restart= on oneshot units, and
    # the pager unit must retry itself when a page cannot be delivered
    # (see tests/test_pager_hardening.py::TestAlertUnitRetriesItself).
    assert "Type=exec" in text


def test_send_posts_unit_name_to_telegram(tmp_path: Path):
    log = fake_curl(tmp_path)
    result = run_send(
        tmp_path,
        "robothor-engine.service",
        {"ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123", "ROBOTHOR_TELEGRAM_CHAT_ID": "42"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    args = log.read_text()
    assert "api.telegram.org/bottok123/sendMessage" in args
    assert "robothor-engine.service" in args
    assert "42" in args


def test_send_fails_loudly_without_token(tmp_path: Path):
    fake_curl(tmp_path)
    # Point the secrets lookup at a path that does not exist. The sender now
    # recovers its credentials from /run/robothor/secrets.env when they are absent
    # from the environment (so it can page during a cold-boot failure, when that is
    # the ONLY place the token lives). On the live box that file is real — without
    # this override the test would source the operator's actual credentials and send
    # a genuine Telegram message.
    result = run_send(
        tmp_path,
        "robothor-engine.service",
        {"ROBOTHOR_SECRETS_FILE": str(tmp_path / "no-such-secrets.env")},
    )
    assert result.returncode != 0
    assert "ROBOTHOR_TELEGRAM_BOT_TOKEN" in result.stdout + result.stderr


def test_install_creates_onfailure_dropins(tmp_path: Path):
    root = tmp_path / "systemd"
    result = subprocess.run(
        [
            "bash",
            str(INSTALL),
            "--root",
            str(root),
            "robothor-engine.service",
            "robothor-bridge.service",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for unit in ("robothor-engine.service", "robothor-bridge.service"):
        dropin = root / f"{unit}.d" / "onfailure.conf"
        assert dropin.exists(), f"missing drop-in for {unit}"
        content = dropin.read_text()
        assert "OnFailure=robothor-alert@%n.service" in content


def test_install_is_idempotent(tmp_path: Path):
    root = tmp_path / "systemd"
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(INSTALL), "--root", str(root), "robothor-engine.service"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
    assert (root / "robothor-engine.service.d" / "onfailure.conf").exists()


class TestCooldownDedup:
    """A unit stuck in a crash loop on a short timer must not page every
    invocation — today's incident was a failing job on a 15-minute timer
    paging the operator 96x/day for the same underlying failure.
    """

    ENV = {"ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123", "ROBOTHOR_TELEGRAM_CHAT_ID": "42"}

    def test_first_call_pages(self, tmp_path: Path):
        log = fake_curl(tmp_path)
        result = run_send(tmp_path, "robothor-engine.service", dict(self.ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert curl_call_count(log) == 1

    def test_second_call_within_cooldown_is_suppressed_without_curling(self, tmp_path: Path):
        log = fake_curl(tmp_path)
        env = dict(self.ENV)
        first = run_send(tmp_path, "robothor-engine.service", env)
        assert first.returncode == 0, first.stdout + first.stderr
        assert curl_call_count(log) == 1

        second = run_send(tmp_path, "robothor-engine.service", env)
        assert second.returncode == 0, second.stdout + second.stderr
        assert curl_call_count(log) == 1, (
            "a second page for the same unit within the cooldown must not curl again"
        )
        assert "suppressed duplicate page for robothor-engine.service" in (
            second.stdout + second.stderr
        )

    def test_a_different_unit_is_not_suppressed_by_the_first_units_cooldown(self, tmp_path: Path):
        log = fake_curl(tmp_path)
        env = dict(self.ENV)
        run_send(tmp_path, "robothor-engine.service", env)
        second = run_send(tmp_path, "robothor-bridge.service", env)
        assert second.returncode == 0, second.stdout + second.stderr
        assert curl_call_count(log) == 2

    def test_call_after_cooldown_expiry_pages_again(self, tmp_path: Path):
        log = fake_curl(tmp_path)
        env = dict(self.ENV)
        env["ROBOTHOR_ALERT_COOLDOWN_SECONDS"] = "60"

        first = run_send(tmp_path, "robothor-engine.service", env)
        assert first.returncode == 0, first.stdout + first.stderr
        assert curl_call_count(log) == 1

        stamps = stamp_files(tmp_path)
        assert len(stamps) == 1, "a successful page must leave exactly one cooldown stamp"
        subprocess.run(["touch", "-d", "-120 seconds", str(stamps[0])], check=True)

        second = run_send(tmp_path, "robothor-engine.service", env)
        assert second.returncode == 0, second.stdout + second.stderr
        assert curl_call_count(log) == 2, "a stamp older than the TTL must page again"

    def test_failed_send_does_not_create_a_stamp(self, tmp_path: Path):
        log = fake_curl_failing(tmp_path)
        env = dict(self.ENV)
        result = run_send(tmp_path, "robothor-engine.service", env)
        assert result.returncode != 0
        assert curl_call_count(log) == 1
        assert stamp_files(tmp_path) == [], "a failed send must not suppress the retry"

    def test_units_that_sanitize_identically_do_not_share_a_stamp(self, tmp_path: Path):
        """systemd unit names legally contain ':' and '\\' unescaped in %i
        values (man systemd.unit, systemd-escape) — characters the stamp-key
        sanitizer collapses to '_'. "robothor-backup:primary.service" and
        "robothor-backup_primary.service" sanitize to the same string, so a
        sanitize-only key would let one unit's cooldown suppress the other's
        real, distinct failure.
        """
        log = fake_curl(tmp_path)
        env = dict(self.ENV)
        first = run_send(tmp_path, "robothor-backup:primary.service", env)
        assert first.returncode == 0, first.stdout + first.stderr

        second = run_send(tmp_path, "robothor-backup_primary.service", env)
        assert second.returncode == 0, second.stdout + second.stderr

        assert curl_call_count(log) == 2, (
            "two unit names that sanitize to the same key must not share a "
            "cooldown — each is a distinct unit and must page independently"
        )


class TestConsequenceLine:
    """A page must name the CONSEQUENCE, not just the unit.

    Every page read "🔴 <unit> FAILED on <host>" — a unit name is a fact
    about systemd, not about what the operator has lost. ~50 of them were
    ignored while every backup path was down, because nothing in the text
    distinguished "a log shipper is behind" from "there is no restorable
    copy of the database tonight".
    """

    ENV = {"ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123", "ROBOTHOR_TELEGRAM_CHAT_ID": "42"}

    def page_for(self, tmp_path: Path, unit: str, **env_extra: str) -> str:
        log = fake_curl(tmp_path)
        env = dict(self.ENV)
        env.update(env_extra)
        result = run_send(tmp_path, unit, env)
        assert result.returncode == 0, result.stdout + result.stderr
        return page_text(log)

    def test_wal_offsite_names_the_pitr_recovery_point(self, tmp_path: Path):
        text = self.page_for(tmp_path, "robothor-wal-offsite.service")
        assert "PITR" in text
        assert "15 min" in text

    def test_backup_local_names_the_dump_and_the_rpo_cost(self, tmp_path: Path):
        text = self.page_for(tmp_path, "robothor-backup-local.service")
        assert "dump" in text
        assert "RPO" in text

    def test_backup_offsite_names_what_a_box_loss_would_restore_from(self, tmp_path: Path):
        text = self.page_for(tmp_path, "robothor-backup-offsite.service")
        assert "Offsite NOT refreshed" in text
        assert "box loss" in text

    def test_engine_says_the_agents_are_down(self, tmp_path: Path):
        text = self.page_for(tmp_path, "robothor-engine.service")
        assert "Agents are DOWN" in text

    def test_an_unmapped_unit_says_so_instead_of_inventing_one(self, tmp_path: Path):
        """A wrong consequence is worse than an absent one — the default has
        to read as a gap in the map, and as a chore to close it."""
        text = self.page_for(tmp_path, "robothor-something-new.service")
        assert "no consequence mapped" in text

    def test_the_consequence_is_the_second_line_of_the_page(self, tmp_path: Path):
        """Telegram truncates the preview; the consequence must be visible
        without opening the message."""
        text = self.page_for(tmp_path, "robothor-engine.service")
        lines = text.splitlines()
        assert lines[0].startswith("🔴 robothor-engine.service FAILED on ")
        assert "Agents are DOWN" in lines[1]

    def test_the_marker_file_supplies_the_newest_good_backup(self, tmp_path: Path):
        state = tmp_path / "backup-state"
        state.mkdir()
        (state / "last-local-dump").write_text("2026-08-30T02:15:04+00:00\n")
        text = self.page_for(tmp_path, "robothor-backup-local.service")
        assert "2026-08-30T02:15:04+00:00" in text

    def test_a_missing_marker_says_unknown_rather_than_blank(self, tmp_path: Path):
        """A blank where a timestamp belongs reads as "recent"; it is the
        opposite — nothing has ever succeeded."""
        text = self.page_for(tmp_path, "robothor-backup-offsite.service")
        assert "unknown (no successful run recorded)" in text

    def test_the_offsite_scripts_own_alert_key_is_mapped(self, tmp_path: Path):
        """scripts/backup-offsite.sh pages with "offsite-backup: ..." — the
        map must recognise the key its real caller actually sends, not only
        the systemd unit name."""
        text = self.page_for(tmp_path, "offsite-backup: 2 CORRUPT archives")
        assert "no consequence mapped" not in text
        assert "Offsite NOT refreshed" in text


class TestBodyOverride:
    """``send_failure_alert.sh <key> [body]``: callers that already know what
    went wrong (thermal-guard, boot-guard) can say it, instead of the pager
    tailing a journal for a pseudo-unit that has none."""

    ENV = {"ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123", "ROBOTHOR_TELEGRAM_CHAT_ID": "42"}

    def test_the_body_replaces_the_journal_tail(self, tmp_path: Path):
        log = fake_curl(tmp_path)
        result = run_send(
            tmp_path,
            "robothor-engine.service",
            dict(self.ENV),
            body="Restore drill FAILED: pg_restore exited 1 on the newest dump",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        text = page_text(log)
        assert "Restore drill FAILED: pg_restore exited 1 on the newest dump" in text
        assert "Last journal lines:" not in text

    def test_the_first_argument_is_still_the_dedup_key(self, tmp_path: Path):
        """Two different bodies under one key are one incident, and must
        page once — otherwise a per-tick body defeats the cooldown."""
        log = fake_curl(tmp_path)
        env = dict(self.ENV)
        first = run_send(tmp_path, "thermal-guard", env, body="THERMAL-CRITICAL 96C")
        assert first.returncode == 0, first.stdout + first.stderr
        assert curl_call_count(log) == 1
        second = run_send(tmp_path, "thermal-guard", env, body="THERMAL-CRITICAL 97C")
        assert second.returncode == 0, second.stdout + second.stderr
        assert curl_call_count(log) == 1
        assert "suppressed duplicate page for thermal-guard" in (second.stdout + second.stderr)

    def test_one_argument_callers_still_get_the_journal_tail(self, tmp_path: Path):
        """Backward compatibility: every existing caller passes one argument."""
        log = fake_curl(tmp_path)
        result = run_send(tmp_path, "robothor-engine.service", dict(self.ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        text = page_text(log)
        assert "Last journal lines:" in text
        assert "Check: systemctl status robothor-engine.service" in text

    def test_the_body_keeps_the_header_and_the_consequence(self, tmp_path: Path):
        log = fake_curl(tmp_path)
        result = run_send(tmp_path, "robothor-engine.service", dict(self.ENV), body="custom detail")
        assert result.returncode == 0, result.stdout + result.stderr
        lines = page_text(log).splitlines()
        assert lines[0].startswith("🔴 robothor-engine.service FAILED on ")
        assert "Agents are DOWN" in lines[1]


class TestDeliveryIsAnnounced:
    """A successful send printed nothing, so a cron log carried no evidence
    a page had gone out — only the failures were visible, which made an
    entirely silent pager indistinguishable from a quiet night."""

    ENV = {"ROBOTHOR_TELEGRAM_BOT_TOKEN": "tok123", "ROBOTHOR_TELEGRAM_CHAT_ID": "42"}

    def test_a_delivered_page_is_logged_with_its_http_status(self, tmp_path: Path):
        fake_curl(tmp_path)
        result = run_send(tmp_path, "robothor-engine.service", dict(self.ENV))
        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            "send_failure_alert: delivered page for robothor-engine.service (http 200)"
            in result.stdout + result.stderr
        )

    def test_an_undelivered_page_is_not_announced_as_delivered(self, tmp_path: Path):
        fake_curl_failing(tmp_path)
        result = run_send(tmp_path, "robothor-engine.service", dict(self.ENV))
        assert result.returncode != 0
        assert "delivered page for" not in result.stdout + result.stderr
