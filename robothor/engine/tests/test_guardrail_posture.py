"""Two ways the guardrail ladder failed quietly.

1. `off` is advertised as a legal value for every governed `*_MODE` flag —
   `valid_values_for` returns it, the Controls API accepts, persists and
   audits it — but `_VALID_ENFORCEMENT_MODES` excludes it, so
   `_enforcement_mode` fell through to `return "observe"`. The operator's
   de-escalation lever was accepted, stored, displayed, and inert.

2. Nothing ever stated a daemon's security posture. The guardrail flags live
   in a drop-in on ONE unit, so a second daemon running the same engine code
   inherited none of them and ran with RBAC, injection-scan, exec-allowlist,
   approval-fail-closed and sandbox-default all off — silently, for days.
   A process that is unguarded should say so in its own journal.
"""

from __future__ import annotations

import logging

import pytest

from robothor.engine import feature_flags as ff


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    for v in (
        "ROBOTHOR_DISABLE_ALL",
        "ROBOTHOR_RBAC_ENABLED",
        "ROBOTHOR_RBAC_MODE",
        "ROBOTHOR_INJECTION_SCAN_ENABLED",
        "ROBOTHOR_INJECTION_SCAN_MODE",
    ):
        monkeypatch.delenv(v, raising=False)


def test_off_is_honoured_as_a_mode(monkeypatch):
    """The de-escalation lever must actually de-escalate."""
    monkeypatch.setenv("ROBOTHOR_RBAC_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_RBAC_MODE", "off")
    assert ff.rbac_enforcement_mode() == "off"


def test_off_is_what_the_api_advertises(monkeypatch):
    """valid_values_for and the engine must agree on the ladder."""
    from robothor.flags.store import valid_values_for

    advertised = set(valid_values_for("ROBOTHOR_RBAC_MODE"))
    monkeypatch.setenv("ROBOTHOR_RBAC_ENABLED", "1")
    for value in advertised:
        monkeypatch.setenv("ROBOTHOR_RBAC_MODE", value)
        assert ff.rbac_enforcement_mode() == value, (
            f"the API accepts {value!r} but the engine reinterprets it"
        )


def test_a_nonsense_mode_still_falls_back_to_observe(monkeypatch):
    monkeypatch.setenv("ROBOTHOR_RBAC_ENABLED", "1")
    monkeypatch.setenv("ROBOTHOR_RBAC_MODE", "banana")
    assert ff.rbac_enforcement_mode() == "observe"


def test_security_posture_names_every_guardrail():
    posture = ff.security_posture()
    for name in ("rbac", "injection_scan", "exec_allowlist_strict", "approval", "sandbox_default"):
        assert name in posture, f"{name} missing from the posture report"


def test_an_unguarded_process_says_so(caplog, monkeypatch):
    """The delphi daemon ran unguarded for days with nothing in its journal."""
    caplog.set_level(logging.WARNING)
    ff.log_security_posture()
    text = caplog.text
    assert "rbac" in text.lower()
    assert "off" in text.lower()


def test_a_guarded_process_does_not_warn(caplog, monkeypatch):
    for v in (
        "ROBOTHOR_RBAC_ENABLED",
        "ROBOTHOR_INJECTION_SCAN_ENABLED",
        "ROBOTHOR_EXEC_ALLOWLIST_STRICT_ENABLED",
        "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED",
        "ROBOTHOR_SANDBOX_DEFAULT_ENABLED",
    ):
        monkeypatch.setenv(v, "1")
    caplog.set_level(logging.WARNING)
    ff.log_security_posture()
    assert "running UNGUARDED" not in caplog.text


def test_the_daemon_actually_calls_it():
    """An unwired posture report is the defect class it exists to catch.

    Asserted against the daemon's own startup source rather than a mock, so
    deleting the call fails this test.
    """
    import inspect

    from robothor.engine import daemon

    src = inspect.getsource(daemon)
    assert "log_security_posture()" in src, "the daemon never reports its posture"


def test_no_guardrail_reports_unknown():
    """A typo'd function name silently degrades to 'unknown' — which reads as
    'guarded' to a tired operator and is the same silence this exists to end."""
    posture = ff.security_posture()
    assert "unknown" not in posture.values(), posture
