"""robothor-guardrail-watch must not go dark the same way it did on
2026-08-16.

Two bugs, one incident:

1. The unit had neither `After=postgresql.service`/`Wants=postgresql.service`
   nor `OnFailure=robothor-alert@%n.service` — every backup unit carries
   both. Its `Persistent=true` timer fired at boot before postgres was up.
2. In `scripts/guardrail_watch.py`'s `main()`, the DB-dependent section
   (guardrail events + run outcomes, `get_connection()`) ran FIRST. When it
   raised, the drift checks (drop-in + host-script drift) — which need no
   database at all — never ran. The drift watchdog was undetectably down:
   no exception reached anyone, no partial report, nothing.

This file pins the unit-file fix and proves the DB-free checks now run
first and survive a database outage with a non-zero exit and a clear
message, instead of a silent skip.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "guardrail_watch", REPO_ROOT / "scripts" / "guardrail_watch.py"
)
gw = importlib.util.module_from_spec(_spec)
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(gw)

SERVICE = REPO_ROOT / "infra" / "systemd" / "robothor-guardrail-watch.service"
TIMER = REPO_ROOT / "infra" / "systemd" / "robothor-guardrail-watch.timer"


class TestRepoUnitOrdersAfterPostgresAndPages:
    """The live unit had no repo mirror at all until this PR."""

    def test_service_unit_has_a_repo_mirror(self) -> None:
        assert SERVICE.exists(), (
            "robothor-guardrail-watch.service has no repo copy — the live unit "
            "was the only copy and its missing After=/OnFailure= drifted "
            "invisibly for who knows how long"
        )

    def test_service_waits_for_postgres(self) -> None:
        body = SERVICE.read_text()
        assert "After=postgresql.service" in body, (
            "no After=postgresql.service: a Persistent=true timer can fire at "
            "boot before postgres is up, exactly like 2026-08-16"
        )
        assert "Wants=postgresql.service" in body

    def test_service_pages_on_failure(self) -> None:
        body = SERVICE.read_text()
        assert "OnFailure=robothor-alert@%n.service" in body, (
            "every backup unit pages the operator on failure via "
            "robothor-alert@%n.service; this drift watchdog did not, so it "
            "died at boot with nobody told"
        )

    def test_timer_has_a_repo_mirror_and_is_persistent(self) -> None:
        assert TIMER.exists(), "robothor-guardrail-watch.timer has no repo copy"
        body = TIMER.read_text()
        assert "Persistent=true" in body
        assert "OnCalendar" in body


class TestDBFreeChecksSurviveADatabaseOutage:
    """The bug: get_connection() raising took the drift checks down with it."""

    @staticmethod
    def _silence_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
        # This environment carries real, live Telegram credentials — a test
        # must never let a nag actually send.
        monkeypatch.setattr(gw, "send_telegram", lambda text: False)

    @staticmethod
    def _raising_get_connection(autocommit: bool = False):
        raise RuntimeError("connection to server failed: postgres is not up yet")

    def test_drift_checks_still_run_when_the_db_is_down(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._silence_telegram(monkeypatch)

        calls: list[str] = []
        monkeypatch.setattr(
            gw,
            "check_dropin_drift",
            lambda: (calls.append("dropin"), print("SENTINEL-DROPIN-OK"))[1],
        )
        monkeypatch.setattr(
            gw,
            "check_host_script_drift",
            lambda pairs=None: (calls.append("host_script"), print("SENTINEL-HOST-OK"))[1],
        )
        monkeypatch.setattr(
            "robothor.db.connection.get_connection", self._raising_get_connection
        )

        exit_code = gw.main()

        assert set(calls) == {"dropin", "host_script"}, (
            "the DB-free drift checks must run even though the DB section "
            "fails — that is exactly what the bug prevented"
        )
        out = capsys.readouterr().out
        assert "SENTINEL-DROPIN-OK" in out
        assert "SENTINEL-HOST-OK" in out
        assert exit_code != 0, (
            "a DB outage must exit non-zero so systemd marks the run failed "
            "and OnFailure pages — swallowing it silently is the bug"
        )

    def test_drift_results_are_reported_before_the_db_failure_notice(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ordering, not just presence: a partial report only works if the
        DB-free section's output reaches the report before the DB section
        can abort the run."""
        self._silence_telegram(monkeypatch)
        monkeypatch.setattr(gw, "check_dropin_drift", lambda: print("SENTINEL-DROPIN-OK"))
        monkeypatch.setattr(
            gw, "check_host_script_drift", lambda pairs=None: print("SENTINEL-HOST-OK")
        )
        monkeypatch.setattr(
            "robothor.db.connection.get_connection", self._raising_get_connection
        )

        gw.main()

        out = capsys.readouterr().out
        assert "SENTINEL-DROPIN-OK" in out and "DATABASE" in out
        assert out.index("SENTINEL-DROPIN-OK") < out.index("DATABASE"), (
            "the drift results must be printed before the DB-failure notice, "
            "not swallowed by it"
        )

    def test_report_says_clearly_that_it_is_partial(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._silence_telegram(monkeypatch)
        monkeypatch.setattr(gw, "check_dropin_drift", lambda: None)
        monkeypatch.setattr(gw, "check_host_script_drift", lambda pairs=None: None)
        monkeypatch.setattr(
            "robothor.db.connection.get_connection", self._raising_get_connection
        )

        gw.main()

        out = capsys.readouterr().out
        assert "postgres is not up yet" in out, "the actual DB error must be visible"
        assert "partial" in out.lower(), (
            "silent skip vs partial report is the whole point of this fix — "
            "the report must say which one this run is"
        )
