"""The daily report has to say what the SLOs actually measured.

WHY THIS SECTION EXISTS

    The guardrail watch already reported guardrail events, run outcomes, drift
    and manifest validity. None of that answers "is this box meeting its
    reliability targets?", so the answer lived nowhere: the backup tier was two
    days stale on 2026-08-27 while every daily report came back clean, because
    no report had an opinion about backup age at all.

    ``scripts/slo_probe.sh`` is the hourly pager for the SLOs that must
    interrupt someone. This is the other half: a daily, non-paging surface for
    ALL of them, plus exactly one ``alert_digest`` row so the operator-facing
    agent's heartbeat carries the breaches without a second page.

THREE PROPERTIES PINNED HERE

  1. THE BACKUP SLO IS MEASURED WITHOUT A DATABASE. It reads the last-good
     markers off NVMe. A database outage is one of the conditions under which
     an operator most needs to know the backup age, so that measurement must
     not be downstream of a connection.

  2. "COULD NOT EVALUATE" IS NOT "OK". A check that has only ever been seen
     staying silent is indistinguishable from one that cannot fire. An SLO
     whose query did not answer is reported as UNEVALUATED, in that word.

  3. ONE DIGEST ROW PER RUN, NOT ONE PER BREACH. Four breached SLOs on a bad
     morning must not become four notification rows racing each other into the
     heartbeat.

Every test pins ``ROBOTHOR_BACKUP_STATE_DIR`` to a tmp_path: the live marker
directory is ``/var/lib/robothor/backup-state`` and this suite must never read
it or be steered by it.
"""

from __future__ import annotations

import datetime as dt
import gzip
import importlib.util
import os
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "guardrail_watch", REPO_ROOT / "scripts" / "guardrail_watch.py"
)
assert _spec is not None and _spec.loader is not None
gw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gw)

MARKERS = ("last-local-dump", "last-offsite-ok", "last-basebackup")


def write_marker(state_dir: Path, name: str, age_hours: float) -> None:
    """scripts/backup-state.sh's own format: ``<date -Is> <identifier>``."""
    state_dir.mkdir(parents=True, exist_ok=True)
    when = dt.datetime.now().astimezone() - dt.timedelta(hours=age_hours)
    (state_dir / name).write_text(f"{when.isoformat(timespec='seconds')} fixture\n")


def fresh_markers(state_dir: Path, age_hours: float = 1) -> Path:
    for name in MARKERS:
        write_marker(state_dir, name, age_hours)
    return state_dir


def write_dump(dump_dir: Path, age_hours: float = 1) -> Path:
    """A dump file the probe's readdir half can find, aged on disk."""
    dump_dir.mkdir(parents=True, exist_ok=True)
    path = dump_dir / "robothor_memory-fixture.sql.gz"
    path.write_bytes(gzip.compress(b"fixture"))
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))
    return path


def probe_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Pin every seam scripts/slo_probe.sh reads.

    The daily surface now RUNS the probe, so an unpinned test would read the
    live backup volume at /mnt/robothor-backup and this box's real units.
    Returns the dump directory.
    """
    dumps = tmp_path / "dumps"
    monkeypatch.setenv("ROBOTHOR_BACKUP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ROBOTHOR_SLO_LOCAL_DUMP_DIR", str(dumps))
    # The basebackup tier falls back to the newest base-* directory when its
    # marker is missing; unpinned, that find() runs against the live volume.
    monkeypatch.setenv("ROBOTHOR_SLO_BASEBACKUP_DIR", str(tmp_path / "basebackup"))
    monkeypatch.setenv("ROBOTHOR_SLO_VOLUME_CHECK_CMD", "/bin/true")
    monkeypatch.setenv("ROBOTHOR_SLO_RCLONE_CMD", "/bin/false")
    monkeypatch.setenv("ROBOTHOR_SLO_SYSTEMCTL_CMD", "/bin/false")
    monkeypatch.setenv("ROBOTHOR_SLO_DB_CHECKS", "0")
    # The daily surface also counts the credential pool and reads the alert
    # unit's journal. Both are live reads on this box, and both would make the
    # verdict depend on how many keys the operator happens to have loaded.
    monkeypatch.setenv("ROBOTHOR_SLO_KEY_POOL_CMD", "/bin/echo 2")
    monkeypatch.setenv("ROBOTHOR_SLO_JOURNALCTL_CMD", "/bin/true")
    # Report mode pages nobody by construction; this is the belt to that
    # braces, because a regression here would page the operator from a test.
    monkeypatch.setenv("ROBOTHOR_SLO_ALERT_CMD", "/bin/true")
    monkeypatch.setenv("ROBOTHOR_ALERT_SUPPRESS", "1")
    return dumps


def healthy_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    dumps = probe_env(monkeypatch, tmp_path)
    fresh_markers(tmp_path)
    write_dump(dumps)
    return dumps


@pytest.fixture(autouse=True)
def _pin_the_slo_marker_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`main()` now stamps ``${ROBOTHOR_SLO_STATE_DIR}/last-guardrail-watch``
    when it finishes, and scripts/slo_probe.sh reads that marker after a reboot
    to decide whether S8 pages.

    Unpinned, a test driving `main()` would write a FRESH marker into the live
    /var/lib/robothor/slo-state — a fixture vouching, for the next 26 hours,
    for a daily report that never ran.
    """
    monkeypatch.setenv("ROBOTHOR_SLO_STATE_DIR", str(tmp_path / "slo-state"))


@pytest.fixture(autouse=True)
def _forbid_live_sibling_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` calls two checks that reach THIS box, and neither belongs in a
    unit test: ``check_flag_truth`` runs the flag audit as a subprocess (up to
    a 180s timeout, against the live flag store) and ``check_instance_doctor``
    shells out to ``instance_doctor.sh``, which reads /etc/systemd/system.

    A test that drives ``main()`` without stubbing them does not fail loudly —
    it quietly takes three minutes and makes its verdict depend on whatever
    state the operator's machine happens to be in this morning. So forgetting
    is made loud here, and every test that wants ``main()`` calls
    ``_stub_sibling_checks`` first. Same guard, same reason, as
    ``tests/test_flag_audit.py``.
    """

    def _forbidden(*args: object, **kwargs: object) -> bool:
        raise AssertionError(
            "this test reached a live-box check (check_flag_truth / "
            "check_instance_doctor) — call _stub_sibling_checks(monkeypatch) "
            "before driving main()"
        )

    monkeypatch.setattr(gw, "check_flag_truth", _forbidden)
    monkeypatch.setattr(gw, "check_instance_doctor", _forbidden)


def _stub_sibling_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the live-box checks to a safe pass, matching each real
    signature. Call this first, then override whatever this test targets."""
    monkeypatch.setattr(gw, "check_flag_truth", lambda **kw: True)
    monkeypatch.setattr(gw, "check_instance_doctor", lambda script=None: True)
    # The nag sender is a live-box check too — it POSTs to api.telegram.org
    # with whatever credentials are in the environment, with no spool, no
    # cooldown and no API-base seam to redirect. The same-named helper in the
    # three sibling files stubs it; this copy had lost it, so every test here
    # that drives main() ran the real sender.
    monkeypatch.setattr(gw, "send_telegram", lambda text: False)


def by_name(slos: list, needle: str):
    matches = [s for s in slos if needle in s.name]
    assert matches, f"no SLO whose name contains {needle!r} in {[s.name for s in slos]}"
    return matches[0]


# ── S4 without a database ────────────────────────────────────────────────────


class TestBackupFreshnessIsMeasuredWithoutADatabase:
    def test_fresh_markers_are_all_ok(self, tmp_path: Path) -> None:
        slos = gw.backup_freshness_slos(fresh_markers(tmp_path))
        assert len(slos) == 3, [s.name for s in slos]
        assert {s.status for s in slos} == {"OK"}

    def test_a_stale_local_dump_breaches(self, tmp_path: Path) -> None:
        fresh_markers(tmp_path)
        write_marker(tmp_path, "last-local-dump", age_hours=27)
        slo = by_name(gw.backup_freshness_slos(tmp_path), "local dump")
        assert slo.status == "BREACH"
        assert "27" in slo.measured, "the measurement must be the AGE, not a boolean"
        assert "26" in slo.target, "the budget it was measured against must be reported"

    def test_the_basebackup_tier_has_its_own_wider_budget(self, tmp_path: Path) -> None:
        """Weekly, not nightly: 30h is fine, 9 days is not."""
        fresh_markers(tmp_path)
        write_marker(tmp_path, "last-basebackup", age_hours=30)
        assert by_name(gw.backup_freshness_slos(tmp_path), "basebackup").status == "OK"

        write_marker(tmp_path, "last-basebackup", age_hours=24 * 9)
        assert by_name(gw.backup_freshness_slos(tmp_path), "basebackup").status == "BREACH"

    def test_a_marker_that_was_never_written_is_a_breach_not_an_ok(self, tmp_path: Path) -> None:
        """An absent marker reads as "recent" to anything that only checks for
        a non-empty string. It means the opposite: no run ever succeeded."""
        fresh_markers(tmp_path)
        (tmp_path / "last-offsite-ok").unlink()
        slo = by_name(gw.backup_freshness_slos(tmp_path), "offsite")
        assert slo.status == "BREACH"
        assert "no successful run" in slo.measured.lower()

    def test_an_unparseable_marker_is_a_breach_not_an_ok(self, tmp_path: Path) -> None:
        """A truncated or corrupt marker is evidence of nothing. Reading it as
        fresh would be the stat()-guard mistake in a different disguise."""
        fresh_markers(tmp_path)
        (tmp_path / "last-local-dump").write_text("not-a-timestamp whatever\n")
        assert by_name(gw.backup_freshness_slos(tmp_path), "local dump").status == "BREACH"

    def test_the_state_dir_default_matches_backup_state_sh(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One marker directory, named the same way in the shell and in Python.
        A second spelling of the default is a guard that silently watches an
        empty directory and reports it as "no successful run"."""
        shell = (REPO_ROOT / "scripts" / "backup-state.sh").read_text()
        assert f"ROBOTHOR_BACKUP_STATE_DIR:-{gw.BACKUP_STATE_DIR_DEFAULT}" in shell
        assert not gw.BACKUP_STATE_DIR_DEFAULT.startswith("/home/")

        # ...and the env var overrides it, so nothing here reads the live dir.
        probe_env(monkeypatch, tmp_path)
        monkeypatch.setenv("ROBOTHOR_BACKUP_STATE_DIR", str(fresh_markers(tmp_path)))
        assert {s.status for s in gw.backup_freshness_slos()} == {"OK"}


# ── one implementation, two surfaces ─────────────────────────────────────────


class TestTheDailySurfaceRunsTheProbe:
    """The daily report used to read the markers ONLY, while the hourly pager
    took the worse of (marker, newest file) and added a readdir plus a volume
    probe. The 2026-08-27 volume drop is precisely the state those two answer
    differently: the markers live on NVMe and stay fresh forever, so the daily
    report said OK while the pager said BREACH."""

    @pytest.fixture(autouse=True)
    def _skip_as_root(self) -> None:
        if os.geteuid() == 0:
            pytest.skip("root ignores directory permissions; the EIO case cannot be staged")

    def test_an_unreadable_dump_directory_breaches_the_daily_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        dumps = healthy_tree(monkeypatch, tmp_path)
        dumps.chmod(0o000)
        try:
            slos = gw.probe_report_slos()
            slo = by_name(slos, "local dump")
            assert slo.status == "BREACH", (
                "a fresh marker on NVMe must not vouch for a dump directory "
                "nobody can read — that is the outage, reported as health"
            )
        finally:
            dumps.chmod(0o755)

    def test_an_unusable_volume_breaches_the_daily_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """backup-volume-check.sh exits 1 for "not usable", which makes the
        backup units SKIP. The daily report had no opinion about it at all."""
        healthy_tree(monkeypatch, tmp_path)
        monkeypatch.setenv("ROBOTHOR_SLO_VOLUME_CHECK_CMD", "/bin/false")
        assert by_name(gw.probe_report_slos(), "volume").status == "BREACH"

    def test_a_healthy_tree_is_all_ok(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        healthy_tree(monkeypatch, tmp_path)
        slos = gw.probe_report_slos()
        backup = [s for s in slos if "S4" in s.name]
        assert backup and {s.status for s in backup} == {"OK"}, [
            (s.name, s.status, s.measured) for s in slos
        ]

    def test_the_report_mode_pages_nobody(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A daily surface that pages would double every S4 page the hourly
        probe already sends."""
        healthy_tree(monkeypatch, tmp_path)
        (tmp_path / "last-local-dump").unlink()
        (tmp_path / "dumps" / "robothor_memory-fixture.sql.gz").unlink()
        alert_log = tmp_path / "would-have-paged.txt"
        alert = tmp_path / "fake-alert.sh"
        alert.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" >> "{alert_log}"\n')
        alert.chmod(0o755)
        monkeypatch.setenv("ROBOTHOR_SLO_ALERT_CMD", str(alert))

        assert by_name(gw.probe_report_slos(), "local dump").status == "BREACH"
        assert not alert_log.exists(), "--report measures; it must never interrupt anyone"

    def test_a_missing_probe_falls_back_to_the_markers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A daily report that goes silent because a shell script moved is the
        inert-control failure again. Degrade to the marker-only reading and
        say so."""
        healthy_tree(monkeypatch, tmp_path)
        slos = gw.probe_report_slos(tmp_path / "no-such-probe.sh")
        assert by_name(slos, "local dump").status == "OK"
        assert "fall" in capsys.readouterr().out.lower()


class TestABudgetIsOneEnvVarForBothSurfaces:
    """docs/runbooks/SLOS.md claimed budgets were environment variables read by
    both surfaces. Only the shell probe read them; the daily report carried its
    own hardcoded tuple, so a budget changed per the runbook moved one surface
    and not the other."""

    def test_one_env_var_moves_the_probe_and_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        healthy_tree(monkeypatch, tmp_path)
        write_marker(tmp_path, "last-local-dump", age_hours=5)
        write_dump(tmp_path / "dumps", age_hours=5)

        assert by_name(gw.probe_report_slos(), "local dump").status == "OK"
        assert by_name(gw.backup_freshness_slos(tmp_path), "local dump").status == "OK"

        monkeypatch.setenv("ROBOTHOR_SLO_LOCAL_DUMP_MAX_HOURS", "4")

        assert by_name(gw.probe_report_slos(), "local dump").status == "BREACH", (
            "the hourly probe must honour the budget from the environment"
        )
        fallback = by_name(gw.backup_freshness_slos(tmp_path), "local dump")
        assert fallback.status == "BREACH", (
            "so must the daily surface, under the SAME variable name — a "
            "budget set per the runbook that moves only one is the defect"
        )
        assert "4" in fallback.target

    def test_the_offsite_and_basebackup_budgets_are_env_driven_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        fresh_markers(tmp_path, age_hours=5)
        monkeypatch.setenv("ROBOTHOR_SLO_OFFSITE_MAX_HOURS", "4")
        monkeypatch.setenv("ROBOTHOR_SLO_BASEBACKUP_MAX_HOURS", "4")
        slos = gw.backup_freshness_slos(tmp_path)
        assert by_name(slos, "offsite").status == "BREACH"
        assert by_name(slos, "basebackup").status == "BREACH"

    def test_a_nonsense_budget_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A typo in robothor.env must not silently make every backup fresh."""
        fresh_markers(tmp_path, age_hours=100)
        monkeypatch.setenv("ROBOTHOR_SLO_LOCAL_DUMP_MAX_HOURS", "twenty-six")
        assert by_name(gw.backup_freshness_slos(tmp_path), "local dump").status == "BREACH"


# ── the halves of S2, S3 and S6 that were never measured ─────────────────────


def install_fake_journalctl(tmp_path: Path, lines: list[str], exit_code: int = 0) -> Path:
    script = tmp_path / "bin" / "journalctl"
    script.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"echo {line!r}\n" for line in lines)
    script.write_text(f"#!/usr/bin/env bash\n{body}exit {exit_code}\n")
    script.chmod(0o755)
    return script


class TestTheHeartbeatDeliveryLag:
    """S2 is "heartbeat delivery", and a heartbeat delivered ten minutes late
    is a briefing nobody acted on. Only the delivered COUNT was measured."""

    def test_a_lag_of_a_minute_breaches(self) -> None:
        slo = gw.heartbeat_slo(delivered=12, beats=12, worst_lag_seconds=60.0)
        assert slo.status == "BREACH"
        assert "60" in slo.measured

    def test_a_lag_inside_a_minute_is_ok(self) -> None:
        assert gw.heartbeat_slo(delivered=12, beats=12, worst_lag_seconds=3.0).status == "OK"

    def test_an_unknown_lag_does_not_invent_a_breach(self) -> None:
        """Nothing delivered in the window yet is not evidence of a slow one."""
        assert gw.heartbeat_slo(delivered=12, beats=12, worst_lag_seconds=None).status == "OK"

    def test_zero_beats_still_breaches_however_fast_delivery_was(self) -> None:
        assert gw.heartbeat_slo(delivered=0, beats=0, worst_lag_seconds=None).status == "BREACH"

    def test_the_target_names_the_lag_budget(self) -> None:
        assert "60s" in gw.heartbeat_slo(delivered=1, beats=1, worst_lag_seconds=1.0).target


class TestThePagerDeliveryJournalHalf:
    """S3 counted alert_fallback rows only. The sender logs a page it could not
    deliver to the journal, and a page lost before any row is written is the
    one that matters most — 432 alerts once went nowhere exactly that way."""

    def test_journal_failures_count_toward_the_breach(self) -> None:
        slo = gw.pager_slo(fallback_rows=0, journal_failures=3)
        assert slo.status == "BREACH"
        assert "3" in slo.measured and "journal" in slo.measured.lower()

    def test_a_clean_pager_is_ok(self) -> None:
        assert gw.pager_slo(fallback_rows=0, journal_failures=0).status == "OK"

    def test_an_unreadable_journal_says_unknown_rather_than_zero(self) -> None:
        slo = gw.pager_slo(fallback_rows=0, journal_failures=None)
        assert "unknown" in slo.measured.lower()
        assert slo.status == "OK", "an unreadable journal is not itself a lost page"

    def test_the_journal_is_read_through_a_seam(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        journal = install_fake_journalctl(
            tmp_path,
            [
                "robothor-alert: failed to send after 3 attempts",
                "robothor-alert: sent",
                "robothor-alert: failed to send (HTTP 401)",
            ],
        )
        monkeypatch.setenv("ROBOTHOR_SLO_JOURNALCTL_CMD", str(journal))
        assert gw.alert_journal_failures() == 2

    def test_a_journalctl_that_cannot_answer_returns_none(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_SLO_JOURNALCTL_CMD", str(tmp_path / "no-such-journalctl"))
        assert gw.alert_journal_failures() is None, (
            "a journal that could not be read is unknown, never zero — zero is "
            "the answer that says the pager is healthy"
        )


class TestTheCredentialPoolSize:
    """One capped OpenRouter key took the whole fleet down on 2026-08-27 —
    every model shares the pool — and the spare slot was empty. A pool of one
    is a single point of failure nothing had ever counted."""

    def test_a_pool_of_one_breaches_the_daily_report(self) -> None:
        slo = gw.credential_pool_slo(1)
        assert slo.status == "BREACH"
        assert "pool size 1" in slo.measured

    def test_a_pool_of_two_is_ok(self) -> None:
        assert gw.credential_pool_slo(2).status == "OK"

    def test_an_uncountable_pool_is_unevaluated_not_ok(self) -> None:
        assert gw.credential_pool_slo(None).status == "UNEVALUATED"

    def test_the_pool_is_counted_through_a_seam(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        script = tmp_path / "bin" / "count-keys"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("#!/usr/bin/env bash\necho 4\n")
        script.chmod(0o755)
        monkeypatch.setenv("ROBOTHOR_SLO_KEY_POOL_CMD", str(script))
        assert gw.credential_pool_size() == 4

    def test_the_default_command_actually_counts_the_key_pool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command is run, not quoted: an invocation nobody executes is
        how a control ends up inert with a green test beside it."""
        monkeypatch.delenv("ROBOTHOR_SLO_KEY_POOL_CMD", raising=False)
        monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-key-1")
        monkeypatch.setenv("OPENROUTER_API_KEY_2", "fixture-key-2")
        monkeypatch.delenv("OPENROUTER_API_KEY_3", raising=False)
        assert gw.credential_pool_size() == 2

    def test_the_pool_size_is_a_digest_line_and_never_a_page(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_dump(probe_env(monkeypatch, tmp_path))
        fresh_markers(tmp_path)
        sent: list[str] = []
        monkeypatch.setattr(gw, "send_telegram", lambda text: sent.append(text) or True)
        monkeypatch.setattr(gw, "credential_pool_size", lambda: 1)

        slos = gw.check_slos()

        assert by_name(slos, "credential pool").status == "BREACH"
        assert "pool size 1" in capsys.readouterr().out
        assert sent == [], "the pool size is a digest line, not a page"


# ── the report ───────────────────────────────────────────────────────────────


class TestTheReportShowsMeasurementAgainstTarget:
    def test_every_line_carries_the_measurement_and_the_target(self, tmp_path: Path) -> None:
        fresh_markers(tmp_path)
        write_marker(tmp_path, "last-local-dump", age_hours=40)
        report = gw.format_slo_report(gw.backup_freshness_slos(tmp_path))
        line = next(line for line in report.splitlines() if "local dump" in line)
        assert "40" in line and "26" in line and "BREACH" in line

    def test_a_clean_report_says_ok_rather_than_saying_nothing(self, tmp_path: Path) -> None:
        report = gw.format_slo_report(gw.backup_freshness_slos(fresh_markers(tmp_path)))
        assert "OK" in report


# ── one digest row per run ───────────────────────────────────────────────────


class TestOneDigestRowPerRun:
    @staticmethod
    def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str]]:
        written: list[tuple[str, str]] = []
        # check_slos() now RUNS scripts/slo_probe.sh, so every seam it reads
        # has to be pinned here too — an unpinned run would readdir the live
        # backup volume at /mnt/robothor-backup.
        write_dump(probe_env(monkeypatch, tmp_path))
        monkeypatch.setattr(gw, "send_telegram", lambda text: False)
        monkeypatch.setattr(
            gw, "write_slo_digest", lambda subject, body: (written.append((subject, body)), True)[1]
        )
        monkeypatch.setattr(gw, "db_slos", lambda: [])
        return written

    def test_two_breaches_write_exactly_one_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        written = self._isolate(monkeypatch, tmp_path)
        fresh_markers(tmp_path)
        write_marker(tmp_path, "last-local-dump", age_hours=27)
        write_marker(tmp_path, "last-offsite-ok", age_hours=27)

        gw.check_db_slos(gw.check_slos())

        assert len(written) == 1, (
            "four breached SLOs on a bad morning must not become four "
            "notification rows racing into the heartbeat"
        )
        subject, body = written[0]
        assert "SLO" in subject
        assert "local dump" in body and "offsite" in body
        assert "=== SLOs (database-free) ===" in capsys.readouterr().out

    def test_a_clean_run_writes_no_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        written = self._isolate(monkeypatch, tmp_path)
        fresh_markers(tmp_path)
        gw.check_db_slos(gw.check_slos())
        assert written == [], "a clean run must not put noise in the heartbeat"

    def test_the_section_prints_even_when_everything_is_fine(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A section that only appears on breach cannot be distinguished from a
        section that stopped running."""
        self._isolate(monkeypatch, tmp_path)
        fresh_markers(tmp_path)
        gw.check_slos()
        out = capsys.readouterr().out
        assert "=== SLOs (database-free) ===" in out and "local dump" in out


# ── unevaluated is not OK ────────────────────────────────────────────────────


class TestUnevaluatedIsNotOk:
    def test_a_database_outage_does_not_hide_the_backup_slos(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write_dump(probe_env(monkeypatch, tmp_path))
        monkeypatch.setattr(gw, "send_telegram", lambda text: False)
        monkeypatch.setattr(gw, "write_slo_digest", lambda subject, body: True)
        monkeypatch.setattr(
            "robothor.db.connection.get_connection",
            lambda autocommit=False: (_ for _ in ()).throw(RuntimeError("postgres is down")),
        )
        fresh_markers(tmp_path)

        gw.check_db_slos(gw.check_slos())  # must not raise

        out = capsys.readouterr().out
        assert "local dump" in out, (
            "the backup SLO reads markers off NVMe — a DB outage is exactly "
            "when the operator needs it, so it must not be downstream of one"
        )
        assert "UNEVALUATED" in out, (
            "an SLO whose query did not answer is not a passing SLO; say so in "
            "that word rather than omitting the row"
        )

    def test_unevaluated_slos_do_not_count_as_breaches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """They are neither OK nor a page — reporting them as breaches would
        make a DB blip page about backups."""
        written: list = []
        write_dump(probe_env(monkeypatch, tmp_path))
        monkeypatch.setattr(gw, "send_telegram", lambda text: False)
        monkeypatch.setattr(
            gw, "write_slo_digest", lambda subject, body: (written.append(body), True)[1]
        )
        monkeypatch.setattr(
            "robothor.db.connection.get_connection",
            lambda autocommit=False: (_ for _ in ()).throw(RuntimeError("postgres is down")),
        )
        fresh_markers(tmp_path)
        gw.check_db_slos(gw.check_slos())
        assert written == []


# ── the DB-backed SLOs belong to the DB section ──────────────────────────────


class TestTheDatabaseBackedSlosRunInTheDatabaseSection:
    """check_slos() ran five SQL queries from main()'s DB-FREE section — the
    section that exists because on 2026-08-16 a DB-dependent call raising took
    the drift checks down with it. A database that hangs there stalls the
    manifest validation that follows, and manifest validation is what caught
    the YAML typo that deleted the primary agent for 3h48m."""

    @staticmethod
    def _quiet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        write_dump(probe_env(monkeypatch, tmp_path))
        fresh_markers(tmp_path)
        _stub_sibling_checks(monkeypatch)
        monkeypatch.setattr(gw, "send_telegram", lambda text: False)
        monkeypatch.setattr(gw, "check_soak_deadlines", lambda: None)
        monkeypatch.setattr(gw, "check_dropin_drift", lambda: None)
        monkeypatch.setattr(gw, "check_host_script_drift", lambda pairs=None: None)
        monkeypatch.setattr(gw, "write_slo_digest", lambda subject, body: True)
        monkeypatch.setattr(
            gw, "check_instance_manifests", lambda: (print("SENTINEL-MANIFESTS"), True)[1]
        )

    def test_the_db_backed_slos_run_after_the_manifest_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._quiet(monkeypatch, tmp_path)
        monkeypatch.setattr(gw, "db_slos", lambda: (print("SENTINEL-DB-SLOS"), [])[1])
        monkeypatch.setattr(
            "robothor.db.connection.get_connection",
            lambda autocommit=False: (_ for _ in ()).throw(RuntimeError("postgres is down")),
        )

        exit_code = gw.main()

        out = capsys.readouterr().out
        assert "local dump" in out, "the DB-free SLO rows must still be reported"
        assert out.index("local dump") < out.index("SENTINEL-MANIFESTS")
        assert out.index("SENTINEL-MANIFESTS") < out.index("SENTINEL-DB-SLOS"), (
            "the SLO queries must run in the DB section, after the manifest "
            "check — not inside the DB-free half where a hang stalls it"
        )
        assert exit_code != 0

    def test_a_database_that_cannot_answer_does_not_stall_the_manifest_check(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._quiet(monkeypatch, tmp_path)
        monkeypatch.setattr(
            gw,
            "db_slos",
            lambda: (_ for _ in ()).throw(RuntimeError("connection timed out after 30s")),
        )
        monkeypatch.setattr(
            "robothor.db.connection.get_connection",
            lambda autocommit=False: (_ for _ in ()).throw(RuntimeError("postgres is down")),
        )

        exit_code = gw.main()

        out = capsys.readouterr().out
        assert "SENTINEL-MANIFESTS" in out, (
            "the fleet's manifests must be validated even when the database "
            "never answers — that check needs no database at all"
        )
        assert "local dump" in out
        assert exit_code != 0
        assert "connection timed out" in out, "the actual failure must be visible"


# ── main() ordering ──────────────────────────────────────────────────────────


class TestMainRunsTheSloSectionBeforeTheDatabaseSection:
    def test_the_slo_section_survives_a_database_outage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same discipline as the drift checks (2026-08-16): the DB-free work
        must already have produced output before the DB section can abort."""
        _stub_sibling_checks(monkeypatch)
        monkeypatch.setattr(gw, "send_telegram", lambda text: False)
        monkeypatch.setattr(gw, "check_soak_deadlines", lambda: None)
        monkeypatch.setattr(gw, "check_dropin_drift", lambda: None)
        monkeypatch.setattr(gw, "check_host_script_drift", lambda pairs=None: None)
        monkeypatch.setattr(gw, "check_instance_manifests", lambda: True)
        monkeypatch.setattr(gw, "check_slos", lambda: print("SENTINEL-SLOS-OK"))
        monkeypatch.setattr(
            "robothor.db.connection.get_connection",
            lambda autocommit=False: (_ for _ in ()).throw(RuntimeError("postgres is down")),
        )

        exit_code = gw.main()

        out = capsys.readouterr().out
        assert "SENTINEL-SLOS-OK" in out, "the SLO section must run before the DB section"
        assert out.index("SENTINEL-SLOS-OK") < out.index("DATABASE")
        assert exit_code != 0


# ── the runbook and the code say the same thing ──────────────────────────────


class TestTheRunbookMatchesTheCode:
    """A runbook that describes behaviour the code does not have is worse than
    no runbook: it is read at 3am, by someone deciding whether silence means
    healthy. SLOS.md said the daily surface "leaves one `alert_digest` row
    covering both" — full stop — while check_db_slos() writes a row only when
    something breached. An operator checking for the row on a clean morning
    would find none and conclude the report had stopped running."""

    SLOS_MD = REPO_ROOT / "docs" / "runbooks" / "SLOS.md"

    def test_a_clean_run_writes_no_digest_row_and_the_runbook_says_so(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        written: list = []
        write_dump(probe_env(monkeypatch, tmp_path))
        fresh_markers(tmp_path)
        monkeypatch.setattr(gw, "send_telegram", lambda text: False)
        monkeypatch.setattr(gw, "db_slos", lambda: [])
        monkeypatch.setattr(
            gw, "write_slo_digest", lambda subject, body: (written.append(body), True)[1]
        )

        gw.check_db_slos(gw.check_slos())

        assert written == [], "the behaviour under test: no breach, no row"
        text = self.SLOS_MD.read_text()
        assert "leaves one `alert_digest` row covering both. |" not in text, (
            "the summary table claims a row every run; the code writes one "
            "only when something breached"
        )
        assert "only when something breached" in text, (
            "SLOS.md must state the condition, not just the row"
        )

    def test_the_test_only_database_mute_is_documented(self) -> None:
        """`ROBOTHOR_SLO_DB_CHECKS=0` retires half the dead-man in silence.
        A knob like that has to be findable in the runbook by name."""
        text = self.SLOS_MD.read_text()
        assert "ROBOTHOR_SLO_DB_CHECKS" in text, (
            "an undocumented mute is one nobody knows to look for when S2 and "
            "S6 have been quiet for a month"
        )
        window = text[text.index("ROBOTHOR_SLO_DB_CHECKS") - 400 :]
        assert "never" in window and "production" in window, (
            "the runbook must say this is test-only and must never be set in "
            "production"
        )


# ── the S8 marker: the run history systemd forgets ───────────────────────────


class TestMainStampsTheS8Marker:
    """S8 asked systemd when this unit last exited. `ExecMainExitTimestamp` is
    per-unit RUNTIME state and a reboot empties it, so at 03:01 on
    2026-09-03 — hours after a reboot — the hourly probe paged

        S8 BREACHED: robothor-guardrail-watch.service has no completed run on
        this box.

    while the 08:30 run the day before had finished cleanly. The report's own
    history therefore has to outlive systemd's memory of it, the same way the
    backup jobs' last-good markers outlive a wedged volume: one line on NVMe,
    written on the run's last step.

    Two properties, and the second matters as much as the first: stamping is
    BOOKKEEPING. A report that genuinely ran and found nothing must never come
    back as a failed unit because /var/lib was read-only — that would page the
    operator about the pager.
    """

    @staticmethod
    def _quiet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Every sibling check stubbed: this class is about the marker."""
        write_dump(probe_env(monkeypatch, tmp_path))
        fresh_markers(tmp_path)
        _stub_sibling_checks(monkeypatch)
        monkeypatch.setattr(gw, "check_soak_deadlines", lambda: None)
        monkeypatch.setattr(gw, "check_dropin_drift", lambda: None)
        monkeypatch.setattr(gw, "check_host_script_drift", lambda pairs=None: None)
        monkeypatch.setattr(gw, "check_slos", lambda: [])
        monkeypatch.setattr(gw, "check_instance_manifests", lambda: True)
        monkeypatch.setattr(gw, "write_slo_digest", lambda subject, body: True)
        monkeypatch.setattr(gw, "db_slos", lambda: [])
        monkeypatch.setattr(
            "robothor.db.connection.get_connection",
            lambda autocommit=False: (_ for _ in ()).throw(RuntimeError("postgres is down")),
        )

    def test_a_clean_run_writes_the_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._quiet(monkeypatch, tmp_path)

        gw.main()

        marker = tmp_path / "slo-state" / "last-guardrail-watch"
        assert marker.exists(), (
            "the report finished, so S8 must be able to prove it ran after the "
            "next reboot empties ExecMainExitTimestamp"
        )
        stamp = marker.read_text().split()[0]
        when = dt.datetime.fromisoformat(stamp)
        assert when.tzinfo is not None, (
            "the timestamp must carry its UTC offset — a bare local time reads "
            "as hours of drift the moment the box changes zone"
        )
        age = (dt.datetime.now().astimezone() - when).total_seconds()
        assert -5 < age < 120, f"the marker must record NOW, not {stamp}"

    def test_the_marker_directory_is_created_when_it_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """First boot after an install: tmpfiles has not run yet, or the path
        was never created. A marker that needs a directory nobody made is a
        marker that is never written."""
        self._quiet(monkeypatch, tmp_path)
        state = tmp_path / "never-created" / "slo-state"
        monkeypatch.setenv("ROBOTHOR_SLO_STATE_DIR", str(state))

        gw.main()

        assert (state / "last-guardrail-watch").exists()

    def test_an_unwritable_marker_directory_does_not_change_the_exit_code(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Bookkeeping never fails its caller — scripts/backup-state.sh's own
        contract. A read-only /var/lib must not turn a healthy report into a
        failed unit, which is an OnFailure= page about nothing."""
        self._quiet(monkeypatch, tmp_path)
        monkeypatch.setattr(gw, "check_instance_manifests", lambda: True)
        blocked = tmp_path / "blocked"
        blocked.write_text("this is a file, not a directory")
        monkeypatch.setenv("ROBOTHOR_SLO_STATE_DIR", str(blocked / "slo-state"))

        with_marker = gw.main()

        monkeypatch.setenv("ROBOTHOR_SLO_STATE_DIR", str(tmp_path / "slo-state"))
        without_marker = gw.main()

        assert with_marker == without_marker, (
            "a marker that could not be written changed the run's verdict — "
            "that is the bookkeeping-fails-the-backup bug, moved"
        )
        assert "WARNING" in capsys.readouterr().out, (
            "a marker that silently fails to write is an S8 that pages after "
            "the next reboot with nobody knowing why"
        )

    def test_a_run_that_died_halfway_leaves_no_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The marker means "this report reached its end". A run killed by an
        exception did not, and must not leave evidence that it did — S8 would
        then read a crash loop as a healthy daily report."""
        self._quiet(monkeypatch, tmp_path)
        monkeypatch.setattr(
            gw,
            "check_instance_manifests",
            lambda: (_ for _ in ()).throw(RuntimeError("killed mid-report")),
        )

        with pytest.raises(RuntimeError):
            gw.main()

        assert not (tmp_path / "slo-state" / "last-guardrail-watch").exists()

    def test_a_run_with_findings_still_counts_as_having_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Exit 1 is the by-design findings exit, and S8's whole question is
        whether the report RAN — not whether it liked what it found."""
        self._quiet(monkeypatch, tmp_path)
        # The DB section stubbed out entirely, so the 1 below can only come
        # from the findings — not from the partial-report exit.
        monkeypatch.setattr(gw, "_run_db_dependent_checks", lambda slos: None)
        monkeypatch.setattr(gw, "check_instance_manifests", lambda: False)

        exit_code = gw.main()

        assert exit_code == 1
        assert (tmp_path / "slo-state" / "last-guardrail-watch").exists(), (
            "a report that found problems is still a report that ran"
        )
