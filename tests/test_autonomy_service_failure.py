"""When the protected-browser service cannot start, somebody has to learn why.

``robothor-autonomy.service`` runs ``Restart=always`` / ``RestartSec=5``, and
its entry point turned every startup exception into

    raise SystemExit("workflow_service_failed") from None

``from None`` discards the cause, ``logging.disable(CRITICAL)`` two lines
earlier has already silenced the logger, and the unit carries no
``OnFailure=``. A persistent misconfiguration — a runtime directory that is
not 0700, a socket path already occupied, no sandbox-capable Chromium — is
therefore a five-second loop that emits one word, forever, to nobody.

Three things fix it, and this file pins all three:

* the exception TYPE and MESSAGE are logged before the exit (never page
  content, never a resource value — this process drives a browser over the
  owner's real accounts);
* the unit has a start limit, so the loop stops and the unit enters
  ``failed`` instead of grinding;
* the unit has the ``OnFailure=robothor-alert@%n.service`` drop-in every
  other long-running unit here has, so entering ``failed`` pages the
  operator. The reconciliation audit found this unit pages nobody.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
UNIT = REPO_ROOT / "infra" / "systemd" / "robothor-autonomy.service"
DROPIN = REPO_ROOT / "infra" / "systemd" / "robothor-autonomy.service.d" / "onfailure.conf"


class TestTheUnitStopsGrindingAndPages:
    def test_the_unit_has_a_start_limit(self):
        body = UNIT.read_text()
        assert "StartLimitIntervalSec=" in body, (
            "Restart=always with no start limit turns a permanent "
            "misconfiguration into an endless five-second loop"
        )
        assert "StartLimitBurst=" in body

    def test_the_start_limit_is_in_the_unit_section(self):
        """systemd reads both directives from [Unit], not [Service].

        Put in [Service] they are silently ignored and the limit does nothing
        — an inert control, which is worse than none because it reads as one.
        """
        unit_section = UNIT.read_text().split("[Service]", 1)[0]
        assert "StartLimitIntervalSec=" in unit_section
        assert "StartLimitBurst=" in unit_section

    def test_the_unit_pages_the_operator_on_failure(self):
        assert DROPIN.exists(), f"{DROPIN} is missing: this unit pages nobody"
        assert "OnFailure=robothor-alert@%n.service" in DROPIN.read_text()
        assert "[Unit]" in DROPIN.read_text()


class TestTheCauseIsLoggedBeforeExit:
    def _run_main(self, monkeypatch, caplog, error):
        from robothor.autonomy.workflows import service

        monkeypatch.setattr(
            "robothor.engine.process_hardening.harden_process", lambda: True, raising=False
        )

        async def boom(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(service, "run", boom)
        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_info:
            service.main()
        return exit_info.value, caplog.text

    def test_the_exception_type_and_message_reach_the_journal(self, monkeypatch, caplog):
        error = PermissionError("runtime directory /run/robothor-autonomy is mode 0755")
        _exit, logged = self._run_main(monkeypatch, caplog, error)
        assert "PermissionError" in logged
        assert "mode 0755" in logged

    def test_the_exit_still_names_the_failure(self, monkeypatch, caplog):
        exit_info, _logged = self._run_main(monkeypatch, caplog, RuntimeError("no chromium"))
        assert "workflow_service_failed" in str(exit_info)

    def test_logging_is_not_left_globally_disabled(self, monkeypatch, caplog):
        """``main`` calls ``logging.disable(CRITICAL)``.

        That is deliberate — this process must never log page content — but
        it also means the one message that has to get out cannot go through a
        disabled logger. Whatever the mechanism, the cause must appear.
        """
        _exit, logged = self._run_main(
            monkeypatch, caplog, OSError("address already in use: broker.sock")
        )
        assert "OSError" in logged
        assert "address already in use" in logged
