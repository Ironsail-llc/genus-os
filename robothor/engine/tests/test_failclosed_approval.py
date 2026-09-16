"""Fail-closed human-approval when no approver is reachable (Wave-1, PR-11).

init_permission_manager was never called, so get_permission_manager() always
returned None and the runner auto-approved every human_approval escalation. Now
the daemon wires the manager to Telegram, and the no-manager branch is gated by
ROBOTHOR_APPROVAL_* (enforce → deny).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from robothor.engine.permission_escalation import fail_closed_on_missing_manager

_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clear(monkeypatch):
    for v in (
        "ROBOTHOR_DISABLE_ALL_RIPS",
        "ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED",
        "ROBOTHOR_APPROVAL_MODE",
    ):
        monkeypatch.delenv(v, raising=False)


class TestFailClosedOnMissingManager:
    def test_off_auto_approves(self):
        assert fail_closed_on_missing_manager() is False

    def test_observe_auto_approves(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_APPROVAL_MODE", "observe")
        assert fail_closed_on_missing_manager() is False

    def test_enforce_denies(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_APPROVAL_MODE", "enforce")
        assert fail_closed_on_missing_manager() is True

    def test_enabled_but_no_mode_defaults_observe(self, monkeypatch):
        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        assert fail_closed_on_missing_manager() is False


class TestTheGateIsReadThroughTheFlagStore:
    """Round 4, Important 4. The round-3 commit was titled "read the approval
    flags where the engine reads them" and every test it shipped used
    `monkeypatch.setenv` only — so reverting `approval_gate_inputs()` to raw
    `os.environ` left the whole suite green. Given that round-3 OPENED with a
    fix that was announced and never written, the missing thing was a test that
    pins the store path.

    `ROBOTHOR_APPROVAL_MODE` is `governed=True`, so an operator can set it from
    the Controls page and it lands in the DB, not in this process's
    environment. A reader that went to the environment would report "unset" for
    a value the engine is actively using.
    """

    @pytest.fixture
    def stored(self, monkeypatch):
        """A flag store that answers from the "DB", with a cold cache."""
        from robothor.flags import store

        values: dict[str, str] = {}
        monkeypatch.setattr(store, "_read_db", lambda name: values.get(name))
        store._cache.clear()
        yield values
        store._cache.clear()

    def test_the_stored_mode_is_what_is_reported(self, monkeypatch, stored):
        from robothor.engine.feature_flags import approval_gate_inputs

        monkeypatch.delenv("ROBOTHOR_APPROVAL_MODE", raising=False)
        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        stored["ROBOTHOR_APPROVAL_MODE"] = "enforce"

        enabled, mode = approval_gate_inputs()
        assert mode == "enforce", "the store value was not read"
        assert enabled == "1"

    def test_the_store_beats_the_environment(self, monkeypatch, stored):
        """The de-escalation lever the runbook promises: `observe` set from the
        Controls page overrides `enforce` in the deployed environment, so an
        operator can roll back without a redeploy."""
        from robothor.engine.feature_flags import approval_gate_inputs, approval_mode

        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        monkeypatch.setenv("ROBOTHOR_APPROVAL_MODE", "enforce")
        stored["ROBOTHOR_APPROVAL_MODE"] = "observe"

        assert approval_gate_inputs()[1] == "observe"
        assert approval_mode() == "observe"

    def test_it_agrees_with_approval_mode(self, monkeypatch, stored):
        """The check built on this must never disagree with the gate itself."""
        from robothor.engine.feature_flags import approval_gate_inputs, approval_mode

        monkeypatch.setenv("ROBOTHOR_APPROVAL_FAILCLOSED_ENABLED", "1")
        monkeypatch.delenv("ROBOTHOR_APPROVAL_MODE", raising=False)
        stored["ROBOTHOR_APPROVAL_MODE"] = "enforce"

        assert approval_gate_inputs()[1] == approval_mode() == "enforce"


def test_daemon_wires_permission_manager():
    """The daemon must initialise the escalation manager when a bot exists."""
    daemon_src = (_ROOT / "robothor" / "engine" / "daemon.py").read_text()
    assert "init_permission_manager(" in daemon_src
