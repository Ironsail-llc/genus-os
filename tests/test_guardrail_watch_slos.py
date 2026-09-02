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
import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
        monkeypatch.setenv("ROBOTHOR_BACKUP_STATE_DIR", str(fresh_markers(tmp_path)))
        assert {s.status for s in gw.backup_freshness_slos()} == {"OK"}


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
        monkeypatch.setenv("ROBOTHOR_BACKUP_STATE_DIR", str(tmp_path))
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

        gw.check_slos()

        assert len(written) == 1, (
            "four breached SLOs on a bad morning must not become four "
            "notification rows racing into the heartbeat"
        )
        subject, body = written[0]
        assert "SLO" in subject
        assert "local dump" in body and "offsite" in body
        assert "=== SLOs ===" in capsys.readouterr().out

    def test_a_clean_run_writes_no_row(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        written = self._isolate(monkeypatch, tmp_path)
        fresh_markers(tmp_path)
        gw.check_slos()
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
        assert "=== SLOs ===" in out and "local dump" in out


# ── unevaluated is not OK ────────────────────────────────────────────────────


class TestUnevaluatedIsNotOk:
    def test_a_database_outage_does_not_hide_the_backup_slos(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ROBOTHOR_BACKUP_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(gw, "send_telegram", lambda text: False)
        monkeypatch.setattr(gw, "write_slo_digest", lambda subject, body: True)
        monkeypatch.setattr(
            "robothor.db.connection.get_connection",
            lambda autocommit=False: (_ for _ in ()).throw(RuntimeError("postgres is down")),
        )
        fresh_markers(tmp_path)

        gw.check_slos()  # must not raise

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
        monkeypatch.setenv("ROBOTHOR_BACKUP_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(gw, "send_telegram", lambda text: False)
        monkeypatch.setattr(
            gw, "write_slo_digest", lambda subject, body: (written.append(body), True)[1]
        )
        monkeypatch.setattr(
            "robothor.db.connection.get_connection",
            lambda autocommit=False: (_ for _ in ()).throw(RuntimeError("postgres is down")),
        )
        fresh_markers(tmp_path)
        gw.check_slos()
        assert written == []


# ── main() ordering ──────────────────────────────────────────────────────────


class TestMainRunsTheSloSectionBeforeTheDatabaseSection:
    def test_the_slo_section_survives_a_database_outage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Same discipline as the drift checks (2026-08-16): the DB-free work
        must already have produced output before the DB section can abort."""
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
